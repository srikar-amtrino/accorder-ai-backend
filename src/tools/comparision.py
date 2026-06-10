from pathlib import Path
from typing import Dict, List

from docx.document import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.config.logging import get_logger
from src.core.container import get_bedrock_model, get_session_manager
from src.schemas.comparision import (
    ChangeEntry,
    CompareResponse,
    CompareSummary,
    HolisticChange,
    HolisticCompareResponse,
    SectionGroup,
)
from src.services.llm.base_model import BaseLLMModel

logger = get_logger(__name__)

AGENT_NAME = "document_comparison_agent"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v1"
_COMPARISON_SYSTEM = (_PROMPTS_DIR / "clause_comparison_holistic_system.mustache").read_text(encoding="utf-8")
_COMPARISON_USER = (_PROMPTS_DIR / "clause_comparison_holistic_user.mustache").read_text(encoding="utf-8")

# Output-token budget for the single holistic comparison call. Kept generous so a
# heavily redlined document never truncates mid-list — a truncated list is a missed change.
_HOLISTIC_MAX_TOKENS = 20000


def _extract_document_text(document: Document) -> str:
    """Extract clean, readable text from a docx Document in document order.

    Uses paragraph text (which preserves the spacing around styled/bold runs) plus
    table-cell text, so nothing is dropped. This deliberately avoids the chunking parser,
    whose run-joining drops spaces around bold terms (e.g. "Epit Specifications" becomes
    "EpitSpecifications") and corrupts the comparison.
    """

    lines: List[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            if paragraph.text.strip():
                lines.append(paragraph.text)
        elif tag == "tbl":
            table = Table(child, document)
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def _to_change_entry(change: HolisticChange) -> ChangeEntry:
    """Map a holistic LLM change onto the public ChangeEntry response contract."""

    return ChangeEntry(
        clause_name=change.clause_name,
        section=change.section,
        change_type=change.change_type,
        modification_type=change.modification_type,
        risk_level=change.risk_level,
        affected_party=change.affected_party,
        confidence="high",
        text_from_doc_a=change.text_from_doc_a,
        text_from_doc_b=change.text_from_doc_b,
        summary=change.summary,
        is_substantive=change.is_substantive,
    )


def group_by_section(changes: List[ChangeEntry]) -> List[SectionGroup]:
    """Group change entries by parent section heading."""

    section_map: Dict[str, List[ChangeEntry]] = {}
    for change in changes:
        section = change.section or "General / Ungrouped"
        section_map.setdefault(section, []).append(change)
    return [SectionGroup(section_name=name, changes=entries) for name, entries in section_map.items()]


def _compute_summary(changes: List[ChangeEntry]) -> CompareSummary:
    """Compute aggregate statistics from all detected changes."""

    added = sum(1 for c in changes if c.change_type == "added")
    removed = sum(1 for c in changes if c.change_type == "removed")
    modified = sum(1 for c in changes if c.change_type == "modified")
    reordered = sum(1 for c in changes if c.change_type == "reordered")
    high_risk = sum(1 for c in changes if c.risk_level == "high")

    if high_risk > 0:
        overall_risk = "high"
    elif any(c.risk_level == "medium" for c in changes):
        overall_risk = "medium"
    else:
        overall_risk = "low"

    return CompareSummary(
        total_changes=len(changes),
        added=added,
        removed=removed,
        modified=modified,
        reordered=reordered,
        overall_risk=overall_risk,
        high_risk_count=high_risk,
        llm_calls_made=1,
        llm_calls_skipped=0,
    )


def _zero_changes_summary() -> CompareSummary:
    """Summary for identical or empty comparisons (no LLM call made)."""

    return CompareSummary(
        total_changes=0,
        added=0,
        removed=0,
        modified=0,
        reordered=0,
        overall_risk="low",
        high_risk_count=0,
        llm_calls_made=0,
        llm_calls_skipped=0,
    )


async def _compare_documents_holistic(doc_text_a: str, doc_text_b: str, llm_client: BaseLLMModel, session_id: str) -> List[HolisticChange]:
    """Send both full documents to the LLM in a single call and return the change list."""

    response: HolisticCompareResponse = await llm_client.generate(  # type: ignore[assignment]
        prompt=_COMPARISON_USER,
        context={"document_a": doc_text_a, "document_b": doc_text_b},
        response_model=HolisticCompareResponse,
        session_id=session_id,
        system_message=_COMPARISON_SYSTEM,
        cache_system=True,
        max_tokens=_HOLISTIC_MAX_TOKENS,
    )
    return response.changes


async def run(session_id: str, document_a: Document, document_b: Document) -> CompareResponse:
    """Compare two documents with a single holistic LLM call and return their differences."""

    llm_client = get_bedrock_model()
    session_manager = get_session_manager()

    doc_text_a = _extract_document_text(document_a)
    doc_text_b = _extract_document_text(document_b)
    hash_a = hash(doc_text_a)
    hash_b = hash(doc_text_b)

    # Cache hit only if BOTH document hashes match in the same order — A->B and B->A
    # produce different comparisons, so the cache key is direction-sensitive.
    cached_data = None
    session_data = session_manager.get_session(session_id=session_id)
    if session_data is not None:
        cached_data = session_data.tool_results.get(AGENT_NAME)
    if cached_data and cached_data.get("doc_1_hash") == hash_a and cached_data.get("doc_2_hash") == hash_b:
        logger.info("Cache hit for session", session_id=session_id, agent=AGENT_NAME)
        return CompareResponse(
            success=cached_data.get("success", True),
            message=cached_data.get("message"),
            summary=cached_data.get("summary", _zero_changes_summary()),
            sections=cached_data.get("sections", []),
        )

    # Guard: same document
    if hash_a == hash_b:
        return CompareResponse(
            success=True,
            message="Both documents are identical. Provide two different documents to compare.",
            summary=_zero_changes_summary(),
            sections=[],
        )

    # Guard: both documents empty
    if not doc_text_a.strip() and not doc_text_b.strip():
        return CompareResponse(success=True, summary=_zero_changes_summary(), sections=[])

    logger.info("Comparing documents holistically", chars_a=len(doc_text_a), chars_b=len(doc_text_b), session_id=session_id)

    try:
        changes = await _compare_documents_holistic(doc_text_a, doc_text_b, llm_client, session_id)
    except Exception as exc:
        logger.error("Holistic comparison failed", error=str(exc), session_id=session_id)
        return CompareResponse(
            success=False,
            error=f"Comparison failed: {exc}",
            summary=_zero_changes_summary(),
            sections=[],
        )

    entries = [_to_change_entry(change) for change in changes]
    sections = group_by_section(entries)
    summary = _compute_summary(entries)

    logger.info(
        "Compare complete",
        total_changes=summary.total_changes,
        added=summary.added,
        removed=summary.removed,
        modified=summary.modified,
        reordered=summary.reordered,
        session_id=session_id,
    )

    message = "Both documents are identical. No differences found." if summary.total_changes == 0 else None

    return CompareResponse(success=True, message=message, summary=summary, sections=sections)
