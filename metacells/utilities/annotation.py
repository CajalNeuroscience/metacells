'''
Utilities for dealing with ``AnnData``.

This tries to deal safely with slicing data layers, observation, variable and unstructured
annotations, as well as two-dimensional variable and observation annotations.

.. todo::

    Move the description here to a more appropriate part of the documentation.

.. todo::

    Discuss the different models with the maintainers of ``AnnData`` to see whether it would be
    possible to reconcile the two approaches.

Data Model
----------

The data model for the managed ``AnnData`` is slightly different from that of the raw ``AnnData``.
Specifically, the mapping is:

.. table:: Data Model Mapping
    :align: left
    :widths: auto

    ======================  =========================================================================
    Managed                 Raw
    ======================  =========================================================================
    metadata                unstructured annotations (``.uns``)
    per_var_per_obs matrix  union of ``.X`` and ``.layers``
    per_obs vector          observation annotations (``.obs``)
    per_var vector          variable annotations (``.var``)
    per_obs_per_obs matrix  observation pair matrix annotations (``.obsp``)
    per_var_per_var matrix  variable pair matrix annotations (``.varp``)
    (none)                  multidimensional annotations (``.obsp``, ``.varp``, ``.obsm``, ``.varm``)
    ======================  =========================================================================

.. todo::

    Support multidimensional annotations.

Mapping Code Complexity
-----------------------

The mapping between the models is mostly simply 1-1 which is trivial to implement. However, there
are three sources of (implementation) code complexity here:

1.  Per-variable-per-observation data.

    In the managed ``AnnData``, all the layers are equal. A special metadata property ``__focus__``
    provides the default layer name for all layer functions. Similarly, operations that output a
    layer allow specifying that the result will be ``infocus``, and the :py:func:`focus_on` function
    allows for code to safely shift the focus for specific code regions.

    In contrast, raw ``AnnData`` contains a magic ``.X`` member which is distinct from the rest of
    the data layers. There is a strong assumption that all work will be done on ``X`` and the other
    layers are secondary at best.

    The managed ``AnnData`` therefore keeps a second special metadata property ``__x__`` which must
    be initialized using :py:func:`metacells.utilities.preparation.prepare`, and pretends that the
    value of ``X`` is just another layer. This gives rise to some edge cases (e.g., one can't delete
    the ``X`` layer).

2.  Layout-optimized data.

    For both dense and sparse formats, having the data in a layout that is friendly for the type of
    processing done on it makes for a big performance difference. For dense data this is "FORTRAN"
    vs. "C" layouts; for sparse data it is "CSR" vs. "CSC" format.

    Therefore, the managed ``AnnData`` allows for requesting a specific layout, and caches the
    results. Instead of monkey-patching the ``AnnData`` object, this is done by creating "hidden"
    layers with suffixed names (``...:__row_major__`` and ``...:__column_major__``).

    Changing the layout of a matrix using the builtin operations is very slow; therefore a parallel
    C++ extension is provided.

3.  Safe and efficient slicing.

    The managed ``AnnData`` provides a :py:func:`slice` operation that performs fast and safe
    slicing.

    To achieve this, the code tracks, for each possible data, whether it is/not safe to slice it
    along each of the axis (e.g., the per-observation ``o:sum_of_vo:...`` data is invalidated when
    slicing some of the variables).

    In addition, since slicing layout-optimized data is much faster, the code ensures that,
    as much as possible, slicing is always applied to the proper layout form of the data.

The interaction between the above three features is the cause of most of the implementation
complexity. The upside is that application code becomes simpler and is more efficient.

Usage Differences
-----------------

The managed ``AnnData`` model prefers to consider all data as immutable. That is, if one wants to
compute the log of some values, the common approach in raw ``AnnData`` is to mutate ``X`` to hold
the log data, while in the managed ``AnnData`` the approach would be to create a new data layer, so
that in principle one never needs to mutate any data. This is (not 100%) enforced by
:py:func:`metacells.utilities.typing.freeze`-ing the data whenever possible.

The managed ``AnnData`` provides standard functions that compute and cache new data from existing
data, using arbitrary compute functions (e.g., :py:func:`get_data`, :py:func:`get_vo_data`). See the
:py:mod:`metacells.utilities.preparation` module for these functions.

Data Names
----------

The names of annotated data must be unique across all the kinds of data, otherwise
functions such as ``sc.pl.scatter`` get confused and may silently(!) use the wrong
data.

In general the auto-generated names are tediously long and detailed (e.g.,
``log_2_of_1_plus_sum_of_var_per_obs_of_UMIs``), but ensure that they are properly unique. The
caller is responsible to ensure that the names of manually set data are also unique.
'''

