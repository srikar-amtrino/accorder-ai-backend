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
    use_document_context: bool = True,
) -> DescribeDraftResponse:
    """Run the describe-and-draft agent for a session."""
    return await generate_describe_draft(
        prompt=prompt,
        session_id=session_id,
        use_document_context=use_document_context,
    )
