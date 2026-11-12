"""Development-only review page for the legal team.

Every successful JSON response from an agent endpoint is saved under
``review_outputs/`` together with the request that produced it. ``GET /review``
serves a single read-only page that lists those saved runs and shows each one
in plain language, so lawyers never have to read raw JSON.

Nothing here is mounted unless the app runs with ``ENV=development``.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from src.config.logging import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "review_portal" / "index.html"
RUNS_DIR = REPO_ROOT / "review_outputs"
REMOVED_DIR = RUNS_DIR / "removed"
AGENTS_PREFIX = "/Accorder/agents/"

# Endpoint path after the agents prefix -> the agent key the page renders.
# Streaming endpoints are deliberately absent: their SSE bodies are not one JSON document.
_AGENT_BY_PATH = {
    "general-review": "general_review",
    "playbook-review": "playbook_review",
    "contract-analyzer": "contract_analyzer",
    "v1/contract-analyzer": "contract_analyzer",
    "v1/contract-analyzer/2call": "contract_analyzer",
    "compare-documents": "compare_documents",
    "describe-draft": "describe_draft",
    "query-document": "doc_chat",
}
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")

router = APIRouter(tags=["review-portal"], include_in_schema=False)


@router.get("/review")
async def review_page() -> FileResponse:
    """Serve the review page."""

    return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})


def _count(n: int, one: str) -> str:
    return f"{n} {one if n == 1 else one + 's'}"


def _detail(agent: Optional[str], record: Dict[str, Any]) -> str:
    """One line telling reviewers what a run contains, shown under its name in the sidebar."""

    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    if agent == "general_review":
        return _count(len(response.get("suggestions") or []), "suggestion")
    if agent == "playbook_review":
        return f"{_count(len(response.get('rules_review') or []), 'rule')} checked · {_count(len(response.get('missing_clauses') or []), 'missing clause')}"
    if agent == "contract_analyzer":
        return _count(len(response.get("risk_and_compliance_insights") or []), "risk")
    if agent == "compare_documents":
        if response.get("success") is False:
            return "Comparison failed"
        return _count((response.get("summary") or {}).get("total_changes") or 0, "change")
    query = " ".join(str(request.get("query") or "").split())
    if not query:
        return ""
    return f"“{query[:80]}…”" if len(query) > 80 else f"“{query}”"


@router.get("/review/runs")
async def list_runs() -> List[Dict[str, Any]]:
    """List the runs on the page, newest first.

    Each run is numbered per agent in the order it was saved, counting removed runs too,
    so "Result 3" keeps pointing at the same run after older ones are removed.
    """

    entries = []
    for folder, active in ((RUNS_DIR, True), (REMOVED_DIR, False)):
        if folder.is_dir():
            entries += [(path, active) for path in folder.glob("*.json")]

    numbers: Dict[str, int] = {}
    runs: List[Dict[str, Any]] = []
    for path, active in sorted(entries, key=lambda entry: entry[0].name):  # names start with a UTC timestamp
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agent = record.get("agent")
        numbers[agent] = numbers.get(agent, 0) + 1
        if active:
            runs.append(
                {
                    "id": path.stem,
                    "agent": agent,
                    "endpoint": record.get("endpoint"),
                    "captured_at": record.get("captured_at"),
                    "number": numbers[agent],
                    "detail": _detail(agent, record),
                }
            )
    runs.reverse()
    return runs


def _run_path(folder: Path, run_id: str) -> Path:
    path = folder / f"{run_id}.json"
    if not _RUN_ID.match(run_id) or not path.is_file():
        raise HTTPException(status_code=404, detail="Run not found")
    return path


@router.get("/review/runs/{run_id}")
async def get_run(run_id: str) -> Response:
    """Return one saved run: the agent, the request it was given and its response."""

    return Response(_run_path(RUNS_DIR, run_id).read_bytes(), media_type="application/json", headers={"Cache-Control": "no-cache"})


@router.delete("/review/runs/{run_id}")
async def remove_run(run_id: str) -> Dict[str, str]:
    """Take a run off the page. The file moves to review_outputs/removed/, so it can be restored."""

    path = _run_path(RUNS_DIR, run_id)
    REMOVED_DIR.mkdir(parents=True, exist_ok=True)
    path.replace(REMOVED_DIR / path.name)
    return {"removed": run_id}


@router.post("/review/runs/{run_id}/restore")
async def restore_run(run_id: str) -> Dict[str, str]:
    """Put a removed run back on the page."""

    path = _run_path(REMOVED_DIR, run_id)
    path.replace(RUNS_DIR / path.name)
    return {"restored": run_id}


def _agent_for(method: str, path: str) -> Optional[str]:
    if method != "POST" or not path.startswith(AGENTS_PREFIX):
        return None
    return _AGENT_BY_PATH.get(path[len(AGENTS_PREFIX) :].rstrip("/"))


def save_run(agent: str, endpoint: str, request_body: Any, response_body: Any) -> Path:
    """Write one run to review_outputs/ and return its path."""

    RUNS_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    path = RUNS_DIR / f"{now:%Y%m%dT%H%M%S}_{agent}_{uuid4().hex[:6]}.json"
    record = {"agent": agent, "endpoint": endpoint, "captured_at": now.isoformat(timespec="seconds"), "request": request_body, "response": response_body}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def install_review_portal(app: FastAPI) -> None:
    """Mount the review page and start saving agent responses. Call only in development."""

    app.include_router(router)

    @app.middleware("http")
    async def capture_agent_runs(request: Request, call_next: Callable) -> Any:
        agent = _agent_for(request.method, request.url.path)
        if agent is None:
            return await call_next(request)

        # JSON bodies hold the contract paragraphs the page needs to show source text.
        # Multipart uploads (compare-documents) are not kept; their responses carry both texts.
        request_body = None
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                request_body = json.loads(await request.body())
            except ValueError:
                request_body = None

        response = await call_next(request)
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("application/json"):
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            save_run(agent, request.url.path, request_body, json.loads(body))
        except Exception:
            logger.exception("Could not save agent run for the review page.", agent=agent)
        return Response(content=body, status_code=response.status_code, headers=dict(response.headers), media_type=response.media_type)
