import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
# One prompt drives both endpoints: the model emits the full clause text on each side, so the
# streaming endpoint can stream it token by token (like the other agents) and the non-streaming
# endpoint accumulates the same generation.
_COMPARISON_SYSTEM = (_PROMPTS_DIR / "clause_comparison_diff_system.mustache").read_text(encoding="utf-8")
_COMPARISON_USER = (_PROMPTS_DIR / "clause_comparison_diff_user.mustache").read_text(encoding="utf-8")

# Output-token budget. Generous so the change list never truncates mid-stream — a truncated
# list is a missed change.
_DIFF_MAX_TOKENS = 20000

# Risk ordering used when collapsing duplicate entries onto a single representative.
_RISK_ORDER = {"high": 3, "medium": 2, "low": 1}


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


def _split_into_sentences(paragraphs: List[str]) -> Tuple[List[str], List[int]]:
    """Split paragraphs into sentence-level units for fine-grained diffing.

    Contracts often pack many sub-clauses into one large paragraph, so diffing whole paragraphs
    lumps unrelated edits together. Splitting into sentences isolates each edit into its own small
    region; the split is deterministic, so unchanged text splits identically on both sides and
    still aligns. Also returns each unit's parent-paragraph index so a changed region can be
    expanded back to its full parent clause.
    """

    units: List[str] = []
    owners: List[int] = []
    for para_index, paragraph in enumerate(paragraphs):
        for sentence in _SENTENCE_BOUNDARY.split(paragraph):
            sentence = sentence.strip()
            if sentence:
                units.append(sentence)
                owners.append(para_index)
    return units, owners


def _normalize(text: str) -> str:
    """Whitespace-insensitive key used to align units during diffing."""

    return " ".join(text.split())


def _strip_whitespace(text: str) -> str:
    """All-whitespace-removed key used to detect spacing-only (trivial) differences."""

    return "".join(text.split())


def _json_safe(text: str) -> str:
    """Fold clause text to characters that never need JSON escaping.

    The streaming model emits each clause as a JSON string value token by token, with no chance
    to validate or retry — so any character that would need escaping (a double quote, backslash,
    or raw newline) risks producing invalid JSON mid-stream and breaking the client's parse.
    Fold those to safe equivalents up front: double/curly quotes -> single quote, backslash ->
    slash, newlines/tabs -> space. A small cosmetic change that guarantees a valid live stream.
    """

    text = text.replace("\\", "/")
    for quote in ("“", "”", '"'):
        text = text.replace(quote, "'")
    return re.sub(r"[\n\r\t\f\v]+", " ", text).strip()


def _full_clause(paras: List[str], owners: List[int], lo: int, hi: int) -> str:
    """Full parent-clause text for the units in [lo:hi); empty when the side has no units."""

    para_indices = sorted({owners[k] for k in range(lo, hi)})
    if not para_indices:
        return ""
    return _json_safe("\n".join(paras[p] for p in para_indices))


def _build_diff_digest(paras_a: List[str], paras_b: List[str]) -> Tuple[str, int]:
    """Diff the two documents at sentence granularity and assemble a compact digest of only the
    changed regions.

    Unchanged units are skipped entirely (this is what bounds cost to the size of the change, not
    the document). Regions that differ only in whitespace are dropped. Each kept region carries
    the FULL parent clause on each side (so the model can quote the whole provision verbatim) plus
    the specific changed wording (so it can pinpoint the edit). Returns (digest, region_count).
    """

    units_a, owners_a = _split_into_sentences(paras_a)
    units_b, owners_b = _split_into_sentences(paras_b)
    keys_a = [_normalize(u) for u in units_a]
    keys_b = [_normalize(u) for u in units_b]
    matcher = difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False)

    parts: List[str] = []
    count = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        old_block = units_a[i1:i2]
        new_block = units_b[j1:j2]

        # Trivial: identical once all whitespace is removed -> a spacing/formatting artifact.
        if _strip_whitespace("".join(old_block)) == _strip_whitespace("".join(new_block)):
            continue

        clause_a = _full_clause(paras_a, owners_a, i1, i2)
        clause_b = _full_clause(paras_b, owners_b, j1, j2)
        count += 1
        lines = [
            f"=== Change region {count} ===",
            "[ORIGINAL CLAUSE]:",
            clause_a if clause_a else "(nothing - this clause appears only in the revised version)",
            "[REVISED CLAUSE]:",
            clause_b if clause_b else "(nothing - this clause appeared only in the original version)",
            "[WHAT CHANGED - original]:",
            "\n".join(old_block) if old_block else "(nothing - newly added)",
            "[WHAT CHANGED - revised]:",
            "\n".join(new_block) if new_block else "(nothing - removed)",
        ]
        parts.append("\n".join(lines))

    return "\n\n".join(parts), count


def _parse_changes(raw: str) -> Optional[List[HolisticChange]]:
    """Parse the model's free-text JSON output into the change list.

    Tolerates a surrounding markdown code fence and any stray prose by extracting the outermost
    {...} span before parsing. Returns None when nothing valid is found, so the caller can retry.
    """

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
        return HolisticCompareResponse.model_validate(data).changes  # type: ignore
    except Exception:
        return None


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


def _change_signature(change: HolisticChange) -> Tuple[str, str]:
    """Whitespace/case-insensitive (original, revised) key identifying the clause region a change
    describes. Two entries with the same signature point at the same edited clause."""

    side_a = _normalize((change.text_from_doc_a or "").lower())
    side_b = _normalize((change.text_from_doc_b or "").lower())
    return side_a, side_b


