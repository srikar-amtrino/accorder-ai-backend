from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from src.api.session_utils import get_session_id
from src.tools.key_information import get_key_information
from src.tools.summarizer import get_summary

router = APIRouter()


@router.get("/summarizer")
async def get_information(
    session_id: str = Depends(get_session_id),
) -> Dict[str, Any]:
    """Get summary for a specific session."""

    # try:
    #     result = await get_summary(session_id=session_id, response="BaseModel")
    # except ValueError as err:
    #     return {"error": str(err), "session_id": session_id}

    # return {
    #     "summary": result,
    #     "session_id": session_id,
    # }

    try:
        result = await get_summary(session_id=session_id, response="markdown")
    except ValueError as err:
        return {"error": str(err), "session_id": session_id}

    return HTMLResponse(content=result, status_code=200)


@router.get("/key-information")
async def get_key_details(
    session_id: str = Depends(get_session_id),
) -> Dict[str, Any]:
    """Get Key Information for a specific session."""

    # try:
    #     result = await get_key_information(session_id=session_id, response_format="BaseModel")
    # except ValueError as err:
    #     return {"error": str(err), "session_id": session_id}

    # return {
    #     "Key Information": result,
    #     "session_id": session_id,
    # }

    try:
        result = await get_key_information(session_id=session_id, response_format="markdown")
    except ValueError as err:
        return {"error": str(err), "session_id": session_id}

    print(result)

    return HTMLResponse(content=result, status_code=200)
