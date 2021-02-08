'''
Candidates
----------
'''

import logging
from dataclasses import dataclass
from math import ceil, floor
from typing import List, Optional, Union

import numpy as np
from anndata import AnnData

import metacells.parameters as pr
import metacells.utilities as ut

__all__ = [
    'compute_candidate_metacells',
]


LOG = logging.getLogger(__name__)


@ut.timed_call()
@ut.expand_doc()
def compute_candidate_metacells(  # pylint: disable=too-many-branches
    adata: AnnData,
    what: Union[str, ut.Matrix] = 'obs_outgoing_weights',
    *,
    partition_method: 'ut.PartitionMethod' = ut.leiden_bounded_surprise,
    target_metacell_size: int,
    cell_sizes: Optional[Union[str, ut.Vector]] = pr.candidates_cell_sizes,
    min_split_size_factor: Optional[float] = pr.candidates_min_split_size_factor,
    max_merge_size_factor: Optional[float] = pr.candidates_max_merge_size_factor,
    min_metacell_cells: Optional[int] = pr.candidates_min_metacell_cells,
    must_complete_cover: bool = False,
    random_seed: int = 0,
    inplace: bool = True,
) -> Optional[ut.PandasSeries]:
    '''
    Assign observations (cells) to (raw, candidate) metacells based ``of`` a weighted directed graph.

    These candidate metacells typically go through additional vetting (e.g. deviant detection and
    dissolving too-small metacells) to obtain the final metacells.

    **Input**

    A :py:func:`metacells.utilities.annotation.setup` annotated ``adata``, where the observations
    are cells and the variables are genes.

    **Returns**

    Observation (Cell) Annotations
        ``candidate``
            The integer index of the (raw, candidate) metacell each cell belongs to. The metacells
            are in no particular order.

    If ``inplace`` (default: {inplace}), this is written to the data, and the function returns
    ``None``. Otherwise this is returned as a pandas series (indexed by the variable names).

    **Computation Parameters**

    1. We are trying to build metacells of ``target_metacell_size``. Use the ``cell_sizes``
       (default: {cell_sizes}) to assign a size for each node (cell). If the cell sizes is a string
       that contains ``<of>``, it is expanded using the name of the ``of`` data. If it is ``None``,
       each has a size of one. These parameters are typically identical to these passed to
       :py:func:`metacells.tools.dissolve.dissolve_metacells`.

       .. note::

            The cell sizes are converted to integer values, so if you have floating point sizes,
            make sure to scale them (and the target metacell size) so that the resulting integer
            values would make sense.

    2. Use the ``partition_method`` to compute initial communities. Several such possible methods
       are provided in this module, and you can also provide your own as long as it is compatible
       with the :py:const:`metacells.utilities.partition.PartitionMethod` interface.

    3. If ``min_split_size_factor`` (default: {min_split_size_factor}) is specified, re-run the
       partition method on each community whose size is at least ``target_metacell_size
       * min_split_size_factor``, to split it to into smaller communities.

    4. If ``max_merge_size_factor`` (default: {max_merge_size_factor}) or ``min_metacell_cells``
       (default: {min_metacell_cells}) are specified, condense each community whose size is at most
       ``target_metacell_size * max_merge_size_factor`` or contains less cells than
       ``min_metacell_cells`` into a single node (using the mean of the edge weights), and re-run
       the partition method on the resulting graph (of just these condensed nodes) to merge these
       communities into large ones.

    5. Repeat the above steps until no further progress can be made.

    6. If the ``max_merge_size_factor`` or the ``min_metacell_cells`` were specified, arbitrarily
       combine the remaining communities whose size is at most the ``target_metacell_size
       * max_merge_size_factor`` or contain less than ``min_metacell_cells`` cells into a single
       community using :py:func:`metacells.utilities.computation.bin_pack` and
       :py:func:`metacells.utilities.computation.bin_fill`. This is done more aggressively if
       ``must_complete_cover``.

    .. note::

        This doesn't guarantee that all communities would be in the size range we want, but comes as
        close as possible to it given the choice of partition method. Also, since we force merging
        of communities beyond what the partition method would have done on its own, not all the
        communities would have the same quality. Any too-low-quality groupings are expected to be
        corrected by removing deviants and/or by dissolving too-small communities.

    .. note::

        The partition method is given the ``random_seed`` to allow making it reproducible, and all
        the necessary size hints so it can, if possible, generate better-sized communities to reduce
        or eliminate the need for the split and merge steps. However, most partition algorithms do
        not naturally allow for this level of control over the resulting communities.
    '''
    level = ut.log_operation(LOG, adata, 'compute_candidate_metacells', what)

    edge_weights = ut.get_oo_proper(adata, what, layout='row_major')
    assert edge_weights.shape[0] == edge_weights.shape[1]

    LOG.debug('  partition_method: %s', partition_method.__qualname__)
    ut.log_use(LOG, adata, cell_sizes, per='o', name='cell_sizes', default='1')
    if cell_sizes is not None:
        node_sizes: Optional[ut.NumpyVector] = \
            ut.get_o_numpy(adata, cell_sizes).astype('int32')
    else:
        node_sizes = None

    assert target_metacell_size > 0
    LOG.debug('  target_metacell_size: %s', target_metacell_size)
    max_metacell_size = None
    min_metacell_size = None

    if min_split_size_factor is not None:
        LOG.debug('  min_split_size_factor: %s', min_split_size_factor)
        assert min_split_size_factor > 0
        max_metacell_size = \
            ceil(target_metacell_size * min_split_size_factor) - 1
        LOG.debug('  max_metacell_size: %s', max_metacell_size)

    if max_merge_size_factor is not None:
        LOG.debug('  max_merge_size_factor: %s', max_merge_size_factor)
        assert max_merge_size_factor > 0
        min_metacell_size = \
            floor(target_metacell_size * max_merge_size_factor) + 1
        LOG.debug('  min_metacell_size: %s', min_metacell_size)

    if min_metacell_cells is not None:
        LOG.debug('  min_metacell_cells: %s', min_metacell_cells)

    if min_split_size_factor is not None and max_merge_size_factor is not None:
        assert max_merge_size_factor < min_split_size_factor
        assert min_metacell_size is not None
        assert max_metacell_size is not None
        assert min_metacell_size <= max_metacell_size

    LOG.debug('  random_seed: %s', random_seed)

    community_of_cells = partition_method(edge_weights=edge_weights,
                                          node_sizes=node_sizes,
                                          target_comm_size=target_metacell_size,
                                          max_comm_size=max_metacell_size,
                                          min_comm_size=min_metacell_size,
                                          min_comm_nodes=min_metacell_cells,
                                          random_seed=random_seed)
    if LOG.isEnabledFor(logging.DEBUG):
        LOG.debug('  communities: %s', np.max(community_of_cells) + 1)

    if max_metacell_size is not None or min_metacell_size is not None:
        improver = Improver(community_of_cells,
                            partition_method=partition_method,
                            edge_weights=edge_weights,
                            node_sizes=node_sizes,
                            must_complete_cover=must_complete_cover,
                            target_comm_size=target_metacell_size,
                            max_comm_size=max_metacell_size,
                            min_comm_size=min_metacell_size,
                            min_comm_nodes=min_metacell_cells,
                            random_seed=random_seed)

        improver.improve()

        if min_metacell_size is not None:
            improver.pack()

        if min_metacell_cells is not None:
            improver.fill()

        community_of_cells = ut.compress_indices(improver.membership)

    if LOG.isEnabledFor(logging.DEBUG):
        LOG.debug('%s surprise: %s',
                  ut.get_name(adata),
                  ut.leiden_surprise_quality(edge_weights=edge_weights,
                                             partition_of_nodes=community_of_cells))

    if inplace:
        ut.set_o_data(adata, 'candidate', community_of_cells,
                      log_value=ut.groups_description)
        return None

    if LOG.isEnabledFor(level):
        LOG.log(level, '  candidates: %s', np.max(community_of_cells) + 1)

    return ut.to_pandas_series(community_of_cells, index=adata.obs_names)


