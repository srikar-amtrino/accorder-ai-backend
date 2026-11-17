"""
run_samples_3_runs.py
---------------------
Executes 3 independent cold-cache runs of the General Review pipeline
on Sample 1 and Sample 2, producing:
  - run_1.json, run_2.json, run_3.json
  - 3_runs_consistency_summary.json
in each document's folder under test/documents/.
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
from src.tools.general_review import general_review_service, _response_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("sample_consistency_benchmark")


def _text_similarity(s1: str, s2: str) -> float:
    """Compute normalized sequence similarity ratio between two strings (0.0 to 1.0)."""
    if not s1 or not s2:
        return 0.0
    return difflib.SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()


async def run_single_audit(
    request: GeneralReviewRequest, session_id: str, audit_trace: list | None = None
) -> GeneralReviewResponse:
    # Guarantee cold cache execution
    _response_cache.clear()
    return await general_review_service(request, session_id=session_id, audit_trace_collector=audit_trace)


async def audit_sample(sample_name: str, payload_filename: str):
    doc_dir = PROJECT_ROOT / "test" / "documents" / sample_name
    payload_path = doc_dir / payload_filename

    if not payload_path.exists():
        logger.error(f"Payload not found: {payload_path}")
        return

    logger.info("=" * 60)
    logger.info(f"STARTING 3-RUN CONSISTENCY BENCHMARK: {sample_name}")
    logger.info("=" * 60)

    data = json.loads(payload_path.read_text(encoding="utf-8"))
    request = GeneralReviewRequest.model_validate(data)
    para_count = len(request.textinformation)

    runs = []
    for r in (1, 2, 3):
        session_id = f"{sample_name.replace(' ', '_').lower()}_run_{r}_{int(time.time())}"
        logger.info(f"\n--- [{sample_name}] RUN {r} OF 3 STARTING ---")
        t0 = time.time()
        audit_trace: list = []
        resp = await run_single_audit(request, session_id, audit_trace=audit_trace)
        elapsed = time.time() - t0
        logger.info(f"--- [{sample_name}] RUN {r} FINISHED in {elapsed:.1f}s | {len(resp.suggestions)} suggestions ---")

        run_path = doc_dir / f"run_{r}.json"
        run_path.write_text(resp.model_dump_json(indent=2), encoding="utf-8")

        # Export transparent audit trace requested by higher authority
        trace_path = doc_dir / f"run_{r}_audit_trace.json"
        trace_doc = {
            "document_metadata": {
                "sample_name": sample_name,
                "run_number": r,
                "agreement_type": request.agreement_type,
                "party_represented": request.party_represented,
                "review_objective": request.review_objective,
                "total_paragraphs": para_count,
                "total_clause_units": len(audit_trace),
                "total_suggestions_issued": len(resp.suggestions),
                "execution_seconds": round(elapsed, 2),
            },
            "clause_units": audit_trace,
        }
        trace_path.write_text(json.dumps(trace_doc, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"--- [{sample_name}] RUN {r} AUDIT TRACE SAVED to {trace_path.name} ({len(audit_trace)} clause units) ---")

        runs.append((r, resp, elapsed))

    # Consistency analysis
    logger.info(f"\n--- [{sample_name}] COMPUTING 3-RUN CONSISTENCY METRICS ---")

    map_run1 = {s.para_identifier: s for s in runs[0][1].suggestions}
    map_run2 = {s.para_identifier: s for s in runs[1][1].suggestions}
    map_run3 = {s.para_identifier: s for s in runs[2][1].suggestions}

    all_paras = sorted(set(map_run1.keys()) | set(map_run2.keys()) | set(map_run3.keys()))

    unanimous_clause_count = 0
    unanimous_risk_count = 0
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

        is_consistent = in_all_three and same_risk and same_clause and avg_reason_sim >= 0.50
        if is_consistent:
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
            "fully_consistent": is_consistent
        })

    overall_avg_reason_sim = round(sum(reason_sims) / len(reason_sims), 3) if reason_sims else 0.0
    overall_avg_fix_sim = round(sum(fix_sims) / len(fix_sims), 3) if fix_sims else 0.0
    clause_rate = round((unanimous_clause_count / len(all_paras)) * 100, 2) if all_paras else 0.0
    risk_rate = round((unanimous_risk_count / len(all_paras)) * 100, 2) if all_paras else 0.0

    summary = {
        "document_name": sample_name,
        "paragraph_count": para_count,
        "agreement_type": request.agreement_type,
        "party_represented": request.party_represented or "Neutral General Review",
        "runs_summary": [
            {"run": r, "suggestions_count": len(resp.suggestions), "elapsed_seconds": round(el, 2)}
            for r, resp, el in runs
        ],
        "total_unique_paragraphs_flagged": len(all_paras),
        "clause_consistency_percentage": clause_rate,
        "risk_level_consistency_percentage": risk_rate,
        "mean_reason_similarity_across_runs": overall_avg_reason_sim,
        "mean_suggested_fix_similarity_across_runs": overall_avg_fix_sim,
        "unanimous_perfect_match_count": unanimous_risk_count,
        "inconsistencies": [
            row for row in detailed_comparisons
            if not (row["clause"]["unanimous"] and row["risk_level"]["unanimous"])
        ],
        "all_comparisons": detailed_comparisons
    }

    summary_file = doc_dir / "3_runs_consistency_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    logger.info(f"\n==========================================")
    logger.info(f"[{sample_name}] CONSISTENCY SUMMARY:")
    logger.info(f"Paragraphs: {para_count}")
    logger.info(f"Suggestions: Run 1={len(runs[0][1].suggestions)}, Run 2={len(runs[1][1].suggestions)}, Run 3={len(runs[2][1].suggestions)}")
    logger.info(f"Unique Flagged Paras: {len(all_paras)}")
    logger.info(f"Clause Consistency: {clause_rate}%")
    logger.info(f"Risk Level Consistency: {risk_rate}%")
    logger.info(f"Mean Reason Similarity: {overall_avg_reason_sim * 100:.1f}%")
    logger.info(f"Mean Suggested Fix Similarity: {overall_avg_fix_sim * 100:.1f}%")
    logger.info(f"Summary saved: {summary_file}")
    logger.info(f"==========================================\n")


async def main():
    all_available = [
        ("Sample 1", "Sample_1_payload.json"),
        ("Sample 2", "Sample_2_payload.json"),
        ("Sample 3", "Sample_3_payload.json"),
        ("Sample 3 Clause Simulation", "Sample_3_clause_payload.json"),
    ]

    # Allow CLI filtering: e.g. py run_samples_3_runs.py "Sample 3" or py run_samples_3_runs.py 3
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg in ("sim", "simulation", "clause_sim", "sample 3 clause simulation", "clause"):
            samples = [("Sample 3 Clause Simulation", "Sample_3_clause_payload.json")]
        elif arg in ("3", "sample 3", "sample3", "sample_3"):
            samples = [("Sample 3", "Sample_3_payload.json")]
        elif arg in ("2", "sample 2", "sample2", "sample_2"):
            samples = [("Sample 2", "Sample_2_payload.json")]
        elif arg in ("1", "sample 1", "sample1", "sample_1"):
            samples = [("Sample 1", "Sample_1_payload.json")]
        else:
            samples = [s for s in all_available if arg in s[0].lower()]
            if not samples:
                logger.error(f"No sample matching {sys.argv[1]!r}. Available: {[s[0] for s in all_available]}")
                return
    else:
        samples = all_available

    total_t0 = time.time()
    for s_name, p_name in samples:
        await audit_sample(s_name, p_name)
    total_elapsed = time.time() - total_t0
    logger.info(f"\nALL SAMPLES COMPLETED IN {total_elapsed / 60:.1f} MINUTES.")


if __name__ == "__main__":
    asyncio.run(main())

