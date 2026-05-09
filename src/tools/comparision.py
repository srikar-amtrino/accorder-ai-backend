import asyncio
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from docx.document import Document

from src.config.logging import Logger
from src.dependencies import get_service_container
from src.schemas.comparision import (
    ChangeEntry,
    ClauseComparisonLLMResponse,
    ClauseUnit,
    CompareResponse,
    CompareSummary,
    MatchResult,
    SectionGroup,
)
from src.schemas.registry import ParseResult
from src.services.registry.registry import ParserRegistry

AGENT_NAME = "document_comparison_agent"

registry = None


def get_registry():
    global registry
    if registry is None:
        registry = ParserRegistry()
    return registry


parser = None


def get_parser():
    global parser
    if parser is None:
        parser = get_registry().get_parser()
    return parser


# Similarity thresholds
SIMILARITY_THRESHOLD = 0.72
SPLIT_MERGE_THRESHOLD = 0.75
MAX_LLM_CALLS = 50
MAX_LLM_CONCURRENCY = 5
CONFIDENCE_HIGH = 0.90
CONFIDENCE_MEDIUM = 0.80
REORDER_DRIFT_THRESHOLD = 0.15
CONTAINMENT_SIZE_RATIO = 1.3

logger = Logger().logger


_GENERIC_HEADINGS = {
    "terms and conditions",
    "agreement",
    "general",
    "general terms",
    "miscellaneous",
    "preamble",
    "recitals",
    "background",
}


def _extract_heading_fallback(content: str) -> Optional[str]:
    """Derive a clause-specific heading from the content itself."""

    stripped = content.strip()
    first_line = stripped.split("\n")[0].strip()

    if re.match(r"^(\d+\.[\d.]*\s|Section\s|ARTICLE\s|[A-Z][A-Z\s]{4,}$)", first_line):
        return first_line

    title_match = re.match(r"^([A-Z][A-Za-z0-9&\s/',\-]{1,60}?)\.\s+[A-Z]", stripped)
    if title_match:
        title = title_match.group(1).strip()
        word_count = len(title.split())
        if 1 <= word_count <= 8:
            return title

    return None


def _is_generic_heading(heading: Optional[str]) -> bool:
    """Check if a heading is too generic to be useful for matching."""

    if not heading:
        return True
    return heading.strip().lower() in _GENERIC_HEADINGS


def _resolve_clause_heading(content: str, metadata_heading: Optional[str]) -> Optional[str]:
    """Prefer a content-derived per-clause title over a generic metadata heading."""

    derived = _extract_heading_fallback(content)
    if derived:
        return derived
    if _is_generic_heading(metadata_heading):
        return metadata_heading
    return metadata_heading


def extract_clauses(document: ParseResult) -> List[ClauseUnit]:
    """Extract clauses from a document's chunks, using metadata and fallback heuristics for headings."""

    clauses: List[ClauseUnit] = []
    order = 0

    for chunk in document.chunks:
        if chunk is None:
            continue

        content = chunk.content.strip()
        if not content:
            continue

        heading = _extract_heading_fallback(content) or chunk.metadata.get("section_heading")

        clauses.append(
            ClauseUnit(
                clause_id=f"{chunk.chunk_id}",
                heading=heading,
                content=content,
                position=chunk.chunk_index,
                doc_order=order,
                embedding=chunk.embedding_vector or [],
            )
        )
        order += 1

    return clauses


def _compute_similarity_matrix(emb_a: np.ndarray, emb_b: np.ndarray) -> np.ndarray:
    """Cosine similarity matrix between two embedding sets."""

    norms_a = np.linalg.norm(emb_a, axis=1, keepdims=True)
    norms_b = np.linalg.norm(emb_b, axis=1, keepdims=True)
    emb_a_norm = emb_a / np.maximum(norms_a, 1e-10)
    emb_b_norm = emb_b / np.maximum(norms_b, 1e-10)
    return emb_a_norm @ emb_b_norm.T


def _cosine_similarity(emb_a: List[float], emb_b: List[float]) -> float:
    """Cosine similarity between two single embedding vectors."""

    a = np.array(emb_a, dtype=np.float32).reshape(1, -1)
    b = np.array(emb_b, dtype=np.float32).reshape(1, -1)
    return float(_compute_similarity_matrix(a, b)[0][0])