from contextlib import contextmanager
from typing import (Any, Callable, Dict, Iterable, Iterator, List,
                    MutableMapping, NamedTuple, Optional, Set, Sized, Tuple,
                    Union)
from warnings import warn

import numpy as np  # type: ignore
import pandas as pd  # type: ignore
import scipy.sparse as sparse  # type: ignore
from anndata import AnnData
from readerwriterlock import rwlock

import metacells.utilities.computation as utc
import metacells.utilities.documentation as utd
import metacells.utilities.timing as utm
import metacells.utilities.typing as utt

__all__ = [
    'SlicingMask',
    'ALWAYS_SAFE',
    'NEVER_SAFE',
    'SAFE_WHEN_SLICING_OBS',
    'SAFE_WHEN_SLICING_VAR',
    'safe_slicing_data',
    'safe_slicing_derived',

    'slice',

    'get_data',
    'get_proper',
    'get_proper_matrix',
    'get_proper_vector',
    'has_data',
    'data_per',

    'get_m_data',
    'get_o_data',
    'get_v_data',
    'get_oo_data',
    'get_vv_data',

    'get_vo_data',
    'del_vo_data',
    'focus_on',
    'intermediate_step',
    'get_focus_name',
    'get_x_name',

]


LOCK = rwlock.RWLockRead()


Annotations = Union[MutableMapping[Any, Any], pd.DataFrame]


class SlicingMask(NamedTuple):
    '''
    A mask for safe operations when slicing some ``AnnData``.
    '''

    #: Whether the operation is safe when slicing observations (cells).
    per_obs: bool

    #: Whether the operation is safe when slicing variables (genes).
    per_var: bool

    def __and__(self, other: 'SlicingMask') -> 'SlicingMask':
        return SlicingMask(per_obs=self.per_obs and other.per_obs,
                           per_var=self.per_var and other.per_var)

    def __or__(self, other: 'SlicingMask') -> 'SlicingMask':
        return SlicingMask(per_obs=self.per_obs or other.per_obs,
                           per_var=self.per_var or other.per_var)


#: A mask for operations which are safe regardless of what is sliced.
ALWAYS_SAFE = SlicingMask(per_obs=True, per_var=True)

#: A mask for operations which are not safe regardless of what is sliced.
NEVER_SAFE = SlicingMask(per_obs=False, per_var=False)

#: A mask for operations which are only safe when slicing variables (genes),
#: but not when slicing observations (cells).
SAFE_WHEN_SLICING_VAR = SlicingMask(per_obs=False, per_var=True)

#: A mask for operations which are only safe when slicing observations (cells),
#: but not when slicing variables (genes).
SAFE_WHEN_SLICING_OBS = SlicingMask(per_obs=True, per_var=False)

SAFE_SLICING: Dict[str, SlicingMask] = {}

DIMENSIONAL_DATA_TYPES = (np.ndarray, sparse.spmatrix, pd.Series, pd.DataFrame)


def safe_slicing_data(name: str, slicing_mask: SlicingMask) -> None:
    '''
    Specify when it is safe to slice the ``name``d data, using the ``slicing_mask``.

    .. note::

        This is thread-safe.
    '''
    with LOCK.gen_wlock():
        SAFE_SLICING[name] = slicing_mask


def _known_safe_slicing() -> None:
    safe_slicing_data('__x__', ALWAYS_SAFE)
    safe_slicing_data('__focus__', ALWAYS_SAFE)


_known_safe_slicing()


def safe_slicing_derived(
    *,
    base: Union[str, List[str]],
    derived: str,
    slicing_mask: SlicingMask,
) -> None:
    '''
    Specify how to slice some ``derived`` data which is computed from some ``base_data``.

    If, when slicing, any of the bases data is not preserved, then the derived data would not be
    either. Otherwise, the derived will be preserved based on the flags given here.
    '''
    with LOCK.gen_rlock():
        if isinstance(base, str):
            slicing_mask = slicing_mask & SAFE_SLICING[base]
        else:
            for name in base:
                slicing_mask = slicing_mask & SAFE_SLICING[name]

    with LOCK.gen_wlock():
        SAFE_SLICING[derived] = slicing_mask


