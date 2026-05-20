"""
Describe & Draft endpoint router.

POST /api/v1/describe-draft/generate
  Body: DescribeDraftRequest (prompt: str)
  Header: X-Session-ID
  Response: DescribeDraftResponse
"""
import logging

from fastapi import APIRouter, Body, Depends

from src.agents.describe_draft import run as run_describe_draft_agent
from src.api.session_utils import get_session_id
from src.schemas.describe_draft import DescribeDraftRequest, DescribeDraftResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Agents"])


@router.post("/generate", response_model=DescribeDraftResponse)
async def generate_draft(
    request: DescribeDraftRequest = Body(...),
    session_id: str = Depends(get_session_id),
) -> DescribeDraftResponse:
    """
    Accept a free-text drafting prompt and return one drafted clause, a full clause
    list for an agreement type, or a clarification question. Session memory preserves
    agreement context across calls.

    When `regenerate=true`, the agent improves on the previous single-clause draft
    stored for this session instead of producing a fresh draft. When
    `target_clause_title` is also supplied, the agent regenerates that specific
    clause (by title) from the last list_of_clauses or single_clause response.

    Domain errors (validation_failed, llm_failed, etc.) are returned as HTTP 200 with
    status="error" in the response body. HTTP 4xx/5xx are reserved for missing headers
    or truly unexpected failures.
    """
    return await run_describe_draft_agent(
        prompt=request.prompt,
        session_id=session_id,
        regenerate=request.regenerate,
        target_clause_title=request.target_clause_title,
        ignore_document=request.ignore_document,
    )