def _confidence_from_similarity(score: float) -> str:
    """Bucket a cosine-similarity score into a confidence label."""

    if score >= CONFIDENCE_HIGH:
        return "high"
    if score >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def _greedy_match(sim_matrix: np.ndarray, threshold: float = SIMILARITY_THRESHOLD) -> Tuple[List[Tuple[int, int, float]], List[int], List[int]]:
    """Greedy 1:1 matching — pick best pair, remove both, repeat."""

    n, m = sim_matrix.shape
    pairs: List[Tuple[int, int, float]] = []
    used_a: set = set()
    used_b: set = set()

    flat = [(float(sim_matrix[i][j]), i, j) for i in range(n) for j in range(m)]
    flat.sort(reverse=True)

    for score, i, j in flat:
        if score < threshold:
            break
        if i in used_a or j in used_b:
            continue
        pairs.append((i, j, score))
        used_a.add(i)
        used_b.add(j)

    unmatched_a = [i for i in range(n) if i not in used_a]
    unmatched_b = [j for j in range(m) if j not in used_b]
    return pairs, unmatched_a, unmatched_b


async def _ensure_embeddings(clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit], indices_a: List[int], indices_b: List[int], embedding_service) -> None:
    """Generate embeddings in parallel for clauses that don't have them yet."""

    targets: List[ClauseUnit] = []
    for idx in indices_a:
        if not clauses_a[idx].embedding:
            targets.append(clauses_a[idx])
    for idx in indices_b:
        if not clauses_b[idx].embedding:
            targets.append(clauses_b[idx])

    if not targets:
        return

    results = await asyncio.gather(*(embedding_service.generate_embeddings(c.content) for c in targets))
    for clause, embedding in zip(targets, results):
        clause.embedding = embedding


async def match_clauses(clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit], embedding_service) -> MatchResult:
    """Match clauses between two versions using a hybrid of heading-based and embedding similarity."""

    n = len(clauses_a)
    m = len(clauses_b)

    # Step 1: heading-based matching (unique headings only)
    heading_pairs: List[Tuple[int, int, float]] = []
    used_a: set = set()
    used_b: set = set()

    heading_map_a: Dict[str, List[int]] = {}
    for i, clause in enumerate(clauses_a):
        if clause.heading:
            key = clause.heading.strip().lower()
            heading_map_a.setdefault(key, []).append(i)

    heading_map_b: Dict[str, List[int]] = {}
    for j, clause in enumerate(clauses_b):
        if clause.heading:
            key = clause.heading.strip().lower()
            heading_map_b.setdefault(key, []).append(j)

    heading_matched_indices: List[Tuple[int, int]] = []

    for key, indices_a_list in heading_map_a.items():
        indices_b_list = heading_map_b.get(key, [])
        if len(indices_a_list) == 1 and len(indices_b_list) == 1:
            i, j = indices_a_list[0], indices_b_list[0]
            heading_matched_indices.append((i, j))
            used_a.add(i)
            used_b.add(j)
            logger.debug(f"Heading match: '{key}' -> A[{i}] <-> B[{j}]")

    # Generate embeddings for all clauses
    await _ensure_embeddings(clauses_a, clauses_b, list(range(n)), list(range(m)), embedding_service)

    # Compute content similarity for heading-matched pairs
    for i, j in heading_matched_indices:
        sim = _cosine_similarity(clauses_a[i].embedding, clauses_b[j].embedding)
        heading_pairs.append((i, j, sim))
        logger.info(f"Heading match: '{clauses_a[i].heading}' — similarity: {sim:.4f}")

    # Step 2: embedding similarity for remaining clauses
    remaining_a = [i for i in range(n) if i not in used_a]
    remaining_b = [j for j in range(m) if j not in used_b]

    embedding_pairs: List[Tuple[int, int, float]] = []

    if remaining_a and remaining_b:
        emb_a = np.array([clauses_a[i].embedding for i in remaining_a], dtype=np.float32)
        emb_b = np.array([clauses_b[j].embedding for j in remaining_b], dtype=np.float32)

        sim_matrix = _compute_similarity_matrix(emb_a, emb_b)
        local_pairs, local_unmatched_a, local_unmatched_b = _greedy_match(sim_matrix)

        for li, lj, score in local_pairs:
            embedding_pairs.append((remaining_a[li], remaining_b[lj], score))

        final_unmatched_a = [remaining_a[li] for li in local_unmatched_a]
        final_unmatched_b = [remaining_b[lj] for lj in local_unmatched_b]
    else:
        final_unmatched_a = remaining_a
        final_unmatched_b = remaining_b

    return MatchResult(
        matched_pairs=heading_pairs + embedding_pairs,
        unmatched_a=final_unmatched_a,
        unmatched_b=final_unmatched_b,
    )


