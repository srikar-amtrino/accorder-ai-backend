"""End-to-end smoke test: hit every LLM-using endpoint once and report pass/fail.

Run AFTER:
  1. Generating test docs:   poetry run python scripts/create_test_docs.py
  2. Starting the FastAPI server in another terminal:
                             poetry run python -m src.api.main
"""

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

BASE_URL = "http://localhost:8000"
SESSION_ID = f"smoke-{int(time.time())}"
DOC1 = "test_contract.docx"
DOC2 = "test_contract_v2.docx"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HEADERS = {"X-Session-ID": SESSION_ID, "X-Session-Id": SESSION_ID}
TIMEOUT = 600

_results: List[Tuple[str, str, str]] = []


def _step(name: str, ok: bool, info: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    _results.append((name, status, info))
    suffix = f" — {info}" if info else ""
    print(f"[{status}] {name}{suffix}")


def _post_files(path: str, files: Dict[str, Any], **kwargs: Any) -> requests.Response:
    return requests.post(BASE_URL + path, files=files, headers=HEADERS, timeout=TIMEOUT, **kwargs)


def _post_json(path: str, body: Dict[str, Any], **kwargs: Any) -> requests.Response:
    h = {**HEADERS, "Content-Type": "application/json"}
    return requests.post(BASE_URL + path, json=body, headers=h, timeout=TIMEOUT, **kwargs)


def _post_query(path: str, params: Dict[str, Any]) -> requests.Response:
    return requests.post(BASE_URL + path, params=params, headers=HEADERS, timeout=TIMEOUT)


def _get(path: str) -> requests.Response:
    return requests.get(BASE_URL + path, headers=HEADERS, timeout=TIMEOUT)


def _print_failure(label: str, r: requests.Response) -> None:
    print(f"  -> {label} responded {r.status_code}")
    body = r.text[:1000]
    if body:
        print(f"  -> body (first 1000 chars): {body}")


def _ingest() -> bool:
    print("\n=== 1. /api/v1/ingest/ (creates session, no LLM call) ===")
    try:
        with open(DOC1, "rb") as f:
            r = _post_files("/api/v1/ingest/", files={"file": (DOC1, f, DOCX_MIME)})
        ok = r.status_code == 200 and r.json().get("success") is True
        _step("ingest", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("ingest", r)
        return ok
    except Exception as exc:
        _step("ingest", False, str(exc))
        return False


def _contract_analyzer() -> None:
    print("\n=== 4. /Accorder/agents/contract-analyzer (JSON LLM call, structured output) ===")
    try:
        with open(DOC1, "rb") as f:
            r = _post_files("/Accorder/agents/contract-analyzer", files={"file": (DOC1, f, DOCX_MIME)})
        ok = r.status_code == 200 and "summary" in r.json()
        _step("contract-analyzer", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("contract-analyzer", r)
    except Exception as exc:
        _step("contract-analyzer", False, str(exc))


def _query_document() -> None:
    print("\n=== 5. /Accorder/agents/query-document (RAG: rewrite + retrieve + JSON LLM) ===")
    try:
        r = _post_query("/Accorder/agents/query-document", {"query": "What is the term of this agreement?"})
        ok = r.status_code == 200 and "answers" in r.json()
        _step("query-document", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("query-document", r)
    except Exception as exc:
        _step("query-document", False, str(exc))


def _general_review() -> None:
    print("\n=== 6. /Accorder/agents/general-review (multi-LLM fan-out, structured) ===")
    try:
        body = {"prompt": "Check the term clause and termination clause for fairness."}
        r = _post_json("/Accorder/agents/general-review", body=body)
        ok = r.status_code == 200 and "suggestions" in r.json()
        _step("general-review", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("general-review", r)
    except Exception as exc:
        _step("general-review", False, str(exc))


def _playbook_review() -> None:
    print("\n=== 7. /Accorder/agents/playbook-review (parallel rule eval + missing-clause LLM) ===")
    try:
        body = {
            "rulesinformation": [
                {
                    "title": "Term",
                    "instruction": "Contract term should not exceed five years.",
                    "description": "Long contract terms reduce flexibility for both parties.",
                    "tags": ["term"],
                    "rule_type": "Primary",
                },
                {
                    "title": "Governing Law",
                    "instruction": "Governing law should be clearly specified.",
                    "description": "Governing law is essential for dispute resolution.",
                    "tags": ["governing law"],
                    "rule_type": "Primary",
                },
            ],
            "textinformation": [
                {"text": "1. Term: This Agreement remains effective for three years.", "paraindetifier": "P1"},
                {"text": "2. Governing Law: This Agreement is governed by Delaware law.", "paraindetifier": "P2"},
                {"text": "3. Termination: Either party may terminate with 30 days notice.", "paraindetifier": "P3"},
            ],
        }
        r = _post_json("/Accorder/agents/playbook-review", body=body)
        ok = r.status_code == 200 and "rules_review" in r.json()
        _step("playbook-review", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("playbook-review", r)
    except Exception as exc:
        _step("playbook-review", False, str(exc))


def _compare_documents() -> None:
    print("\n=== 8. /Accorder/agents/compare-documents (heaviest: multi-LLM, may take 1-3 min) ===")
    try:
        with open(DOC1, "rb") as f1, open(DOC2, "rb") as f2:
            r = _post_files(
                "/Accorder/agents/compare-documents",
                files={
                    "file_a": (DOC1, f1, DOCX_MIME),
                    "file_b": (DOC2, f2, DOCX_MIME),
                },
            )
        ok = r.status_code == 200 and "summary" in r.json()
        _step("compare-documents", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("compare-documents", r)
    except Exception as exc:
        _step("compare-documents", False, str(exc))


def _draft() -> None:
    print("\n=== 9. /Accorder/agents/draft (JSON LLM call, structured) ===")
    try:
        body = {"user_query": "Draft a one-year non-compete clause for an employment contract."}
        r = _post_json("/Accorder/agents/draft", body=body)
        ok = r.status_code == 200 and "data" in r.json()
        _step("draft", ok, f"status={r.status_code}")
        if not ok:
            _print_failure("draft", r)
    except Exception as exc:
        _step("draft", False, str(exc))


def main() -> None:
    print(f"Session ID: {SESSION_ID}")
    print(f"Base URL:   {BASE_URL}")

    if not Path(DOC1).exists() or not Path(DOC2).exists():
        print(f"\nMissing test docs. Run:  poetry run python scripts/create_test_docs.py")
        sys.exit(1)

    if not _ingest():
        print("\nIngest failed; aborting downstream tests (they need the session).")
        sys.exit(1)

    _contract_analyzer()
    _query_document()
    _general_review()
    _playbook_review()
    _compare_documents()
    _draft()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, s, _ in _results if s == "PASS")
    failed = sum(1 for _, s, _ in _results if s == "FAIL")
    for name, status, info in _results:
        print(f"  {status:4}  {name}  ({info})")
    print()
    print(f"Total: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
