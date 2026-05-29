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
    Accept a free-text drafting prompt and return either one drafted clause
    (single_clause mode) or a full clause list for an agreement type
    (list_of_clauses mode). Each response carries a short summary plus the full
    drafted content.

    When `use_document_context=true` (default) and the session has an uploaded
    document, the draft is grounded in the full document content. When false, the
    agent ignores any uploaded document and drafts a reusable template with
    [PLACEHOLDER] tokens.

    Domain errors (validation_failed, llm_failed, etc.) are returned as HTTP 200 with
    status="error" in the response body. HTTP 4xx/5xx are reserved for missing headers
    or truly unexpected failures.
    """
    return await run_describe_draft_agent(
        prompt=request.prompt,
        session_id=session_id,
        use_document_context=request.use_document_context,
    )
