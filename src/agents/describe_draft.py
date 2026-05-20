"""
Describe & Draft Agent — thin dispatcher.

Delegates all logic to src/tools/describe_draft.py. No business logic here.
"""
from typing import Optional

from src.schemas.describe_draft import DescribeDraftResponse
from src.tools.describe_draft import generate_describe_draft


async def run(
    prompt: Optional[str],
    session_id: str,
    regenerate: bool = False,
    target_clause_title: Optional[str] = None,
    ignore_document: bool = False,
) -> DescribeDraftResponse:
    """Run the describe-and-draft agent for a session."""
    return await generate_describe_draft(
        prompt=prompt,
        session_id=session_id,
        regenerate=regenerate,
        target_clause_title=target_clause_title,
        ignore_document=ignore_document,
    )