@utm.timed_call()
def slice(  # pylint: disable=redefined-builtin,too-many-branches,too-many-statements
    adata: AnnData,
    *,
    obs: Optional[Union[Sized, utt.Vector]] = None,
    vars: Optional[Union[Sized, utt.Vector]] = None,
    invalidated_prefix: Optional[str] = None,
) -> AnnData:
    '''
    Return new annotated data which includes a subset of the full ``adata``.

    If ``obs`` and/or ``vars`` are specified, they should include either a boolean
    mask or a collection of indices to include in the data slice. In the case of an indices array,
    it is assumed the indices are unique and sorted, that is that their effect is similar to a mask.

    In general, data might become invalid when slicing (e.g., the per-observation
    ``o:sum_of_vo:...`` data is invalidated when slicing some of the variables). Therefore, such
    data will be removed from the result unless it was explicitly marked as preserved using
    :py:func:`safe_slicing_data` or :py:func:`safe_slicing_derived`.

    If ``invalidated_prefix`` is specified, then invalidated data will not be removed; instead it
    will be renamed with the addition of the provided prefix.

    .. note::

        Setting the prefix to the empty string would simply preserve all the invalidated data. As
        this is unsafe, it will trigger a run-time exception. If you wish to perform such an unsafe
        slicing operation, invoke the built-in ``adata[..., ...]``.
    '''
    assert invalidated_prefix != ''
    x_name = get_x_name(adata)
    focus = get_focus_name(adata)
    assert x_name not in adata.layers
    assert has_data(adata, focus)

    shaped = utt.Shaped.maybe(obs)
    if shaped is None:
        if obs is None:
            obs = range(adata.n_obs)
        will_slice_obs = len(obs) != adata.n_obs
    else:
        obs = utt.to_dense_vector(shaped)
        if obs.dtype == 'bool':
            will_slice_obs = not np.all(obs)
        else:
            will_slice_obs = obs.size != adata.n_obs

    shaped = utt.Shaped.maybe(vars)
    if shaped is None:
        if vars is None:
            vars = range(adata.n_vars)
        will_slice_var = len(vars) != adata.n_vars
    else:
        vars = utt.to_dense_vector(shaped)
        if vars.dtype == 'bool':
            will_slice_var = not np.all(vars)
        else:
            will_slice_var = vars.size != adata.n_vars

    if not will_slice_obs and not will_slice_var:
        with utm.timed_step('.copy'):
            return adata[:, :]

    saved_data = \
        _save_data(adata, will_slice_obs,
                   will_slice_var, invalidated_prefix)

    with utm.timed_step('.builtin'):
        bdata = adata[obs, vars]

    did_slice_obs = bdata.n_obs != adata.n_obs
    did_slice_var = bdata.n_vars != adata.n_vars

    assert did_slice_obs == will_slice_obs
    assert did_slice_var == will_slice_var

    if invalidated_prefix is not None:
        _prefix_data(saved_data, bdata, did_slice_obs, did_slice_var,
                     invalidated_prefix)

    _restore_data(saved_data, adata)

    assert get_x_name(adata) == x_name
    assert get_focus_name(adata) == focus
    assert x_name not in adata.layers
    assert has_data(adata, focus)

    assert get_x_name(bdata) == x_name
    if focus == x_name or focus in bdata.layers:
        assert get_focus_name(bdata) == focus
    else:
        focus = bdata.uns['__focus__'] = x_name
    assert x_name not in bdata.layers
    assert has_data(bdata, focus)

    return bdata


def _save_data(
    adata: AnnData,
    will_slice_obs: bool,
    will_slice_var: bool,
    invalidated_prefix: Optional[str],
) -> Dict[Tuple[str, str], Any]:
    saved_data: Dict[Tuple[str, str], Any] = {}

    for per, annotations in (('vo', adata.layers),
                             ('o', adata.obs),
                             ('v', adata.var),
                             ('oo', adata.obsp),
                             ('vv', adata.varp),
                             ('m', adata.uns)):
        _save_per_data(saved_data, adata, per, annotations,
                       will_slice_obs, will_slice_var, invalidated_prefix)

    return saved_data


def _save_per_data(  # pylint: disable=too-many-locals
    saved_data: Dict[Tuple[str, str], Any],
    adata: AnnData,
    per: str,
    annotations: Annotations,
    will_slice_obs: bool,
    will_slice_var: bool,
    invalidated_prefix: Optional[str],
) -> None:
    delete_names: Set[str] = set()
    prefixed_data: Dict[str, Any] = {}

    if per != 'vo':
        def patch(name: str) -> None:  # pylint: disable=unused-argument
            pass
    else:
        will_slice_only_obs = will_slice_obs and not will_slice_var
        will_slice_only_var = will_slice_var and not will_slice_obs

        def patch(name: str) -> None:
            if will_slice_only_obs:
                patch_only(name, ':__row_major__')
            if will_slice_only_var:
                patch_only(name, ':__column_major__')

        def patch_only(name: str, by_suffix: str) -> None:
            if not name.endswith(by_suffix):
                return

            data = adata.layers[name]
            delete_names.add(name)

            base_name = name[:-len(by_suffix)]
            if base_name == get_x_name(adata):
                with utm.timed_step('.swap_x'):
                    with _modify(adata):
                        adata.X = data
            else:
                base_data = adata.layers[base_name]
                saved_data[('vo:', base_name)] = base_data

    for name, data in _items(annotations):
        action = _slice_action(name, will_slice_obs, will_slice_var,
                               invalidated_prefix is not None)

        if action == 'discard':
            saved_data[(per, name)] = data
            delete_names.add(name)
            continue

        patch(name)

        if action == 'preserve':
            continue

        assert action == 'prefix'
        assert invalidated_prefix is not None
        prefixed_name = invalidated_prefix + name
        prefixed_data[prefixed_name] = data
        delete_names.add(name)
        continue

    for name in delete_names:
        del annotations[name]


