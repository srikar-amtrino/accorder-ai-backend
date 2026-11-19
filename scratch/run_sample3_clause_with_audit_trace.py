"""
scratch/run_sample3_clause_with_audit_trace.py
----------------------------------------------
Executes Sample 3 Clause Payload with full audit trace collector active,
generating:
  - test/documents/Sample 3 Clause Simulation/sample3_clause_audit_trace.json
  - test/documents/Sample 3 Clause Simulation/run_1_audit_trace.json
  - test/documents/Sample 3 Clause Simulation/run_1.json
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
logger = logging.getLogger("clause_audit_runner")


async def main():
    doc_dir = PROJECT_ROOT / "test" / "documents" / "Sample 3 Clause Simulation"
    payload_path = doc_dir / "Sample_3_clause_payload.json"

    if not payload_path.exists():
        logger.error(f"Payload not found: {payload_path}")
        return

    data = json.loads(payload_path.read_text(encoding="utf-8"))
    data["simulation_clause_payload"] = True

    request = GeneralReviewRequest.model_validate(data)
    session_id = f"sample3_clause_audit_trace_{int(time.time())}"

    logger.info("=" * 65)
    logger.info("STARTING SAMPLE 3 CLAUSE AUDIT TRACE EXECUTION")
    logger.info(f"Total Clauses: {len(request.clauses or [])}")
    logger.info(f"Session ID: {session_id}")
    logger.info("=" * 65)

    _response_cache.clear()
    audit_trace: list = []
    t0 = time.time()
    response = await general_review_service(
        request, session_id=session_id, audit_trace_collector=audit_trace
    )
    elapsed = time.time() - t0

    clause_units, recon_pass, party_audit = extract_audit_trace_bundle(audit_trace)

    logger.info(f"Review completed in {elapsed:.1f}s | {len(response.suggestions)} suggestions")
    logger.info(f"Audit trace collected: {len(clause_units)} clause units")
    if recon_pass:
        resolved_cnt = recon_pass.get("total_criteria_fully_resolved", recon_pass.get("total_criteria_resolved", 0))
        partial_cnt = recon_pass.get("total_criteria_partially_resolved", 0)
        blobs_cnt = recon_pass.get("total_fix_blobs_removed", 0)
        logger.info(f"  CCRL Registered: {recon_pass.get('total_ccrl_entries_registered', 0)} | Fully Resolved: {resolved_cnt} | Partial: {partial_cnt} | Fix Blobs Removed: {blobs_cnt}")

    # Determine run number dynamically
    run_num = 1
    while (doc_dir / f"run_{run_num}_audit_trace.json").exists():
        run_num += 1

    # Build the full audit trace document
    trace_doc = {
        "document_metadata": {
            "sample_name": "Sample 3 Clause Simulation",
            "run_number": run_num,
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

    # Save to target locations
    trace_file_primary = doc_dir / "sample3_clause_audit_trace.json"
    trace_file_run = doc_dir / f"run_{run_num}_audit_trace.json"
    run_file = doc_dir / f"run_{run_num}.json"

    trace_json_str = json.dumps(trace_doc, indent=2, ensure_ascii=False)
    trace_file_primary.write_text(trace_json_str, encoding="utf-8")
    trace_file_run.write_text(trace_json_str, encoding="utf-8")
    run_file.write_text(response.model_dump_json(indent=2), encoding="utf-8")

    logger.info("=" * 65)
    logger.info(f"AUDIT TRACE SAVED TO:")
    logger.info(f"  1. {trace_file_primary}")
    logger.info(f"  2. {trace_file_run}")
    logger.info(f"  3. {run_file}")
    logger.info("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
