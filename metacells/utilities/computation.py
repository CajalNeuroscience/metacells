'''
Utilities for performing efficient parallel computations.

.. todo::

    Further investigate performance of suspiciously slow operations:

    * The builtin ``max_sparse_matrix`` seems way to slow. Parallelization helps but perhaps a
      different implementation is called for, or opening a bug with ``scipy``.

    * Parallelizing ``bincount`` did not work well. Perhaps another approach?
'''

import os
import sys
from typing import Callable, Optional, Union
from warnings import warn

import numpy as np  # type: ignore
import pandas as pd  # type: ignore
from scipy import sparse  # type: ignore

import metacells.extensions as xt  # type: ignore
import metacells.utilities.documentation as utd
import metacells.utilities.timing as timed
from metacells.utilities.threading import (SharedStorage, parallel_collect,
                                           parallel_for)

__all__ = [
    'DATA_TYPES',
    'BUILTIN_OPERATIONS',
    'PARALLELIZE_BUILTINS',
    'use_all_private_implementations',

    'Matrix',
    'Vector',

    'to_array',
    'to_layout',
    'relayout_compressed',

    'corrcoef',

    'log_matrix',
    'sum_matrix',
    'nnz_matrix',
    'max_matrix',

    'bincount_array',

    'downsample_matrix',
    'downsample_array',
    'downsample_tmp_size',
]


#: The data types supported by the C++ extensions code.
DATA_TYPES = ['float32', 'float64', 'int32', 'int64', 'uint32', 'uint64']

#: A ``mypy`` type for matrices.
Matrix = Union[sparse.spmatrix, np.ndarray, pd.DataFrame]

#: A ``mypy`` type for vectors.
Vector = Union[np.ndarray, pd.Series]

#: The list of builtin operations for which there are alternative implementations here.
BUILTIN_OPERATIONS = ['bincount_array',
                      'max_sparse_matrix', 'max_dense_matrix',
                      'nnz_sparse_matrix', 'nnz_dense_matrix',
                      'sum_sparse_matrix', 'sum_dense_matrix']

#: Which built-in operations to parallelize. Override this by setting the
#: ``METACELLS_PARALLELIZE_BUILTINS`` environment variable to a comma-separated list of values from
#: the :py:member:`metacells.utilities.computations.BUILTIN_OPERATIONS` list.
#:
#: .. todo::
#:
#:    You would expect that the built-in implementation would be the best possible, but it seems
#:    that for some operations it is not, specifically, ``max_sparse_matrix`` seems to be slow (even
#:    when the data is in the most efficient ``csc``/``csr`` format). Collect
#:    :py:module:`metacell.utilities.timing` to ensure you are using the best option.
PARALLELIZE_BUILTINS = set(['max_sparse_matrix'])

if not 'sphinx' in sys.argv[0]:
    PARALLELIZE_BUILTINS = \
        {name.strip() for name
         in os.environ.get('METACELLS_PARALLELIZE_BUILTINS', ','.join(PARALLELIZE_BUILTINS)).split(',')}
    for operation in PARALLELIZE_BUILTINS:
        if operation not in BUILTIN_OPERATIONS:
            raise ValueError('unknown operation: %s '
                             'specified in METACELLS_PARALLELIZE_BUILTINS; '
                             'valid operations are: %s'
                             % (operation, ', '.join(BUILTIN_OPERATIONS)))


def use_all_private_implementations() -> None:
    '''
    Force everything to use the private implementation.

    .. note::

        This will likely ruin performance. It is only meant to be used for tests.
    '''
    global PARALLELIZE_BUILTINS
    PARALLELIZE_BUILTINS = set(BUILTIN_OPERATIONS)


def to_array(data: Union[Matrix, Vector]) -> np.ndarray:
    '''
    Convert some (possibly sparse) data to an (full dense size) array.

    This should only be applied if only one dimension has size greater than one.

    .. todo::

        There are some strange cases where ``np.reshape(data, -1)`` returns a **matrix** rather than
        an array. The code included a workaround but it sure is ugly.
    '''
    if sparse.issparse(data):
        data = data.todense()

    if data.ndim == 1:
        return data

    seen_large_dimension = False
    for size in data.shape:
        if size == 1:
            continue
        assert not seen_large_dimension
        seen_large_dimension = True

    array = np.reshape(data, -1)
    if array.ndim > 1:
        array = np.reshape(array.__array__(), -1)
        assert array.ndim == 1

    return array