# --- Stage 2.5: Split/Merge Detection ---


def _detect_splits_and_merges(match_result: MatchResult, clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit]) -> Tuple[List[ChangeEntry], List[int], List[int]]:
    """Identify cases where unmatched clauses on one side are highly similar to matched clauses on the other side, indicating a possible split or merge."""

    entries: List[ChangeEntry] = []
    explained_a: set = set()
    explained_b: set = set()

    matched_a_indices = sorted({idx_a for idx_a, _, _ in match_result.matched_pairs})
    matched_b_indices = sorted({idx_b for _, idx_b, _ in match_result.matched_pairs})

    # --- Splits: unmatched B compared against matched A ---
    if match_result.unmatched_b and matched_a_indices:
        emb_matched_a = np.array([clauses_a[i].embedding for i in matched_a_indices], dtype=np.float32)
        emb_unmatched_b = np.array([clauses_b[j].embedding for j in match_result.unmatched_b], dtype=np.float32)
        sim = _compute_similarity_matrix(emb_unmatched_b, emb_matched_a)  # (|Bu|, |Am|)

        # Rank (unmatched_b row, matched_a col) pairs by similarity, highest first
        flat = [(float(sim[r, c]), r, c) for r in range(sim.shape[0]) for c in range(sim.shape[1])]
        flat.sort(reverse=True)

        used_a: set = set()
        for score, r, c in flat:
            if score < SPLIT_MERGE_THRESHOLD:
                break
            j = match_result.unmatched_b[r]
            idx_a = matched_a_indices[c]
            if j in explained_b or idx_a in used_a:
                continue
            clause_a, clause_b = clauses_a[idx_a], clauses_b[j]
            entries.append(
                ChangeEntry(
                    clause_name=clause_a.heading or clause_b.heading or f"Clause at position {clause_a.doc_order + 1}",
                    section=clause_a.section_heading or clause_b.section_heading,
                    change_type="modified",
                    modification_type="structural",
                    risk_level="low",
                    affected_party="Both",
                    confidence=_confidence_from_similarity(score),
                    text_from_doc_a=clause_a.content,
                    text_from_doc_b=clause_b.content,
                    summary="Clause was split into multiple clauses in the revised version. Wording preserved.",
                    is_substantive=False,
                )
            )
            explained_b.add(j)
            used_a.add(idx_a)
            logger.info(f"Split detected: A[{idx_a}] -> B[{j}] (sim={score:.4f})")

    # --- Merges: unmatched A compared against matched B ---
    if match_result.unmatched_a and matched_b_indices:
        emb_matched_b = np.array([clauses_b[j].embedding for j in matched_b_indices], dtype=np.float32)
        emb_unmatched_a = np.array([clauses_a[i].embedding for i in match_result.unmatched_a], dtype=np.float32)
        sim = _compute_similarity_matrix(emb_unmatched_a, emb_matched_b)  # (|Au|, |Bm|)

        flat = [(float(sim[r, c]), r, c) for r in range(sim.shape[0]) for c in range(sim.shape[1])]
        flat.sort(reverse=True)

        used_b: set = set()
        for score, r, c in flat:
            if score < SPLIT_MERGE_THRESHOLD:
                break
            i = match_result.unmatched_a[r]
            idx_b = matched_b_indices[c]
            if i in explained_a or idx_b in used_b:
                continue
            clause_a, clause_b = clauses_a[i], clauses_b[idx_b]
            entries.append(
                ChangeEntry(
                    clause_name=clause_a.heading or clause_b.heading or f"Clause at position {clause_a.doc_order + 1}",
                    section=clause_a.section_heading or clause_b.section_heading,
                    change_type="modified",
                    modification_type="structural",
                    risk_level="low",
                    affected_party="Both",
                    confidence=_confidence_from_similarity(score),
                    text_from_doc_a=clause_a.content,
                    text_from_doc_b=clause_b.content,
                    summary="Clause was merged with another clause in the revised version. Wording preserved.",
                    is_substantive=False,
                )
            )
            explained_a.add(i)
            used_b.add(idx_b)
            logger.info(f"Merge detected: A[{i}] -> B[{idx_b}] (sim={score:.4f})")

    remaining_a = [i for i in match_result.unmatched_a if i not in explained_a]
    remaining_b = [j for j in match_result.unmatched_b if j not in explained_b]

    return entries, remaining_a, remaining_b