@dataclass  # pylint: disable=too-many-instance-attributes
class Community:
    '''
    Metadata about a community.
    '''

    #: The index identifying the community.
    index: int

    #: The number of nodes in the community.
    nodes: int

    #: The total size of the community (sum of the node sizes).
    size: int

    #: A boolean mask of all the nodes that belong to the community.
    mask: ut.NumpyVector

    #: By how much (if at all) does the community have fewer nodes than the minimum allowed.
    too_few: int

    #: By how much (if at all) is the community smaller than the minimum allowed.
    too_small: int

    #: By how much (if at all) is the community larger than the maximum allowed.
    too_large: int

    #: Whether this community can't be split.
    monolithic: bool


class Improver:  # pylint: disable=too-many-instance-attributes
    '''
    Improve the communities.
    '''

    def __init__(  #
        self,
        membership: ut.NumpyVector,
        *,
        partition_method: 'ut.PartitionMethod',
        edge_weights: ut.ProperMatrix,
        node_sizes: Optional[ut.NumpyVector],
        target_comm_size: int,
        must_complete_cover: bool,
        min_comm_size: Optional[int],
        max_comm_size: Optional[int],
        min_comm_nodes: Optional[int],
        random_seed: int,
    ) -> None:
        #: The vector assigning a partition index to each node.
        self.membership = membership

        #: The partition method to use.
        self.partition_method = partition_method

        #: The random seed to use for reproducibility.
        self.random_seed = random_seed

        #: The edge weights we are using to compute partitions.
        self.edge_weights = edge_weights

        #: The size of each node (if not all-1).
        self.node_sizes = node_sizes

        #: Try to obtain communities of this size.
        self.target_comm_size = target_comm_size

        #: Split communities larger than this.
        self.max_comm_size = max_comm_size

        #: Merge communities smaller than this.
        self.min_comm_size = min_comm_size

        #: Merge communities with less nodes than this.
        self.min_comm_nodes = min_comm_nodes

        #: The list of communities.
        self.communities: List[Community] = []

        #: The sum of the too-few penalties across all communities.
        self.too_few = 0

        #: The sum of the too-small penalties across all communities.
        self.too_small = 0

        #: The sum of the too-large penalties across all communities.
        self.too_large = 0

        #: The next unused community index.
        self.next_community_index = 0

        #: Whether the metacell computation is required to cover all cells.
        self.must_complete_cover = must_complete_cover

        self.add()

    def add(  #
        self,
        count: Optional[int] = None
    ) -> None:
        '''
        Add new communities to the list by using the membership vector.
        '''
        if count is None:
            assert self.next_community_index == 0
            count = np.max(self.membership) + 1
        else:
            assert self.next_community_index > 0

        for _ in range(count):
            community_index = self.next_community_index
            self.next_community_index += 1

            mask = self.membership == community_index

            total_nodes = np.sum(mask)

            if self.min_comm_nodes is not None and total_nodes < self.min_comm_nodes:
                few = self.min_comm_nodes - total_nodes
                self.too_few += few
            else:
                few = 0

            if self.node_sizes is None:
                total_size = np.sum(mask)
            else:
                total_size = np.sum(self.node_sizes[mask])

            if self.min_comm_size is not None and total_size < self.min_comm_size:
                small = self.min_comm_size - total_size
                self.too_small += small
            else:
                small = 0

            if self.max_comm_size is not None and total_size > self.max_comm_size:
                large = total_size - self.max_comm_size
                self.too_large += large
            else:
                large = 0

            self.communities.append(Community(index=community_index,
                                              nodes=total_nodes, size=total_size,
                                              mask=mask, too_few=few,
                                              too_small=small, too_large=large,
                                              monolithic=False))

    def remove(self, position: int) -> None:
        '''
        Remove an existing community from the list.
        '''
        community = self.communities[position]
        self.communities[position:position+1] = []
        self.too_few -= community.too_few
        self.too_small -= community.too_small
        self.too_large -= community.too_large
        assert self.too_few >= 0
        assert self.too_small >= 0
        assert self.too_large >= 0

    @ut.timed_call('.improve')
    def improve(self) -> None:
        '''
        Improve the communities by splitting and merging.
        '''
        if self.min_comm_size is not None:
            while self.too_few > 0 or self.too_small > 0:
                if not self.merge_few_or_small():
                    break

        penalty = (self.too_few, self.too_small + self.too_large + 1)
        while (self.too_few, self.too_small + self.too_large) < penalty:
            penalty = (self.too_few, self.too_small + self.too_large)

            if self.max_comm_size is not None:
                did_split = False
                while self.too_large > 0:
                    if not self.split_large():
                        break
                    did_split = True

                if did_split and self.min_comm_size is not None:
                    while self.too_few > 0 or self.too_small > 0:
                        if not self.merge_few_or_small():
                            break

    @ut.timed_call('.merge_few_or_small')
    def merge_few_or_small(self) -> bool:
        '''
        Merge too-few or too-small communities.
        '''
        nodes_count = self.edge_weights.shape[0]
        merged_nodes_mask = np.zeros(nodes_count, dtype='bool')
        location_of_nodes = np.full(nodes_count, -1)
        merged_communities: List[Community] = []
        position_of_merged_communities: List[int] = []
        size_of_merged_communities: List[int] = []

        for position, community in enumerate(self.communities):
            if community.too_few == 0 and community.too_small == 0:
                continue
            merged_nodes_mask |= community.mask
            location_of_nodes[community.mask] = len(merged_communities)
            merged_communities.append(community)
            position_of_merged_communities.append(position)
            size_of_merged_communities.append(community.size)

        if len(merged_communities) < 2:
            return False

        edge_weights_of_merged_nodes = self.edge_weights[merged_nodes_mask, :]
        edge_weights_of_merged_nodes = \
            ut.to_layout(edge_weights_of_merged_nodes, 'column_major')
        edge_weights_of_merged_nodes = \
            edge_weights_of_merged_nodes[:, merged_nodes_mask]
        edge_weights_of_merged_nodes = \
            ut.to_numpy_matrix(edge_weights_of_merged_nodes)
        merge_frame = ut.to_pandas_frame(edge_weights_of_merged_nodes)
        location_of_merged_nodes = location_of_nodes[merged_nodes_mask]
        merge_frame = \
            merge_frame.groupby(location_of_merged_nodes,  # type: ignore
                                axis=0).mean()
        merge_frame = \
            merge_frame.groupby(location_of_merged_nodes,  # type: ignore
                                axis=1).mean()
        merged_communities_edge_weights = ut.to_proper_matrix(merge_frame)
        np.fill_diagonal(merged_communities_edge_weights, 0)

        merged_communities_node_sizes = \
            np.array(size_of_merged_communities)

        merged_communities_membership = \
            self.partition_method(edge_weights=merged_communities_edge_weights,
                                  node_sizes=merged_communities_node_sizes,
                                  target_comm_size=self.target_comm_size,
                                  max_comm_size=self.max_comm_size,
                                  min_comm_size=self.min_comm_size,
                                  random_seed=self.random_seed)

        for merged_community, merged_index \
                in zip(merged_communities, merged_communities_membership):
            self.membership[merged_community.mask] = \
                merged_index + self.next_community_index

        before_too_few = self.too_few
        before_too_small = self.too_small
        for position in reversed(position_of_merged_communities):
            self.remove(position)

        merged_communities_count = \
            np.max(merged_communities_membership) + 1
        self.add(merged_communities_count)

        did_improve = \
            (self.too_few, self.too_small) < (before_too_few, before_too_small)
        if did_improve:
            LOG.debug('  merged %s too-small into %s larger communities',
                      len(merged_communities), merged_communities_count)
        else:
            LOG.debug('  could not merge %s too-small communities',
                      len(merged_communities))

        return did_improve

    @ut.timed_call('.split_large')
    def split_large(self) -> bool:
        '''
        Split too-large communities.
        '''
        did_split = False

        position = 0
        while position < len(self.communities):
            split_community = self.communities[position]
            if split_community.too_large == 0 or split_community.monolithic:
                position += 1
                continue

            split_edge_weights = self.edge_weights[split_community.mask, :]
            split_edge_weights = \
                ut.to_layout(split_edge_weights, 'column_major')
            split_edge_weights = split_edge_weights[:,
                                                    split_community.mask]
            split_node_sizes = None if self.node_sizes is None \
                else self.node_sizes[split_community.mask]
            split_nodes_membership = \
                self.partition_method(edge_weights=split_edge_weights,
                                      node_sizes=split_node_sizes,
                                      target_comm_size=self.target_comm_size,
                                      max_comm_size=self.max_comm_size,
                                      min_comm_size=self.min_comm_size,
                                      random_seed=self.random_seed)

            split_communities_count = np.max(split_nodes_membership) + 1
            if split_communities_count == 1:
                LOG.debug('  could not split a too-large community')

                split_community.monolithic = True
                position += 1
                continue

            LOG.debug('  split too-large community into %s smaller communities',
                      split_communities_count)

            did_split = True

            self.remove(position)

            split_nodes_membership += self.next_community_index
            self.membership[split_community.mask] = split_nodes_membership
            self.add(split_communities_count)

        return did_split

    @ut.timed_call('.pack')
    def pack(self) -> None:
        '''
        Bin-pack too-small communities.
        '''
        list_of_small_community_sizes: List[int] = []
        list_of_small_community_indices: List[int] = []

        position = 0
        while position < len(self.communities):
            community = self.communities[position]
            if community.too_small == 0 and community.too_few == 0:
                position += 1
                continue
            list_of_small_community_sizes.append(community.size)
            list_of_small_community_indices.append(community.index)
            self.remove(position)

        if len(list_of_small_community_indices) == 0:
            return

        small_community_sizes = np.array(list_of_small_community_sizes)
        small_community_bins = \
            ut.bin_pack(small_community_sizes, self.target_comm_size)

        bins_count = np.max(small_community_bins) + 1

        for small_community_index, community_bin \
                in zip(list_of_small_community_indices, small_community_bins):
            merged_community_index = self.next_community_index + community_bin
            self.membership[self.membership == small_community_index] = \
                merged_community_index

        LOG.debug('  packed %s too-small communities into %s larger communities',
                  len(list_of_small_community_indices), bins_count)

        self.add(bins_count)

    @ut.timed_call('.fill')
    def fill(self) -> None:
        '''
        Bin-fill too-few communities.
        '''
        assert self.min_comm_nodes is not None

        list_of_few_community_nodes: List[int] = []
        list_of_few_community_indices: List[int] = []

        position = 0
        total_nodes = 0
        while position < len(self.communities):
            community = self.communities[position]
            if community.too_few == 0:
                position += 1
                continue
            total_nodes += community.nodes
            list_of_few_community_nodes.append(community.nodes)
            list_of_few_community_indices.append(community.index)
            self.remove(position)

        if len(list_of_few_community_indices) == 0:
            return

        if self.must_complete_cover and total_nodes < self.min_comm_nodes:
            candidates = \
                sorted([(community.nodes, community.size,
                         community_position, community.index)
                        for (community_position, community)
                        in enumerate(self.communities)
                        if not community.too_few
                        ])

            positions: List[int] = []
            for community_nodes, _, community_position, community_index in candidates:
                positions.append(community_position)
                total_nodes += community_nodes
                list_of_few_community_nodes.append(community_nodes)
                list_of_few_community_indices.append(community_index)
                if total_nodes >= self.min_comm_nodes:
                    break

            for position in reversed(sorted(positions)):
                self.remove(position)

        few_community_nodes = np.array(list_of_few_community_nodes)
        few_community_bins = \
            ut.bin_fill(few_community_nodes, self.min_comm_nodes)

        bins_count = np.max(few_community_bins) + 1

        for few_community_index, community_bin \
                in zip(list_of_few_community_indices, few_community_bins):
            merged_community_index = self.next_community_index + community_bin
            self.membership[self.membership == few_community_index] = \
                merged_community_index

        LOG.debug('  filled %s too-few communities into %s larger communities',
                  len(list_of_few_community_indices), bins_count)

        self.add(bins_count)
