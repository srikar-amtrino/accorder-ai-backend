"""
src/services/reference_graph/reference_extractor.py
---------------------------------------------------
Extracts explicit and implicit cross-references from contract text,
including section numbers, range references, exhibit pointers, defined terms,
statutes, and modifier clauses ("notwithstanding the foregoing").
Resolves extracted references against the ContractStructuralTree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Literal, Optional, Sequence, Set

from src.services.reference_graph.structural_tree import ContractStructuralTree, ParaNode

# Section/Article reference regex with optional subsections and ranges
_SECTION_REF_RE = re.compile(
    r"""
    \b(?:Section|Article|Clause|Part|Sec\.|Art\.)\s*
    (\d+(?:\.\d+)*)                                      # top number: 7, 7.1, 7.1.2
    (?:\s*[\(\[]([a-zA-Z]+|[ivxlcdm]+|\d+)[\)\]])*      # sub-items: (a), (i), (1)
    (?:\s*[-–—]\s*                                      # range: (a)-(c), 7-9
      (?:Section|Article|Clause|Sec\.)?\s*
      (\d+(?:\.\d+)*)?
      (?:\s*[\(\[]([a-zA-Z]+|[ivxlcdm]+|\d+)[\)\]])*
    )?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Exhibit / Schedule / Attachment references
_EXHIBIT_REF_RE = re.compile(
    r"\b(?:Exhibit|Schedule|Annex|Appendix|Attachment)\s*([A-Z0-9]+(?:\s*[-–]\s*[A-Z0-9]+)?)\b",
    re.IGNORECASE,
)

# External statute references
_STATUTE_REF_RE = re.compile(
    r"\b(?:K\.S\.A\.|U\.S\.C\.|C\.F\.R\.|§)\s*[\d\-\.]+(?:\s+et\s+seq\.)?\b",
    re.IGNORECASE,
)

# Implicit dependency indicators
_IMPLICIT_REF_RE = re.compile(
    r"\b("
    r"notwithstanding\s+(?:anything\s+to\s+the\s+contrary|the\s+foregoing|the\s+provisions\s+of)|"
    r"as\s+set\s+forth\s+(?:above|herein|below)|"
    r"as\s+defined\s+herein|"
    r"subject\s+to\s+the\s+terms\s+of|"
    r"except\s+as\s+(?:set\s+forth|provided)\s+herein|"
    r"in\s+accordance\s+with\s+(?:this\s+Agreement|the\s+terms\s+hereof)|"
    r"pursuant\s+to\s+this\s+Agreement"
    r")\b",
    re.IGNORECASE,
)

# Defined terms in quotes
_DEFINED_TERM_DEF_RE = re.compile(
    r'[\u201c\u201d\"\'\‘\’]([A-Z][A-Za-z0-9\s\,\-\']{1,40})[\u201c\u201d\"\'\‘\’]\s*(?:\([^\)]*\)\s*)?(?:means|shall mean|refers to|has the meaning)',
    re.IGNORECASE,
)


@dataclass
class ExtractedReference:
    """Represents a reference identified in a contract paragraph."""
    source_para_index: int
    source_address: str
    raw_reference: str
    ref_type: Literal["section", "section_range", "exhibit", "statute", "implicit", "defined_term"]
    target_addresses: List[str]
    target_nodes: List[ParaNode]
    is_external: bool = False
    context_snippet: Optional[str] = None


class ReferenceExtractor:
    """Scans contract paragraphs and resolves all cross-references using the structural tree."""

    def __init__(self, tree: ContractStructuralTree) -> None:
        self.tree = tree

    def extract_from_paragraph(self, para_index: int) -> List[ExtractedReference]:
        if para_index not in self.tree.index_to_node:
            return []

        node = self.tree.index_to_node[para_index]
        text = node.text
        refs: List[ExtractedReference] = []

        # 1. Explicit Section references
        for m in _SECTION_REF_RE.finditer(text):
            raw = m.group(0).strip()
            # Guard against self-reference (e.g. section heading declaring its own number)
            if node.is_heading and raw.casefold() in node.text[:len(raw) + 5].casefold():
                continue

            target_nodes = self.tree.resolve_reference(raw)
            # Filter out self
            target_nodes = [t for t in target_nodes if t.para_index != para_index]
            target_addrs = [t.canonical_address for t in target_nodes]

            ref_type: Literal["section", "section_range"] = "section_range" if ("-" in raw or "–" in raw or "—" in raw) else "section"
            refs.append(
                ExtractedReference(
                    source_para_index=para_index,
                    source_address=node.canonical_address,
                    raw_reference=raw,
                    ref_type=ref_type,
                    target_addresses=target_addrs,
                    target_nodes=target_nodes,
                    is_external=False,
                )
            )

        # 2. Exhibit references
        for m in _EXHIBIT_REF_RE.finditer(text):
            raw = m.group(0).strip()
            # Guard against self exhibit heading
            if node.is_back_matter and raw.casefold() in node.display_title.casefold():
                continue

            target_nodes = self.tree.resolve_reference(raw)
            target_nodes = [t for t in target_nodes if t.para_index != para_index]
            target_addrs = [t.canonical_address for t in target_nodes]

            refs.append(
                ExtractedReference(
                    source_para_index=para_index,
                    source_address=node.canonical_address,
                    raw_reference=raw,
                    ref_type="exhibit",
                    target_addresses=target_addrs,
                    target_nodes=target_nodes,
                    is_external=len(target_nodes) == 0,  # External if exhibit is missing from doc
                )
            )

        # 3. Statute references
        for m in _STATUTE_REF_RE.finditer(text):
            raw = m.group(0).strip()
            refs.append(
                ExtractedReference(
                    source_para_index=para_index,
                    source_address=node.canonical_address,
                    raw_reference=raw,
                    ref_type="statute",
                    target_addresses=[],
                    target_nodes=[],
                    is_external=True,
                )
            )

        # 4. Implicit modifier references ("notwithstanding the foregoing", etc.)
        for m in _IMPLICIT_REF_RE.finditer(text):
            raw = m.group(0).strip()
            # By default, "notwithstanding the foregoing" binds to preceding paragraphs in the same section
            target_nodes: List[ParaNode] = []
            if "foregoing" in raw.lower() and para_index > 0:
                # Target immediate predecessor node
                prev_node = self.tree.nodes[para_index - 1]
                target_nodes.append(prev_node)

            refs.append(
                ExtractedReference(
                    source_para_index=para_index,
                    source_address=node.canonical_address,
                    raw_reference=raw,
                    ref_type="implicit",
                    target_addresses=[t.canonical_address for t in target_nodes],
                    target_nodes=target_nodes,
                    is_external=False,
                )
            )

        return refs

    def extract_all(self) -> List[ExtractedReference]:
        all_refs: List[ExtractedReference] = []
        for idx in range(len(self.tree.paragraphs)):
            all_refs.extend(self.extract_from_paragraph(idx))
        return all_refs
