"""Schemas for the Describe & Draft Agent."""

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class DescribeDraftRequest(BaseModel):
    """Request body — prompt and optional regenerate / target clause; session_id arrives via X-Session-ID header.

    Modes of use:
      - Free-text drafting: send `prompt`. Classifier routes to single_clause / list_of_clauses / clarification.
      - Regenerate a named clause from a prior list or prior single-clause response:
        send `target_clause_title` + `regenerate=True`. `prompt` is optional here
        (use it to pass a short refinement like "make it stricter" or leave blank).
    """

    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Free-text drafting request (e.g. 'draft an NDA' or 'draft a liquidity cap clause'). "
            "Optional when `target_clause_title` is supplied."
        ),
        max_length=2000,
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "If true and the session has a prior draft (single_clause or list_of_clauses), "
            "produce an improved variation instead of a fresh draft."
        ),
    )
    target_clause_title: Optional[str] = Field(
        default=None,
        description=(
            "Exact title of a clause from the session's last list_of_clauses response "
            "or last single_clause draft. When set, the agent regenerates that specific "
            "clause instead of classifying a fresh prompt."
        ),
        max_length=300,
    )
    ignore_document: bool = Field(
        default=False,
        description=(
            "When true, the agent treats this call as no-document mode even if a "
            "document is attached to the session — it skips document grounding, "
            "skips the existing-clause duplicate check, and drafts a reusable "
            "template with [PLACEHOLDER] tokens. Useful when the user wants a "
            "generic template draft on a session that already has an upload from "
            "an earlier interaction. Default false preserves the document-grounded "
            "behavior."
        ),
    )

    @model_validator(mode="after")
    def _require_prompt_or_target(self) -> "DescribeDraftRequest":
        prompt_empty = self.prompt is None or not self.prompt.strip()
        target_empty = self.target_clause_title is None or not self.target_clause_title.strip()
        if prompt_empty and target_empty:
            raise ValueError(
                "Either 'prompt' or 'target_clause_title' must be provided."
            )
        if prompt_empty and not target_empty and not self.regenerate:
            # Targeting a clause without prompt is only meaningful as a regenerate.
            self.regenerate = True
        return self


# --- LLM internal schemas (not exposed in API response) ---


class IntentClassification(BaseModel):
    """Output of the intent-classifier LLM call."""

    mode: Literal["list_of_clauses", "single_clause", "clarification"]
    detected_agreement_type: Optional[str] = None  # e.g. "NDA", "SaaS Agreement"
    clarification_question: Optional[str] = None  # populated when mode == "clarification"


class ClauseListEntry(BaseModel):
    """One clause entry in the agreement's full clause list (list_of_clauses mode).

    When a document is attached to the session, `drafted_clause` uses the extracted
    party names. Otherwise it uses `[PLACEHOLDER]` tokens the user can fill in.
    """

    title: str = Field(description="Clause name (e.g. 'Confidentiality Obligations')")
    summary: str = Field(
        description="One-sentence description of what this clause covers"
    )
    drafted_clause: str = Field(
        description=(
            "Full drafted clause text ready to drop into the agreement. "
            "Contains `[PLACEHOLDER]` tokens when no document is attached."
        )
    )
    placeholders: List[str] = Field(
        default_factory=list,
        description=(
            "Distinct placeholder tokens used in drafted_clause "
            "(e.g. ['[PARTY A]', '[EFFECTIVE DATE]']). In doc-grounded mode "
            "party-identity and governing-law tokens are not allowed, but "
            "factual placeholders for values the document does not supply "
            "(specific amounts, dates, durations, cure / notice periods) are."
        ),
    )