def _restore_data(saved_data: Dict[Tuple[str, str], Any], adata: AnnData) -> None:
    per_annotations = dict(vo=adata.layers,
                           o=adata.obs,
                           v=adata.var,
                           oo=adata.obsp,
                           vv=adata.varp,
                           m=adata.uns)
    for (per, name), data in saved_data.items():
        per_annotations[per][name] = data


def _prefix_data(
    saved_data: Dict[Tuple[str, str], Any],
    bdata: AnnData,
    did_slice_obs: bool,
    did_slice_var: bool,
    invalidated_prefix: str,
) -> None:
    for per, annotations in (('vo', bdata.layers),
                             ('o', bdata.obs),
                             ('v', bdata.var),
                             ('oo', bdata.obsp),
                             ('vv', bdata.varp),
                             ('m', bdata.uns)):
        _prefix_per_data(saved_data, per, annotations,
                         did_slice_obs, did_slice_var, invalidated_prefix)


def _prefix_per_data(
    saved_data: Dict[Tuple[str, str], Any],
    per: str,
    annotations: MutableMapping[str, Any],
    did_slice_obs: bool,
    did_slice_var: bool,
    invalidated_prefix: str,
) -> None:
    delete_names: List[str] = []

    for name, data in _items(annotations):
        action = _slice_action(name, did_slice_obs, did_slice_var, True)

        if action == 'preserve':
            continue
        assert action == 'prefix'

        prefixed_name = invalidated_prefix + name
        assert prefixed_name != name

        saved_data[(per, prefixed_name)] = data
        delete_names.append(name)

        with LOCK.gen_wlock():
            SAFE_SLICING[prefixed_name] = SAFE_SLICING[name]

    for name in delete_names:
        del annotations[name]


def _items(annotations: Annotations) -> Iterable[Tuple[str, Any]]:
    if isinstance(annotations, pd.DataFrame):
        return annotations.iteritems()
    return annotations.items()


def _get(annotations: Annotations, name: str) -> Any:
    if isinstance(annotations, pd.DataFrame):
        if name not in annotations.columns:
            return None
        return annotations[name]
    return annotations.get(name)


def _slice_action(
    name: str,
    do_slice_obs: bool,
    do_slice_var: bool,
    will_prefix_invalidated: bool,
) -> str:
    base_name = name
    if '__' in name:
        is_per = True
        is_per_obs = name.endswith(':__row_major__')
        is_per_var = name.endswith(':__column_major__')
        if is_per_obs or is_per_var:
            base_name = ':'.join(name.split(':')[:-1])
    else:
        is_per = False
        is_per_obs = False
        is_per_var = False

    with LOCK.gen_rlock():
        slicing_mask = SAFE_SLICING.get(base_name)

    if slicing_mask is None:
        with LOCK.gen_wlock():
            slicing_mask = SAFE_SLICING.get(base_name)
            if slicing_mask is None:
                unknown_sliced_data = \
                    'Slicing an unknown data: %s; ' \
                    'assuming it should not be preserved' \
                    % base_name
                warn(unknown_sliced_data)
                slicing_mask = SAFE_SLICING[base_name] = NEVER_SAFE

    if is_per_var:
        slicing_mask = slicing_mask & SAFE_WHEN_SLICING_VAR
    if is_per_obs:
        slicing_mask = slicing_mask & SAFE_WHEN_SLICING_OBS

    slicing_mask = slicing_mask | SlicingMask(per_obs=not do_slice_obs,
                                              per_var=not do_slice_var)

    if slicing_mask.per_obs and slicing_mask.per_var:
        return 'preserve'

    if is_per or not will_prefix_invalidated:
        return 'discard'

    return 'prefix'