@ timed.call()
def to_layout(matrix: Matrix, *, axis: int) -> Matrix:  # pylint: disable=too-many-return-statements
    '''
    Re-layout a matrix for efficient axis slicing/processing.

    That is, for ``axis=0``, re-layout the matrix for efficient per-column (variable, gene)
    slicing/processing. For sparse matrices, this is ``csc`` format; for
    dense matrices, this is Fortran (column-major) format.

    Similarly, for ``axis=1``, re-layout the matrix for efficient per-row (observation, cell)
    slicing/processing. For sparse matrices, this is ``csr`` format; for dense matrices, this is C
    (row-major) format.

    If the matrix is already in the correct layout, it is returned as-is. Otherwise, a new copy is
    created. This is a costly operation as it needs to move a lot of data. However, it makes the
    following processing much more efficient, so it is typically a net performance gain overall.
    '''
    assert matrix.ndim == 2
    assert 0 <= axis <= 1

    if sparse.issparse(matrix):
        to_axis_format = ['csc', 'csr'][axis]
        from_axis_format = ['csc', 'csr'][1 - axis]
        if matrix.getformat() == to_axis_format:
            return matrix

        if matrix.getformat() == from_axis_format:
            return relayout_compressed(matrix, axis)

        name = '.to' + to_axis_format
        with timed.step(name):
            return getattr(matrix, name[1:])()

    if axis == 0:
        if matrix.flags['F_CONTIGUOUS']:
            return matrix
        with timed.step('.ravel'):
            return np.reshape(np.ravel(matrix, order='F'), matrix.shape, order='F')

    if matrix.flags['C_CONTIGUOUS']:
        return matrix
    with timed.step('.ravel'):
        return np.reshape(np.ravel(matrix, order='C'), matrix.shape, order='C')


@ timed.call()
def relayout_compressed(matrix: sparse.spmatrix, axis: int) -> sparse.spmatrix:
    '''
    Efficient parallel conversion of a CSR/CSC matrix to a CSC/CSR matrix.
    '''
    assert matrix.ndim == 2
    assert matrix.getformat() == ['csr', 'csc'][axis]

    _output_elements_count = matrix_bands_count = matrix.shape[axis]
    output_bands_count = matrix_elements_count = matrix.shape[1 - axis]

    nnz_elements_of_output_bands = \
        bincount_array(matrix.indices, minlength=matrix_elements_count)

    output_indptr = np.empty(output_bands_count + 1, dtype=matrix.indptr.dtype)
    output_indptr[0:2] = 0
    with timed.step('cumsum'):
        np.cumsum(nnz_elements_of_output_bands[:-1], out=output_indptr[2:])

    output_indices = np.empty(matrix.indices.size, dtype=matrix.indices.dtype)
    output_data = np.empty(matrix.data.size, dtype=matrix.data.dtype)

    extension_name = 'collect_compressed_%s_t_%s_t_%s_t' \
        % (matrix.data.dtype, matrix.indices.dtype, matrix.indptr.dtype)
    extension = getattr(xt, extension_name)

    def collect_compressed(matrix_band_indices: range) -> None:
        extension(matrix_band_indices.start, matrix_band_indices.stop,
                  matrix.data, matrix.indices, matrix.indptr,
                  output_data, output_indices, output_indptr[1:])

    with timed.step('.collect_compressed'):
        parallel_for(collect_compressed, matrix_bands_count)

    assert output_indptr[-1] == matrix.indptr[-1]

    constructor = [sparse.csc_matrix, sparse.csr_matrix][axis]
    return constructor((output_data, output_indices, output_indptr), shape=matrix.shape)


@timed.call()
def corrcoef(matrix: Matrix) -> np.ndarray:
    '''
    Compute correlations between all observations (rows, cells) containing variables (columns,
    genes).

    This should give the same results as ``numpy.corrcoef``, but faster for sparse matrices.

    .. note::

        To correlate between observations (cells), the expected layout is the transpose of the
        layout of ``X`` in ``AnnData``.
    '''
    if not sparse.issparse(matrix):
        return np.corrcoef(matrix)

    obs_count = matrix.shape[0]
    var_count = matrix.shape[1]
    timed.parameters(obs_count=obs_count, var_count=var_count)
    sum_of_rows = matrix.sum(axis=1)
    assert sum_of_rows.size == obs_count
    centering = sum_of_rows.dot(sum_of_rows.T) / var_count
    correlations = (matrix.dot(matrix.T) - centering) / (var_count - 1)
    assert correlations.shape == (obs_count, obs_count)
    diagonal = np.diag(correlations)
    correlations /= np.sqrt(np.outer(diagonal, diagonal))
    return correlations


