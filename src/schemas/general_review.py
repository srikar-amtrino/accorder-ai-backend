import json
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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


# --- Model-facing schemas ----------------------------------------------------
#
# The review makes two kinds of model call, and neither chooses a risk level or
# decides which paragraph is flagged:
#
#   1. Assessment: for each clause the model answers that clause type's fixed
#      checklist (BatchAssessment). Several independent answers are taken and
#      the majority decides; the server turns the failed items into the points
#      to resolve and computes each risk level in code.
#   2. Drafting: for each paragraph with points to resolve, the model redrafts
#      that paragraph and writes the accompanying note (DraftedRevision).
#
# These types are internal and never leave the server.

MET = "met"
PARTLY_MET = "partly_met"
NOT_MET = "not_met"
NOT_APPLICABLE = "not_applicable"
Verdict = Literal["met", "partly_met", "not_met", "not_applicable"]

_VERDICT_SYNONYMS = {"n/a": NOT_APPLICABLE, "na": NOT_APPLICABLE, "partially_met": PARTLY_MET, "partial": PARTLY_MET, "unclear": PARTLY_MET, "yes": MET, "no": NOT_MET}

# Drafting defects the model may report: only kinds that can be checked against the words it quotes.
DEFECT_TYPES = ("blank_or_placeholder", "broken_cross_reference", "typographical_error")


def _fold_label(value: str) -> str:
    return "_".join(value.strip().lower().replace("-", " ").split())


class CriterionAnswer(BaseModel):
    """The model's answer to one checklist item for one clause."""

    criterion_id: str = Field(description="The checklist item's id exactly as given, for example FEE-04")
    evidence: str = Field(default="", description='The words of the agreement the answer rests on, quoted exactly and kept short; "No provision" when the answer rests on an absence')
    verdict: Verdict = Field(description="met, partly_met, not_met or not_applicable, by the item's own tests")
    paragraphs: List[str] = Field(
        default_factory=list,
        description='For partly_met or not_met caused by particular wording: the id of every paragraph whose wording is the cause, for example ["P12"]. Empty otherwise',
    )

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, value: Any) -> Any:
        """Fold casing/spacing ("Partly met" -> partly_met); an unknown label counts as not applicable rather than failing the batch."""

        if not isinstance(value, str):
            return value
        folded = _fold_label(value)
        folded = _VERDICT_SYNONYMS.get(folded, folded)
        return folded if folded in (MET, PARTLY_MET, NOT_MET, NOT_APPLICABLE) else NOT_APPLICABLE

    @field_validator("criterion_id", "evidence", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else str(value).strip()

    @field_validator("paragraphs", mode="before")
    @classmethod
    def _ids_as_list(cls, value: Any) -> Any:
        """Accept a single id, a comma-separated string, or a JSON-encoded list in place of a list."""

        if value is None:
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return parse_string_array(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value


class ClauseAnswers(BaseModel):
    """The model's answers to one clause's checklist."""

    clause_id: str = Field(description="The clause's id exactly as given, for example C3")
    clause_type_fits: bool = Field(
        default=True,
        description="Only for a clause given two checklists: true when the clause is, in substance, a clause of the type its first checklist names; false when it is not",
    )
    answers: List[CriterionAnswer] = Field(default_factory=list, description="One answer per checklist item of this clause, in the order the items were given")

    @field_validator("answers", mode="before")
    @classmethod
    def _parse_string_array(cls, value: Any) -> Any:
        return parse_string_array(value)


class DraftingDefect(BaseModel):
    """A drafting defect the model can point to in one paragraph's own words."""

    paragraph: str = Field(description="The id of the paragraph that contains the defect, for example P12")
    defect_type: str = Field(description="blank_or_placeholder, broken_cross_reference or typographical_error")
    evidence: str = Field(description="The defective words, quoted exactly from that paragraph")
    note: str = Field(default="", description="One sentence: what is wrong and what the paragraph should say")

    @field_validator("defect_type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> Any:
        return _fold_label(value) if isinstance(value, str) else value

    @field_validator("paragraph", "evidence", "note", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else str(value).strip()


class BatchAssessment(BaseModel):
    """The model's structured output for one assessment call."""

    clauses: List[ClauseAnswers] = Field(default_factory=list, description="The checklist answers for every clause under assessment, in the order the clauses were given")
    drafting_defects: List[DraftingDefect] = Field(default_factory=list, description="Drafting defects in the paragraphs under assessment. Empty when there are none")

    @model_validator(mode="before")
    @classmethod
    def _split_run_together_arrays(cls, data: Any) -> Any:
        """Separate the two arrays when the model writes both into the clauses field as one string.

        Bedrock tool-use sometimes returns clauses as a JSON-encoded string, and
        then now and again appends the drafting_defects array to that same
        string ('[ ...answers ] [ ...defects ]'). The first array is the
        answers; an array that follows it is the defects, unless the defects
        were also given in their own field.
        """

        if not isinstance(data, dict) or not isinstance(data.get("clauses"), str):
            return data
        text = data["clauses"].strip()
        try:
            first, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return data
        rest = text[end:].lstrip(" \t\r\n,")
        if not rest:
            return data
        split = {**data, "clauses": first}
        try:
            trailing = json.loads(rest)
        except json.JSONDecodeError:
            return split
        if isinstance(trailing, list) and not data.get("drafting_defects"):
            split["drafting_defects"] = trailing
        return split

    @field_validator("clauses", "drafting_defects", mode="before")
    @classmethod
    def _parse_string_array(cls, value: Any) -> Any:
        return [] if value is None else parse_string_array(value)

    @field_validator("drafting_defects", mode="after")
    @classmethod
    def _known_defects_only(cls, value: List[DraftingDefect]) -> List[DraftingDefect]:
        return [defect for defect in value if defect.defect_type in DEFECT_TYPES]


class DraftedRevision(BaseModel):
    """The model's redraft of one paragraph and the two-sentence note that goes with it."""

    exposure: str = Field(description="One sentence, 30 words at most: the exposure for the client as the paragraph stands — what can happen, and to whom")
    change: str = Field(description='One sentence, 30 words at most, beginning "The revision": what the redraft does. It does not go on to say why that is good')
    revised_paragraph: str = Field(description="The complete replacement paragraph, ready to go into the agreement as written")

    @field_validator("exposure", "change", "revised_paragraph", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else str(value).strip()

    @property
    def comment(self) -> str:
        """The note shown with the suggestion: the exposure, then the change."""

        return " ".join(part for part in (self.exposure, self.change) if part)