class ClauseVersion(BaseModel):
    """One drafted version of a single clause (single_clause mode).

    A single_clause call returns one ClauseVersion. Clicking regenerate produces
    another improved ClauseVersion for the same clause.
    """

    title: str = Field(description="Clause name or title")
    summary: str = Field(description="One-sentence summary of this version's approach")
    drafted_clause: str = Field(description="The full drafted clause text")
    placeholders: List[str] = Field(
        default_factory=list,
        description=(
            "Distinct placeholder tokens used in drafted_clause. In doc-grounded "
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


class DuplicateCheckResult(BaseModel):
    """LLM output for duplicate-clause detection against an uploaded document."""

    is_duplicate: bool = Field(
        description=(
            "True if the candidate text is already a clause on the same topic "
            "as the user's drafting request."
        )
    )
    matched_title: Optional[str] = Field(
        default=None,
        description="Heading/title of the existing clause, if one is discernible.",
    )
    summary: Optional[str] = Field(
        default=None,
        description=(
            "2-3 sentence plain-English explanation of what the matched clause "
            "actually does — scope, allocation of risk, and notable carve-outs. "
            "Only populated when is_duplicate is true."
        ),
    )


class ClauseLocation(BaseModel):
    """Where an existing clause sits inside the uploaded document."""

    chunk_index: Optional[int] = Field(
        default=None,
        description="Zero-based chunk index in the document, if available.",
    )
    section_heading: Optional[str] = Field(
        default=None,
        description="Nearest section heading, if the parser captured one.",
    )
    page_number: Optional[int] = Field(
        default=None,
        description="Page number the clause appears on, when the parser recorded it.",
    )


class ExistingClauseMatch(BaseModel):
    """Describes a clause already present in the uploaded document that matches the request.

    Field names mirror the freshly-drafted clause shape (title, summary,
    drafted_clause) so frontends can use the same renderer for both. The only
    difference is `drafted_clause` here is verbatim text pulled from the user's
    document, not a new draft.
    """

    title: Optional[str] = Field(
        default=None,
        description="Heading or inferred title of the existing clause.",
    )
    summary: Optional[str] = Field(
        default=None,
        description=(
            "2-3 sentence plain-English explanation of what the matched clause "
            "does — scope, allocation of risk, and notable carve-outs. Surfaces "
            "the same kind of summary a freshly drafted clause carries."
        ),
    )
    drafted_clause: str = Field(
        description=(
            "Verbatim text of the existing clause as pulled from the document. "
            "Field is named drafted_clause to match the shape of freshly-drafted "
            "single_clause and list_of_clauses responses."
        ),
    )
    similarity_score: float = Field(
        description="Retrieval similarity score of the matched chunk."
    )
    location: Optional[ClauseLocation] = Field(
        default=None,
        description=(
            "Where the matching clause sits in the document — chunk index, "
            "nearest heading, and page number when available."
        ),
    )


class DescribeDraftLLMResponse(BaseModel):
    """Raw LLM output for single_clause mode — exactly 1 ClauseVersion per call.

    Additional versions are produced by calling the endpoint again with regenerate=true.
    """

    versions: List[ClauseVersion] = Field(
        min_length=1,
        max_length=1,
        description="Exactly 1 version per call — validated post-generation",
    )


# --- API response schema ---


class DescribeDraftErrorType(str, Enum):
    """Typed error categories for the describe-draft agent."""

    CLARIFICATION_NEEDED = "clarification_needed"
    VALIDATION_FAILED = "validation_failed"
    LLM_FAILED = "llm_failed"
    RATE_LIMITED = "rate_limited"
    TARGET_NOT_FOUND = "target_not_found"


class DescribeDraftResponse(BaseModel):
    """Public API response from the describe-draft endpoint.

    Which field is populated depends on `mode`:
      - list_of_clauses      → `clauses` (the full clause list, each with drafted body)
      - single_clause        → `versions` (exactly 1 drafted version per call; regenerate for more)
      - single_clause_exists → `existing_clause` (the matching clause already in the uploaded doc)
      - clarification        → `clarification_question` (no generation)
    """

    session_id: str
    mode: Literal[
        "list_of_clauses",
        "single_clause",
        "single_clause_exists",
        "clarification",
    ]
    status: Literal["ok", "error"]
    disclaimer: Optional[str] = "AI-generated draft. Subject to attorney review before use."
    clarification_question: Optional[str] = None
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
        description="Populated only in single_clause mode — 1 entry per call.",
    )
    existing_clause: Optional[ExistingClauseMatch] = Field(
        default=None,
        description=(
            "Populated only in single_clause_exists mode — the matching clause "
            "already present in the uploaded document."
        ),
    )
    regenerated: bool = Field(
        default=False,
        description="True when this response was produced by a regenerate call.",
    )
    grounded_in_document: bool = Field(
        default=False,
        description=(
            "True when a document was attached to the session and the draft was grounded "
            "in it (parties, governing law, and relevant existing clauses)."
        ),
    )
    error_type: Optional[DescribeDraftErrorType] = None
    error_message: Optional[str] = None
