r"""Measure real per-agent response times against a running server.

Hits every LLM-using agent flow once, in dependency order (ingest first so the
session-scoped agents have a document), records exact wall-clock time per agent,
and writes a timing table + machine-readable JSON/CSV.

WHY a dedicated script (vs scripts/test_endpoints.py): that one records pass/fail
only, has a stale /draft route, skips clause-extraction, and runs a 7-paragraph
toy NDA that makes every agent look far faster than production. This one times
each agent against a realistic document so the numbers reflect real usage.

USAGE
  1. (optional) Archive old logs so the run log is clean:
       PowerShell:  New-Item -ItemType Directory -Force logs\_archive | Out-Null; Move-Item logs\AI_Contract_Review_*.log logs\_archive\ -Force
  2. Start the server (fresh, so it writes today's dated log):
       poetry run python -m src.api.main
  3. In another terminal, set your token (for the two protected endpoints) and run:
       PowerShell:  $env:AGENT_AUTH_TOKEN = "<your Cognito bearer token>"
                    poetry run python scripts/measure_agent_timings.py --doc "RealContract.docx"

  Compare-documents needs two files. If you don't pass --doc-b, a lightly modified
  variant of --doc is generated automatically so the comparison has real diffs.

NOTES
  - Each agent is timed with time.perf_counter() (full client-side wall-clock) and
    cross-checked against requests' response.elapsed. Absolute start/end timestamps
    are recorded so the server log can be sliced per agent afterward.
  - A non-2xx response is recorded and the run continues (one slow/failing agent
    does not abort the rest).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TIMEOUT = 900  # seconds; some agents (compare, general-review on a big doc) run minutes


class Runner:
    def __init__(self, base_url: str, session_id: str, token: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.headers: Dict[str, str] = {"X-Session-ID": session_id, "X-Session-Id": session_id}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.results: List[Dict[str, Any]] = []

    # --- response shape summary (lengths of the lists the UI renders) ---
    @staticmethod
    def _summarize(body: Any) -> str:
        if not isinstance(body, dict):
            return ""
        parts = []
        for k, v in body.items():
            if isinstance(v, list):
                parts.append(f"{k}={len(v)}")
            elif isinstance(v, str) and len(v) > 40:
                parts.append(f"{k}~{len(v)}ch")
        return ", ".join(parts)

    def _call(self, label: str, method: str, path: str, **kwargs: Any) -> Optional[requests.Response]:
        url = self.base_url + path
        start_dt = datetime.now()
        t0 = time.perf_counter()
        status: Any = None
        ok = False
        resp_bytes = 0
        summary = ""
        error = ""
        r: Optional[requests.Response] = None
        try:
            r = requests.request(method, url, headers=self.headers, timeout=TIMEOUT, **kwargs)
            status = r.status_code
            resp_bytes = len(r.content or b"")
            ok = 200 <= r.status_code < 300
            try:
                summary = self._summarize(r.json())
            except Exception:
                summary = ""
            if not ok:
                error = (r.text or "")[:400]
        except Exception as exc:  # network error / timeout
            error = f"{type(exc).__name__}: {exc}"
        wall = time.perf_counter() - t0
        end_dt = datetime.now()
        elapsed = r.elapsed.total_seconds() if (r is not None and r.elapsed) else None

        self.results.append({
            "agent": label,
            "method": method,
            "path": path,
            "start": start_dt.isoformat(timespec="milliseconds"),
            "end": end_dt.isoformat(timespec="milliseconds"),
            "wall_s": round(wall, 2),
            "server_elapsed_s": round(elapsed, 2) if elapsed is not None else None,
            "status": status,
            "ok": ok,
            "resp_bytes": resp_bytes,
            "resp_summary": summary,
            "error": error,
        })
        flag = "OK " if ok else "ERR"
        extra = f"  [{summary}]" if summary else ""
        print(f"  [{flag}] {label:<28} {wall:7.1f}s  status={status}{extra}")
        if error:
            print(f"        ! {error[:200]}")
        return r

    def post_files(self, label: str, path: str, files: Dict[str, Any]) -> Optional[requests.Response]:
        return self._call(label, "POST", path, files=files)

    def post_json(self, label: str, path: str, body: Any) -> Optional[requests.Response]:
        # pass json= so requests sets the Content-Type header itself
        return self._call(label, "POST", path, json=body)

    def post_query(self, label: str, path: str, params: Dict[str, Any]) -> Optional[requests.Response]:
        return self._call(label, "POST", path, params=params)


def _docx_files(doc_path: Path) -> Dict[str, Any]:
    return {"file": (doc_path.name, doc_path.read_bytes(), DOCX_MIME)}


def _make_variant(doc_path: Path) -> Path:
    """Create a lightly modified copy of the docx so compare-documents has real diffs."""
    try:
        from docx import Document
    except Exception:
        print("  (python-docx not importable; using the same doc twice for compare — timing will be understated)")
        return doc_path

    doc = Document(str(doc_path))
    changed = 0
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if not text:
            continue
        # Modify roughly every 3rd non-empty paragraph: tweak a number or append a sentence.
        if i % 3 == 0:
            new = text
            for a, b in (("three (3)", "five (5)"), ("thirty (30)", "sixty (60)"), ("Delaware", "California")):
                if a in new:
                    new = new.replace(a, b)
            if new == text:
                new = text + " The parties further agree to revisit this provision annually."
            para.text = new
            changed += 1
    out = doc_path.with_name(doc_path.stem + "__variant.docx")
    doc.save(str(out))
    print(f"  generated compare variant ({changed} paragraphs changed): {out.name}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure per-agent response times.")
    ap.add_argument("--doc", required=True, help="Path to a realistic .docx contract to measure against.")
    ap.add_argument("--doc-b", default=None, help="Second .docx for compare-documents (auto-generated if omitted).")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--session", default=f"timing-{int(time.time())}")
    ap.add_argument("--skip-compare", action="store_true", help="Skip the heavy compare-documents agent.")
    args = ap.parse_args()

    doc = Path(args.doc)
    if not doc.exists():
        sys.exit(f"Doc not found: {doc}")
    if not doc.name.lower().endswith(".docx"):
        sys.exit("Only .docx is supported (clause-extraction requires a .docx extension).")

    token = os.environ.get("AGENT_AUTH_TOKEN")
    print(f"Base URL : {args.base_url}")
    print(f"Session  : {args.session}")
    print(f"Document : {doc.name}  ({doc.stat().st_size:,} bytes)")
    print(f"Auth     : {'Bearer token set' if token else 'NO TOKEN (protected agents may 401/403)'}")
    print("-" * 78)

    run = Runner(args.base_url, args.session, token)

    # 1. Ingest (no LLM; populates the session for the session-scoped agents below)
    run.post_files("ingest", "/api/v1/ingest/", _docx_files(doc))

    # 2. Contract Analyzer (1 LLM call, structured) — PROTECTED
    run.post_files("contract-analyzer", "/Accorder/agents/contract-analyzer", _docx_files(doc))

    # 3. Clause Extraction (parsing/retrieval; check whether it uses an LLM)
    run.post_files("clause-extraction", "/api/v1/clause-extraction/extract-clauses/", _docx_files(doc))

    # 4. Query Document / Doc Chat (rewrite + retrieve + answer)
    run.post_query("query-document", "/Accorder/agents/query-document",
                   {"query": "What is the term of this agreement and how can it be terminated?"})

    # 5. General Review — full-document mode (multi-call fan-out across clauses)
    run.post_json("general-review (full doc)", "/Accorder/agents/general-review",
                  {"prompt": "Review the whole contract for unfair liability, termination, and confidentiality terms."})

    # 6a. Describe & Draft — single clause (1 classifier + 1 generation)
    run.post_json("describe-draft (single)", "/api/v1/describe-draft/generate",
                  {"prompt": "Draft a mutual limitation of liability clause.", "use_document_context": True})

    # 6b. Describe & Draft — list of clauses (classifier + a large multi-clause generation)
    run.post_json("describe-draft (list)", "/api/v1/describe-draft/generate",
                  {"prompt": "Draft a full SaaS subscription agreement.", "use_document_context": False})

    # 7. Playbook Review — uses the realistic payload1.json (6 rules) if present, else a 2-rule fallback
    payload1 = Path("payload1.json")
    if payload1.exists():
        try:
            body = json.loads(payload1.read_text(encoding="utf-8"))
        except Exception:
            body = None
    else:
        body = None
    if not body:
        body = {
            "rulesinformation": [
                {"title": "Term", "instruction": "Term should not exceed five years.", "description": "Long terms reduce flexibility.", "rule_type": "Primary"},
                {"title": "Governing Law", "instruction": "Governing law should be specified.", "description": "Essential for disputes.", "rule_type": "Primary"},
            ],
            "textinformation": [
                {"text": "Term: This Agreement remains effective for three years.", "paraindetifier": "P1"},
                {"text": "Governing Law: This Agreement is governed by Delaware law.", "paraindetifier": "P2"},
            ],
        }
    rules_n = len(body.get("rulesinformation", []))
    run.post_json(f"playbook-review ({rules_n} rules)", "/Accorder/agents/playbook-review", body)

    # 8. Compare Documents (heaviest: per-clause fan-out) — PROTECTED
    if not args.skip_compare:
        doc_b = Path(args.doc_b) if args.doc_b else _make_variant(doc)
        run.post_files("compare-documents", "/Accorder/agents/compare-documents",
                       {"file_a": (doc.name, doc.read_bytes(), DOCX_MIME),
                        "file_b": (doc_b.name, doc_b.read_bytes(), DOCX_MIME)})

    # ---- Report ----
    print("\n" + "=" * 78)
    print("PER-AGENT RESPONSE TIME (slowest first)")
    print("=" * 78)
    ordered = sorted(run.results, key=lambda x: (x["wall_s"] is None, -(x["wall_s"] or 0)))
    print(f"  {'agent':<30}{'wall_s':>9}{'status':>9}   detail")
    for x in ordered:
        print(f"  {x['agent']:<30}{(x['wall_s'] or 0):>9.1f}{str(x['status']):>9}   {x['resp_summary']}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = Path("logs") / f"agent_timings_{stamp}.json"
    out_csv = Path("logs") / f"agent_timings_{stamp}.csv"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({"session": args.session, "doc": doc.name, "results": run.results}, indent=2), encoding="utf-8")
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(run.results[0].keys()))
        w.writeheader()
        w.writerows(run.results)
    print(f"\nSaved: {out_json}\n       {out_csv}")
    print(f"Session id for log correlation: {args.session}")


if __name__ == "__main__":
    main()