def _normalize_for_containment(text: str) -> str:
    """Collapse whitespace for robust substring containment checks."""

    return " ".join(text.split()).lower()


def _reconcile_containment(unmatched_a: List[int], unmatched_b: List[int], clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit]) -> Tuple[List[ChangeEntry], List[int], List[int]]:
    """Detect cases where an unmatched clause on one side is mostly contained within an unmatched clause on the other side, indicating a possible addition or removal of content rather than a true new/deleted clause."""

    entries: List[ChangeEntry] = []
    explained_a: set = set()
    explained_b: set = set()

    # Check if removed (A) clause text is contained in an added (B) clause
    for i in unmatched_a:
        norm_a = _normalize_for_containment(clauses_a[i].content)
        if len(norm_a) < 20:
            continue

        for j in unmatched_b:
            if j in explained_b:
                continue
            norm_b = _normalize_for_containment(clauses_b[j].content)

            if norm_a in norm_b:
                clause_a, clause_b = clauses_a[i], clauses_b[j]
                entries.append(
                    ChangeEntry(
                        clause_name=clause_b.heading or clause_a.heading or f"Clause at position {clause_b.doc_order + 1}",
                        section=clause_b.section_heading or clause_a.section_heading,
                        change_type="added",
                        modification_type="structural",
                        risk_level="medium",
                        affected_party=None,
                        confidence="high",
                        text_from_doc_a=None,
                        text_from_doc_b=clause_b.content,
                        summary="New content was appended alongside the existing clause text in the revised version.",
                        is_substantive=True,
                    )
                )
                explained_a.add(i)
                explained_b.add(j)
                logger.info(f"Containment: A[{i}] found inside B[{j}] — reporting as addition")
                break

    # Reverse: B text contained in A (content was trimmed)
    for j in unmatched_b:
        if j in explained_b:
            continue
        norm_b = _normalize_for_containment(clauses_b[j].content)
        if len(norm_b) < 20:
            continue

        for i in unmatched_a:
            if i in explained_a:
                continue
            norm_a = _normalize_for_containment(clauses_a[i].content)

            if norm_b in norm_a:
                clause_a, clause_b = clauses_a[i], clauses_b[j]
                entries.append(
                    ChangeEntry(
                        clause_name=clause_a.heading or clause_b.heading or f"Clause at position {clause_a.doc_order + 1}",
                        section=clause_a.section_heading or clause_b.section_heading,
                        change_type="removed",
                        modification_type="structural",
                        risk_level="medium",
                        affected_party=None,
                        confidence="high",
                        text_from_doc_a=clause_a.content,
                        text_from_doc_b=None,
                        summary="Content was trimmed from this clause in the revised version.",
                        is_substantive=True,
                    )
                )
                explained_a.add(i)
                explained_b.add(j)
                logger.info(f"Containment: B[{j}] found inside A[{i}] — reporting as removal")
                break

    remaining_a = [i for i in unmatched_a if i not in explained_a]
    remaining_b = [j for j in unmatched_b if j not in explained_b]

    return entries, remaining_a, remaining_b


