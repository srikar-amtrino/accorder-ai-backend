from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# class GeneralReviewResponse(BaseModel):
#     reason: str = Field(..., description="Reason for the review result, explaining why the paragraph does or does not comply with the rule.")
#     suggested_fix: str = Field(..., description="Suggested fix or improvement to make the paragraph comply with the rule, if applicable.")


# class GeneralReviewRequest(BaseModel):
#     paragraph: str = Field(..., description="The specific paragraph from the contract that needs to be reviewed against the rule.")
#     rule: str = Field(..., description="The specific rule or guideline that the paragraph is being reviewed against (e.g., 'Confidentiality clause must specify duration of at least 3 years').")


class Suggestion(BaseModel):
    """A single apply/dismiss-able suggestion returned by the agent.

    Minimal by design — the frontend's apply button needs ``original_text``
    as an anchor and ``suggested_fix`` as the replacement text; ``reason``
    is what we show the reviewer to justify the change.
    """

    clause_title: str = Field(description="Title/heading of the clause this suggestion applies to")
    reason: str = Field(description="Plain-language justification for the change, grounded in the clause text")
    original_text: str = Field(description="Exact verbatim substring of the clause text to be replaced")
    suggested_fix: str = Field(description="Proposed replacement text for original_text")


# --- LLM response shapes (internal) ------------------------------------------


class ClauseSuggestionsLLMResponse(BaseModel):
    """What the per-clause review LLM call returns.

    Zero or more suggestions for a single clause. Empty when the clause
    does not need to change, does not contain the topic, or the reviewer's
    ask does not apply to it.
    """

    suggestions: List[Suggestion] = Field(
        default_factory=list,
        description="Suggestions for this clause (empty list when nothing to flag)",
    )


class PromptSplitLLMResponse(BaseModel):
    """What the Mode-2 prompt-splitter LLM call returns.

    Breaks a multi-topic user instruction into atomic sub-instructions.
    Single-topic queries come back as a one-element list. An empty list
    is treated by the tool as "splitter failed" and the caller falls back
    to the original prompt verbatim.
    """

    subtopics: List[str] = Field(
        default_factory=list,
        description=("Atomic sub-instructions extracted from the user prompt. " "Each element is one self-contained contract-review ask."),
    )


class RelevanceCheckLLMResponse(BaseModel):
    """What the Mode-1 relevance-gate LLM call returns."""

    relevant: bool = Field(description="True if the user's query applies to the selected clause")
    reason: str = Field(description=("Short explanation for the user. When relevant=false, this is " "the alert message telling them why the clause and query don't match."))


# --- API request / response --------------------------------------------------


class GeneralReviewRequest(BaseModel):
    """Request body for the general review endpoint."""

    prompt: str = Field(description="User's review instruction (e.g., 'Check for unfair liability terms')")
    selected_clause: Optional[str] = Field(
        default=None,
        description="Text of the clause selected by the user. Omit for full document review.",
    )
    clause_title: Optional[str] = Field(
        default=None,
        description="Title/heading of the selected clause, if available.",
    )


class GeneralReviewResponse(BaseModel):
    """Response from the general review endpoint."""

    session_id: str = Field(description="Session ID the review ran against")
    mode: Literal["clause", "document"] = Field(description="'clause' if the user selected a clause, 'document' for full review")
    status: Literal["ok", "clause_query_mismatch"] = Field(
        default="ok",
        description=(
            "'ok' means the response was built normally (suggestions may be "
            "empty and alert_message may carry a 'not found' note). "
            "'clause_query_mismatch' means the user's query did not match the "
            "selected clause; the frontend should show alert_message instead."
        ),
    )
    alert_message: Optional[str] = Field(
        default=None,
        description=(
            "Informational message for the user. Populated when the query "
            "did not match the selected clause (Mode 1), when the selected "
            "clause did not contain matching content (Mode 1), or when one "
            "or more sub-topics of the query were not found anywhere in the "
            "document (Mode 2). Null when the review completed normally."
        ),
    )
    suggestions: List[Suggestion] = Field(
        default_factory=list,
        description="Flat list of apply/dismiss suggestions. Empty when nothing to flag.",
    )
