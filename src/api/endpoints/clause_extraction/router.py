import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile

from src.schemas.clause_extraction import ClauseExtractionResult
from src.services.retrieval.clause_extraction import extract_clauses, result_to_dict

router = APIRouter()


@router.post("/extract-clauses/", response_model=ClauseExtractionResult)
async def extract_clauses_from_document(file: UploadFile) -> ClauseExtractionResult:
    """
    Extract clauses from an uploaded legal document (.docx).

    Accepts a .docx file and returns the extracted clauses with their numbers, titles, and content.
    """
    if not file.filename.endswith(".docx"):
        raise ValueError("Only .docx files are supported")

    # Save uploaded file to a temporary location
    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_file:
        contents = await file.read()
        temp_file.write(contents)
        temp_file_path = temp_file.name

    try:
        # Extract clauses using the service
        result = extract_clauses(temp_file_path)

        # Convert to dict and then to Pydantic model
        result_dict = result_to_dict(result)
        return ClauseExtractionResult(**result_dict)
    finally:
        # Clean up temporary file
        Path(temp_file_path).unlink(missing_ok=True)
