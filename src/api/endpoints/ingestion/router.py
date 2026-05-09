from io import BytesIO
from typing import List

from fastapi import APIRouter, Depends, UploadFile

from src.api.session_utils import get_session_id
from src.dependencies import get_service_container
from src.schemas.playbook_review import TextInfo
from src.schemas.registry import ParseResult

router = APIRouter()


@router.post("/ingest/")
async def ingest_data(file: UploadFile, session_id: str = Depends(get_session_id)) -> ParseResult:
    """Ingest the provided file data for a specific session."""

    contents = await file.read()
    file_like = BytesIO(contents)

    # Get service container and session manager
    service_container = get_service_container()
    session_manager = service_container.session_manager

    # Get or create session
    session_data = session_manager.get_or_create_session(session_id)

    # Parse data with session context
    return await service_container.ingestion_service._parse_data(
        data=file_like,
        session_data=session_data,
    )


@router.post("/ingest-json/")
async def ingest_json(json_data: List[TextInfo], session_id: str = Depends(get_session_id)) -> ParseResult:
    """Ingest the provided JSON data for a specific session."""

    # Get service container and session manager
    service_container = get_service_container()
    session_manager = service_container.session_manager

    # Get or create session
    session_data = session_manager.get_or_create_session(session_id)

    # Parse data with session context
    return await service_container.ingestion_service._parse_data(
        data=json_data,
        session_data=session_data,
    )
