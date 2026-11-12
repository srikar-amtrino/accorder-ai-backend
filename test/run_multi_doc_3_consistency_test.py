"""
run_multi_doc_3_consistency_test.py
-----------------------------------
Runs 3 independent cold-cache generations for each of the 3 new document payloads:
  1. coreweave_microsoft_msa
  2. brightstone_crestview_msa
  3. epit_xyz_spec_agreement

For each document, it performs a deep 3-way consistency audit:
  - Clause type match %
  - Risk level (High / Medium / Low) match %
  - Reason & Suggested Fix text similarity
  - Detailed inconsistency logging
And saves run_1.json, run_2.json, run_3.json, and 3_runs_consistency_summary.json
directly inside each document's folder.
"""

import asyncio
import difflib
import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path("c:/accorder ai/Applications/accorder-ai-backend")
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from src.schemas.general_review import GeneralReviewRequest, GeneralReviewResponse
from src.tools.general_review import (
    _build_batches_v2,
    _build_clause_groups,
    _classify_all_paragraphs,
    _evaluate_batch,
    _smooth_classifications,
    _response_cache,
    MAX_CONCURRENT_CALLS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("multi_doc_test")


def _text_similarity(s1: str, s2: str) -> float:
    """Compute normalized sequence similarity ratio between two strings (0.0 to 1.0)."""
    if not s1 or not s2:
        return 0.0
    return difflib.SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()


async def execute_single_run(request: GeneralReviewRequest, session_prefix: str, run_id: int) -> GeneralReviewResponse:
    _response_cache.clear()
    session_id = f"{session_prefix}_run_{run_id}"
    paragraphs = request.textinformation

    t0 = time.time()
    raw_matches = await _classify_all_paragraphs(request, session_id)
    t_vec = time.time() - t0

    smoothed = _smooth_classifications(raw_matches, paragraphs)
    groups = _build_clause_groups(smoothed, paragraphs)
    batches = _build_batches_v2(groups, paragraphs)
    logger.info(f"[{session_prefix} Run {run_id}] {len(paragraphs)} paras -> {len(groups)} groups -> {len(batches)} batches (vector prep: {t_vec:.1f}s)")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    batch_results = await asyncio.gather(
        *[_evaluate_batch(batch, request, semaphore, session_id) for batch in batches]
    )

    seen_positions = set()
    positioned = []
    for b_res in batch_results:
        for pos, sugg in b_res:
            if pos in seen_positions:
                continue
            seen_positions.add(pos)
            positioned.append((pos, sugg))

    positioned.sort(key=lambda pair: pair[0])
    return GeneralReviewResponse(suggestions=[sugg for _, sugg in positioned])


async def audit_document(doc_folder: Path):
    doc_name = doc_folder.name
    payload_file = doc_folder / "payload.json"
    if not payload_file.exists():
        logger.error(f"Payload not found in {doc_folder}")
        return None

    data = json.loads(payload_file.read_text(encoding="utf-8"))
    request = GeneralReviewRequest.model_validate(data)

    logger.info("=================================================================")
    logger.info(f"STARTING 3-RUN AUDIT FOR: {doc_name} ({len(request.textinformation)} paragraphs)")
    logger.info("=================================================================")

    runs = []
    for r in (1, 2, 3):
        logger.info(f"\n--- {doc_name}: Run {r} of 3 (Cold Cache) ---")
        t_start = time.time()
        resp = await execute_single_run(request, session_prefix=doc_name, run_id=r)
        elapsed = time.time() - t_start
        logger.info(f"[{doc_name} Run {r}] Finished in {elapsed:.1f}s with {len(resp.suggestions)} suggestions.")

        run_file = doc_folder / f"run_{r}.json"
        run_file.write_text(resp.model_dump_json(indent=2), encoding="utf-8")
        runs.append((r, resp, elapsed))

    # Perform 3-Way Consistency Comparison
    map_run1 = {s.para_identifier: s for s in runs[0][1].suggestions}
    map_run2 = {s.para_identifier: s for s in runs[1][1].suggestions}
    map_run3 = {s.para_identifier: s for s in runs[2][1].suggestions}

    all_paras = sorted(set(map_run1.keys()) | set(map_run2.keys()) | set(map_run3.keys()))

    unanimous_risk_count = 0
    unanimous_clause_count = 0
    perfect_all_match_count = 0
    detailed_comparisons = []
    reason_sims = []
    fix_sims = []

    for pid in all_paras:
        s1 = map_run1.get(pid)
        s2 = map_run2.get(pid)
        s3 = map_run3.get(pid)

        in_all_three = bool(s1 and s2 and s3)

        clause_1 = s1.clause if s1 else "MISSING"
        clause_2 = s2.clause if s2 else "MISSING"
        clause_3 = s3.clause if s3 else "MISSING"

        risk_1 = s1.risk_level if s1 else "None"
        risk_2 = s2.risk_level if s2 else "None"
        risk_3 = s3.risk_level if s3 else "None"

        same_clause = (clause_1 == clause_2 == clause_3) and clause_1 != "MISSING"
        same_risk = (risk_1 == risk_2 == risk_3) and risk_1 != "None"

        if same_clause:
            unanimous_clause_count += 1
        if same_risk and same_clause:
            unanimous_risk_count += 1

        r_sim_12 = _text_similarity(s1.reason, s2.reason) if in_all_three else 0.0
        r_sim_23 = _text_similarity(s2.reason, s3.reason) if in_all_three else 0.0
        r_sim_13 = _text_similarity(s1.reason, s3.reason) if in_all_three else 0.0
        avg_reason_sim = round((r_sim_12 + r_sim_23 + r_sim_13) / 3.0, 3) if in_all_three else 0.0

        f_sim_12 = _text_similarity(s1.suggested_fix, s2.suggested_fix) if in_all_three else 0.0
        f_sim_23 = _text_similarity(s2.suggested_fix, s3.suggested_fix) if in_all_three else 0.0
        f_sim_13 = _text_similarity(s1.suggested_fix, s3.suggested_fix) if in_all_three else 0.0
        avg_fix_sim = round((f_sim_12 + f_sim_23 + f_sim_13) / 3.0, 3) if in_all_three else 0.0

        if in_all_three:
            reason_sims.append(avg_reason_sim)
            fix_sims.append(avg_fix_sim)

        is_consistent_content = in_all_three and same_risk and same_clause and avg_reason_sim >= 0.65
        if is_consistent_content:
            perfect_all_match_count += 1

        detailed_comparisons.append({
            "para_identifier": pid,
            "present_in_runs": [r for r, s in ((1, s1), (2, s2), (3, s3)) if s is not None],
            "clause": {
                "run_1": clause_1,
                "run_2": clause_2,
                "run_3": clause_3,
                "unanimous": same_clause
            },
            "risk_level": {
                "run_1": risk_1,
                "run_2": risk_2,
                "run_3": risk_3,
                "unanimous": same_risk
            },
            "reason_similarity": {
                "avg_pairwise_ratio": avg_reason_sim,
                "run_1_preview": (s1.reason[:120] + "...") if s1 else None,
                "run_2_preview": (s2.reason[:120] + "...") if s2 else None,
                "run_3_preview": (s3.reason[:120] + "...") if s3 else None,
            },
            "suggested_fix_similarity": {
                "avg_pairwise_ratio": avg_fix_sim,
                "run_1_len": len(s1.suggested_fix) if s1 else 0,
                "run_2_len": len(s2.suggested_fix) if s2 else 0,
                "run_3_len": len(s3.suggested_fix) if s3 else 0,
            },
            "fully_consistent": is_consistent_content
        })

    overall_avg_reason_sim = round(sum(reason_sims) / len(reason_sims), 3) if reason_sims else 0.0
    overall_avg_fix_sim = round(sum(fix_sims) / len(fix_sims), 3) if fix_sims else 0.0

    risk_consistency_rate = round((unanimous_risk_count / len(all_paras)) * 100, 2) if all_paras else 100.0
    clause_consistency_rate = round((unanimous_clause_count / len(all_paras)) * 100, 2) if all_paras else 100.0

    summary = {
        "document_name": doc_name,
        "paragraph_count": len(request.textinformation),
        "agreement_type": request.agreement_type,
        "party_represented": request.party_represented,
        "runs_summary": [
            {"run": r, "suggestions_count": len(resp.suggestions), "elapsed_seconds": round(el, 2)}
            for r, resp, el in runs
        ],
        "total_unique_paragraphs_flagged": len(all_paras),
        "clause_consistency_percentage": clause_consistency_rate,
        "risk_level_consistency_percentage": risk_consistency_rate,
        "mean_reason_similarity_across_runs": overall_avg_reason_sim,
        "mean_suggested_fix_similarity_across_runs": overall_avg_fix_sim,
        "unanimous_perfect_match_count": unanimous_risk_count,
        "inconsistencies": [
            row for row in detailed_comparisons
            if not (row["clause"]["unanimous"] and row["risk_level"]["unanimous"])
        ],
        "all_comparisons": detailed_comparisons
    }

    summary_file = doc_folder / "3_runs_consistency_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    logger.info("-----------------------------------------------------------------")
    logger.info(f"AUDIT SUMMARY FOR {doc_name}:")
    logger.info(f"  Flagged paragraphs: {len(all_paras)}")
    logger.info(f"  Runs count: {[len(r[1].suggestions) for r in runs]}")
    logger.info(f"  Clause Consistency: {clause_consistency_rate}%")
    logger.info(f"  Risk Level Consistency: {risk_consistency_rate}%")
    logger.info(f"  Saved: {summary_file}")
    logger.info("-----------------------------------------------------------------")
    return summary


async def main():
    docs_dir = PROJECT_ROOT / "test" / "documents"
    doc_folders = [
        docs_dir / "coreweave_microsoft_msa",
        docs_dir / "brightstone_crestview_msa",
        docs_dir / "epit_xyz_spec_agreement"
    ]

    all_summaries = []
    t_all_start = time.time()

    for df in doc_folders:
        summary = await audit_document(df)
        if summary:
            all_summaries.append(summary)

    total_time = time.time() - t_all_start

    master_summary = {
        "total_elapsed_seconds": round(total_time, 2),
        "documents_audited": len(all_summaries),
        "results": [
            {
                "document": s["document_name"],
                "paragraphs": s["paragraph_count"],
                "runs_suggestions": [r["suggestions_count"] for r in s["runs_summary"]],
                "clause_consistency_percentage": s["clause_consistency_percentage"],
                "risk_level_consistency_percentage": s["risk_level_consistency_percentage"],
                "unanimous_perfect_matches": f"{s['unanimous_perfect_match_count']}/{s['total_unique_paragraphs_flagged']}",
                "mean_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in s["runs_summary"]) / len(s["runs_summary"]), 1)
            }
            for s in all_summaries
        ]
    }

    master_file = docs_dir / "master_3_documents_consistency_report.json"
    master_file.write_text(json.dumps(master_summary, indent=2), encoding="utf-8")

    logger.info("\n=================================================================")
    logger.info("ALL 3 DOCUMENTS AUDITED SUCCESSFULLY")
    logger.info(f"Master Report: {master_file}")
    logger.info("=================================================================")


if __name__ == "__main__":
    asyncio.run(main())

