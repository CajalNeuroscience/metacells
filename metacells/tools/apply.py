'''
Apply
-----
'''

import logging
from typing import Any, Callable, Dict, NamedTuple, Optional, Union

import numpy as np  # type: ignore
from anndata import AnnData

import metacells.utilities as ut

__all__ = [
    'DefaultValues',
    'Skip',
    'Raise',
    'apply_obs_annotations',
    'apply_var_annotations',
]


LOG = logging.getLogger(__name__)


class DefaultValues(NamedTuple):
    '''
    Default values to use in :py:func:`apply_obs_annotations` and :py:func:`apply_var_annotations`.
    '''

    #: The default value to use for the slice data.
    slice: Any

    #: The default value to use for the full data.
    full: Any


class Skip:
    '''
    A special value indicating to skip the annotation if it does not exist.
    '''


class Raise:
    '''
    A special value indicating to raise a ``KeyError`` if an annotation does not exist.
    '''


@ut.timed_call()
def apply_obs_annotations(
    adata: AnnData,
    sdata: AnnData,
    annotations: Dict[str, DefaultValues],
    *,
    indices: Union[str, ut.Vector],
) -> None:
    '''
    Apply per-observation (cell) annotations of a slice ``sdata`` to the full ``adata``.

    **Input**

    A :py:func:`metacells.utilities.annotation.setup` annotated ``adata``, and a slice
    of it ``sdata``, where the ``indices`` is either the vector of full indices of the
    slice observations, or the name of a per-observation annotation of ``sdata`` that
    contains this vector.

    **Computation Parameters**

    1. Loop on each of the named ``annotations``, where the value associated with the
       name is used as the default value (see below).

    2. If the slice data does not contain a per-observation (cell) annotation of this name, consider
       the :py:attr:`DefaultValues.slice`:

       * If it is :py:const:`Raise`, raise a ``KeyError``.

       * If it is :py:const:`Skip`, do not apply the annotation to the full data.

       * Otherwise, behave as if the annotation's value was a vector containing the
         :py:attr:`DefaultValues.slice` value.

    3. If the full data does not contain a per-observation (cell) annotation of this name, consider
       the :py:attr:`DefaultValues.full`:

       * If it is :py:const:`Raise`, raise a ``KeyError``.

       * If it is :py:const:`Skip`, do not apply the annotation to the full data.

       * Otherwise, initialize the annotation to a vector containing the
         :py:attr:`DefaultValues.full` value.

    4. Apply the slice data values to the entries of the full data identified by the ``indices``.
    '''
    ut.log_operation(LOG, adata, 'apply_var_annotations', of='__no_of__')
    _apply_annotations(adata, sdata, 'o', annotations, indices)


@ut.timed_call()
def apply_var_annotations(
    adata: AnnData,
    sdata: AnnData,
    annotations: Dict[str, DefaultValues],
    *,
    indices: Union[str, ut.Vector],
) -> None:
    '''
    Apply per-variable (gene) annotations of a slice ``sdata`` to the full ``adata``.

    **Input**

    A :py:func:`metacells.utilities.annotation.setup` annotated ``adata``, and a slice
    of it ``sdata``, where the ``indices`` is either the vector of full indices of the
    slice variables, or the name of a per-variable annotation of ``sdata`` that
    contains this vector.

    **Computation Parameters**

    1. Loop on each of the named ``annotations``, where the value associated with the
       name is used as the default value (see below).

    2. If the slice data does not contain a per-variable (gene) annotation of this name, consider
       the :py:attr:`DefaultValues.slice`:

       * If it is :py:const:`Raise`, raise a ``KeyError``.

       * If it is :py:const:`Skip`, do not apply the annotation to the full data.

       * Otherwise, behave as if the annotation's value was a vector containing the
         :py:attr:`DefaultValues.slice` value.

    3. If the full data does not contain a per-variable (gene) variable of this name, consider
       the :py:attr:`DefaultValues.full`:

       * If it is :py:const:`Raise`, raise a ``KeyError``.

       * If it is :py:const:`Skip`, do not apply the annotation to the full data.

       * Otherwise, initialize the annotation to a vector containing the
         :py:attr:`DefaultValues.full` value.

    4. Apply the slice data values to the entries of the full data identified by the ``indices``.
    '''
    ut.log_operation(LOG, adata, 'apply_var_annotations', of='__no_of__')
    _apply_annotations(adata, sdata, 'v', annotations, indices)


def _apply_annotations(  # pylint: disable=too-many-branches
    adata: AnnData,
    sdata: AnnData,
    per: str,
    annotations: Dict[str, DefaultValues],
    indices: Union[str, ut.Vector],
) -> None:
    full_name = ut.get_name(adata)
    slice_name = ut.get_name(sdata)
    if slice_name is not None:
        LOG.debug('  from: %s', slice_name)

    assert per in ('o', 'v')

    if per == 'o':
        full_data = adata.obs
        full_size = adata.n_obs
        slice_data = sdata.obs
        slice_size = sdata.n_obs
    else:
        full_data = adata.var
        full_size = adata.n_vars
        slice_data = sdata.var
        slice_size = sdata.n_vars

    full_indices = \
        ut.get_vector_parameter_data(LOG, adata, indices,
                                     indent='', per='o', name='full_indices')
    assert full_indices is not None
    assert full_indices.size == slice_size

    for name, default_values in annotations.items():
        slice_value = slice_data.get(name)
        if slice_value is not None:
            log_value: Optional[Callable[[Any], str]] = None
        else:
            if default_values.slice == Skip or isinstance(default_values.slice, Skip):
                continue

            if default_values.slice == Raise or isinstance(default_values.slice, Raise):
                if slice_name is None:
                    raise KeyError('unknown slice data name: %s' % name)
                raise KeyError('unknown slice data: %s name: %s'
                               % (slice_name, name))

            slice_value = default_values.slice

            def log_value(_: Any) -> str:
                # pylint: disable=cell-var-from-loop
                return '%s <- %s' % (slice_size, slice_value)
            # pylint: enable=cell-var-from-loop

        full_value = full_data.get(name)
        if full_value is not None:
            ut.unfreeze(full_value)
        else:
            if default_values.full == Skip or isinstance(default_values.full, Skip):
                continue

            if default_values.full == Raise or isinstance(default_values.full, Raise):
                if full_name is None:
                    raise KeyError('unknown full data name: %s' % name)
                raise KeyError('unknown full data: %s name: %s'
                               % (full_name, name))

            if default_values.full is None:
                full_value = np.full(full_size, None, dtype='float')
            else:
                full_value = np.full(full_size, default_values.full)

        full_value[full_indices] = slice_value
        if per == 'o':
            ut.set_o_data(adata, name, full_value, log_value=log_value)
        else:
            ut.set_v_data(adata, name, full_value, log_value=log_value)