r"""Validate ONLY the Describe & Draft agent against a running server.

This is the describe-draft analogue of scripts/validate_contract_analyzer.py.
The Describe & Draft agent has FOUR meaningfully-distinct paths, along two
independent axes:

  mode  : single_clause | list_of_clauses   (chosen by the classifier from the prompt)
  ground: template       | grounded         (use_document_context + a doc on the session)

This script exercises all four combos, runs lightweight automated correctness
checks on each, prints a compact per-combo diagnostic, and writes the FULL
structured JSON of every run to one file for human correctness review.

USAGE
  1. Keep the grounded test doc in the repo (default: Old-1.docx, the Epit
     vendor agreement).
  2. Start the server in another terminal:
       poetry run python -m src.api.main
  3. Run:
       poetry run python scripts/validate_describe_draft.py
     (override the doc with --doc "Some.docx")

  Output JSON is written to DescribeDraftResponse.json (override with --out).
  If the endpoints are auth-protected, set $env:AGENT_AUTH_TOKEN before running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TIMEOUT = 600

# Prompts chosen so the classifier routes each run to the intended mode:
#   - naming ONE clause  -> single_clause
#   - naming a whole agreement / "complete ..." -> list_of_clauses
# The grounded runs ingest the doc first and send use_document_context=true; the
# template runs send a fresh session with use_document_context=false.
COMBOS = [
    {
        "key": "single_template",
        "mode": "single_clause",
        "grounded": False,
        "prompt": "Draft a limitation of liability clause for a SaaS agreement.",
    },
    {
        "key": "single_grounded",
        "mode": "single_clause",
        "grounded": True,
        "prompt": "Draft an indemnification clause for this agreement.",
    },
    {
        "key": "list_template",
        "mode": "list_of_clauses",
        "grounded": False,
        "prompt": "Draft a mutual NDA between two software companies.",
    },
    {
        "key": "list_grounded",
        "mode": "list_of_clauses",
        "grounded": True,
        "prompt": "Draft a complete vendor services agreement between the parties in the attached document.",
    },
]

# Mirrors src/tools/describe_draft.py — party-identity / governing-law token
# substrings that MUST NOT appear in grounded mode (the doc supplied those).
GROUNDED_FORBIDDEN_SUBSTRINGS = [
    "PARTY", "TENANT", "LANDLORD", "CUSTOMER", "CLIENT", "VENDOR", "SUPPLIER",
    "EMPLOYER", "EMPLOYEE", "CONTRACTOR", "DISCLOSING", "RECEIVING", "COMPANY",
    "CORPORATION", "BUYER", "SELLER", "LICENSOR", "LICENSEE", "INDEMNIFIER",
    "INDEMNITEE", "GOVERNING LAW", "GOVERNING STATE", "JURISDICTION", "VENUE",
    "FORUM",
]

# Mirrors _BANNED_PHRASES in the tool.
BANNED_PHRASES = [
    "witnesseth", "party of the first part", "party of the second part",
    "in witness whereof", "now therefore", "know all men by these presents",
]


def _grounded_forbidden(placeholders) -> list:
    out = []
    for tok in placeholders or []:
        name = tok.strip("[]").upper()
        if any(needle in name for needle in GROUNDED_FORBIDDEN_SUBSTRINGS):
            out.append(tok)
    return out


def _banned_hits(text: str) -> list:
    low = (text or "").lower()
    return [p for p in BANNED_PHRASES if p in low]


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK " if ok else "XX "
    print(f"    [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    return ok


def _review_single(body: dict, grounded: bool) -> bool:
    ok = True
    versions = body.get("versions", [])
    ok &= _check("exactly 1 version", len(versions) == 1, f"got {len(versions)}")
    if not versions:
        return False
    v = versions[0]
    clause = v.get("drafted_clause", "") or ""
    summary = v.get("summary", "") or ""
    phs = v.get("placeholders", [])
    print(f"    title  : {v.get('title')!r}")
    print(f"    clause : {len(clause)} chars   summary: {len(summary)} chars")
    print(f"    placeholders: {phs}")
    print(f"    clause[:240]: {clause[:240]!r}")
    ok &= _check("clause >= 300 chars", len(clause.strip()) >= 300, f"{len(clause.strip())}")
    ok &= _check("summary >= 80 chars", len(summary.strip()) >= 80, f"{len(summary.strip())}")
    bh = _banned_hits(clause)
    ok &= _check("no banned phrases", not bh, ", ".join(bh))
    if grounded:
        gf = _grounded_forbidden(phs)
        ok &= _check("no party/gov-law placeholders (grounded)", not gf, ", ".join(gf))
    return ok


def _review_list(body: dict, grounded: bool) -> bool:
    ok = True
    clauses = body.get("clauses", [])
    summ = body.get("agreement_summary", "") or ""
    ok &= _check("agreement_summary >= 60 chars", len(summ.strip()) >= 60, f"{len(summ.strip())}")
    ok &= _check(">= 12 clauses", len(clauses) >= 12, f"got {len(clauses)}")
    titles = [c.get("title", "") for c in clauses]
    ok &= _check("no duplicate titles", len(titles) == len(set(t.lower().strip() for t in titles)))
    bodies = [(c.get("drafted_clause", "") or "") for c in clauses]
    avg = sum(len(b.strip()) for b in bodies) / max(1, len(bodies))
    print(f"    clauses: {len(clauses)}   avg body: {avg:.0f} chars")
    print(f"    titles : {titles}")
    all_banned = sorted({h for b in bodies for h in _banned_hits(b)})
    ok &= _check("no banned phrases in any body", not all_banned, ", ".join(all_banned))
    n_with_ph = sum(1 for c in clauses if c.get("placeholders"))
    print(f"    clauses with placeholders: {n_with_ph}/{len(clauses)}")
    if grounded:
        gf = sorted({tok for c in clauses for tok in _grounded_forbidden(c.get("placeholders"))})
        ok &= _check("no party/gov-law placeholders in any clause (grounded)", not gf, ", ".join(gf))
        # surface first body so a human can eyeball whether real party names were used
        if bodies:
            print(f"    clause[0][:240]: {bodies[0][:240]!r}")
    return ok


def run_combo(base: str, headers_base: dict, doc: Path, combo: dict) -> dict:
    key = combo["key"]
    session = f"validate-dd-{key}-{int(time.time())}"
    headers = dict(headers_base, **{"X-Session-ID": session, "X-Session-Id": session})

    print("=" * 72)
    print(f"COMBO {key}  (expect mode={combo['mode']} grounded={combo['grounded']})")
    print(f"  prompt : {combo['prompt']!r}")
    print(f"  session: {session}")

    # Grounded combos need the document ingested into THIS session first.
    if combo["grounded"]:
        files = {"file": (doc.name, doc.read_bytes(), DOCX_MIME)}
        t0 = time.perf_counter()
        r = requests.post(base + "/api/v1/ingest/", files=files, headers=headers, timeout=TIMEOUT)
        print(f"  [{r.status_code}] ingest ({time.perf_counter() - t0:.1f}s)")
        if r.status_code != 200:
            print(f"  ingest FAILED: {r.text[:300]}")
            return {"combo": key, "error": "ingest_failed", "body": r.text[:1000]}

    payload = {"prompt": combo["prompt"], "use_document_context": combo["grounded"]}
    t0 = time.perf_counter()
    r = requests.post(
        base + "/api/v1/describe-draft/generate",
        json=payload,
        headers=headers,
        timeout=TIMEOUT,
    )
    wall = time.perf_counter() - t0
    print(f"  [{r.status_code}] generate ({wall:.1f}s)")
    if r.status_code != 200:
        print(f"  generate FAILED: {r.text[:500]}")
        return {"combo": key, "error": "http", "status_code": r.status_code, "body": r.text[:1000]}

    body = r.json()
    status = body.get("status")
    mode = body.get("mode")
    grounded_flag = body.get("grounded_in_document")
    print(f"  status={status}  mode={mode}  grounded_in_document={grounded_flag}  latency={wall:.1f}s")

    ok = True
    ok &= _check("status == ok", status == "ok", str(body.get("error_message")) if status != "ok" else "")
    ok &= _check(f"mode == {combo['mode']}", mode == combo["mode"], f"got {mode}")
    ok &= _check(f"grounded_in_document == {combo['grounded']}", grounded_flag == combo["grounded"])

    if status == "ok":
        if mode == "single_clause":
            ok &= _review_single(body, combo["grounded"])
        elif mode == "list_of_clauses":
            ok &= _review_list(body, combo["grounded"])

    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return {"combo": key, "expected": combo, "latency_s": round(wall, 1), "passed": ok, "body": body}


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the Describe & Draft agent (all 4 mode combos).")
    ap.add_argument("--doc", default="Old-1.docx", help="Path to the grounded test .docx.")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--out", default="DescribeDraftResponse.json")
    ap.add_argument("--only", default=None, help="Comma-separated combo keys to run a subset.")
    args = ap.parse_args()

    doc = Path(args.doc)
    if not doc.exists():
        sys.exit(f"Doc not found: {doc} (needed for grounded combos)")

    base = args.base_url.rstrip("/")
    headers_base = {}
    token = os.environ.get("AGENT_AUTH_TOKEN")
    if token:
        headers_base["Authorization"] = f"Bearer {token}"

    combos = COMBOS
    if args.only:
        wanted = {k.strip() for k in args.only.split(",")}
        combos = [c for c in COMBOS if c["key"] in wanted]

    print(f"Base URL : {base}")
    print(f"Document : {doc.name}  ({doc.stat().st_size:,} bytes)")
    print(f"Auth     : {'Bearer token set' if token else 'no token'}")
    print(f"Combos   : {[c['key'] for c in combos]}")

    results = [run_combo(base, headers_base, doc, c) for c in combos]

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 72)
    print("SUMMARY")
    for r in results:
        verdict = "PASS" if r.get("passed") else ("ERROR" if r.get("error") else "FAIL")
        print(f"  {r['combo']:<16} {verdict:<6} {('%.1fs' % r['latency_s']) if 'latency_s' in r else ''}")
    print(f"saved: {args.out}")
    n_pass = sum(1 for r in results if r.get("passed"))
    print(f"{n_pass}/{len(results)} combos passed")


if __name__ == "__main__":
    main()