def _derive_delta_heading(longer: ClauseUnit, shorter: ClauseUnit, shared_text: str) -> Optional[str]:
    """Try to derive a more specific heading for a split/merge delta by looking for a title in the longer clause's content that matches the shared text, rather than relying on the original metadata heading which may be generic or missing."""

    stripped = longer.content.strip()
    shared_norm = " ".join(shared_text.split()).lower()
    lower = stripped.lower()
    pos = lower.find(shared_norm[:40]) if shared_norm else -1
    if pos > 0:
        delta = stripped[:pos].strip()
        derived = _extract_heading_fallback(delta)
        if derived:
            return derived
    derived = _extract_heading_fallback(stripped)
    if derived and derived != shorter.heading:
        return derived
    return longer.heading or shorter.heading


def _reconcile_matched_containment(pairs: List[Tuple[int, int, float]], clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit]) -> Tuple[List[Tuple[int, int, float]], List[ChangeEntry]]:
    """For matched pairs with moderate similarity, check if one clause's text is mostly contained within the other, which may indicate an addition or removal of content rather than a true modification. Emit a corresponding ChangeEntry and remove from LLM comparison."""

    remaining: List[Tuple[int, int, float]] = []
    entries: List[ChangeEntry] = []

    for idx_a, idx_b, score in pairs:
        clause_a = clauses_a[idx_a]
        clause_b = clauses_b[idx_b]
        norm_a = _normalize_for_containment(clause_a.content)
        norm_b = _normalize_for_containment(clause_b.content)

        if len(norm_a) < 20 or len(norm_b) < 20:
            remaining.append((idx_a, idx_b, score))
            continue

        if norm_a in norm_b and len(norm_b) >= len(norm_a) * CONTAINMENT_SIZE_RATIO:
            shared_is_identical = clause_a.content.strip() in clause_b.content
            heading = _derive_delta_heading(clause_b, clause_a, clause_a.content)
            entries.append(
                ChangeEntry(
                    clause_name=heading or f"Clause at position {clause_b.doc_order + 1}",
                    section=clause_b.section_heading or clause_a.section_heading,
                    change_type="added",
                    modification_type="structural",
                    risk_level="medium",
                    affected_party=None,
                    confidence="high",
                    text_from_doc_a=None,
                    text_from_doc_b=clause_b.content,
                    summary="A new clause was added alongside existing text in the revised version.",
                    is_substantive=True,
                )
            )
            logger.info(f"Matched-pair containment: A[{idx_a}] inside B[{idx_b}] — " f"emitting addition; shared_identical={shared_is_identical}")
            if not shared_is_identical:
                # Shared portion has cosmetic/word-level differences too — LLM still runs.
                remaining.append((idx_a, idx_b, score))
            continue

        if norm_b in norm_a and len(norm_a) >= len(norm_b) * CONTAINMENT_SIZE_RATIO:
            shared_is_identical = clause_b.content.strip() in clause_a.content
            heading = _derive_delta_heading(clause_a, clause_b, clause_b.content)
            entries.append(
                ChangeEntry(
                    clause_name=heading or f"Clause at position {clause_a.doc_order + 1}",
                    section=clause_a.section_heading or clause_b.section_heading,
                    change_type="removed",
                    modification_type="structural",
                    risk_level="medium",
                    affected_party=None,
                    confidence="high",
                    text_from_doc_a=clause_a.content,
                    text_from_doc_b=None,
                    summary="Clause content was removed from the revised version.",
                    is_substantive=True,
                )
            )
            logger.info(f"Matched-pair containment: B[{idx_b}] inside A[{idx_a}] — " f"emitting removal; shared_identical={shared_is_identical}")
            if not shared_is_identical:
                remaining.append((idx_a, idx_b, score))
            continue

        remaining.append((idx_a, idx_b, score))

    return remaining, entries


async def _compare_single_pair(clause_a: ClauseUnit, clause_b: ClauseUnit, llm_client) -> ClauseComparisonLLMResponse:
    """Send one clause pair to the LLM for detailed comparison."""

    prompt = Path(r"src\services\prompts\v1\clause_comparison_prompt.mustache").read_text()

    context = {
        "clause_heading": clause_a.heading or clause_b.heading or "Unnamed Clause",
        "clause_a_text": clause_a.content,
        "clause_b_text": clause_b.content,
    }
    return await llm_client.generate(
        prompt=prompt,
        context=context,
        response_model=ClauseComparisonLLMResponse,
    )