def _is_genuine_reorder(change: HolisticChange) -> bool:
    """True only for a real move: the SAME clause text is present on both sides (content
    unchanged, only position moved). The sentence diff never reports moves — it emits a removal
    plus an addition — so a 'reordered' entry whose two sides differ is not actually a move."""

    if change.change_type != "reordered":
        return False
    if not change.text_from_doc_a or not change.text_from_doc_b:
        return False
    return _normalize(change.text_from_doc_a.lower()) == _normalize(change.text_from_doc_b.lower())


def _merge_change_group(group: List[HolisticChange]) -> HolisticChange:
    """Collapse several entries that describe the same clause region into one.

    The highest-risk member supplies the representative fields; every distinct summary is kept so
    no individual edit is lost, and the risk level is the max across the group.
    """

    primary = max(group, key=lambda c: _RISK_ORDER.get(c.risk_level, 0))

    summaries: List[str] = []
    for change in group:
        text = (change.summary or "").strip()
        if text and text not in summaries:
            summaries.append(text)

    return HolisticChange(
        clause_name=primary.clause_name,
        # Prefer a named section over null so a clause's edits never scatter across groups.
        section=next((c.section for c in group if c.section), primary.section),
        change_type=primary.change_type,
        modification_type=primary.modification_type,
        risk_level=max((c.risk_level for c in group), key=lambda r: _RISK_ORDER.get(r, 0)),
        affected_party=primary.affected_party,
        text_from_doc_a=primary.text_from_doc_a,
        text_from_doc_b=primary.text_from_doc_b,
        summary=" ".join(summaries),
        is_substantive=any(c.is_substantive for c in group),
    )


def _dedupe_changes(changes: List[HolisticChange]) -> List[HolisticChange]:
    """Remove the redundant entries the classifier sometimes emits for a single edited clause.

    The model can report one edited clause two or three times — independent in-clause edits as
    separate entries, plus a spurious 'reordered' entry repeating the same text. That shows the
    same full clause several times and inflates the change counts. Fix it by:
      1. dropping a 'reordered' entry that repeats a clause already reported as modified/added/
         removed (the diff never detects moves, so that reorder claim is unfounded);
      2. reclassifying any surviving 'reordered' whose two sides differ to 'modified' (a move
         preserves the clause text);
      3. merging entries that carry the same clause text on both sides into one, so the clause is
         reported — and counted — once.
    """

    groups: Dict[Tuple[str, str], List[HolisticChange]] = {}
    order: List[Tuple[str, str]] = []
    standalone: List[HolisticChange] = []

    for change in changes:
        # Nothing to align on when both sides are empty — leave the entry untouched.
        if not change.text_from_doc_a and not change.text_from_doc_b:
            standalone.append(change)
            continue
        signature = _change_signature(change)
        if signature not in groups:
            groups[signature] = []
            order.append(signature)
        groups[signature].append(change)

    result: List[HolisticChange] = []
    for signature in order:
        group = groups[signature]

        # 1. A 'reordered' duplicate of a clause reported another way is noise — drop it.
        non_reordered = [c for c in group if c.change_type != "reordered"]
        if non_reordered:
            group = non_reordered

        # 2. A surviving 'reordered' that isn't a genuine move is an in-place modification.
        for change in group:
            if change.change_type == "reordered" and not _is_genuine_reorder(change):
                change.change_type = "modified"

        # 3. One entry per clause region.
        result.append(group[0] if len(group) == 1 else _merge_change_group(group))

    return result + standalone


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
    """Classify the changed regions and return the change list (with full clause text).

    Uses free-text JSON generation (accumulated from the streaming call) rather than forced
    tool-use: with full-clause output the tool-use path intermittently serializes the `changes`
    array as a malformed JSON string and fails schema validation, whereas the model emits clean
    JSON text reliably even for large outputs. The accumulated text is parsed; one retry covers a
    rare bad parse.
    """

    for attempt in range(2):
        chunks: List[str] = []
        async for chunk in llm_client.generate_stream(
            prompt=_COMPARISON_USER,
            context={"diff_digest": diff_digest},
            session_id=session_id,
            system_message=_COMPARISON_SYSTEM,
            # cache_system=True,
            # max_tokens=_DIFF_MAX_TOKENS,
        ):
            chunks.append(chunk)

        changes = _parse_changes("".join(chunks))
        if changes is not None:
            return changes

        logger.warning("Change classification parse failed", attempt=attempt + 1, session_id=session_id)

    raise ValueError("Could not parse a valid change list from the model output.")


async def compare_documents_service(session_id: str, document_a: Document, document_b: Document) -> CompareResponse:
    """Compare two documents: deterministic sentence diff, then one LLM call to classify the changed regions."""

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

    digest, region_count = _build_diff_digest(paras_a, paras_b)
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

    raw_count = len(changes)
    changes = _dedupe_changes(changes)
    if len(changes) != raw_count:
        logger.info("Deduplicated change entries", raw=raw_count, deduped=len(changes), session_id=session_id)

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

    Code computes the sentence diff (fast); the single classification call is then streamed so
    chunks reach the client as the model produces them — raw text chunks wrapped as `data: ...`
    frames, terminated by `data: [DONE]`. The client accumulates the chunks and parses the change
    list as it builds up.

    De-duplication is NOT post-processed here: a live token stream cannot be de-duplicated without
    buffering the whole list first, which would defeat chunk-by-chunk streaming. Clean,
    one-entry-per-clause output is enforced by the classification prompt instead. The non-streaming
    endpoint keeps the post-processing dedup safety net.
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

        digest, region_count = _build_diff_digest(paras_a, paras_b)
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
        )

        async for chunk in stream:
            yield f"data: {json.dumps(chunk)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as exc:
        logger.exception("Compare streaming failed", session_id=session_id)
        yield f'data: {json.dumps({"error": str(exc)})}\n\n'
