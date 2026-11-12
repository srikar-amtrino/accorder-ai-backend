import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "test":
    PROJECT_ROOT = BASE_DIR.parent
else:
    PROJECT_ROOT = BASE_DIR

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from src.schemas.general_review import (
    GeneralReviewRequest,
    GeneralReviewResponse,
    Suggestion,
    TextInformation,
)
from src.services.criteria_store import get_criteria_by_slug, slug_to_display
from src.tools.general_review import (
    ClauseGroup,
    _build_batches_v2,
    _build_clause_groups,
    _classify_all_paragraphs,
    _evaluate_batch,
    _is_fragment,
    _smooth_classifications,
    CONFIDENCE_THRESHOLD,
    MAX_CONCURRENT_CALLS,
)


def setup_logger(log_file_path: Path) -> logging.Logger:
    """Setup logger writing to both console and log file."""
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("payload_test")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file_path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


async def run_test(
    payload_path: Path,
    test_dir: Path,
    max_groups_to_review: Optional[int] = None,
    session_id: str = "payload_test_session",
    force_vector: bool = False
):
    start_time = time.time()
    
    # 1. Directories
    input_dir = test_dir / "input"
    vector_mapping_dir = test_dir / "vector_mapping"
    clause_units_dir = test_dir / "clause_units_assumptions"
    log_dir = test_dir / "log_file_of_the_input_file"
    classification_dir = test_dir / "classification"
    output_dir = test_dir / "output"

    for d in (input_dir, vector_mapping_dir, clause_units_dir, log_dir, classification_dir, output_dir):
        d.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "input_processing.log"
    logger = setup_logger(log_file)

    logger.info("=================================================================")
    logger.info("STARTING DETERMINISTIC GENERAL REVIEW PAYLOAD TEST")
    logger.info(f"Payload Source: {payload_path}")
    logger.info(f"Test Directory: {test_dir}")
    logger.info("=================================================================")

    # Copy input payload to test/input if different
    dest_payload = input_dir / "input_payload.json"
    if payload_path.resolve() != dest_payload.resolve():
        dest_payload.write_text(payload_path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info(f"Copied payload to {dest_payload}")

    # 2. Ingest & Validate Payload
    logger.info("Stage 1: Ingesting payload...")
    raw_json = json.loads(payload_path.read_text(encoding="utf-8"))
    request = GeneralReviewRequest.model_validate(raw_json)
    paragraphs = request.textinformation
    logger.info(
        f"Payload Ingested: {len(paragraphs)} paragraphs | "
        f"Party Represented: {request.party_represented} | "
        f"Agreement Type: {request.agreement_type}"
    )

    # 3. Vector Mapping
    logger.info("\nStage 2: Running Vector Search against Qdrant collection...")
    vector_mapping_file = vector_mapping_dir / "vector_mapping_results.json"
    raw_matches: Dict[int, Any] = {}

    if vector_mapping_file.exists() and not force_vector:
        logger.info(f"Loading cached vector search results from {vector_mapping_file}...")
        from src.schemas.clause_mapping import ClauseMatch
        cached_records = json.loads(vector_mapping_file.read_text(encoding="utf-8"))
        for rec in cached_records:
            if rec.get("canonical_slug") and rec.get("score", 0.0) > 0:
                raw_matches[rec["para_index"]] = ClauseMatch(
                    canonical_type=rec["canonical_slug"],
                    sub_type=rec.get("sub_type"),
                    score=rec["score"],
                )
        logger.info(f"Loaded {len(raw_matches)} cached vector matches from disk.")
    else:
        raw_matches = await _classify_all_paragraphs(request, session_id)
        logger.info(f"Vector search returned matches for {len(raw_matches)} eligible paragraphs.")

    vector_mapping_records = []
    classified_count = 0
    for idx, para in enumerate(paragraphs):
        match = raw_matches.get(idx)
        score = match.score if match else 0.0
        slug = match.canonical_type if match else None
        sub_type = match.sub_type if match else None
        status = "unclassified"
        if score >= CONFIDENCE_THRESHOLD:
            status = "classified"
            classified_count += 1
        elif match:
            status = "low_confidence"

        vector_mapping_records.append({
            "para_index": idx,
            "para_identifier": para.paraindetifier,
            "word_count": len(para.text.split()),
            "status": status,
            "canonical_slug": slug,
            "sub_type": sub_type,
            "score": round(score, 4),
            "text_preview": para.text[:120].strip() + ("..." if len(para.text) > 120 else ""),
        })

    vector_mapping_file = vector_mapping_dir / "vector_mapping_results.json"
    vector_mapping_file.write_text(json.dumps(vector_mapping_records, indent=2, ensure_ascii=False), encoding="utf-8")
    
    vm_summary = {
        "total_paragraphs": len(paragraphs),
        "paragraphs_with_match": len(raw_matches),
        "high_confidence_matches": classified_count,
        "unclassified_paragraphs": len(paragraphs) - classified_count,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    }
    (vector_mapping_dir / "vector_mapping_summary.json").write_text(json.dumps(vm_summary, indent=2), encoding="utf-8")
    logger.info(f"Vector mapping saved to {vector_mapping_file}")

    # 4. Smoothing & Classification
    logger.info("\nStage 3: Applying 3 Smoothing Rules to classifications...")
    smoothed = _smooth_classifications(raw_matches, paragraphs)

    classification_records = []
    types_count: Dict[str, int] = {}
    for idx, para in enumerate(paragraphs):
        raw_m = raw_matches.get(idx)
        sm_m = smoothed.get(idx)
        
        raw_slug = raw_m.canonical_type if raw_m else None
        raw_score = raw_m.score if raw_m else 0.0
        sm_slug = sm_m.canonical_type if sm_m else None
        sm_score = sm_m.score if sm_m else 0.0
        display_name = slug_to_display(sm_slug) if sm_slug else None

        change_type = "identical"
        if raw_slug != sm_slug:
            if raw_slug is None and sm_slug is not None:
                change_type = "gap_fill_or_continuation_absorbed"
            elif raw_slug is not None and sm_slug is None:
                change_type = "downgraded_isolated_solo"
            else:
                change_type = "reassigned"

        display_label = display_name or ("Unclassified Fragment" if _is_fragment(para.text) else "Unclassified Clause")
        types_count[display_label] = types_count.get(display_label, 0) + 1

        classification_records.append({
            "para_index": idx,
            "para_identifier": para.paraindetifier,
            "raw_match": {"slug": raw_slug, "score": round(raw_score, 4)},
            "smoothed_match": {"slug": sm_slug, "score": round(sm_score, 4)},
            "display_name": display_name,
            "change_type": change_type,
            "is_fragment": _is_fragment(para.text),
            "text_preview": para.text[:120].strip() + ("..." if len(para.text) > 120 else ""),
        })

    classification_file = classification_dir / "smoothed_classifications.json"
    classification_file.write_text(json.dumps(classification_records, indent=2, ensure_ascii=False), encoding="utf-8")
    (classification_dir / "classification_distribution.json").write_text(json.dumps(types_count, indent=2), encoding="utf-8")
    logger.info(f"Smoothed classifications saved to {classification_file}")

    # 5. Clause Units & Assumptions
    logger.info("\nStage 4: Building Clause Units and Grouping Assumptions...")
    groups = _build_clause_groups(smoothed, paragraphs)
    logger.info(f"Formed {len(groups)} cohesive clause groups from {len(paragraphs)} paragraphs.")

    clause_units_data = []
    assumptions_markdown = [
        "# Clause Units & Grouping Assumptions Report\n\n",
        f"- **Total Paragraphs Ingested:** {len(paragraphs)}\n",
        f"- **Total Clause Units Formed:** {len(groups)}\n\n",
        "## Clause Units Breakdown\n\n",
    ]

    for g_idx, g in enumerate(groups):
        route = "PATH A (Taxonomy Criteria)" if g.is_classified else ("PATH C (Fragment Defect)" if g.is_fragment else "PATH B (General Criteria)")
        first_para_text = g.paragraphs[g.para_indices[0]]
        
        group_info = {
            "group_index": g_idx + 1,
            "canonical_type": g.canonical_type,
            "canonical_slug": g.canonical_slug,
            "confidence": round(g.confidence, 4),
            "is_classified": g.is_classified,
            "is_fragment": g.is_fragment,
            "routing_path": route,
            "para_count": len(g.para_indices),
            "para_indices": g.para_indices,
            "para_identifiers": [paragraphs[i].paraindetifier for i in g.para_indices],
            "text_preview": first_para_text[:160].strip() + ("..." if len(first_para_text) > 160 else ""),
        }
        clause_units_data.append(group_info)

        assumptions_markdown.append(
            f"### Unit {g_idx + 1}: {g.canonical_type or 'Unclassified'}\n"
            f"- **Route:** `{route}`\n"
            f"- **Confidence:** `{group_info['confidence']}`\n"
            f"- **Paragraphs ({len(g.para_indices)}):** `{', '.join(group_info['para_identifiers'])}`\n"
            f"- **Preview:** *\"{group_info['text_preview']}\"*\n\n"
        )

    clause_units_file = clause_units_dir / "clause_units.json"
    clause_units_file.write_text(json.dumps(clause_units_data, indent=2, ensure_ascii=False), encoding="utf-8")
    
    assumptions_report_file = clause_units_dir / "assumptions_report.md"
    assumptions_report_file.write_text("".join(assumptions_markdown), encoding="utf-8")
    logger.info(f"Clause units saved to {clause_units_file} and {assumptions_report_file}")

    # 6. Batching & Review Pipeline
    logger.info("\nStage 5: Packing Batches & Running Review Pipeline...")
    batches = _build_batches_v2(groups, paragraphs)
    logger.info(f"Packed {len(groups)} groups into {len(batches)} batches (budget: 6,000 chars, group integrity preserved).")

    if max_groups_to_review is not None:
        logger.info(f"Limiting review to first {max_groups_to_review} groups for test run...")
        # Flatten first N groups into test batches
        limited_groups = groups[:max_groups_to_review]
        batches = _build_batches_v2(limited_groups, paragraphs)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    logger.info("Evaluating batches concurrently with Bedrock Model & Deterministic Scorer...")
    
    batch_results = await asyncio.gather(
        *[_evaluate_batch(batch, request, semaphore, session_id) for batch in batches]
    )

    seen_positions = set()
    positioned: List[Tuple[int, Suggestion]] = []
    for b_res in batch_results:
        for pos, sugg in b_res:
            if pos in seen_positions:
                continue
            seen_positions.add(pos)
            positioned.append((pos, sugg))

    positioned.sort(key=lambda pair: pair[0])
    suggestions = [sugg for _, sugg in positioned]

    # 7. Output Generation
    logger.info(f"\nStage 6: Outputting final business results ({len(suggestions)} suggestions generated)...")
    final_response = GeneralReviewResponse(suggestions=suggestions)
    
    output_file = output_dir / "general_review_output.json"
    output_file.write_text(final_response.model_dump_json(indent=2), encoding="utf-8")
    logger.info(f"Exact Business Output written to: {output_file}")

    # Risk level distribution
    risk_counts: Dict[str, int] = {"High": 0, "Medium": 0, "Low": 0}
    for s in suggestions:
        risk_counts[s.risk_level] = risk_counts.get(s.risk_level, 0) + 1

    summary_file = output_dir / "review_summary.json"
    summary_data = {
        "status": "success",
        "total_paragraphs": len(paragraphs),
        "total_clause_units": len(groups),
        "batches_processed": len(batches),
        "suggestions_count": len(suggestions),
        "risk_distribution": risk_counts,
        "elapsed_seconds": round(time.time() - start_time, 2),
        "output_file": str(output_file),
    }
    summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

    logger.info("=================================================================")
    logger.info("TEST EXECUTION COMPLETED SUCCESSFULLY")
    logger.info(f"Elapsed Time: {summary_data['elapsed_seconds']}s")
    logger.info(f"Suggestions: {len(suggestions)} (High: {risk_counts['High']}, Medium: {risk_counts['Medium']}, Low: {risk_counts['Low']})")
    logger.info("=================================================================")

    return summary_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test General Review Pipeline with Payload")
    parser.add_argument(
        "--payload",
        type=str,
        default=str(PROJECT_ROOT / "test" / "input" / "input_payload.json"),
        help="Path to input payload JSON file"
    )
    parser.add_argument(
        "--test-dir",
        type=str,
        default=str(PROJECT_ROOT / "test"),
        help="Path to test output directory"
    )
    parser.add_argument(
        "--limit-groups",
        type=int,
        default=None,
        help="Limit number of groups evaluated for fast testing"
    )
    parser.add_argument(
        "--force-vector",
        action="store_true",
        help="Force fresh vector search against Qdrant instead of cached mapping"
    )

    args = parser.parse_args()
    asyncio.run(run_test(Path(args.payload), Path(args.test_dir), max_groups_to_review=args.limit_groups, force_vector=args.force_vector))

