import json
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from src.schemas.general_review import _escape_inner_quotes, TextInformation

# Every extracted fact carries how sure the model is of it:
#   clear    — stated plainly in the document; copied, not inferred.
#   inferred — not stated word for word but a high-confidence reading of the
#              text (e.g. "Amtrino" and "Amtrino Pvt Ltd" are the same entity).
#   flagged  — genuinely ambiguous, contradictory, or missing in the document.
#              A flagged item is not a failure; it is the honest answer, and it
#              is usually a drafting defect worth surfacing to the reviewer.
Confidence = Literal["clear", "inferred", "flagged"]


def _coerce_json_array(value: Any) -> Any:
    """Tolerate a nested array arriving as a JSON-encoded string.

    Bedrock tool-use intermittently serializes long arrays as a JSON string
    instead of a real array (the same quirk the general-review schema guards
    against); parse it back so validation succeeds.
    """

    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    # strict=False accepts the raw tabs and newlines the model copies out of the
    # document into the embedded JSON. Verbatim excerpts routinely carry them —
    # tabbed signature blocks, indented lists — and strict parsing rejects a
    # control character inside a string, discarding a recoverable response.
    try:
        return json.loads(stripped, strict=False)
    except json.JSONDecodeError:
        # Verbatim anchor_text and names copy document quotes into the embedded
        # JSON unescaped, which breaks parsing; repair them before giving up
        # (the same fix general_review applies to its stringified arrays).
        try:
            return json.loads(_escape_inner_quotes(stripped), strict=False)
        except json.JSONDecodeError:
            return value


class Party(BaseModel):
    """A party to the contract, as the document identifies it."""

    defined_as: str = Field(description="The label the document uses for this party (e.g. 'the Supplier', 'Company'). Empty if none is given.")
    name_as_written: str = Field(description="The party's actual name exactly as written in the document. Never decomposed or normalized.")
    role: Optional[str] = Field(default=None, description="What this party does in the contract (e.g. 'provides the services', 'pays the fees'). Null if the document does not state it.")
    source_location: Optional[str] = Field(default=None, description="Where in the document this party is established (e.g. 'Recitals', 'Preamble').")
    confidence: Confidence = Field(description="clear, inferred, or flagged — see the confidence scale.")


class DefinedTerm(BaseModel):
    """A term the document defines and relies on elsewhere."""

    term: str = Field(description="The defined term exactly as written (e.g. 'Confidential Information').")
    meaning: Optional[str] = Field(default=None, description="A faithful plain-language statement of the definition. Null when the term is used as if defined but never actually defined in the provided text.")
    source_location: Optional[str] = Field(default=None, description="Where the term is defined (e.g. 'Section 1.2'). Null if it is used but never defined.")
    confidence: Confidence = Field(description="clear when defined outright; flagged when used-but-undefined or defined inconsistently.")


class Clause(BaseModel):
    """One operative clause, identified by what it actually is — not just its heading."""

    number: Optional[str] = Field(default=None, description="The clause's number as written (e.g. '8.2', 'Section 3'). Null if unnumbered.")
    name: str = Field(description="The clause's identity. Its heading when it has one; otherwise the function it performs (e.g. 'Indemnification', 'Limitation of Liability').")
    clause_type: Optional[str] = Field(default=None, description="A normalized category for the clause (e.g. 'indemnification', 'termination', 'confidentiality', 'payment', 'governing_law'). Null if it does not map to a standard type.")
    anchor_text: str = Field(description="The opening of the clause's first paragraph, copied character-for-character, at most 80 characters. Just enough to locate the paragraph — never the whole sentence.")
    summary: Optional[str] = Field(default=None, description="What the clause does, in at most 15 words. A label, not a paraphrase.")
    source_location: Optional[str] = Field(default=None, description="Where the clause sits in the document.")
    confidence: Confidence = Field(description="clear when the clause and its identity are unambiguous; inferred when the identity is read from function rather than a heading; flagged when its identity or boundaries are unclear.")

    grounded: bool = Field(default=False, description="Server-filled: whether anchor_text was found verbatim in the source document.")


class Ambiguity(BaseModel):
    """A place where the document itself is ambiguous, contradictory, or incomplete.

    These are the residual cases no amount of model capability can resolve —
    because the information is not in the text — and each is typically a real
    drafting defect a reviewer would want raised.
    """

    kind: str = Field(description="The nature of the problem (e.g. 'undefined_term', 'broken_cross_reference', 'placeholder_value', 'internal_contradiction', 'unclear_party_reference', 'missing_value').")
    description: str = Field(description="A concise plain-language explanation of what is ambiguous or missing and why.")
    location: Optional[str] = Field(default=None, description="Where in the document the problem occurs.")


class DocumentMap(BaseModel):
    """A grounded, structured understanding of a single contract.

    Produced once per document by the document-understanding pass and shared as
    read-only context by every downstream agent, so they all reason from one
    correct, consistent reading of the document instead of each re-interpreting
    the raw text.
    """

    contract_type: Optional[str] = Field(default=None, description="The kind of contract (e.g. 'Master Services Agreement', 'NDA', 'Employment Agreement'). Null if it cannot be determined from the text.")
    parties: List[Party] = Field(default_factory=list, description="Every party the document establishes.")
    defined_terms: List[DefinedTerm] = Field(default_factory=list, description="The key terms the document defines and relies on.")
    clauses: List[Clause] = Field(default_factory=list, description="The operative clauses, in document order.")
    ambiguities: List[Ambiguity] = Field(default_factory=list, description="Genuine ambiguities, contradictions, or missing values found in the document.")

    @field_validator("parties", "defined_terms", "clauses", "ambiguities", mode="before")
    @classmethod
    def _tolerate_string_array(cls, value: Any) -> Any:
        return _coerce_json_array(value)


class DocumentMapRequest(BaseModel):
    """JSON request for the extraction layer — the same paragraph shape the review agents already send."""

    textinformation: List[TextInformation] = Field(..., description="Document paragraphs, in document order")


class ClauseStart(BaseModel):
    """One paragraph at which a clause begins.

    Internal to the extraction layer: a long document is cut into concurrent
    sections, and these are the only places a cut is allowed to fall, so a
    clause is never split across two calls that each see half of it.
    """

    paragraph_index: int = Field(description="The [n] index of the paragraph where this clause begins.")
    opening_words: str = Field(description="The first few words of that paragraph, copied verbatim (at most eight words), so the index can be verified against the source.")


class ClauseBoundaries(BaseModel):
    """Every point in the document at which a clause begins, in document order."""

    clause_starts: List[ClauseStart] = Field(default_factory=list, description="The paragraph at which each clause begins, in ascending document order.")

    @field_validator("clause_starts", mode="before")
    @classmethod
    def _tolerate_string_array(cls, value: Any) -> Any:
        return _coerce_json_array(value)