def _build_change_entry(clause_a: ClauseUnit, clause_b: ClauseUnit, comparison: ClauseComparisonLLMResponse, similarity: float) -> ChangeEntry:
    """Convert an LLM comparison result into a ChangeEntry."""

    return ChangeEntry(
        clause_name=clause_a.heading or clause_b.heading or f"Clause at position {clause_a.doc_order + 1}",
        section=clause_a.section_heading or clause_b.section_heading,
        change_type=comparison.change_type,
        modification_type=comparison.modification_type,
        risk_level=comparison.risk_level,
        affected_party=comparison.affected_party,
        confidence=_confidence_from_similarity(similarity),
        text_from_doc_a=clause_a.content,
        text_from_doc_b=clause_b.content,
        summary=comparison.summary,
        is_substantive=comparison.is_substantive,
    )


def _make_skipped_entry(clause_a: ClauseUnit, clause_b: ClauseUnit, reason: str) -> ChangeEntry:
    """Placeholder entry when an LLM call could not complete."""

    return ChangeEntry(
        clause_name=clause_a.heading or clause_b.heading or f"Clause at position {clause_a.doc_order + 1}",
        section=clause_a.section_heading or clause_b.section_heading,
        change_type="unknown",
        modification_type=None,
        risk_level=None,
        confidence="low",
        text_from_doc_a=clause_a.content,
        text_from_doc_b=clause_b.content,
        summary=reason,
        is_substantive=True,
    )


def _make_reorder_entry(clause_a: ClauseUnit, clause_b: ClauseUnit) -> ChangeEntry:
    """Entry for a matched pair with identical content but different position."""

    return ChangeEntry(
        clause_name=clause_a.heading or clause_b.heading or f"Clause at position {clause_a.doc_order + 1}",
        section=clause_a.section_heading or clause_b.section_heading,
        change_type="reordered",
        modification_type=None,
        risk_level="low",
        affected_party=None,
        confidence="high",
        text_from_doc_a=clause_a.content,
        text_from_doc_b=clause_b.content,
        summary=(f"Clause text is unchanged but its position moved from " f"#{clause_a.doc_order + 1} to #{clause_b.doc_order + 1} in the revised version."),
        is_substantive=False,
    )


def _position_drift(clause_a: ClauseUnit, clause_b: ClauseUnit, len_a: int, len_b: int) -> float:
    """Normalized absolute position drift between a matched pair."""

    if len_a <= 1 or len_b <= 1:
        return 0.0
    return abs(clause_a.doc_order / (len_a - 1) - clause_b.doc_order / (len_b - 1))


async def compare_matched_pairs(pairs: List[Tuple[int, int, float]], clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit], llm_client) -> Tuple[List[ChangeEntry], int, int]:
    """Run LLM comparison on matched pairs with differing content."""

    len_a = len(clauses_a)
    len_b = len(clauses_b)

    reorder_entries: List[ChangeEntry] = []
    llm_jobs: List[Tuple[int, int, float]] = []
    skipped_entries: List[ChangeEntry] = []

    # Pass 1: classify each pair — identical/reorder (no LLM), needs LLM, or
    # skipped because we hit the budget.
    for idx_a, idx_b, similarity in pairs:
        clause_a = clauses_a[idx_a]
        clause_b = clauses_b[idx_b]

        if clause_a.content == clause_b.content:
            if _position_drift(clause_a, clause_b, len_a, len_b) >= REORDER_DRIFT_THRESHOLD:
                reorder_entries.append(_make_reorder_entry(clause_a, clause_b))
            continue

        if len(llm_jobs) >= MAX_LLM_CALLS:
            skipped_entries.append(_make_skipped_entry(clause_a, clause_b, "Comparison skipped due to LLM call limit."))
            continue

        llm_jobs.append((idx_a, idx_b, similarity))

    # Pass 2: run LLM calls concurrently, bounded by MAX_LLM_CONCURRENCY.
    semaphore = asyncio.Semaphore(MAX_LLM_CONCURRENCY)

    async def _run_one(idx_a: int, idx_b: int, similarity: float) -> ChangeEntry:
        clause_a = clauses_a[idx_a]
        clause_b = clauses_b[idx_b]
        async with semaphore:
            try:
                comparison = await _compare_single_pair(clause_a, clause_b, llm_client)
                return _build_change_entry(clause_a, clause_b, comparison, similarity)
            except Exception as e:
                logger.error(f"LLM comparison failed for {clause_a.clause_id} vs {clause_b.clause_id}: {e}")
                return _make_skipped_entry(clause_a, clause_b, f"Comparison failed: {e}")

    llm_results: List[ChangeEntry] = []
    llm_calls = 0
    llm_call_failures = 0

    if llm_jobs:
        llm_results = await asyncio.gather(*(_run_one(*job) for job in llm_jobs))
        for entry in llm_results:
            if entry.change_type == "unknown":
                llm_call_failures += 1
            else:
                llm_calls += 1

    llm_skipped = len(skipped_entries) + llm_call_failures
    results = reorder_entries + llm_results + skipped_entries
    return results, llm_calls, llm_skipped


