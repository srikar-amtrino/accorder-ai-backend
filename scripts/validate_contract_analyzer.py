r"""Validate ONLY the Contract Analyzer agent against a running server.

Unlike scripts/measure_agent_timings.py (which fires every agent and only records
list-lengths), this ingests one .docx, calls /Accorder/agents/contract-analyzer
once, saves the FULL structured JSON for correctness review, and reports timing.

USAGE
  1. Put the contract .docx in the repo (e.g. the Epit vendor agreement).
  2. Start the server in another terminal:
       poetry run python -m src.api.main
  3. Run:
       poetry run python scripts/validate_contract_analyzer.py --doc "Epit.docx"

  Output JSON is written to AnalyzerResponse.json (override with --out).
  If the endpoint is auth-protected, set $env:AGENT_AUTH_TOKEN before running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TIMEOUT = 600


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the Contract Analyzer agent.")
    ap.add_argument("--doc", required=True, help="Path to the contract .docx to analyze.")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--session", default=f"validate-analyzer-{int(time.time())}")
    ap.add_argument("--out", default="AnalyzerResponse.json")
    args = ap.parse_args()

    doc = Path(args.doc)
    if not doc.exists():
        sys.exit(f"Doc not found: {doc}")
    if not doc.name.lower().endswith(".docx"):
        sys.exit("Only .docx is supported (clause extraction requires a .docx extension).")

    base = args.base_url.rstrip("/")
    headers = {"X-Session-ID": args.session, "X-Session-Id": args.session}
    token = os.environ.get("AGENT_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"Base URL : {base}")
    print(f"Session  : {args.session}")
    print(f"Document : {doc.name}  ({doc.stat().st_size:,} bytes)")
    print(f"Auth     : {'Bearer token set' if token else 'no token'}")
    print("-" * 70)

    # 1. Ingest (no LLM; creates the session the analyzer reads from).
    files = {"file": (doc.name, doc.read_bytes(), DOCX_MIME)}
    t0 = time.perf_counter()
    r = requests.post(base + "/api/v1/ingest/", files=files, headers=headers, timeout=TIMEOUT)
    print(f"[{r.status_code}] ingest  ({time.perf_counter() - t0:.1f}s)")
    if r.status_code != 200:
        sys.exit(f"Ingest failed: {r.text[:500]}")

    # 2. Contract Analyzer (the one LLM call we are validating).
    files = {"file": (doc.name, doc.read_bytes(), DOCX_MIME)}
    t0 = time.perf_counter()
    r = requests.post(base + "/Accorder/agents/contract-analyzer", files=files, headers=headers, timeout=TIMEOUT)
    wall = time.perf_counter() - t0
    print(f"[{r.status_code}] contract-analyzer  ({wall:.1f}s)")
    if r.status_code != 200:
        sys.exit(f"Analyzer failed: {r.text[:1000]}")

    body = r.json()
    Path(args.out).write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")

    ki = body.get("key_information", [])
    tl = body.get("timeline_and_key_milestones", [])
    risks = body.get("risk_and_compliance_insights", [])
    crit = sum(1 for x in risks if x.get("severity") == "Critical")
    high = sum(1 for x in risks if x.get("severity") == "High")

    print("-" * 70)
    print(f"summary           : {len((body.get('summary') or '').split())} words")
    print(f"key_information   : {len(ki)} items")
    print(f"timeline          : {len(tl)} items")
    print(f"risks             : {len(risks)} items  ({crit} Critical / {high} High)")
    print(f"wall time         : {wall:.1f}s")
    print(f"saved             : {args.out}")


if __name__ == "__main__":
    main()