@utm.timed_call()
@utd.expand_doc()
def get_data(
    adata: AnnData,
    name: Optional[str] = None,
    *,
    per: Optional[str] = None,
    compute: Optional[Callable[[], Any]] = None,
    inplace: bool = True,
    infocus: bool = False,
    layout: Optional[str] = None,
) -> Any:
    '''
    Lookup any data by its ``name`` (default: {name}).

    If no ``name`` is specified, return the focus.

    If the data does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the data for future reuse. This requires ``per`` (one
    of: ``vo``, ``vv``, ``oo``, ``v``, ``o``, ``m``, default: {per}) to specify where to store the
    data.

    If the data is per-variable-per-observation, and ``infocus`` (default: {infocus}, implies
    ``inplace``), also makes the result the new focus.

    If the data is 2-dimensional, and ``layout`` (default: {layout}) is specified, it forces the
    layout of the returned data.
    '''
    assert layout is None or layout in utt.LAYOUT_OF_AXIS

    if name is None:
        name = get_focus_name(adata)

    if per == 'vo' \
            or (per is None
                and (name == get_x_name(adata) or name in adata.layers)):
        return get_vo_data(adata, name, compute=compute,
                           inplace=inplace, infocus=infocus, layout=layout)

    assert not infocus

    if per == 'oo' or (per is None and name in adata.obsp):
        return get_oo_data(adata, name, compute=compute,
                           inplace=inplace, layout=layout)

    if per == 'vv' or (per is None and name in adata.varp):
        return get_vv_data(adata, name, compute=compute,
                           inplace=inplace, layout=layout)

    assert layout is None

    if per == 'm' or (per is None and name in adata.uns):
        return get_m_data(adata, name, compute=compute, inplace=inplace)

    if per == 'o' or (per is None and name in adata.obs):
        return get_o_data(adata, name, compute=compute, inplace=inplace)

    if per == 'v' or (per is None and name in adata.var):
        return get_v_data(adata, name, compute=compute, inplace=inplace)

    raise KeyError('unknown data name: %s' % name)


@utm.timed_call()
def get_proper(
    adata: AnnData,
    name: Optional[str] = None,
    *,
    per: Optional[str] = None,
    compute: Optional[Callable[[], Any]] = None,
    inplace: bool = True,
    infocus: bool = False,
    layout: Optional[str] = None,
) -> utt.ProperShaped:
    '''
    Same as :py:func:`get_data`, except that the returned data is
    passed through :py:func:`metacells.utilities.typing.to_proper`.
    '''
    return utt.to_proper(get_data(adata, name, per=per, compute=compute,
                                  inplace=inplace, infocus=infocus,
                                  layout=layout))


@utm.timed_call()
def get_proper_matrix(
    adata: AnnData,
    name: Optional[str] = None,
    *,
    per: Optional[str] = None,
    compute: Optional[Callable[[], Any]] = None,
    inplace: bool = True,
    infocus: bool = False,
    layout: Optional[str] = None,
) -> utt.ProperMatrix:
    '''
    Same as :py:func:`get_data`, except that the returned data is
    passed through :py:func:`metacells.utilities.typing.to_proper_matrix`
    ensuring it is a :py:const:`metacells.utilities.typing.ProperMatrix`.
    '''
    return utt.to_proper_matrix(get_data(adata, name, per=per, compute=compute,
                                         inplace=inplace, infocus=infocus,
                                         layout=layout))


@utm.timed_call()
def get_proper_vector(
    adata: AnnData,
    name: Optional[str] = None,
    *,
    per: Optional[str] = None,
    compute: Optional[Callable[[], Any]] = None,
    inplace: bool = True,
    infocus: bool = False,
) -> utt.ProperVector:
    '''
    Same as :py:func:`get_data`, except that the returned data is
    passed through :py:func:`metacells.utilities.typing.to_proper_vector`
    ensuring it is a :py:const:`metacells.utilities.typing.ProperVector`.
    '''
    return utt.to_proper_vector(get_data(adata, name, per=per, compute=compute,
                                         inplace=inplace, infocus=infocus))


def has_data(
    adata: AnnData,
    name: str,
    layout: Optional[str] = None,
) -> bool:
    '''
    Test whether we have the specified data.

    If the data is per-variable-per-observation, and ``layout`` is specified (one of ``row_major``
    and ``column_major``), it returns whether the specific data layout is available without
    having to compute or relayout existing data.
    '''
    assert layout is None or layout in utt.LAYOUT_OF_AXIS

    for annotations in (adata.layers, adata.obsp, adata.varp):
        if (id(annotations) == id(adata.layers) and name == get_x_name(adata)) \
                or name in annotations:
            if layout is None:
                return True
            name = '%s:__%s__' % (name, layout)
            if name in annotations:
                return True
            return utt.matrix_layout(get_data(adata, name)) == layout

    if name in adata.uns or name in adata.obs or name in adata.var:
        assert layout is None
        return True

    return False


