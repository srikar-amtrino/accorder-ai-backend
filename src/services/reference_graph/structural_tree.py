"""
src/services/reference_graph/structural_tree.py
-----------------------------------------------
Constructs a canonical structural tree of the contract document.
Assigns hierarchical addresses (e.g. S1, S7.a, S7.c.i, ExhA.S3.a.i)
to each paragraph, resolves sub-clause addresses even when bullets
are stripped, and maintains an alias lookup table for reference resolution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.schemas.general_review import TextInformation

# Standard roman numeral matcher
_ROMAN_RE = re.compile(r"^(?:[ivxlcdm]+)$", re.IGNORECASE)

# Matches top-level sections: "SECTION 7. Title", "ARTICLE III. Title", "Section 1 - Term"
_SECTION_HEADER_RE = re.compile(
    r"^(?:SECTION|ARTICLE|CLAUSE|PART)\s+([0-9A-Z]+)[\.\:\-\s]+(.*)$",
    re.IGNORECASE,
)

# Matches standalone numbered headings: "3. Investment Management Services." or "4. Duties of the City."
_NUMBERED_HEADING_RE = re.compile(
    r"^(\d+)\.\s+([A-Z][A-Za-z0-9\s\,\-\'\(\)]+[\.\:]?.*)$"
)

# Matches back-matter / exhibits: "EXHIBIT A: Scope of Services", "SCHEDULE 1"
_BACK_MATTER_RE = re.compile(
    r"^(?:EXHIBIT|SCHEDULE|ATTACHMENT|ANNEX|APPENDIX)\s+([A-Z0-9]+)[\:\.\-\s]*(.*)$",
    re.IGNORECASE,
)

# Matches execution block
_EXECUTION_BLOCK_RE = re.compile(
    r"(?:IN\s+WITNESS\s+WHEREOF|EXECUTED\s+as\s+of|NOW\s+THEREFORE)",
    re.IGNORECASE,
)

# Explicit alpha marker: "a. ", "(a) ", "a) "
_ALPHA_MARKER_RE = re.compile(r"^(?:\(([a-z])\)|([a-z])[\.\)])\s+(.*)$", re.IGNORECASE)

# Explicit roman marker: "(i) ", "i. ", "i) "
_ROMAN_MARKER_RE = re.compile(r"^(?:\(([ivxlcdm]+)\)|([ivxlcdm]+)[\.\)])\s+(.*)$", re.IGNORECASE)

# Explicit numeric sub-marker: "1. ", "(1) ", "1) "
_NUMERIC_SUB_MARKER_RE = re.compile(r"^(?:\((\d+)\)|(\d+)[\.\)])\s+(.*)$")

# Run-on inline heading: "Discrimination Prohibited. Consultant agrees to..."
_INLINE_HEADING_RE = re.compile(r"^([A-Z][A-Za-z0-9\s\,\-\'\(\)\/]{2,50}\.)\s+(.*)$")


def _to_alpha(n: int) -> str:
    """Convert 1-based integer to alpha: 1 -> 'a', 2 -> 'b', 26 -> 'z', 27 -> 'aa'."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(97 + rem) + result
    return result


def _alpha_to_int(s: str) -> int:
    """Convert alpha string 'a' -> 1, 'b' -> 2, etc."""
    s = s.lower().strip()
    res = 0
    for char in s:
        res = res * 26 + (ord(char) - ord('a') + 1)
    return res


@dataclass
class ParaNode:
    """Structural node representing a single paragraph in the contract."""
    para_index: int
    paraindetifier: str
    text: str
    canonical_address: str       # e.g. "S7", "S7.a", "S7.c.i", "ExhA.S3.a.i"
    display_title: str          # e.g. "Section 7: Compliance with Equal Opportunity..."
    section_id: str             # e.g. "S7", "ExhA.S3"
    sub_address: str            # e.g. "a", "c.i", ""
    depth: int                  # 0 = top section, 1 = subsection (a), 2 = item (i)
    is_heading: bool = False
    is_back_matter: bool = False
    heading_text: Optional[str] = None
    aliases: Set[str] = field(default_factory=set)


