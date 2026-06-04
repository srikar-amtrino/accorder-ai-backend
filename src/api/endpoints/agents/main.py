import io
from typing import Any

from docx import Document
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

# from src.api.session_utils import get_session_id
from src.core.auth import generate_access_token, get_session_id, verify_token
from src.schemas.contract_analyzer import ContractAnalyzerResponse
from src.schemas.doc_chat import DocChatResponse, DocuChatRequest
from src.schemas.general_review import GeneralReviewRequest, GeneralReviewResponse
from src.schemas.playbook_review import (
    PlayBookReviewFinalResponse,
    RuleCheckRequest,
)
from src.tools.comparision import run as compare_documents_service
from src.tools.doc_chat import document_chat_service, document_chat_stream_service
from src.tools.general_review import clause_review, full_document_review
from src.tools.key_information import (
    get_key_information_generate,
    get_key_information_stream,
)
from src.tools.playbook_review import (
    playbook_review_service,
    playbook_review_stream_service,
)

router = APIRouter(tags=["agents"])


# @router.get("/generate-access-token")
# def get_access_token() -> Any:
#     """Generate an access token for the application."""

#     access_token = generate_access_token()

#     return access_token


@router.post("/compare-documents")
async def compare_documents_endpoint(file_a: UploadFile, file_b: UploadFile, session_id: str = Depends(get_session_id)) -> Any:
    """Compare two documents and return their differences."""

    document_a = Document(io.BytesIO(await file_a.read()))
    document_b = Document(io.BytesIO(await file_b.read()))

    comparison_result = await compare_documents_service(session_id=session_id, document_a=document_a, document_b=document_b)
    return comparison_result


@router.post("/general-review", response_model=GeneralReviewResponse)
async def review_contract(request: GeneralReviewRequest, session_id: str = Depends(get_session_id)) -> GeneralReviewResponse:
    """Run the general review agent against an ingested document."""

    try:
        if request.selected_clause and request.selected_clause.strip():
            return await clause_review(
                session_id=session_id,
                clause_text=request.selected_clause,
                user_prompt=request.prompt,
                clause_title=(request.clause_title or "Selected Clause").strip() or "Selected Clause",
            )

        return await full_document_review(
            session_id=session_id,
            user_prompt=request.prompt,
        )

    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"General review error: {str(err)}")


@router.post("/contract-analyzer", response_model=ContractAnalyzerResponse, status_code=status.HTTP_200_OK)
async def contract_analyzer_endpoint(file: UploadFile, session_id: str = Depends(get_session_id)) -> ContractAnalyzerResponse:
    """Analyze a contract document and extract key information."""

    document = Document(io.BytesIO(await file.read()))
    document_data = "\n".join([para.text for para in document.paragraphs if para.text.strip() != ""])

    analysis_result: ContractAnalyzerResponse = await get_key_information_generate(content=document_data, session_id=session_id)
    return analysis_result


@router.post("/contract-analyzer/stream", response_class=StreamingResponse, status_code=status.HTTP_200_OK)
async def contract_analyzer_stream_endpoint(file: UploadFile, session_id: str = Depends(get_session_id)) -> StreamingResponse:
    """Analyze a contract document and stream the extracted key information as it arrives."""

    document = Document(io.BytesIO(await file.read()))
    document_data = "\n".join([para.text for para in document.paragraphs if para.text.strip() != ""])

    # Ensure CORS headers are present on the streaming response
    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache", "Connection": "keep-alive"}

    return StreamingResponse(get_key_information_stream(content=document_data, session_id=session_id), media_type="text/event-stream", headers=headers)


@router.post("/playbook-review", response_model=PlayBookReviewFinalResponse)
async def playbook_review_endpoint(request: RuleCheckRequest, session_id: str = Depends(get_session_id)) -> PlayBookReviewFinalResponse:
    """Run playbook validation checks."""

    review_result = await playbook_review_service(session_id=session_id, request=request)
    return review_result


@router.post("/playbook-review/stream", response_class=StreamingResponse, status_code=status.HTTP_200_OK)
async def playbook_review_stream_endpoint(request: RuleCheckRequest, session_id: str = Depends(get_session_id)) -> StreamingResponse:
    """Run playbook validation checks."""

    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return StreamingResponse(playbook_review_stream_service(session_id=session_id, request=request), media_type="text/event-stream", headers=headers)


@router.post("/query-document", response_model=DocChatResponse)
async def query_document_endpoint(request: DocuChatRequest, session_id: str = Depends(get_session_id)) -> DocChatResponse:
    """Query the document chunks based on the given query and session ID."""

    llm_result = await document_chat_service(session_id=session_id, payload=request)
    return llm_result


@router.post("/query-document/stream", response_class=StreamingResponse, status_code=status.HTTP_200_OK)
async def query_document_stream_endpoint(request: DocuChatRequest, session_id: str = Depends(get_session_id)) -> StreamingResponse:
    """Run playbook validation checks."""

    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return StreamingResponse(document_chat_stream_service(session_id=session_id, payload=request), media_type="text/event-stream", headers=headers)