def _build_unmatched_entries(unmatched_a: List[int], unmatched_b: List[int], clauses_a: List[ClauseUnit], clauses_b: List[ClauseUnit]) -> List[ChangeEntry]:
    """Create ChangeEntry objects for clauses only present in one document."""

    entries: List[ChangeEntry] = []

    for idx in unmatched_a:
        clause = clauses_a[idx]
        entries.append(
            ChangeEntry(
                clause_name=clause.heading or f"Clause at position {clause.doc_order + 1}",
                section=clause.section_heading,
                change_type="removed",
                risk_level="medium",
                text_from_doc_a=clause.content,
                text_from_doc_b=None,
                summary="This clause was removed in the revised version.",
            )
        )

    for idx in unmatched_b:
        clause = clauses_b[idx]
        entries.append(
            ChangeEntry(
                clause_name=clause.heading or f"Clause at position {clause.doc_order + 1}",
                section=clause.section_heading,
                change_type="added",
                risk_level="medium",
                text_from_doc_a=None,
                text_from_doc_b=clause.content,
                summary="This clause was added in the revised version.",
            )
        )

    return entries


def group_by_section(changes: List[ChangeEntry]) -> List[SectionGroup]:
    """Group change entries by parent section heading."""

    section_map: Dict[str, List[ChangeEntry]] = {}
    for change in changes:
        section = change.section or "General / Ungrouped"
        section_map.setdefault(section, []).append(change)

    return [SectionGroup(section_name=name, changes=entries) for name, entries in section_map.items()]


def _compute_summary(changes: List[ChangeEntry], llm_calls_made: int, llm_calls_skipped: int) -> CompareSummary:
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
        llm_calls_made=llm_calls_made,
        llm_calls_skipped=llm_calls_skipped,
    )


def _zero_changes_summary() -> CompareSummary:
    """Summary for identical or same-document comparisons."""

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


# --- Stage 5: Full Pipeline ---


async def extract_text(document: Document) -> str:
    """Extract full text from a docx Document object."""

    paragraphs = [para.text for para in document.paragraphs if para.text.strip()]
    return "\n".join(paragraphs)


