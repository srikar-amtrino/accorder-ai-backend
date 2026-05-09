# src/api/endpoints/orchestrator/router.py
import time

from fastapi import APIRouter, Depends, HTTPException

from src.api.session_utils import get_session_id
from src.dependencies import get_service_container
from src.orchestrator.orchestrator_agent import get_azure_agent
from src.schemas.tool_schema import ToolResponse

router = APIRouter()


@router.post("/query/")
async def get_query_response(query: str, session_id: str = Depends(get_session_id)) -> ToolResponse:
    """Return the response query response."""

    service_container = get_service_container()
    session_manager = service_container.session_manager

    session_data = session_manager.get_session(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found. Please ingest documents first.")

    agent = await get_azure_agent()
    start_time = time.time()

    try:
        full_query = f"What is the {query} for the document with session_id: {session_id}"
        print(f"Sending query: {full_query}")

        response = await agent.run(full_query, thread=None)
        print(response.text)

        return ToolResponse(
            tool_id=None,
            status=True,
            response={"context": response.text},
            metadata={"session_id": session_id},
            response_time=str(time.time() - start_time),
        )

    except Exception as e:

        return ToolResponse(
            tool_id=None,
            status=False,
            response={"error": "Failed to process query", "details": str(e)},
            metadata={"session_id": session_id},
            response_time=str(time.time() - start_time),
        )
