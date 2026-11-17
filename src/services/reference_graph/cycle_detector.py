"""
src/services/reference_graph/cycle_detector.py
----------------------------------------------
Constructs a directed Reference Graph (CRG) representing inter-clause and
term dependencies across the contract. Detects circular definition loops,
self-references, and recursive cross-clause obligations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from src.services.reference_graph.reference_extractor import ExtractedReference
from src.services.reference_graph.structural_tree import ContractStructuralTree


@dataclass
class CycleDetectionResult:
    """Summary of cyclic dependency analysis on the contract reference graph."""
    has_cycles: bool
    cycles: List[List[str]]
    strongly_connected_components: List[List[str]]
    is_dag: bool
    topological_order: Optional[List[str]] = None


class ReferenceGraph:
    """
    Directed dependency graph of contract clauses.
    An edge (U -> V) denotes that clause U depends on / references clause V.
    """

    def __init__(self, tree: ContractStructuralTree, references: List[ExtractedReference]) -> None:
        self.tree = tree
        self.references = references
        self.graph = nx.DiGraph()
        self._build_graph()

    def _build_graph(self) -> None:
        # Add all section / subsection nodes from structural tree
        for node in self.tree.nodes:
            self.graph.add_node(
                node.canonical_address,
                title=node.display_title,
                para_index=node.para_index,
                is_back_matter=node.is_back_matter,
            )

        # Add directed edges from references (Source -> Target)
        for ref in self.references:
            src = ref.source_address
            for tgt in ref.target_addresses:
                if src != tgt:
                    self.graph.add_edge(
                        src,
                        tgt,
                        ref_type=ref.ref_type,
                        raw=ref.raw_reference,
                    )

    def detect_cycles(self) -> CycleDetectionResult:
        """Finds all cycles and recursive dependency loops in the contract."""
        simple_cycles = list(nx.simple_cycles(self.graph))
        is_dag = nx.is_directed_acyclic_graph(self.graph)

        sccs = [
            list(scc)
            for scc in nx.strongly_connected_components(self.graph)
            if len(scc) > 1
        ]

        topo = None
        if is_dag:
            try:
                topo = list(nx.topological_sort(self.graph))
            except Exception:
                topo = None

        return CycleDetectionResult(
            has_cycles=len(simple_cycles) > 0,
            cycles=simple_cycles,
            strongly_connected_components=sccs,
            is_dag=is_dag,
            topological_order=topo,
        )

    def get_dependencies_for_clause(self, canonical_address: str, max_depth: int = 2) -> List[str]:
        """Returns all clauses that the given clause depends on up to max_depth."""
        if canonical_address not in self.graph:
            return []

        visited: Set[str] = set()
        queue: List[Tuple[str, int]] = [(canonical_address, 0)]

        while queue:
            curr, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for successor in self.graph.successors(curr):
                if successor not in visited:
                    visited.add(successor)
                    queue.append((successor, depth + 1))

        return list(visited)

    def get_dependents_for_clause(self, canonical_address: str, max_depth: int = 2) -> List[str]:
        """Returns all clauses that depend on / reference the given clause."""
        if canonical_address not in self.graph:
            return []

        visited: Set[str] = set()
        queue: List[Tuple[str, int]] = [(canonical_address, 0)]

        while queue:
            curr, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for predecessor in self.graph.predecessors(curr):
                if predecessor not in visited:
                    visited.add(predecessor)
                    queue.append((predecessor, depth + 1))

        return list(visited)