@utd.expand_doc()
def data_per(  # pylint: disable=too-many-return-statements
    adata: AnnData,
    name: str,
    must_exist: bool = True,
) -> Optional[str]:
    '''
    Return the kind of data identified by the ``name``.

    Possible return values are ``vo``, ``vv``, ``oo``, ``v``, ``o`` and ``m``.

    If the data is missing, and ``must_exist`` (default: {must_exist}), raise ``KeyError``.
    Otherwise, return ``None``.
    '''

    if name == get_x_name(adata) or name in adata.layers:
        return 'vo'

    if name in adata.uns:
        return 'm'

    if name in adata.obs:
        return 'o'

    if name in adata.obsp:
        return 'oo'

    if name in adata.var:
        return 'v'

    if name in adata.varp:
        return 'vo'

    if must_exist:
        raise KeyError('unknown data: %s' % name)

    return None


@utm.timed_call()
@utd.expand_doc()
def get_m_data(
    adata: AnnData,
    name: str,
    compute: Optional[Callable[[], utt.Vector]] = None,
    *,
    inplace: bool = True,
) -> Any:
    '''
    Lookup metadata (unstructured annotation) in ``adata`` by its ``name``.

    If the metadata does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the result in ``adata``.

    .. note::

        The caller is responsible for specifying the slicing behavior of the data.
    '''
    data = adata.uns.get(name)
    if data is not None:
        return data

    if compute is None:
        raise KeyError('unavailable metadata: ' + name)

    data = compute()
    assert data is not None

    if inplace:
        with _modify(adata):
            adata.uns[name] = data

    return data


@utm.timed_call()
@utd.expand_doc()
def get_o_data(
    adata: AnnData,
    name: str,
    *,
    compute: Optional[Callable[[], utt.Vector]] = None,
    inplace: bool = True,
) -> utt.Vector:
    '''
    Lookup per-observation (cell) data in ``adata`` by its ``name``.

    If the data does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the result in ``adata`` for future reuse.

    .. note::

        The caller is responsible for specifying the slicing behavior of the data.
    '''
    return _get_shaped_data(adata, adata.obs, shape=(adata.n_obs,),
                            per_text='per-observation', name=name,
                            compute=compute, inplace=inplace)


@utm.timed_call()
@utd.expand_doc()
def get_v_data(
    adata: AnnData,
    name: str,
    *,
    compute: Optional[Callable[[], utt.Vector]] = None,
    inplace: bool = True,
) -> utt.Vector:
    '''
    Lookup per-variable (gene) data in ``adata`` by its ``name``.

    If the data does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the result in ``adata`` for future reuse.

    .. note::

        The caller is responsible for specifying the slicing behavior of the data.
    '''
    return _get_shaped_data(adata, adata.var, shape=(adata.n_vars,),
                            per_text='per-variable', name=name,
                            compute=compute, inplace=inplace)


@utm.timed_call()
@utd.expand_doc()
def get_oo_data(
    adata: AnnData,
    name: str,
    *,
    compute: Optional[Callable[[], utt.Matrix]] = None,
    inplace: bool = True,
    layout: Optional[str] = None,
) -> utt.Matrix:
    '''
    Lookup per-observation-per-observation (cell) data in ``adata`` by its ``name``.

    If the data does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the result in ``adata`` for future reuse.

    If ``layout`` (default: {layout}) is specified, it must be one of ``row_major`` or
    ``column_major`` (genes). This returns the data in a layout optimized for by-observation
    (row-major / csr) or by-variable (column-major / csc). If also ``inplace``, this is cached in an
    additional "hidden" annotation whose name is suffixed (e.g. ``...:__row_major__``).

    .. note::

        * In general names that contain ``__`` are reserved and should not be explicitly used.

        * The original data returned by ``compute`` is always preserved under its non-suffixed name.

        * The caller is responsible for specifying the slicing behavior of the data.
    '''
    return _get_layout_data(adata, adata.obsp,
                            shape=(adata.n_obs, adata.n_obs),
                            per_text='pre-observation-per-observation',
                            name=name, compute=compute,
                            inplace=inplace, layout=layout)


@utm.timed_call()
@utd.expand_doc()
def get_vv_data(
    adata: AnnData,
    name: str,
    *,
    compute: Optional[Callable[[], utt.Matrix]] = None,
    inplace: bool = True,
    layout: Optional[str] = None,
) -> utt.Matrix:
    '''
    Lookup per-variable-per-variable (gene) data in ``adata``.

    If the data does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the result in ``adata`` for future reuse.

    If ``layout`` (default: {layout}) is specified, it must be one of ``row_major`` or
    ``column_major`` (genes). This returns the data in a layout optimized for by-observation
    (row-major / csr) or by-variable (column-major / csc). If also ``inplace``, this is cached in an
    additional "hidden" annotation whose name is suffixed (e.g. ``...:__row_major__``).

    .. note::

        * In general names that contain ``__`` are reserved and should not be explicitly used.

        * The original data returned by ``compute`` is always preserved under its non-suffixed name.

        * The caller is responsible for specifying the slicing behavior of the data.
    '''
    return _get_layout_data(adata, adata.varp,
                            shape=(adata.n_vars, adata.n_vars),
                            per_text='pre-variable-per-variable',
                            name=name, compute=compute,
                            inplace=inplace, layout=layout)