@timed.call()
def log_matrix(
    matrix: Matrix,
    *,
    base: Optional[float] = None,
    normalization: float = 1,
) -> np.ndarray:
    '''
    Compute the ``log2`` of some ``matrix``.

    The ``base`` is added to the count before ``log2`` is applied, to handle the common case of zero
    values in sparse data.

    The ``normalization`` (default: {normalization}) is added to the count before the log is
    applied, to handle the common case of sparse data.
    '''
    if sparse.issparse(matrix):
        matrix = matrix.todense()
    elif isinstance(matrix, pd.DataFrame):
        matrix = np.copy(matrix.values)
    else:
        assert isinstance(matrix, np.ndarray)
        matrix = np.copy(matrix)

    matrix += normalization
    if base == 2:
        np.log2(matrix, out=matrix)
    else:
        np.log(matrix, out=matrix)
        if base is not None:
            assert base > 0
            matrix /= np.log(base)

    return matrix


def sum_matrix(matrix: Matrix, *, axis: int) -> np.ndarray:
    '''
    Compute the total per row (``axis`` = 1) or column (``axis`` = 0) of some ``matrix``.
    '''
    if sparse.issparse(matrix):
        return _reduce_matrix('sum_sparse_matrix', matrix, axis, lambda matrix: matrix.sum(axis=axis))
    return _reduce_matrix('sum_dense_matrix', matrix, axis, lambda matrix: matrix.sum(axis=axis))


def nnz_matrix(matrix: Matrix, *, axis: int) -> np.ndarray:
    '''
    Compute the number of non-zero elements per row (``axis`` = 1) or column (``axis`` = 0) of some
    ``matrix``.
    '''
    if sparse.issparse(matrix):
        return _reduce_matrix('nnz_sparse_matrix', matrix, axis, lambda matrix: matrix.getnnz(axis=axis))
    return _reduce_matrix('nnz_dense_matrix', matrix, axis, lambda matrix: np.count_nonzero(matrix, axis=axis))


def max_matrix(matrix: Matrix, *, axis: int) -> np.ndarray:
    '''
    Compute the maximal element per row (``axis`` = 1) or column (``axis`` = 0) of some ``matrix``.
    '''
    if sparse.issparse(matrix):
        return _reduce_matrix('max_sparse_matrix', matrix, axis, lambda matrix: matrix.max(axis=axis))
    return _reduce_matrix('max_dense_matrix', matrix, axis, lambda matrix: np.amax(matrix, axis=axis))


def _reduce_matrix(
    name: str,
    matrix: Matrix,
    axis: int,
    reducer: Callable,
) -> np.ndarray:
    assert matrix.ndim == 2
    assert 0 <= axis <= 1

    results_count = matrix.shape[1 - axis]
    results = np.empty(results_count)

    if sparse.issparse(matrix):
        elements_count = matrix.nnz / results_count
        axis_format = ['csc', 'csr'][axis]
        if matrix.getformat() != axis_format:
            reducing_sparse_matrix_of_inefficient_format = \
                'reducing axis: %s ' \
                'of a sparse matrix in the format: %s ' \
                'instead of the efficient format: %s' \
                % (axis, matrix.getformat(), axis_format)
            warn(reducing_sparse_matrix_of_inefficient_format)
    else:
        elements_count = matrix.shape[axis]

    if name not in PARALLELIZE_BUILTINS:
        with timed.step(name + '(builtin)'):
            timed.parameters(obs_count=results_count, var_count=elements_count)
            return to_array(reducer(matrix))

    if axis == 0:
        def batch_reducer(indices: range) -> None:
            results[indices] = to_array(reducer(matrix[:, indices]))
    else:
        def batch_reducer(indices: range) -> None:
            results[indices] = to_array(reducer(matrix[indices, :]))

    with timed.step(name + '(parallel)'):
        timed.parameters(obs_count=results_count, var_count=elements_count)
        parallel_for(batch_reducer, results_count)

    return results


