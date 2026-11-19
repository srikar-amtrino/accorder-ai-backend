"""
scratch/run_sample3_dual_eval.py
--------------------------------
Executes Sample 3 under both:
1. Clause-level mode (Sample_3_clause_payload.json, simulation_clause_payload=True)
2. Paragraph-level mode (Sample_3_payload.json, simulation_clause_payload=False)
using the newly integrated Prompt 1 (Evaluator) and Prompt 2 (Drafting Counsel).
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
from src.tools.general_review import general_review_service, _response_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sample3_dual_runner")


async def run_clause_level():
    logger.info("=" * 65)
    logger.info("PHASE 1: RUNNING SAMPLE 3 IN CLAUSE-LEVEL MODE")
    logger.info("=" * 65)

    payload_path = PROJECT_ROOT / "test" / "documents" / "Sample 3 Clause Simulation" / "Sample_3_clause_payload.json"
    data = json.loads(payload_path.read_text(encoding="utf-8"))
    data["simulation_clause_payload"] = True

    request = GeneralReviewRequest.model_validate(data)
    session_id = f"sample3_clause_new_prompts_{int(time.time())}"

    _response_cache.clear()
    t0 = time.time()
    response = await general_review_service(request, session_id=session_id)
    elapsed = time.time() - t0

    out_file = PROJECT_ROOT / "test" / "documents" / "Sample 3 Clause Simulation" / "sample3_clause_new_prompts_run.json"
    out_file.write_text(json.dumps(response.model_dump(), indent=2), encoding="utf-8")

    logger.info(f"Clause-level run complete in {elapsed:.1f}s | {len(response.suggestions)} suggestions")
    return response, elapsed


async def run_paragraph_level():
    logger.info("=" * 65)
    logger.info("PHASE 2: RUNNING SAMPLE 3 IN PARAGRAPH-LEVEL MODE")
    logger.info("=" * 65)

    payload_path = PROJECT_ROOT / "test" / "documents" / "Sample 3" / "Sample_3_payload.json"
    data = json.loads(payload_path.read_text(encoding="utf-8"))
    data["simulation_clause_payload"] = False

    request = GeneralReviewRequest.model_validate(data)
    session_id = f"sample3_paragraph_new_prompts_{int(time.time())}"

    _response_cache.clear()
    t0 = time.time()
    response = await general_review_service(request, session_id=session_id)
    elapsed = time.time() - t0

    out_file = PROJECT_ROOT / "test" / "documents" / "Sample 3" / "sample3_paragraph_new_prompts_run.json"
    out_file.write_text(json.dumps(response.model_dump(), indent=2), encoding="utf-8")

    logger.info(f"Paragraph-level run complete in {elapsed:.1f}s | {len(response.suggestions)} suggestions")
    return response, elapsed


async def main():
    res_clause, time_clause = await run_clause_level()
    res_para, time_para = await run_paragraph_level()

    logger.info("=" * 65)
    logger.info("DUAL RUN SUMMARY")
    logger.info(f"Clause Mode:    {len(res_clause.suggestions)} suggestions in {time_clause:.1f}s")
    logger.info(f"Paragraph Mode: {len(res_para.suggestions)} suggestions in {time_para:.1f}s")
    logger.info("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
