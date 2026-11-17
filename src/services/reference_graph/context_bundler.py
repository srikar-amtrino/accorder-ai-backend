"""
src/services/reference_graph/context_bundler.py
-----------------------------------------------
Assembles compact, bounded context bundles of referenced clauses for a given
ClauseGroup during evaluation. Enforces strict token budgets and annotates
snippets with party-specific perspective if requested.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set

from src.services.reference_graph.reference_extractor import ExtractedReference
from src.services.reference_graph.structural_tree import ContractStructuralTree, ParaNode

# Truncation limits
MAX_SNIPPET_WORDS = 80
MAX_TOTAL_BUNDLE_WORDS = 450


def _truncate_snippet(text: str, max_words: int = MAX_SNIPPET_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " [...]"


class ContextBundler:
    """Extracts and formats referenced clause snippets for evaluation prompts."""

    def __init__(self, tree: ContractStructuralTree, references: List[ExtractedReference]) -> None:
        self.tree = tree
        self.references = references
        # Map para_index -> list of ExtractedReferences
        self.para_to_refs: Dict[int, List[ExtractedReference]] = {}
        for ref in references:
            self.para_to_refs.setdefault(ref.source_para_index, []).append(ref)

    def build_bundle_for_indices(
        self,
        para_indices: Sequence[int],
        party_represented: Optional[str] = None,
    ) -> Optional[str]:
        """
        Builds a concise Markdown block of referenced clauses for the given paragraphs.
        Returns None if no cross-references are present or resolvable.
        """
        # Collect all referenced target nodes
        target_nodes: List[ParaNode] = []
        seen_addresses: Set[str] = set()

        for idx in para_indices:
            refs = self.para_to_refs.get(idx, [])
            for ref in refs:
                for tgt in ref.target_nodes:
                    if tgt.para_index in para_indices:
                        continue  # Skip references inside the group itself
                    if tgt.canonical_address not in seen_addresses:
                        seen_addresses.add(tgt.canonical_address)
                        target_nodes.append(tgt)

        if not target_nodes:
            return None

        # Build formatted lines up to word budget
        lines: List[str] = []
        total_words = 0

        lines.append("\n### CROSS-CLAUSE REFERENCE CONTEXT")
        lines.append("The clause under review explicitly or implicitly references the following sections.")
        lines.append("Use them as supplementary context to assess caps, scope, conditions, or carve-outs:\n")

        for node in target_nodes:
            snippet = _truncate_snippet(node.text.strip())
            snippet_wc = len(snippet.split())
            if total_words + snippet_wc > MAX_TOTAL_BUNDLE_WORDS:
                lines.append(f"- *[Additional references omitted to preserve token budget]*")
                break

            total_words += snippet_wc
            heading_info = f" ({node.heading_text})" if node.heading_text else ""
            lines.append(f"- **[{node.canonical_address}] {node.display_title}{heading_info}**:")
            lines.append(f'  "{snippet}"')

        if party_represented:
            lines.append(f"\n*Review Perspective: Representing {party_represented}.*")

        return "\n".join(lines)