def bincount_array(
    array: np.ndarray,
    *,
    minlength: int = 0,
) -> np.ndarray:
    '''
    Count the number of occurrences of each value in an ``array``.

    This is identical to Numpy's ``bincount``, except hopefully faster.
    '''
    if 'bincount_array' not in PARALLELIZE_BUILTINS:
        with timed.step('bincount(builtin)'):
            result = np.bincount(array, minlength=minlength)
            timed.parameters(elements=array.size, bins=result.size)
            return result

    shared_storage = SharedStorage()

    def compute(indices: range) -> None:
        new_tmp = np.bincount(array[indices], minlength=minlength)
        shared_storage.get_private('tmp',
                                   make=lambda: new_tmp,
                                   update=lambda old_tmp: _sum_bincounts(old_tmp, new_tmp))

    def merge(from_thread, into_thread: str) -> None:
        from_tmp = shared_storage.get_private('tmp', thread=from_thread)
        into_tmp = shared_storage.get_private('tmp', thread=into_thread)
        sum_tmp = _sum_bincounts(from_tmp, into_tmp)
        shared_storage.set_private('tmp', sum_tmp, thread=into_thread)

    with timed.step('bincount(parallel)'):
        final_thread = \
            parallel_collect(compute, merge, array.size, batches_per_thread=1)
        result = shared_storage.get_private('tmp', thread=final_thread)
        timed.parameters(elements=array.size, bins=result.size)
        return result


