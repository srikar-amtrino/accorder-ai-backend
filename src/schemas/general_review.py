import json
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from src.services.general_review.risk_matrix import MATERIAL_DEVIATION, SEVERITY_CATEGORIES


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


def parse_string_array(value: Any) -> Any:
    """Tolerate a JSON array arriving as a JSON-encoded string.

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
    """A single apply/dismiss-able review suggestion — the shape the Word add-in receives."""

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


class GeneralReviewResponse(BaseModel):
    """Response from the general review endpoint."""

    suggestions: List[Suggestion] = Field(default_factory=list, description="Flat list of apply/dismiss suggestions. Empty when nothing to flag.")

    @field_validator("suggestions", mode="before")
    @classmethod
    def _parse_string_array(cls, value: Any) -> Any:
        return parse_string_array(value)


# --- Model-facing schema -----------------------------------------------------
#
# The model never chooses a risk level. It reports the facts below for each
# paragraph it flags; the server computes risk_level from them and the clause
# type's inherent risk tier (see src/services/general_review/risk_matrix.py)
# and converts each finding into a Suggestion. These types are internal and
# never leave the server.

SeverityCategory = Literal["uncapped_or_severe_exposure", "material_commercial_deviation", "minor_drafting_hygiene"]

CLAUSE_WIDE_OMISSION = "clause_wide_omission"
PARAGRAPH_SPECIFIC_DEFECT = "paragraph_specific_defect"
FindingScope = Literal["clause_wide_omission", "paragraph_specific_defect"]


class ReviewFinding(BaseModel):
    """One flagged paragraph as the model reports it: the fix plus structured facts about the clause."""

    clause: str = Field(description="Short title of the clause the paragraph belongs to")
    reason: str = Field(description="Concise plain-language explanation of the issue and its practical impact for the client, written as counsel's own assessment")
    original_text: str = Field(description="The complete paragraph copied verbatim from the provided text, character for character")
    suggested_fix: str = Field(description="The complete replacement for original_text, changed only where needed")
    triggered_unacceptable_flags: List[str] = Field(
        default_factory=list,
        description=(
            "Every unacceptable trigger listed in this clause's legal team policy that the paragraph's wording actually triggers, each copied exactly as written in the policy. "
            "Empty when none applies or the clause has no policy."
        ),
    )
    falls_short_of_acceptable_standard: bool = Field(
        description=(
            "True when the clause omits a protection, carve-out, right, or term that the clause's acceptable standard "
            "(or, without a policy, market standard for the client's position) requires for the client"
        ),
    )
    is_materially_one_sided_against_client: bool = Field(
        description="True when an obligation, cap, indemnity, remedy, or termination right disproportionately burdens the client where market practice expects mutuality",
    )
    severity_category: SeverityCategory = Field(
        description=(
            "uncapped_or_severe_exposure: unlimited liability, loss of core IP, missing IP indemnity, termination without refund, missing data/confidentiality protection. "
            "material_commercial_deviation: sub-standard cap, one-sided burden, unfavourable payment terms, missing cure period, restrictive covenants. "
            "minor_drafting_hygiene: ambiguity, mechanics, or clarification with no material shift in exposure."
        ),
    )
    finding_scope: FindingScope = Field(
        default="paragraph_specific_defect",
        description=(
            "clause_wide_omission: the finding adds a protection, carve-out, right, or term the clause as a whole lacks; always anchored on the clause's [PRIMARY_OPERATIVE_ANCHOR] paragraph. "
            "paragraph_specific_defect: wording already in the paragraph is itself defective."
        ),
    )

    @field_validator("finding_scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: Any) -> Any:
        """Fold casing/spacing; an unknown or missing label counts as a defect in the paragraph it was raised on."""

        if not isinstance(value, str):
            return PARAGRAPH_SPECIFIC_DEFECT if value is None else value
        folded = "_".join(value.strip().lower().replace("-", " ").split())
        return CLAUSE_WIDE_OMISSION if folded == CLAUSE_WIDE_OMISSION else PARAGRAPH_SPECIFIC_DEFECT

    @field_validator("triggered_unacceptable_flags", mode="before")
    @classmethod
    def _flags_as_list(cls, value: Any) -> Any:
        """Accept a JSON-encoded list or a single string in place of a list."""

        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return parse_string_array(stripped)
            return [stripped] if stripped else []
        if value is None:
            return []
        return value

    @field_validator("severity_category", mode="before")
    @classmethod
    def _normalize_severity(cls, value: Any) -> Any:
        """Fold casing/spacing; an unknown label counts as a material deviation rather than failing the batch."""

        if not isinstance(value, str):
            return value
        folded = "_".join(value.strip().lower().replace("-", " ").split())
        return folded if folded in SEVERITY_CATEGORIES else MATERIAL_DEVIATION


class GeneralReviewFindings(BaseModel):
    """The model's structured output for one review batch."""

    findings: List[ReviewFinding] = Field(default_factory=list, description="One finding per paragraph that needs a change, in document order. Empty when nothing needs to change.")

    @field_validator("findings", mode="before")
    @classmethod
    def _parse_string_array(cls, value: Any) -> Any:
        return parse_string_array(value)