class ContractStructuralTree:
    """
    Builds a hierarchical address tree for the entire contract document.
    Enables instant bidirectional lookup between paragraph index and canonical addresses.
    """

    def __init__(self, paragraphs: Sequence[TextInformation]) -> None:
        self.paragraphs = paragraphs
        self.nodes: List[ParaNode] = []
        self.address_to_node: Dict[str, ParaNode] = {}
        self.alias_to_nodes: Dict[str, List[ParaNode]] = {}
        self.index_to_node: Dict[int, ParaNode] = {}
        self._build_tree()

    def _build_tree(self) -> None:
        current_scope = ""           # "" for main body, "ExhA" for Exhibit A
        current_section = ""         # "S7", "S8", etc.
        current_section_title = ""
        current_subsection = ""      # "a", "b", etc.
        current_sub_idx = 0          # counter for inferred subsections
        current_item_idx = 0         # counter for inferred sub-items
        in_execution_block = False

        for idx, para in enumerate(self.paragraphs):
            raw_text = para.text.strip()
            first_line = raw_text.split("\n", 1)[0].strip()

            # Check execution block
            if _EXECUTION_BLOCK_RE.search(first_line) and not current_scope:
                in_execution_block = True

            # 1. Check for Exhibit / Back matter start
            bm_match = _BACK_MATTER_RE.match(first_line)
            if bm_match:
                in_execution_block = False
                exh_id = bm_match.group(1).upper()
                current_scope = f"Exh{exh_id}"
                current_section = ""
                current_section_title = first_line
                current_subsection = ""
                current_sub_idx = 0
                current_item_idx = 0
                exhibit_sec_idx = 0

                node = ParaNode(
                    para_index=idx,
                    paraindetifier=para.paraindetifier,
                    text=raw_text,
                    canonical_address=current_scope,
                    display_title=first_line,
                    section_id=current_scope,
                    sub_address="",
                    depth=0,
                    is_heading=True,
                    is_back_matter=True,
                    heading_text=first_line,
                )
                self._register_node(node)
                continue

            # 2. Check for Top-Level Section Header ("SECTION 7. Title")
            sec_match = _SECTION_HEADER_RE.match(first_line)
            if sec_match:
                sec_num = sec_match.group(1)
                sec_title = sec_match.group(2).strip()
                prefix = f"{current_scope}." if current_scope else ""
                current_section = f"{prefix}S{sec_num}"
                current_section_title = f"Section {sec_num}: {sec_title}" if sec_title else f"Section {sec_num}"
                current_subsection = ""
                current_sub_idx = 0
                current_item_idx = 0

                node = ParaNode(
                    para_index=idx,
                    paraindetifier=para.paraindetifier,
                    text=raw_text,
                    canonical_address=current_section,
                    display_title=current_section_title,
                    section_id=current_section,
                    sub_address="",
                    depth=0,
                    is_heading=True,
                    is_back_matter=bool(current_scope),
                    heading_text=first_line,
                )
                self._register_node(node)
                continue

            # 3. Check for Numbered Heading in Exhibit or Body ("3. Investment Management Services.")
            num_match = _NUMBERED_HEADING_RE.match(first_line)
            if num_match and (current_scope or len(raw_text.split()) < 30):
                sec_num = num_match.group(1)
                sec_title = num_match.group(2).strip()
                prefix = f"{current_scope}." if current_scope else ""
                current_section = f"{prefix}S{sec_num}"
                current_section_title = f"{sec_num}. {sec_title}"
                current_subsection = ""
                current_sub_idx = 0
                current_item_idx = 0

                node = ParaNode(
                    para_index=idx,
                    paraindetifier=para.paraindetifier,
                    text=raw_text,
                    canonical_address=current_section,
                    display_title=current_section_title,
                    section_id=current_section,
                    sub_address="",
                    depth=0,
                    is_heading=True,
                    is_back_matter=bool(current_scope),
                    heading_text=first_line,
                )
                self._register_node(node)
                continue

            # 3b. Check for unnumbered section headings inside Exhibit / Back Matter
            if current_scope and not sec_match and not num_match:
                inline_match = _INLINE_HEADING_RE.match(first_line)
                is_standalone = (
                    len(first_line.split()) <= 8
                    and first_line.endswith(".")
                    and not first_line.rstrip().endswith((";", ","))
                    and first_line[0].isupper()
                )
                if inline_match or is_standalone:
                    heading = inline_match.group(1).rstrip(".") if inline_match else first_line.rstrip(".")
                    if heading and heading[0].isupper() and not first_line.rstrip().endswith((";", ",")):
                        exhibit_sec_idx += 1
                        current_section = f"{current_scope}.S{exhibit_sec_idx}"
                        current_section_title = heading
                        current_subsection = ""
                        current_sub_idx = 0
                        current_item_idx = 0
                        node = ParaNode(
                            para_index=idx,
                            paraindetifier=para.paraindetifier,
                            text=raw_text,
                            canonical_address=current_section,
                            display_title=f"{current_scope} - {heading}",
                            section_id=current_scope,
                            sub_address=f"S{exhibit_sec_idx}",
                            depth=1,
                            is_heading=True,
                            is_back_matter=True,
                            heading_text=heading,
                        )
                        self._register_node(node)
                        continue

            # 4. If we are inside a section, check sub-clause structure
            if current_section:
                # 4a. Check explicit alpha marker: "a. ", "(a) "
                alpha_match = _ALPHA_MARKER_RE.match(first_line)
                if alpha_match:
                    letter = (alpha_match.group(1) or alpha_match.group(2)).lower()
                    current_subsection = letter
                    current_sub_idx = _alpha_to_int(letter)
                    current_item_idx = 0
                    addr = f"{current_section}.{current_subsection}"
                    node = ParaNode(
                        para_index=idx,
                        paraindetifier=para.paraindetifier,
                        text=raw_text,
                        canonical_address=addr,
                        display_title=f"{current_section_title} ({letter})",
                        section_id=current_section,
                        sub_address=current_subsection,
                        depth=1,
                        is_back_matter=bool(current_scope),
                    )
                    self._register_node(node)
                    continue

                # 4b. Check explicit roman marker: "i. ", "(i) "
                roman_match = _ROMAN_MARKER_RE.match(first_line)
                if roman_match:
                    roman = (roman_match.group(1) or roman_match.group(2)).lower()
                    parent = f"{current_section}.{current_subsection}" if current_subsection else current_section
                    addr = f"{parent}.{roman}"
                    node = ParaNode(
                        para_index=idx,
                        paraindetifier=para.paraindetifier,
                        text=raw_text,
                        canonical_address=addr,
                        display_title=f"{current_section_title} ({current_subsection})({roman})" if current_subsection else f"{current_section_title} ({roman})",
                        section_id=current_section,
                        sub_address=f"{current_subsection}.{roman}" if current_subsection else roman,
                        depth=2,
                        is_back_matter=bool(current_scope),
                    )
                    self._register_node(node)
                    continue

                # 4c. Check explicit numeric sub-marker: "(1) ", "1. "
                num_sub_match = _NUMERIC_SUB_MARKER_RE.match(first_line)
                if num_sub_match:
                    num_sub = num_sub_match.group(1) or num_sub_match.group(2)
                    parent = f"{current_section}.{current_subsection}" if current_subsection else current_section
                    addr = f"{parent}.{num_sub}"
                    node = ParaNode(
                        para_index=idx,
                        paraindetifier=para.paraindetifier,
                        text=raw_text,
                        canonical_address=addr,
                        display_title=f"{current_section_title} ({num_sub})",
                        section_id=current_section,
                        sub_address=f"{current_subsection}.{num_sub}" if current_subsection else num_sub,
                        depth=2,
                        is_back_matter=bool(current_scope),
                    )
                    self._register_node(node)
                    continue

                # 4d. Check inline heading ("Discrimination Prohibited. Consultant agrees to...")
                # In contracts without bullet numbers, inline headings serve as the (a), (b), (c) subsections
                inline_match = _INLINE_HEADING_RE.match(first_line)
                if inline_match:
                    heading = inline_match.group(1).rstrip(".")
                    current_sub_idx += 1
                    current_subsection = _to_alpha(current_sub_idx)
                    current_item_idx = 0
                    addr = f"{current_section}.{current_subsection}"
                    node = ParaNode(
                        para_index=idx,
                        paraindetifier=para.paraindetifier,
                        text=raw_text,
                        canonical_address=addr,
                        display_title=f"{current_section_title} ({current_subsection}) - {heading}",
                        section_id=current_section,
                        sub_address=current_subsection,
                        depth=1,
                        is_heading=True,
                        is_back_matter=bool(current_scope),
                        heading_text=heading,
                    )
                    self._register_node(node)
                    continue

                # 4e. Sub-paragraph / sub-bullet continuation under current subsection
                if current_subsection:
                    current_item_idx += 1
                    sub_addr = f"{current_subsection}.item{current_item_idx}"
                    addr = f"{current_section}.{sub_addr}"
                    node = ParaNode(
                        para_index=idx,
                        paraindetifier=para.paraindetifier,
                        text=raw_text,
                        canonical_address=addr,
                        display_title=f"{current_section_title} ({current_subsection}) [item {current_item_idx}]",
                        section_id=current_section,
                        sub_address=sub_addr,
                        depth=2,
                        is_back_matter=bool(current_scope),
                    )
                    self._register_node(node)
                    continue

                # Default section body paragraph
                node = ParaNode(
                    para_index=idx,
                    paraindetifier=para.paraindetifier,
                    text=raw_text,
                    canonical_address=current_section,
                    display_title=current_section_title,
                    section_id=current_section,
                    sub_address="",
                    depth=0,
                    is_back_matter=bool(current_scope),
                )
                self._register_node(node)
                continue

            # 5. Non-section paragraph (front matter, preamble, recitals)
            addr = f"P{idx}"
            node = ParaNode(
                para_index=idx,
                paraindetifier=para.paraindetifier,
                text=raw_text,
                canonical_address=addr,
                display_title=f"Paragraph {idx}",
                section_id=addr,
                sub_address="",
                depth=0,
                is_back_matter=bool(current_scope),
            )
            self._register_node(node)

    def _register_node(self, node: ParaNode) -> None:
        self.nodes.append(node)
        self.index_to_node[node.para_index] = node
        self.address_to_node[node.canonical_address] = node

        # Generate all natural aliases
        aliases = self._generate_aliases(node)
        node.aliases = aliases
        for alias in aliases:
            norm_alias = self._normalize_alias(alias)
            self.alias_to_nodes.setdefault(norm_alias, []).append(node)

    @staticmethod
    def _normalize_alias(alias: str) -> str:
        """Normalizes an alias string for case-insensitive and punctuation-tolerant lookup."""
        return re.sub(r"[\s\.\,\:\-\(\)\[\]]", "", alias).casefold()

    def _generate_aliases(self, node: ParaNode) -> Set[str]:
        aliases: Set[str] = {node.canonical_address}

        # S7 -> "Section 7", "Sec 7", "Article 7", "7"
        sec_num_match = re.search(r"S(\d+)", node.section_id)
        if sec_num_match:
            sec_num = sec_num_match.group(1)
            aliases.add(f"Section {sec_num}")
            aliases.add(f"Sec. {sec_num}")
            aliases.add(f"Article {sec_num}")
            aliases.add(f"Art. {sec_num}")
            aliases.add(f"Clause {sec_num}")

            if node.sub_address:
                # e.g. sub_address is "a" or "a.i"
                parts = node.sub_address.split(".")
                alpha_part = parts[0]
                aliases.add(f"Section {sec_num}({alpha_part})")
                aliases.add(f"Section {sec_num}.{alpha_part}")
                aliases.add(f"Section {sec_num}{alpha_part}")
                aliases.add(f"Sec. {sec_num}({alpha_part})")
                aliases.add(f"Article {sec_num}({alpha_part})")
                aliases.add(f"{sec_num}({alpha_part})")

                if len(parts) > 1:
                    item_part = parts[1]
                    aliases.add(f"Section {sec_num}({alpha_part})({item_part})")
                    aliases.add(f"Section {sec_num}.{alpha_part}.{item_part}")
                    aliases.add(f"{sec_num}({alpha_part})({item_part})")

        # Exhibit aliases: ExhA -> "Exhibit A", "Exh. A", "Schedule A"
        if "Exh" in node.canonical_address:
            exh_match = re.search(r"Exh([A-Z0-9]+)", node.canonical_address)
            if exh_match:
                exh_letter = exh_match.group(1)
                aliases.add(f"Exhibit {exh_letter}")
                aliases.add(f"Exh {exh_letter}")
                aliases.add(f"Schedule {exh_letter}")
                aliases.add(f"Attachment {exh_letter}")

        # Heading alias
        if node.heading_text:
            aliases.add(node.heading_text)

        return aliases

    def resolve_reference(self, raw_ref: str) -> List[ParaNode]:
        """Resolves a raw reference string like 'Section 7(a)' or 'Exhibit A' to matching ParaNode(s)."""
        norm = self._normalize_alias(raw_ref)
        if norm in self.alias_to_nodes:
            nodes = list(self.alias_to_nodes[norm])
            # If resolving an exhibit (e.g. "exhibit a"), also include substantive section nodes of that exhibit
            if norm.startswith("exhibit ") or norm.startswith("exh "):
                exh_letter = norm.split()[-1].upper()
                exh_scope = f"Exh{exh_letter}"
                for n in self.nodes:
                    if n.section_id == exh_scope and n not in nodes and n.is_heading:
                        nodes.append(n)
            return nodes

        # Try range resolution: e.g. "Section 7(a)-(c)" or "Section 7(a)-(d)"
        range_match = re.search(
            r"(?:Section|Article|Clause|Sec\.)?\s*(\d+)[\.\s]*\(([a-z])\)\s*[-–—]\s*(?:\(([a-z])\)|([a-z]))",
            raw_ref,
            re.IGNORECASE,
        )
        if range_match:
            sec_num = range_match.group(1)
            start_letter = range_match.group(2).lower()
            end_letter = (range_match.group(3) or range_match.group(4)).lower()
            start_idx = _alpha_to_int(start_letter)
            end_idx = _alpha_to_int(end_letter)

            results: List[ParaNode] = []
            for i in range(start_idx, end_idx + 1):
                letter = _to_alpha(i)
                key = self._normalize_alias(f"Section {sec_num}({letter})")
                if key in self.alias_to_nodes:
                    results.extend(self.alias_to_nodes[key])
            if results:
                return results

        return []