@utm.timed_call()
@utd.expand_doc()
def get_vo_data(
    adata: AnnData,
    name: Optional[str] = None,
    *,
    compute: Optional[Callable[[], utt.Matrix]] = None,
    inplace: bool = True,
    infocus: bool = False,
    layout: Optional[str] = None,
) -> utt.Matrix:
    '''
    Lookup a per-variable-per-observation matrix (data layer) in ``adata`` by its ``name``.

    If the ``name`` is not specified, get the focus data.

    If the layer does not exist, ``compute`` it. If no ``compute`` function was given, ``raise``.

    If ``inplace`` (default: {inplace}), store the result in ``adata`` for future reuse.

    If ``infocus`` (default: {infocus}, implies ``inplace``), also makes the result the new focus
    data.

    If ``layout`` (default: {layout}) is specified, it must be one of ``row_major`` or
    ``column_major`` (genes). This returns the data in a layout optimized for by-observation
    (row-major / csr) or by-variable (column-major / csc). If also ``inplace`` (or ``infocus``),
    this is cached in an additional "hidden" layer whose name is suffixed (e.g.
    ``...:__row_major__``).

    Returns the result data.

    .. note::

        * In general names that contain ``__`` are reserved and should not be explicitly used.

        * The original data returned by ``compute`` is always preserved under its non-suffixed name.

        * The caller is responsible for specifying the slicing behavior of the data.
    '''
    if name is None:
        name = get_focus_name(adata)

    data = _get_layout_data(adata, adata.layers,
                            shape=(adata.n_obs, adata.n_vars),
                            per_text='pre-variable-per-observation',
                            name=name, compute=compute,
                            inplace=inplace or infocus, layout=layout)

    if infocus:
        with _modify(adata):
            adata.uns['__focus__'] = name

    return data


def _get_layout_data(
    adata: AnnData,
    annotations: Annotations,
    *,
    shape: Tuple[int, ...],
    per_text: str,
    name: str,
    compute: Optional[Callable[[], utt.Shaped]],
    inplace: bool,
    layout: Optional[str],
) -> Any:
    assert '__' not in name

    def get_base_data() -> Any:
        return _get_shaped_data(adata, annotations, shape=shape,
                                per_text=per_text, name=name,
                                compute=compute, inplace=inplace)

    if layout is None:
        return get_base_data()

    assert layout in utt.LAYOUT_OF_AXIS

    layout_name = '%s:__%s__' % (name, layout)
    data = adata.layers.get(layout_name)
    if data is not None:
        return data

    data = get_base_data()
    if utt.matrix_layout(data) == layout:
        return data

    data = utc.to_layout(data, layout=layout)
    if inplace:
        utt.freeze(data)
        with _modify(adata):
            with utm.timed_step('.set_annotation'):
                annotations[layout_name] = data

    return data


def _get_shaped_data(
    adata: AnnData,
    annotations: Annotations,
    *,
    shape: Tuple[int, ...],
    per_text: str,
    name: str,
    compute: Optional[Callable[[], utt.Shaped]],
    inplace: bool
) -> Any:
    assert '__' not in name

    if id(annotations) == id(adata.layers) and name == get_x_name(adata):
        with utm.timed_step('.get_x'):
            return adata.X

    if (isinstance(annotations, pd.DataFrame) and name in annotations.columns) \
            or name in annotations:
        return annotations[name]

    if compute is None:
        raise KeyError('unavailable %s data: %s' % (per_text, name))

    data = compute()
    assert data is not None
    assert data.shape == shape

    if inplace:
        utt.freeze(data)
        with utm.timed_step('.set_annotation'):
            annotations[name] = data

    return data


