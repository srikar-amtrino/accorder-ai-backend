import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "comparsion"
_COMPARISON_SYSTEM = (_PROMPTS_DIR / "clause_comparison_diff_system.mustache").read_text(encoding="utf-8")
_COMPARISON_USER = (_PROMPTS_DIR / "clause_comparison_diff_user.mustache").read_text(encoding="utf-8")

# Output-token budget for the single classification call. Generous so the change list
# never truncates mid-stream — a truncated list is a missed change.
_DIFF_MAX_TOKENS = 20000


def _extract_paragraphs(document: Document) -> List[str]:
    """Extract clean paragraph and table text as an ordered list of lines.

    Paragraph text preserves the spacing around styled/bold runs (unlike the chunking
    parser, which joins runs and drops spaces around bold terms). Table rows are flattened
    so no content is missed.
    """

    lines: List[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            if paragraph.text.strip():
                lines.append(paragraph.text.strip())
        elif tag == "tbl":
            table = Table(child, document)
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    lines.append(" | ".join(cells))
    return lines


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.;:!?])\s+(?=[A-Z(“\"'])")


def _split_into_sentences(paragraphs: List[str]) -> List[str]:
    """Split paragraphs into sentence-level units for fine-grained diffing.

    Contracts often pack many sub-clauses into one large paragraph, so diffing whole
    paragraphs lumps unrelated edits together and sends large unchanged spans to the LLM.
    Splitting into sentences isolates each edit into its own small region. The split is
    deterministic, so unchanged text splits identically on both sides and still aligns.
    """

    units: List[str] = []
    for paragraph in paragraphs:
        for sentence in _SENTENCE_BOUNDARY.split(paragraph):
            sentence = sentence.strip()
            if sentence:
                units.append(sentence)
    return units


def _normalize(text: str) -> str:
    """Whitespace-insensitive key used to align paragraphs during diffing."""

    return " ".join(text.split())


def _strip_whitespace(text: str) -> str:
    """All-whitespace-removed key used to detect spacing-only (trivial) differences."""

    return "".join(text.split())


def _build_diff_digest(units_a: List[str], units_b: List[str]) -> Tuple[str, int]:
    """Diff the two sentence-unit lists and assemble a compact digest of only the changed regions.

    Unchanged units are skipped entirely (this is what bounds cost to the size of the change,
    not the document). Regions that differ only in whitespace are treated as trivial and
    dropped. Returns the digest text and the number of changed regions kept.
    """

    keys_a = [_normalize(u) for u in units_a]
    keys_b = [_normalize(u) for u in units_b]
    matcher = difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False)

    regions: List[Tuple[str, List[str], List[str]]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        old_block = units_a[i1:i2]
        new_block = units_b[j1:j2]

        # Trivial: identical once all whitespace is removed -> a spacing/formatting artifact.
        if _strip_whitespace("".join(old_block)) == _strip_whitespace("".join(new_block)):
            continue

        # One unit of unchanged context before the region (often the clause heading),
        # taken from whichever side has a preceding unit. For orientation only.
        context = ""
        if i1 > 0:
            context = units_a[i1 - 1]
        elif j1 > 0:
            context = units_b[j1 - 1]

        regions.append((context, old_block, new_block))

    if not regions:
        return "", 0

    parts: List[str] = []
    for index, (context, old_block, new_block) in enumerate(regions, start=1):
        lines = [f"=== Change region {index} ==="]
        if context:
            lines.append(f"[Context - unchanged, for orientation only]: {context}")
        lines.append("[ORIGINAL]:")
        lines.append("\n".join(old_block) if old_block else "(nothing - this text appears only in the revised version)")
        lines.append("[REVISED]:")
        lines.append("\n".join(new_block) if new_block else "(nothing - this text appeared only in the original version)")
        parts.append("\n".join(lines))

    return "\n\n".join(parts), len(regions)


def _to_change_entry(change: HolisticChange) -> ChangeEntry:
    """Map a classified change onto the public ChangeEntry response contract."""

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
    """Summary for identical, empty, or only-trivially-different comparisons."""

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


async def _classify_changes(diff_digest: str, llm_client: BaseLLMModel, session_id: str) -> List[HolisticChange]:
    """Send the changed-regions digest to the LLM in one call and return the classified change list."""

    response: HolisticCompareResponse = await llm_client.generate(
        prompt=_COMPARISON_USER,
        context={"diff_digest": diff_digest},
        response_model=HolisticCompareResponse,
        session_id=session_id,
        system_message=_COMPARISON_SYSTEM,
        # cache_system=True,
        # max_tokens=_DIFF_MAX_TOKENS,
    )
    return response.changes


async def compare_documents_service(session_id: str, document_a: Document, document_b: Document) -> CompareResponse:
    """Compare two documents: deterministic paragraph diff, then one LLM call to classify the changed regions."""

    llm_client = get_bedrock_model()
    session_manager = get_session_manager()

    paras_a = _extract_paragraphs(document_a)
    paras_b = _extract_paragraphs(document_b)
    text_a = "\n".join(paras_a)
    text_b = "\n".join(paras_b)
    hash_a = hash(text_a)
    hash_b = hash(text_b)

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
    if not text_a.strip() and not text_b.strip():
        return CompareResponse(success=True, summary=_zero_changes_summary(), sections=[])

    units_a = _split_into_sentences(paras_a)
    units_b = _split_into_sentences(paras_b)
    digest, region_count = _build_diff_digest(units_a, units_b)
    logger.info("Diff computed", changed_regions=region_count, chars_a=len(text_a), chars_b=len(text_b), session_id=session_id)

    # Only whitespace/trivial differences: nothing substantive to classify.
    if region_count == 0:
        return CompareResponse(
            success=True,
            message="No substantive differences found.",
            summary=_zero_changes_summary(),
            sections=[],
        )

    try:
        changes = await _classify_changes(digest, llm_client, session_id)
    except Exception as exc:
        logger.error("Change classification failed", error=str(exc), session_id=session_id)
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
        changed_regions=region_count,
        session_id=session_id,
    )

    message = "No substantive differences found." if summary.total_changes == 0 else None

    return CompareResponse(success=True, message=message, summary=summary, sections=sections)


async def compare_documents_stream_service(session_id: str, document_a: Document, document_b: Document) -> Any:
    """Stream document differences as Server-Sent Events.

    Code computes the paragraph diff (fast); the single classification call is then streamed
    so chunks reach the client as the model produces them, instead of after the whole list is
    built. Mirrors the SSE convention used by the other streaming agents: raw text chunks wrapped
    as `data: ...` frames, terminated by `data: [DONE]`.
    """

    try:
        llm_client = get_bedrock_model()

        paras_a = _extract_paragraphs(document_a)
        paras_b = _extract_paragraphs(document_b)
        text_a = "\n".join(paras_a)
        text_b = "\n".join(paras_b)

        if hash(text_a) == hash(text_b):
            yield f"data: {json.dumps({'message': 'Both documents are identical. Provide two different documents to compare.'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        if not text_a.strip() and not text_b.strip():
            yield f"data: {json.dumps({'message': 'No content found in the documents.'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        units_a = _split_into_sentences(paras_a)
        units_b = _split_into_sentences(paras_b)
        digest, region_count = _build_diff_digest(units_a, units_b)
        logger.info("Diff computed", changed_regions=region_count, chars_a=len(text_a), chars_b=len(text_b), session_id=session_id)

        if region_count == 0:
            yield f"data: {json.dumps({'message': 'No substantive differences found.'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        stream = llm_client.generate_stream(
            prompt=_COMPARISON_USER,
            context={"diff_digest": digest},
            session_id=session_id,
            system_message=_COMPARISON_SYSTEM,
            # cache_system=True,
            # max_tokens=_DIFF_MAX_TOKENS,
        )

        async for chunk in stream:
            yield f"data: {json.dumps(chunk)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as exc:
        logger.exception("Compare streaming failed", session_id=session_id)
        yield f'data: {json.dumps({"error": str(exc)})}\n\n'
