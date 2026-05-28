"""Schemas for the Describe & Draft Agent."""

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class DescribeDraftRequest(BaseModel):
    """Request body — a free-text drafting prompt and the document-context toggle.

    `session_id` arrives via the X-Session-ID header, not the body.

    The classifier routes `prompt` to single_clause (one clause asked for) or
    list_of_clauses (a whole agreement asked for). When `use_document_context`
    is true and the session has an uploaded document, the full document text is
    fed to the model so the draft is grounded in it.
    """

    prompt: str = Field(
        description=(
            "Free-text drafting request, e.g. 'I need a liquidated damages clause' "
            "or 'Draft a Non-Disclosure Agreement for multiple parties'."
        ),
        max_length=2000,
    )
    use_document_context: bool = Field(
        default=True,
        description=(
            "When true (checkbox ticked) and the session has an uploaded document, "
            "the agent grounds the draft in the full document content. When false, "
            "the agent ignores any uploaded document and drafts a reusable template "
            "with [PLACEHOLDER] tokens from the prompt alone."
        ),
    )


# --- LLM internal schemas (not exposed in API response) ---


class IntentClassification(BaseModel):
    """Output of the intent-classifier LLM call."""

    mode: Literal["list_of_clauses", "single_clause"]
    detected_agreement_type: Optional[str] = None  # e.g. "NDA", "SaaS Agreement"


class ClauseListEntry(BaseModel):
    """One clause entry in the agreement's full clause list (list_of_clauses mode).

    When a document is attached and document context is on, `drafted_clause` uses
    the party names from the document. Otherwise it uses `[PLACEHOLDER]` tokens the
    user can fill in.
    """

    title: str = Field(description="Clause name (e.g. 'Confidentiality Obligations')")
    summary: str = Field(
        description="One-sentence description of what this clause covers"
    )
    drafted_clause: str = Field(
        description=(
            "Full drafted clause text ready to drop into the agreement. "
            "Contains `[PLACEHOLDER]` tokens when no document context is used."
        )
    )
    placeholders: List[str] = Field(
        default_factory=list,
        description=(
            "Distinct placeholder tokens used in drafted_clause "
            "(e.g. ['[PARTY A]', '[EFFECTIVE DATE]']). In document-grounded mode "
            "party-identity and governing-law tokens are not allowed, but "
            "factual placeholders for values the document does not supply "
            "(specific amounts, dates, durations, cure / notice periods) are."
        ),
    )


class ClauseVersion(BaseModel):
    """The drafted single clause (single_clause mode)."""

    title: str = Field(description="Clause name or title")
    summary: str = Field(description="Short summary of the requested clause")
    drafted_clause: str = Field(description="The full drafted clause text")
    placeholders: List[str] = Field(
        default_factory=list,
        description=(
            "Distinct placeholder tokens used in drafted_clause. In document-grounded "
            "mode party-identity and governing-law tokens are not allowed, but "
            "factual placeholders for values the document does not supply "
            "(specific amounts, dates, durations, cure / notice periods) are."
        ),
    )


class ClauseListLLMResponse(BaseModel):
    """Raw LLM output for list_of_clauses mode — one complete clause list."""

    agreement_summary: str = Field(
        description=(
            "Overall 3-5 sentence summary of the agreement: what it is for, who "
            "the parties are, the core exchange or obligations, and any notable "
            "structural features (term, key carve-outs). Shown at the top of the "
            "list so the user can orient themselves before scrolling clauses."
        ),
    )
    clauses: List[ClauseListEntry] = Field(
        min_length=8,
        description=(
            "Complete list of clauses for the requested agreement type. "
            "Minimum 8; prompt requests 12+ for most agreements."
        ),
    )


class DescribeDraftLLMResponse(BaseModel):
    """Raw LLM output for single_clause mode — exactly 1 ClauseVersion per call."""

    versions: List[ClauseVersion] = Field(
        min_length=1,
        max_length=1,
        description="Exactly 1 version per call — validated post-generation",
    )


# --- API response schema ---


class DescribeDraftErrorType(str, Enum):
    """Typed error categories for the describe-draft agent."""

    VALIDATION_FAILED = "validation_failed"
    LLM_FAILED = "llm_failed"
    RATE_LIMITED = "rate_limited"


class DescribeDraftResponse(BaseModel):
    """Public API response from the describe-draft endpoint.

    Which field is populated depends on `mode`:
      - list_of_clauses → `agreement_summary` (short summary) + `clauses` (full content)
      - single_clause   → `versions` (one drafted clause: summary + full clause text)
    """

    session_id: str
    mode: Literal["list_of_clauses", "single_clause"]
    status: Literal["ok", "error"]
    disclaimer: Optional[str] = "AI-generated draft. Subject to attorney review before use."
    agreement_summary: Optional[str] = Field(
        default=None,
        description=(
            "Populated only in list_of_clauses mode — overall 3-5 sentence "
            "summary of the agreement (what it is for, parties, core exchange, "
            "notable structural features). Shown at the top of the list."
        ),
    )
    clauses: List[ClauseListEntry] = Field(
        default_factory=list,
        description="Populated only in list_of_clauses mode.",
    )
    versions: List[ClauseVersion] = Field(
        default_factory=list,
        description="Populated only in single_clause mode — exactly 1 entry.",
    )
    grounded_in_document: bool = Field(
        default=False,
        description=(
            "True when document context was used and the draft was grounded in the "
            "full content of the uploaded document."
        ),
    )
    error_type: Optional[DescribeDraftErrorType] = None
    error_message: Optional[str] = None