def _sum_bincounts(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    if left.size < right.size:
        right[:left.size] += left
        return right

    left[:right.size] += right
    return left


@timed.call()
def downsample_matrix(
    matrix: Matrix,
    *,
    axis: int,
    samples: int,
    eliminate_zeros: bool = True,
    inplace: bool = False,
    random_seed: int = 0,
) -> Matrix:
    '''
    Downsample the rows (``axis`` = 1) or columns (``axis`` = 0) of some ``matrix`` such that the
    sum of each one becomes ``samples``.

    If the matrix is sparse, if not ``eliminate_zeros``, then do not perform the final phase of
    eliminating leftover zero values from the compressed format.

    If ``inplace``, modify the matrix, otherwise, return a modified copy.

    A ``random_seed`` can be provided to make the operation replicable.
    '''
    assert matrix.ndim == 2
    assert 0 <= axis <= 1

    if not sparse.issparse(matrix):
        return _downsample_dense_matrix(matrix, axis, samples, inplace, random_seed)

    return _downsample_sparse_matrix(matrix, axis, samples, eliminate_zeros, inplace, random_seed)


def _downsample_sparse_matrix(
    matrix: sparse.spmatrix,
    axis: int,
    samples: int,
    eliminate_zeros: bool,
    inplace: bool,
    random_seed: int
) -> sparse.spmatrix:
    elements_count = matrix.shape[axis]
    results_count = matrix.shape[1 - axis]

    axis_format = ['csc', 'csr'][axis]
    if matrix.getformat() != axis_format:
        raise NotImplementedError('downsample axis: %s '
                                  'of a sparse matrix in the format: %s '
                                  'instead of the efficient format: %s'
                                  % (axis, matrix.getformat(), axis_format))

    if inplace:
        output = matrix
    else:
        constructor = [sparse.csc_matrix, sparse.csr_matrix][axis]
        output_data = np.empty(matrix.data.shape, dtype=matrix.data.dtype)
        output = constructor((output_data, matrix.indices, matrix.indptr))

    shared_storage = SharedStorage()
    max_tmp_size = downsample_tmp_size(elements_count)

    def downsample_sparse_vectors(indices: range) -> None:
        for index in indices:
            start_index = matrix.indptr[index]
            stop_index = matrix.indptr[index + 1]

            input_vector = matrix.data[start_index:stop_index]
            output_vector = output.data[start_index:stop_index]

            tmp = \
                shared_storage.get_private('tmp',
                                           make=lambda: np.empty(max_tmp_size,
                                                                 dtype=matrix.dtype))

            if random_seed != 0:
                index_seed = random_seed + index
            else:
                index_seed = 0

            _downsample_array(input_vector, samples, tmp,
                              output_vector, index_seed)

    with timed.step('.sparse'):
        parallel_for(downsample_sparse_vectors, results_count)

    if eliminate_zeros:
        with timed.step('.eliminate_zeros'):
            output.eliminate_zeros()

    return output


def _downsample_dense_matrix(  # pylint: disable=too-many-locals,too-many-statements
    matrix: np.ndarray,
    axis: int,
    samples: int,
    inplace: bool,
    random_seed: int
) -> np.ndarray:
    if inplace:
        output = matrix
    elif axis == 0:
        output = np.empty(matrix.shape, dtype=matrix.dtype, order='F')
    else:
        output = np.empty(matrix.shape, dtype=matrix.dtype, order='C')

    if axis == 0:
        sample_input_vector = matrix[:, 0]
        sample_output_vector = output[:, 0]
    else:
        sample_input_vector = matrix[0, :]
        sample_output_vector = output[0, :]

    input_is_contiguous = \
        sample_input_vector.flags['C_CONTIGUOUS'] and sample_input_vector.flags['F_CONTIGUOUS']
    if not input_is_contiguous:
        downsampling_dense_input_matrix_of_inefficient_format = \
            'downsampling axis: %s ' \
            'of a dense input matrix with inefficient strides: %s' \
            % (axis, matrix.strides)
        warn(downsampling_dense_input_matrix_of_inefficient_format)

    output_is_contiguous = \
        sample_output_vector.flags['C_CONTIGUOUS'] and sample_output_vector.flags['F_CONTIGUOUS']
    if not inplace and not output_is_contiguous:
        downsampling_dense_output_matrix_of_inefficient_format = \
            'downsampling axis: %s ' \
            'of a dense output matrix with inefficient strides: %s' \
            % (axis, output.strides)
        warn(downsampling_dense_output_matrix_of_inefficient_format)

    shared_storage = SharedStorage()

    elements_count = matrix.shape[axis]
    tmp_size = downsample_tmp_size(elements_count)

    def downsample_dense_vectors(indices: range) -> None:
        for index in indices:
            if axis == 0:
                input_vector = matrix[:, index]
            else:
                input_vector = matrix[index, :]
            if not input_is_contiguous:
                input_vector = np.copy(input_vector)
            input_array = to_array(input_vector)

            if not output_is_contiguous:
                output_vector = \
                    shared_storage.get_private('output',
                                               make=lambda: np.empty(elements_count,
                                                                     dtype=output.dtype))
            elif axis == 0:
                output_vector = output[:, index]
            else:
                output_vector = output[index, :]
            output_array = to_array(output_vector)

            tmp = shared_storage.get_private('tmp',
                                             make=lambda: np.empty(tmp_size,
                                                                   dtype=matrix.dtype))

            if random_seed != 0:
                index_seed = random_seed + index
            else:
                index_seed = 0

            _downsample_array(input_array, samples, tmp,
                              output_array, index_seed)

            if output_is_contiguous:
                continue

            if axis == 0:
                output[:, index] = output_array
            else:
                output[index, :] = output_array

    results_count = matrix.shape[1 - axis]
    with timed.step('.dense'):
        parallel_for(downsample_dense_vectors, results_count)

    return output


@timed.call()
@utd.expand_doc(data_types=','.join(['``%s``' % data_type for data_type in DATA_TYPES]))
def downsample_array(
    array: np.ndarray,
    samples: int,
    *,
    tmp: Optional[np.ndarray] = None,
    output: Optional[np.ndarray] = None,
    random_seed: int = 0
) -> None:
    '''
    Downsample a vector of sample counters.

    **Input**

    * A Numpy ``array`` containing non-negative integer sample counts.

    * A desired total number of ``samples``.

    * An optional temporary storage Numpy array minimize allocations for multiple invocations.

    * An optional Numpy array ``output`` to hold the results (otherwise, the input is overwritten).

    * An optional random seed to make the operation replicable.

    The arrays may have any of the data types: {data_types}.

    **Operation**

    If the total number of samples (sum of the array) is not higher than the required number of
    samples, the output is identical to the input.

    Otherwise, treat the input as if it was a set where each index appeared its value number of
    times. Randomly select the desired number of samples from this set (without repetition), and
    store in the output the number of times each index was chosen.

    **Motivation**

    Downsampling is an effective way to get the same number of samples in multiple observations (in
    particular, the same number of total UMIs in multiple cells), and serves as an alternative to
    normalization (e.g., working with UMI fractions instead of raw UMI counts).

    Downsampling is especially important when computing correlations between observations. When
    there is high variance between the total samples count in different observations (total UMI
    count in different cells), then normalization will return higher values when correlating
    observations with a higher sample count, which will result in an inflated estimation of their
    similarity to other observations. Downsampling avoids this effect.
    '''
    return _downsample_array(array, samples, tmp, output, random_seed)


def _downsample_array(
    array: np.ndarray,
    samples: int,
    tmp: Optional[np.ndarray] = None,
    output: Optional[np.ndarray] = None,
    random_seed: int = 0
) -> None:
    assert array.ndim == 1

    if tmp is None:
        tmp = np.empty(downsample_tmp_size(array.size), dtype='int32')
    else:
        assert tmp.size >= downsample_tmp_size(array.size)

    if output is None:
        output = array
    else:
        assert output.shape == array.shape

    extension_name = \
        'downsample_%s_t_%s_t_%s_t' % (array.dtype, tmp.dtype, output.dtype)
    extension = getattr(xt, extension_name)
    extension(array, tmp, output, samples, random_seed)


def downsample_tmp_size(size: int) -> int:
    '''
    Return the size of the temporary array needed to ``downsample`` an array of the specified size.
    '''
    return xt.downsample_tmp_size(size)
