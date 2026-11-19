"""scratch/run_sample5_verification.py
------------------------------------
Executes Sample 5 under both:
1. Clause-level mode (Sample_5_clause_payload.json, simulation_clause_payload=True)
2. Paragraph-level mode (Sample_5_payload.json, simulation_clause_payload=False)

Generates:
  - test/documents/Sample 5 Clause Simulation/sample5_clause_audit_trace.json
  - test/documents/Sample 5 Clause Simulation/run_{n}_audit_trace.json
  - test/documents/Sample 5 Clause Simulation/run_{n}.json
  - test/documents/Sample 5/sample5_paragraph_audit_trace.json
  - test/documents/Sample 5/run_{n}_audit_trace.json
  - test/documents/Sample 5/run_{n}.json
"""

import asyncio
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

from src.schemas.general_review import GeneralReviewRequest
from src.tools.general_review import (
    extract_audit_trace_bundle,
    general_review_service,
    _response_cache,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sample5_verification")


async def run_clause_mode():
    doc_dir = PROJECT_ROOT / "test" / "documents" / "Sample 5 Clause Simulation"
    payload_path = doc_dir / "Sample_5_clause_payload.json"

    logger.info("=" * 70)
    logger.info("TEST 1: SAMPLE 5 — CLAUSE SIMULATION MODE (ON)")
    logger.info("=" * 70)

    data = json.loads(payload_path.read_text(encoding="utf-8"))
    data["simulation_clause_payload"] = True

    request = GeneralReviewRequest.model_validate(data)
    session_id = f"sample5_clause_ccrl_{int(time.time())}"

    _response_cache.clear()
    audit_trace_raw: list = []
    t0 = time.time()
    response = await general_review_service(request, session_id=session_id, audit_trace_collector=audit_trace_raw)
    elapsed = time.time() - t0

    clause_units, recon_pass, party_audit = extract_audit_trace_bundle(audit_trace_raw)

    run_num = 1
    while (doc_dir / f"run_{run_num}_audit_trace.json").exists():
        run_num += 1

    trace_doc = {
        "document_metadata": {
            "sample_name": "Sample 5 Clause Simulation",
            "run_number": run_num,
            "simulation_clause_payload": True,
            "agreement_type": request.agreement_type,
            "party_represented": request.party_represented,
            "review_objective": request.review_objective,
            "total_clauses": len(request.clauses or []),
            "total_clause_units": len(clause_units),
            "total_suggestions_issued": len(response.suggestions),
            "execution_seconds": round(elapsed, 2),
        },
        "party_representation_audit": party_audit,
        "reconciliation_pass": recon_pass,
        "clause_units": clause_units,
    }

    trace_file = doc_dir / f"run_{run_num}_audit_trace.json"
    latest_trace_file = doc_dir / "sample5_clause_audit_trace.json"
    run_file = doc_dir / f"run_{run_num}.json"

    trace_json = json.dumps(trace_doc, indent=2, ensure_ascii=False)
    trace_file.write_text(trace_json, encoding="utf-8")
    latest_trace_file.write_text(trace_json, encoding="utf-8")
    run_file.write_text(response.model_dump_json(indent=2), encoding="utf-8")

    logger.info(f"Clause Mode finished in {elapsed:.1f}s | {len(response.suggestions)} suggestions")
    resolved_cnt = recon_pass.get('total_criteria_fully_resolved', recon_pass.get('total_criteria_resolved', 0))
    partial_cnt = recon_pass.get('total_criteria_partially_resolved', 0)
    blobs_cnt = recon_pass.get('total_fix_blobs_removed', 0)
    logger.info(f"  CCRL Registered: {recon_pass.get('total_ccrl_entries_registered', 0)} | Fully Resolved: {resolved_cnt} | Partial: {partial_cnt} | Fix Blobs Removed: {blobs_cnt}")
    logger.info(f"  Saved to: {trace_file.name} & {run_file.name}")
    return response, trace_doc, elapsed


async def run_paragraph_mode():
    doc_dir = PROJECT_ROOT / "test" / "documents" / "Sample 5"
    payload_path = doc_dir / "Sample_5_payload.json"

    logger.info("=" * 70)
    logger.info("TEST 2: SAMPLE 5 — CLAUSE SIMULATION MODE (OFF) / PARAGRAPH MODE")
    logger.info("=" * 70)

    data = json.loads(payload_path.read_text(encoding="utf-8"))
    data["simulation_clause_payload"] = False

    request = GeneralReviewRequest.model_validate(data)
    session_id = f"sample5_para_ccrl_{int(time.time())}"

    _response_cache.clear()
    audit_trace_raw: list = []
    t0 = time.time()
    response = await general_review_service(request, session_id=session_id, audit_trace_collector=audit_trace_raw)
    elapsed = time.time() - t0

    clause_units, recon_pass, party_audit = extract_audit_trace_bundle(audit_trace_raw)

    run_num = 1
    while (doc_dir / f"run_{run_num}_audit_trace.json").exists():
        run_num += 1

    trace_doc = {
        "document_metadata": {
            "sample_name": "Sample 5 Paragraph Mode",
            "run_number": run_num,
            "simulation_clause_payload": False,
            "agreement_type": request.agreement_type,
            "party_represented": request.party_represented,
            "review_objective": request.review_objective,
            "total_paragraphs": len(request.textinformation or []),
            "total_clause_units": len(clause_units),
            "total_suggestions_issued": len(response.suggestions),
            "execution_seconds": round(elapsed, 2),
        },
        "party_representation_audit": party_audit,
        "reconciliation_pass": recon_pass,
        "clause_units": clause_units,
    }

    trace_file = doc_dir / f"run_{run_num}_audit_trace.json"
    latest_trace_file = doc_dir / "sample5_paragraph_audit_trace.json"
    run_file = doc_dir / f"run_{run_num}.json"

    trace_json = json.dumps(trace_doc, indent=2, ensure_ascii=False)
    trace_file.write_text(trace_json, encoding="utf-8")
    latest_trace_file.write_text(trace_json, encoding="utf-8")
    run_file.write_text(response.model_dump_json(indent=2), encoding="utf-8")

    logger.info(f"Paragraph Mode finished in {elapsed:.1f}s | {len(response.suggestions)} suggestions")
    resolved_cnt = recon_pass.get('total_criteria_fully_resolved', recon_pass.get('total_criteria_resolved', 0))
    partial_cnt = recon_pass.get('total_criteria_partially_resolved', 0)
    blobs_cnt = recon_pass.get('total_fix_blobs_removed', 0)
    logger.info(f"  CCRL Registered: {recon_pass.get('total_ccrl_entries_registered', 0)} | Fully Resolved: {resolved_cnt} | Partial: {partial_cnt} | Fix Blobs Removed: {blobs_cnt}")
    logger.info(f"  Saved to: {trace_file.name} & {run_file.name}")
    return response, trace_doc, elapsed


async def main():
    logger.info("Starting Sample 5 verification across both modes...")
    t_start = time.time()

    res_clause, trace_clause, time_clause = await run_clause_mode()
    res_para, trace_para, time_para = await run_paragraph_mode()

    total_time = time.time() - t_start
    logger.info("=" * 70)
    logger.info("SAMPLE 5 DUAL VERIFICATION COMPLETE")
    logger.info(f"Total Execution Time: {total_time:.1f}s")
    logger.info(f"Clause Mode:    {len(res_clause.suggestions)} suggestions ({time_clause:.1f}s)")
    logger.info(f"Paragraph Mode: {len(res_para.suggestions)} suggestions ({time_para:.1f}s)")
    logger.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
