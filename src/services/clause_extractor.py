"""
Shared clause-extraction utilities.

Used by both the compare agent and the general review agent so that both
see the same clause boundaries and headings. Originally lived in
``src/agents/compare.py``; lifted here to avoid duplication.

The core ideas:

- ``ClauseUnit`` is the shared clause representation (heading, content,
  position, embedding).
- ``_extract_heading_fallback`` derives a heading from the first line of
  content when the parser's ``section_heading`` metadata is absent or
  unhelpful (e.g. the parser sometimes stamps every chunk with the same
  outer title like "Terms and Conditions").
- ``extract_clauses`` walks a single document in a session (by
  ``document_id``) and returns its clause units.
- ``extract_all_clauses`` walks every ingested document in a session (or
  falls back to the flat chunk store) and returns all clause units.
"""

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class ClauseUnit:
    """A single clause extracted from a document's chunks."""

    clause_id: str
    heading: Optional[str]
    content: str
    position: int  # chunk index for ordering
    doc_order: int = 0  # sequential index within its document (0, 1, 2...)
    embedding: List[float] = field(default_factory=list)


# --- Heading extraction ------------------------------------------------------


_FUNCTION_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "this",
    "that",
    "these",
    "those",
    "and",
    "or",
    "if",
    "but",
    "for",
    "it",
    "we",
    "you",
    "he",
    "she",
    "they",
    "all",
    "any",
    "each",
    "please",
    "however",
    "furthermore",
    "moreover",
    "also",
    "in",
    "on",
    "at",
    "to",
    "of",
    "by",
    "with",
    "from",
}


def _extract_heading_fallback(content: str) -> Optional[str]:
    """Derive a heading from content when metadata lacks a useful section_heading.

    Matches patterns like:
      - "Audit Rights. Epit shall..."       -> "Audit Rights"
      - "1.2 Termination. Either party..."  -> "1.2 Termination"
      - "Section 5 - Liability"             -> "Section 5 - Liability"
      - "ARTICLE III"                       -> "ARTICLE III"
    """
    first_line = content.strip().split("\n")[0].strip()

    # Numbered sections: "1.2 Something" or "Section X" or "ARTICLE X"
    if re.match(r"^(\d+\.[\d.]*\s|Section\s|ARTICLE\s)", first_line):
        return first_line

    # ALL CAPS heading on its own line
    if re.match(r"^[A-Z][A-Z\s]{4,}$", first_line):
        return first_line

    # Title-case phrase before first ". " -- e.g. "Audit Rights. Content..."
    m = re.match(r"^([A-Z][A-Za-z\s/&,-]{2,60})\.\s", first_line)
    if m:
        candidate = m.group(1).strip()
        words = candidate.lower().split()
        content_words = [w for w in words if w not in _FUNCTION_WORDS]
        if len(content_words) >= 1 and len(words) <= 8:
            return candidate

    return None


# --- Extraction helpers ------------------------------------------------------


def _clause_from_chunk(
    chunk: Any,
    chunk_index: int,
    doc_order: int,
    clause_id: str,
) -> Optional[ClauseUnit]:
    """Turn a single chunk into a ``ClauseUnit``.

    Returns ``None`` when the chunk has no usable content.
    """
    if chunk is None:
        return None

    content = (getattr(chunk, "content", None) or "").strip()
    if not content:
        return None

    metadata = getattr(chunk, "metadata", None)
    metadata_heading = None
    if isinstance(metadata, dict):
        metadata_heading = metadata.get("section_heading")

    # Prefer clause-level title extracted from content over generic
    # metadata (which is often just "Terms and Conditions").
    heading = _extract_heading_fallback(content) or metadata_heading

    return ClauseUnit(
        clause_id=clause_id,
        heading=heading,
        content=content,
        position=chunk_index,
        doc_order=doc_order,
        embedding=getattr(chunk, "embedding_vector", None) or [],
    )


def extract_clauses(session: Any, document_id: str) -> List[ClauseUnit]:
    """Build ``ClauseUnit`` list from a single document's chunks in the session.

    Empty chunks are skipped. ``doc_order`` provides sequential numbering
    independent of chunk_index gaps between documents.
    """
    doc_info = session.documents.get(document_id)
    if doc_info is None:
        return []

    chunk_indices = doc_info.get("chunk_indices", [])
    clauses: List[ClauseUnit] = []
    order = 0

    for idx in chunk_indices:
        chunk = session.chunk_store.get(idx)
        clause = _clause_from_chunk(
            chunk,
            chunk_index=idx,
            doc_order=order,
            clause_id=f"{document_id}_{idx}",
        )
        if clause is None:
            continue
        clauses.append(clause)
        order += 1

    return clauses


def extract_all_clauses(session: Any) -> List[ClauseUnit]:
    """Extract clauses from every document ingested in a session.

    Primary path walks each registered document via ``extract_clauses``.
    Fallback path iterates ``session.chunk_store`` directly, ordered by
    ``chunk_index``, for sessions where document metadata is absent.
    """
    clauses: List[ClauseUnit] = []

    documents = getattr(session, "documents", None) or {}
    if documents:
        for doc_id in documents.keys():
            clauses.extend(extract_clauses(session, doc_id))
        if clauses:
            return clauses

    # Fallback: walk the flat chunk store in chunk_index order.
    chunk_store = getattr(session, "chunk_store", None) or {}
    ordered = sorted(
        chunk_store.items(),
        key=lambda kv: getattr(kv[1], "chunk_index", kv[0]),
    )

    order = 0
    for idx, chunk in ordered:
        clause = _clause_from_chunk(
            chunk,
            chunk_index=idx,
            doc_order=order,
            clause_id=f"session_{idx}",
        )
        if clause is None:
            continue
        clauses.append(clause)
        order += 1

    return clauses
