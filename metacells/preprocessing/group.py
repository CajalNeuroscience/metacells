'''
Group
-----
'''

import logging
from typing import Any, Optional, Union

import numpy as np  # type: ignore
import pandas as pd  # type: ignore
from anndata import AnnData

import metacells.utilities as ut

__all__ = [
    'group_obs_data',
    'group_obs_annotation',
]


LOG = logging.getLogger(__name__)


@ut.timed_call()
@ut.expand_doc()
def group_obs_data(
    adata: AnnData,
    *,
    of: Optional[str] = None,
    groups: Union[str, ut.Vector],
    name: Optional[str] = None,
    tmp: bool = False,
) -> Optional[AnnData]:
    '''
    Compute new data which has the sum ``of`` some data of the observations (cells) for each group.

    For example, having computed a metacell index for each cell, compute the per-metacell data
    for further analysis.

    If ``groups`` is a string, it is expected to be the name of a per-observation vector annotation.
    Otherwise it should be a vector. The group indices should be integers, where negative values
    indicate "no group" and non-negative values indicate the index of the group to which each
    observation (cell) belongs to.

    **Input**

    A :py:func:`metacells.utilities.annotation.setup` ``adata``, where the observations are cells
    and the variables are genes.

    **Returns**

    An annotated data where each observation is the sum of the group of original observations
    (cells). Observations with a negative group index are discarded. If all observations are
    discarded, return ``None``.

    The new data will contain only:

    * An ``X`` member holding the summed-per-group ``of`` data. This will also be the focus.

    * A new ``grouped`` per-observation data which counts, for each group, the number
      of grouped observations summed into it.

    If ``name`` is not specified, the data will be unnamed. Otherwise, if it starts with a ``.``, it
    will be appended to the current name (if any). Otherwise, ``name`` is the new name.

    If ``tmp`` (default: {tmp}) is set, logging of modifications to the result will use the
    ``DEBUG`` logging level. By default, logging of modifications is done using the ``INFO`` logging
    level.
    '''
    ut.log_operation(LOG, adata, 'group_obs_data')
    level = ut.get_log_level(adata)

    with ut.focus_on(ut.get_vo_data, adata, of, layout='row_major') as data:
        group_of_cells = \
            ut.get_vector_parameter_data(LOG, level, adata, groups,
                                         per='o', name='groups')
        assert group_of_cells is not None

        results = ut.sum_groups(data, group_of_cells, per='row')
        if results is None:
            return None
        summed_data, cell_counts = results

        gdata = AnnData(summed_data)

        if name is not None:
            if name.startswith('.'):
                base_name = ut.get_name(adata)
                if base_name is None:
                    name = name[1:]
                else:
                    name = base_name + name
        ut.setup(gdata, name=name, x_name=ut.get_focus_name(adata), tmp=tmp)

        ut.set_o_data(gdata, 'grouped', cell_counts,
                      log_value=ut.sizes_description)

    return gdata


@ut.timed_call()
@ut.expand_doc()
def group_obs_annotation(
    adata: AnnData,
    gdata: AnnData,
    *,
    groups: Union[str, ut.Vector],
    name: str,
    method: str = 'majority',
    min_value_fraction: float = 0.5,
    conflict: Optional[Any] = None,
    inplace: bool = True,
) -> Optional[pd.Series]:
    '''
    Transfer per-observation data from the per-observation (cell) ``adata`` to the
    per-group-of-observations (metacells) ``gdata``.

    **Input**

    A :py:func:`metacells.utilities.annotation.setup` annotated ``adata``, where the observations
    are cells and the variables are genes, and the ``gdata`` containing the per-metacells summed
    data.

    **Returns**

    Observations (Cell) Annotations
        ``<name>``
            The per-group-observation annotation computed based on the per-observation annotation.

    If ``inplace`` (default: {inplace}), this is written to the ``gdata``, and the function returns
    ``None``. Otherwise this is returned as a pandas series (indexed by the group observation
    names).

    **Computation Parameters**

    1. Iterate on all the observations (groups, metacells) in ``gdata``.

    2. Consider all the cells whose ``groups`` annotation maps them into this group.

    3. Consider all the ``name`` annotation values of these cells.

    4. Compute an annotation value for the whole group of cells using the ``method``. Supported
       methods are:

       ``unique``
            All the values of all the cells in the group are expected to be the same, use this
            unique value for the whole groups.

       ``majority``
            Use the most common value across all cells in the group as the value for the whole
            group. If this value doesn't have at least ``min_value_fraction`` (default:
            {min_value_fraction}) of the cells, use the ``conflict`` (default: {conflict}) value
            instead.
    '''
    ut.log_operation(LOG, adata, 'group_obs_annotation')
    level = ut.get_log_level(adata)

    group_of_cells = \
        ut.get_vector_parameter_data(LOG, level, adata, groups,
                                     per='o', name='groups')
    assert group_of_cells is not None

    values_of_cells = ut.get_vector_parameter_data(LOG, level, adata, name,
                                                   per='o', name='values')
    assert values_of_cells is not None

    value_of_groups = np.empty(gdata.n_obs, dtype=values_of_cells.dtype)

    LOG.log(level, '  method: %s', method)

    assert method in ('unique', 'majority')

    if method == 'unique':
        with ut.timed_step('.unique'):
            value_of_groups[group_of_cells] = values_of_cells

    else:
        assert method == 'majority'
        with ut.timed_step('.majority'):
            for group_index in range(gdata.n_obs):
                cells_mask = group_of_cells == group_index
                cells_count = np.sum(cells_mask)
                assert cells_count > 0
                values_of_cells_of_group = values_of_cells[cells_mask]
                unique_values_of_group, unique_counts_of_group = \
                    np.unique(values_of_cells_of_group, return_counts=True)
                majority_index = np.argmax(unique_counts_of_group)
                majority_count = unique_counts_of_group[majority_index]
                if majority_count / cells_count < min_value_fraction:
                    value_of_groups[group_index] = conflict
                else:
                    majority_value = unique_values_of_group[majority_index]
                    value_of_groups[group_index] = majority_value

    if inplace:
        ut.set_o_data(gdata, name, value_of_groups)
        return None

    return pd.Series(value_of_groups, index=gdata.obs_names)
