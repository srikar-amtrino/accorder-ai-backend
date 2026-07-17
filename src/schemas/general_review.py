import json
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


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

    The three optional reviewer-context fields come from the pre-review
    questionnaire (v3). Skip sends none of them, which keeps the request —
    and the resulting review — identical to the questionnaire-less flow.
    """

    textinformation: List[TextInformation] = Field(..., description="Paragraphs to review, in document order")
    party_represented: Optional[str] = Field(default=None, description="Which party to the contract the user represents (e.g. Buyer, Seller, Customer)")
    review_objective: Optional[str] = Field(default=None, description="The user's primary objective for this review")
    specific_concerns: Optional[str] = Field(default=None, description="Specific concerns the user wants covered in addition to the full review")

    @field_validator("party_represented", "review_objective", "specific_concerns", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        """Treat empty or whitespace-only questionnaire fields the same as absent ones."""

        if not isinstance(value, str):
            return value
        return value.strip() or None


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

    @field_validator("suggestions", mode="before")
    @classmethod
    def _parse_string_array(cls, value: Any) -> Any:
        """Tolerate the array arriving as a JSON-encoded string.

        Bedrock tool-use intermittently serializes long arrays as a JSON string
        instead of a real array; parse it back so validation succeeds.
        """

        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`").removeprefix("json").strip()
        return json.loads(stripped)