async def run(session_id: str, document_a: Document, document_b: Document) -> CompareResponse:
    """Execute the full document comparison pipeline."""

    container = get_service_container()
    embedding_service = container.embedding_service
    llm_client = container.azure_openai_model

    parser = get_parser()

    # load cached data for this agent
    session_data = container.session_manager.get_session(session_id=session_id)
    cached_data: Dict[str, CompareResponse] = session_data.tool_results[AGENT_NAME] if AGENT_NAME in session_data.tool_results else []
    if cached_data:
        logger.info(f"Loaded cached data for session {session_id} and agent {AGENT_NAME}")
        return cached_data

    doc_a: ParseResult = await parser.parse_document(document_a)
    doc_b: ParseResult = await parser.parse_document(document_b)

    doc_text_a = await extract_text(document_a)
    doc_text_b = await extract_text(document_b)

    # if hash(doc_text_a) == cached_data.get("doc_1_hash") and hash(doc_text_b) == cached_data.get("doc_2_hash"):
    #     logger.info(f"Document hashes match cached data for session {session_id} and agent {AGENT_NAME} — returning cached response")
    #     return CompareResponse(
    #         success=cached_data.get("success", True),
    #         message=cached_data.get("message"),
    #         summary=cached_data.get("summary", _zero_changes_summary()),
    #         sections=cached_data.get("sections", []),
    #     )

    # Guard: same document
    if hash(doc_text_a) == hash(doc_text_b):
        return CompareResponse(
            success=True,
            message="Both document IDs are the same. Provide two different documents to compare.",
            summary=_zero_changes_summary(),
            sections=[],
        )

    # Stage 1: Extract clauses
    logger.info("Extracting clauses for the doccuments...")
    clauses_a = extract_clauses(doc_a)
    clauses_b = extract_clauses(doc_b)

    # Edge case: empty documents
    if not clauses_a and not clauses_b:
        return CompareResponse(success=True, summary=_zero_changes_summary(), sections=[])

    if not clauses_a:
        entries = _build_unmatched_entries([], list(range(len(clauses_b))), [], clauses_b)
        return CompareResponse(
            success=True,
            summary=_compute_summary(entries, 0, 0),
            sections=group_by_section(entries),
        )

    if not clauses_b:
        entries = _build_unmatched_entries(list(range(len(clauses_a))), [], clauses_a, [])
        return CompareResponse(
            success=True,
            summary=_compute_summary(entries, 0, 0),
            sections=group_by_section(entries),
        )

    # Stage 2: Match clauses
    logger.info(f"Matching {len(clauses_a)} clauses (A) against {len(clauses_b)} clauses (B)")
    match_result = await match_clauses(clauses_a, clauses_b, embedding_service)
    logger.info(f"Matched: {len(match_result.matched_pairs)}, " f"Unmatched A: {len(match_result.unmatched_a)}, " f"Unmatched B: {len(match_result.unmatched_b)}")

    # Stage 2.5: Reconcile matched pairs where one side merges multiple clauses
    surviving_pairs, matched_containment_entries = _reconcile_matched_containment(match_result.matched_pairs, clauses_a, clauses_b)
    match_result.matched_pairs = surviving_pairs

    # Stage 3: Split/merge detection
    split_merge_entries, remaining_a, remaining_b = _detect_splits_and_merges(match_result, clauses_a, clauses_b)

    # Stage 3.5: Containment reconciliation
    containment_entries, remaining_a, remaining_b = _reconcile_containment(remaining_a, remaining_b, clauses_a, clauses_b)

    # Stage 4: LLM comparison for content differences
    llm_changes, llm_calls_made, llm_calls_skipped = await compare_matched_pairs(
        match_result.matched_pairs,
        clauses_a,
        clauses_b,
        llm_client,
    )

    # Stage 5: Build unmatched entries
    unmatched_entries = _build_unmatched_entries(remaining_a, remaining_b, clauses_a, clauses_b)

    # Stage 6: Combine, group, summarize
    all_changes = llm_changes + matched_containment_entries + split_merge_entries + containment_entries + unmatched_entries
    sections = group_by_section(all_changes)
    summary = _compute_summary(all_changes, llm_calls_made, llm_calls_skipped)

    logger.info(
        f"Compare complete: {summary.total_changes} changes "
        f"({summary.added}A/{summary.removed}R/{summary.modified}M/{summary.reordered}O), "
        f"LLM calls: {llm_calls_made} made, {llm_calls_skipped} skipped"
    )

    message = "Both documents are identical. No differences found." if summary.total_changes == 0 else None

    # Store data in cache for this session and agent
    session_data.tool_results[AGENT_NAME] = {
        "doc_1_hash": hash(doc_text_a),
        "doc_2_hash": hash(doc_text_b),
        "success": True,
        "message": message,
        "summary": summary,
        "sections": sections,
    }

    return CompareResponse(
        success=True,
        message=message,
        summary=summary,
        sections=sections,
    )
