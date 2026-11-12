import json
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _escape_inner_quotes(malformed: str) -> str:
    """Escape unescaped double quotes inside JSON string values.

    When the model serializes the suggestions array as a string, quotes it
    copies from the document (e.g. ("Agreement")) land unescaped inside the
    embedded JSON and break parsing. A quote inside a string is treated as
    the closing delimiter only when the next non-space character is a JSON
    structural character; every other quote is content and gets escaped.
    """

    out: list = []
    in_string = False
    i = 0
    while i < len(malformed):
        char = malformed[i]
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
        elif char == "\\":
            out.append(malformed[i : i + 2])
            i += 2
            continue
        elif char == '"':
            tail = malformed[i + 1 :].lstrip()
            if not tail or tail[0] in ",:]}":
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
        else:
            out.append(char)
        i += 1
    return "".join(out)


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

    agreement_type, when known, narrows clause-type mapping and policy lookup
    to that kind of agreement.
    """

    textinformation: List[TextInformation] = Field(..., description="Paragraphs to review, in document order")
    party_represented: Optional[str] = Field(default=None, description="Which party to the contract the user represents (e.g. Buyer, Seller, Customer)")
    review_objective: Optional[str] = Field(default=None, description="The user's primary objective for this review")
    specific_concerns: Optional[str] = Field(default=None, description="Specific concerns the user wants covered in addition to the full review")
    agreement_type: Optional[str] = Field(default=None, description="Kind of agreement under review (e.g. MSA, NDA), when known")

    @field_validator("party_represented", "review_objective", "specific_concerns", "agreement_type", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        """Treat empty or whitespace-only optional fields the same as absent ones."""

        if not isinstance(value, str):
            return value
        return value.strip() or None


class Suggestion(BaseModel):
    """A single apply/dismiss-able review suggestion."""

    clause: str = Field(description="Title/heading of the clause this suggestion applies to")
    reason: str = Field(description="Plain-language justification for the change, grounded in the clause text")
    risk_level: Literal["Low", "Medium", "High"] = Field(description="Risk level of the clause for the client if left unaddressed: Low, Medium, or High")
    original_text: str = Field(description="Complete paragraph to be replaced, copied verbatim from the document")
    suggested_fix: str = Field(description="Complete revised replacement for original_text")
    para_identifier: str = Field(default="", description="Leave empty; the server fills in the paragraph identifier")

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalize_risk_level(cls, value: Any) -> Any:
        """Fold casing and common synonyms into the three canonical levels."""

        if not isinstance(value, str):
            return value
        canonical = value.strip().capitalize()
        return {"Minor": "Low", "Moderate": "Medium", "Severe": "High", "Major": "High", "Critical": "High"}.get(canonical, canonical)


class CriterionAnswerRaw(BaseModel):
    """A single criterion evaluation from the extraction LLM call."""

    criterion_id: str = Field(description="ID of the criterion evaluated, e.g. 'LOL-01' or 'GEN-01'")
    answer: Literal["Met", "Partly met", "Not met", "Unclear", "Not applicable"] = Field(
        description="The evaluation outcome"
    )
    quote: Optional[str] = Field(default=None, description="Exact verbatim quote from the clause text, or 'Not stated in text'")
    source_para_id: Optional[str] = Field(default=None, description="The paragraph identifier where the issue/quote is located")

    @field_validator("answer", mode="before")
    @classmethod
    def _normalize_answer(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        v = value.strip().replace("_", " ").title()
        mapping = {
            "Met": "Met",
            "Partly Met": "Partly met",
            "Not Met": "Not met",
            "Unclear": "Unclear",
            "Not Applicable": "Not applicable",
            "N/A": "Not applicable",
            "Na": "Not applicable",
        }
        return mapping.get(v, v)


class ExtractionResponse(BaseModel):
    """Response model for the criteria extraction stage."""

    is_type_match: bool = Field(
        default=True,
        description="Whether the clause text actually pertains to the assigned clause type"
    )
    answers: List[CriterionAnswerRaw] = Field(default_factory=list, description="List of answers for each criterion")

    @field_validator("answers", mode="before")
    @classmethod
    def _parse_answers_array(cls, value: Any) -> Any:
        """Parse answers array if Bedrock serialized it as a JSON string."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("```"):
                stripped = stripped.strip("`").removeprefix("json").strip()
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return json.loads(_escape_inner_quotes(stripped))
        return value


class ExplanationResponse(BaseModel):
    """Response model for the explanation and rewrite stage."""

    reason: str = Field(description="Plain-language justification for the change, grounded in the clause text")
    suggested_fix: str = Field(description="Complete revised replacement for original_text")


class FragmentFindingRaw(BaseModel):
    """A single defect finding on an orphaned fragment."""

    defect_id: str = Field(description="FRG-01 or FRG-02")
    reason: str = Field(description="Short plain-language explanation of the drafting defect")
    suggested_fix: str = Field(description="Corrected replacement for the paragraph")
    risk_level: Literal["Low", "Medium", "High"] = Field(default="Low", description="Risk level (always Low)")


class FragmentDefectResponse(BaseModel):
    """Response model for the fragment defect check."""

    findings: List[FragmentFindingRaw] = Field(default_factory=list, description="List of drafting defects found")


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
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            # Document quotes copied into the embedded JSON arrive unescaped
            # and break parsing; repair them before giving up. If the repaired
            # text still fails, the error propagates into the retry path.
            return json.loads(_escape_inner_quotes(stripped))