@utd.expand_doc()
def del_vo_data(
    adata: AnnData,
    name: str,
    *,
    layout: Optional[str] = None,
    must_exist: bool = False,
) -> None:
    '''
    Delete a per-variable-per-observation matrix in ``adata`` by its ``name``.

    If ``layout`` (default: {layout}) is specified, it must be one of ``row_major`` (cells) or
    ``column_major`` (genes). This will only delete the cached layout-specific data, if any. If
    ``layout`` is not specified, both the data and any cached layout-specific data will be deleted.

    If ``must_exist`` (default: {must_exist}), will ``raise`` if the data does not currently exist.

    .. note::

        You can't delete the ``x_name`` (layer of ``X``). Deleting the focus (default) data changes
        the focus to become whatever is in ``X``.

    .. todo::

        The function :py:func:`del_vo_data` is only required due to the need to delete cached
        layout-specific data. A better way would be to intercept ``del`` of the ``AnnData``
        ``layers`` field, but we do not own it.
    '''
    assert '__' not in name

    x_name = get_x_name(adata)
    assert name != x_name

    if name == get_focus_name(adata):
        with _modify(adata):
            adata.uns['__focus__'] = x_name

    if layout is not None:
        layout_name = '%s:__%s__' % (name, layout)
        if layout_name in adata.layers:
            del adata.layers[layout_name]
        elif must_exist:
            assert layout_name in adata.layers
        return

    if must_exist:
        assert name in adata.layers

    for suffix in ('', ':__row_major__', ':__column_major__'):
        suffixed_name = name + suffix
        if suffixed_name in adata.layers:
            del adata.layers[suffixed_name]


@contextmanager
@utd.expand_doc()
def focus_on(
    accessor: Callable,
    adata: AnnData,
    *args: Any,
    intermediate: bool = True,
    **kwargs: Any
) -> Iterator[Any]:
    '''
    Get some per-variable-per-observation data by invoking the ``accessor`` with ``adata`` (and some
    ``args`` and ``kwargs``), and make it the focus for the duration of the ``with`` statement.

    If the original focus data is deleted inside the ``with`` statement, then when it is done, the
    focus will revert to whatever is in ``X``.

    If not ``intermediate`` (default: {intermediate}), this discards all the intermediate
    ``inplace`` data (e.g. sums) which was set within the ``with`` statement block. Otherwise, such
    data is kept for future reuse.

    For example, in order to temporarily focus on the log of some linear measurements,
    write:

    .. code:: python

        ...
        # The focus data is some linear measurements.
        with ut.focus_on(ut.get_log, adata) as log_data:
            # log_data contains the log of the measurements.
            # The focus data is the log of the measurements.
            ...
        # The focus data is back to the linear measurements.
        ...

    It is also possible to focus on some data by its name by writing ``focus_on(get_vo_data, adata,
    name)``.

    .. note::

        Do not specify ``inplace`` and/or ``infocus`` in the ``kwargs``, as they are implied.
    '''
    for name in ('infocus', 'inplace'):
        if name in kwargs:
            ignoring_redundant_explict_flags_to_focus_on = \
                'ignoring explicit %s flag for focus_on' % name
            warn(ignoring_redundant_explict_flags_to_focus_on)
            del kwargs[name]

    with intermediate_step(adata, intermediate=intermediate):
        yield accessor(adata, *args, infocus=True, **kwargs)


@contextmanager
@utd.expand_doc()
def intermediate_step(
    adata: AnnData,
    *,
    intermediate: bool = True,
) -> Iterator[None]:
    '''
    Execute some code in a ``with`` statements and restore the focus at the end, if it was modified
    by the wrapped code.

    If not ``intermediate`` (default: {intermediate}), this discards all the intermediate
    ``inplace`` data (e.g. sums) which was set within the ``with`` statement block. Otherwise, such
    data is kept for future reuse.

    .. note::

        This does not protected against deletion of data by the wrapped code. If the original focus
        data is deleted inside the ``with`` statement, then when it is done, the focus will revert
        to whatever is in ``X``.
    '''
    if not intermediate:
        old_data = _all_data(adata)

    old_focus = get_focus_name(adata)

    try:
        yield

    finally:
        with _modify(adata):
            if not intermediate:
                new_data = _all_data(adata)
                for name in new_data:
                    if name in old_data:
                        continue
                    if name.endswith(':__row_major__') \
                            or name.endswith(':__column_major__'):
                        base_name = ':'.join(name.split(':')[:-1])
                        if base_name in old_data:
                            continue
                    del_vo_data(adata, name)

            if has_data(adata, old_focus):
                adata.uns['__focus__'] = old_focus
            else:
                adata.uns['__focus__'] = get_x_name(adata)


def _all_data(adata: AnnData) -> Set[str]:
    names: Set[str] = set()

    for annotations in (adata.layers, adata.uns,
                        adata.obs, adata.var, adata.obsp, adata.varp):
        for name, _ in _items(annotations):
            names.add(name)

    return names


def get_focus_name(adata: AnnData) -> str:
    '''
    Return the name of the focus per-variable-per-observation data.
    '''
    return adata.uns['__focus__']


def get_x_name(adata: AnnData) -> str:
    '''
    Return the name of the ``X`` per-variable-per-observation data.
    '''
    return adata.uns['__x__']


@contextmanager
def _modify(adata: AnnData) -> Iterator[None]:
    if adata.is_view:
        with utm.timed_step('unview'):
            yield
    else:
        yield
