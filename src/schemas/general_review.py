from typing import List

from pydantic import BaseModel, Field


class TextInformation(BaseModel):
    """A single document paragraph sent by the frontend."""

    text: str = Field(..., description="Text content of the paragraph")
    paraindetifier: str = Field(..., description="Identifier for the paragraph")


class GeneralReviewRequest(BaseModel):
    """Request body for the general review endpoint.

    The frontend sends the whole document when nothing is selected, or only
    the selected paragraphs when the user has a selection. Unknown extra
    fields (e.g. the retired ``query``) are ignored by pydantic, so older
    clients keep working.
    """

    textinformation: List[TextInformation] = Field(..., description="Paragraphs to review, in document order")


class Suggestion(BaseModel):
    """A single apply/dismiss-able review suggestion."""

    clause: str = Field(description="Title/heading of the clause this suggestion applies to")
    reason: str = Field(description="Plain-language justification for the change, grounded in the clause text")
    original_text: str = Field(description="Complete paragraph to be replaced, copied verbatim from the document")
    suggested_fix: str = Field(description="Complete revised replacement for original_text")
    para_identifier: str = Field(default="", description="Leave empty; the server fills in the paragraph identifier")


class GeneralReviewResponse(BaseModel):
    """Response from the general review endpoint."""

    suggestions: List[Suggestion] = Field(default_factory=list, description="Flat list of apply/dismiss suggestions. Empty when nothing to flag.")
