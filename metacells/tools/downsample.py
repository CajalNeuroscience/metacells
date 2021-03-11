'''
Downsample
----------
'''

from typing import Optional, Union

import numpy as np
from anndata import AnnData

import metacells.parameters as pr
import metacells.utilities as ut

__all__ = [
    'downsample_cells',
]


@ut.logged()
@ut.timed_call()
@ut.expand_doc()
def downsample_cells(
    adata: AnnData,
    what: Union[str, ut.Matrix] = '__x__',
    *,
    downsample_cell_quantile: float = pr.downsample_cell_quantile,
    random_seed: int = pr.random_seed,
    inplace: bool = True,
) -> Optional[ut.PandasFrame]:
    '''
    Downsample the values of ``what`` (default: {what}) data.

    Downsampling is an effective way to get the same number of samples in multiple cells
    (that is, the same number of total UMIs in multiple cells), and serves as an alternative to
    normalization (e.g., working with UMI fractions instead of raw UMI counts).

    Downsampling is especially important when computing correlations between cells. When there is
    high variance between the total UMI count in different cells, then normalization will return
    higher correlation values between cells with a higher total UMI count, which will result in an
    inflated estimation of their similarity to other cells. Downsampling avoids this effect.

    **Input**

    Annotated ``adata``, where the observations are cells and the variables are genes, where
    ``what`` is a per-variable-per-observation matrix or the name of a per-variable-per-observation
    annotation containing such a matrix.

    **Returns**

    Variable-Observation (Gene-Cell) Annotations
        ``downsampled``
            The downsampled data where the total number of samples in each cell is at most
            ``samples``.

    If ``inplace`` (default: {inplace}), this is written to the data, and the function returns
    ``None``. Otherwise this is returned as a pandas data frame (indexed by the cell and gene
    names).

    **Computation Parameters**

    1. Compute the total samples in each cell, and use the ``downsample_cell_quantile`` (default:
       {downsample_cell_quantile}) to select our target number of samples. That is, if this quantile
       is 0.1, then 10% of the cells would have less samples than the target and will be unchanged,
       while 90% of the cells will have more samples than the target, and will be downsampled so
       that they will have exactly the target number of samples.

    2. Downsample each cell so that it has at most the selected number of samples. Use the
       ``random_seed`` to allow making this replicable.
    '''
    total_per_cell = ut.get_o_numpy(adata, what, sum=True)

    samples = round(np.quantile(total_per_cell, downsample_cell_quantile))
    ut.log_calc('samples', samples)

    data = ut.get_vo_proper(adata, what, layout='row_major')
    downsampled = ut.downsample_matrix(data, per='row', samples=samples,
                                       random_seed=random_seed)
    if inplace:
        ut.set_vo_data(adata, 'downsampled', downsampled)
        return None

    return ut.to_pandas_frame(downsampled,
                              index=adata.obs_names, columns=adata.var_names)
