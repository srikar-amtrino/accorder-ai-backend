from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from src.api.session_utils import get_session_id
from src.core.container import get_ingestion_service, get_session_manager
from src.schemas.document_map import DocumentMap, DocumentMapRequest
from src.schemas.registry import ParseResult
from src.tools.document_map import build_document_map_from_paragraphs

router = APIRouter()


def _paragraphs(request: DocumentMapRequest) -> list[str]:
    """Non-empty paragraph texts from the JSON request, in document order."""

    return [para.text for para in request.textinformation if para.text and para.text.strip()]


@router.post("/ingest/")
async def ingest_data(file: UploadFile, session_id: str = Depends(get_session_id)) -> ParseResult:
    """Ingest the provided file data for a specific session."""

    contents = await file.read()
    file_like = BytesIO(contents)

    session_manager = get_session_manager()

    # Get or create session
    session_data = session_manager.get_session(session_id)

    if not session_data:
        session_data = session_manager.create_session(session_id)

    ingestion_service = get_ingestion_service()

    # Parse data with session context
    return await ingestion_service._parse_data(
        data=file_like,
        session_data=session_data,
    )


@router.post("/document-map/", response_model=DocumentMap)
async def build_document_map_endpoint(request: DocumentMapRequest, session_id: str = Depends(get_session_id)) -> DocumentMap:
    """Build the grounded document-understanding map from the document JSON.

    This is the comprehension pre-pass — a document-processing step, not a
    review agent. It reads the whole document once and returns the parties,
    defined terms, clauses (each grounded to verbatim source, combined clauses
    split apart), and flagged ambiguities that the review agents share as one
    consistent understanding of the document. Takes the same paragraph JSON the
    review agents already receive.
    """

    try:
        return await build_document_map_from_paragraphs(_paragraphs(request), session_id)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Document map error: {str(err)}")


# @router.post("/ingest-json/")
# async def ingest_json(json_data: List[TextInfo], session_id: str = Depends(get_session_id)) -> ParseResult:
#     """Ingest the provided JSON data for a specific session."""

#     # Get service container and session manager
#     service_container = get_service_container()
#     session_manager = service_container.session_manager

#     # Get or create session
#     session_data = session_manager.get_or_create_session(session_id)

#     # Parse data with session context
#     return await service_container.ingestion_service._parse_data(
#         data=json_data,
#         session_data=session_data,
#     )
