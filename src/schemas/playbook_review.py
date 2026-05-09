from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ParaSimilarity(BaseModel):
    """Paragraph Similarity Schema."""

    paragraph: str = Field(..., description="Paragraph of the document.")
    Similarity: float = Field(..., description="Similarity score of the given para to the rule.")


# --------------- Playbook Review Input Schemas -------------


class RuleInfo(BaseModel):
    title: str = Field(..., description="Title of the rule")
    instruction: str = Field(..., description="Instruction for the rule")
    description: str = Field(..., description="Detailed description of the rule")
    tags: Optional[List[str]] = Field(None, description="List of tags for the rule")
    rule_type: Optional[str] = Field("Primary", description="Type of the rule, e.g., obligation, prohibition, etc.")


class TextInfo(BaseModel):
    text: str = Field(..., description="Text content of the paragraph")
    paraindetifier: str = Field(..., description="Identifier for the paragraph")


class RuleCheckRequest(BaseModel):
    rulesinformation: List[RuleInfo] = Field(..., description="List of rules to check against")
    textinformation: List[TextInfo] = Field(..., description="List of text paragraphs to check")


# --------------- Rule Matching Result Schema -------------


class RuleResult(BaseModel):
    title: str
    instruction: str
    description: str
    paragraphidentifier: str
    paragraphcontext: str
    similarity_scores: List[float] = []


# --------------- Missing Clauses Schema -------------


class MissingClauseStatus(str, Enum):
    """Status of the missing clause."""

    absent = "absent"
    incomplete = "incomplete"
    ambiguous = "ambiguous"


class MissingClauseImportance(str, Enum):
    """Importance of the missing clause."""

    low = "low"
    medium = "medium"
    high = "high"


class MissingClause(BaseModel):
    clause_name: str = Field(..., description="Name of the missing clause")
    status: MissingClauseStatus = Field(..., description="Status of the missing clause")
    importance: MissingClauseImportance = Field(..., description="Importance of the missing clause")
    explanation: str = Field(..., description="Explanation of why the clause is missing or incomplete")


class MissingClausesLLMResponse(BaseModel):
    missing_clauses: List[MissingClause] = Field(..., description="List of identified missing clauses")
    total_missing: int = Field(..., description="Total number of missing clauses identified")
    summary: str = Field(..., description="Summary of the missing clauses and their implications")


# ------------- Playbook Review Output Schemas -------------


class ResponseStatus(str, Enum):
    """Response status for the AI Review."""

    CRITICAL = "Critical"
    MEDIUM = "Medium"
    LOW = "Low"
    GOOD = "Good"
    NOT_FOUND = "Not Found"


# # LLM Request
# class PlayBookReviewRequest(BaseModel):
#     """Schema for the AI review request."""

#     rule_title: str = Field(..., description="Title of the rule to be compared.")
#     # instruction: str = Field(..., description="Instructions for the given rule")
#     description: str = Field(..., description="Detailed description of the given rule.")
#     paragraphs: List[str] = Field(..., description="list of retrieved paragraphs to validate with.")


class PlayBookReviewLLMResponse(BaseModel):
    """Schema for the LLM response for the given rule and para."""

    para_identifiers: List[str] = Field(..., description="List of Paragraphs that matched the rule.")
    status: ResponseStatus = Field(..., description="Status of the given rules and para (critical, medium, low, good)")
    reason: str = Field(..., description="Reason of the Review either good or bad,")
    suggestion: str = Field(..., description="A brief suggestion of the paragraphs over the rule.")
    suggested_fix: str = Field(..., description="Suggested fix for the given rule and the paragraph.")


class PlayBookReviewResponse(BaseModel):
    """Schema for AI review reponse for the given rules and paras."""

    rule_title: str = Field(..., description="Title of the rule that was considered.")
    rule_type: Optional[str] = Field("primary", description="Type of the rule, e.g., obligation, prohibition, etc.")
    rule_instruction: str = Field(..., description="Rule Instruction that was considered.")
    rule_description: str = Field(..., description="Rule Description that was considered.")
    content: PlayBookReviewLLMResponse = Field(..., description="Content of the review for the given rule and paragraphs.")


class PlayBookReviewFinalResponse(BaseModel):
    """Schema for the final response of the playbook review."""

    rules_review: List[PlayBookReviewResponse] = Field(..., description="List of reviews for each rule.")
    missing_clauses: Optional[MissingClausesLLMResponse] = Field(None, description="Identified missing clauses in the contract, if any.")


class Clause(BaseModel):
    """Schema for a contract clause."""

    title: str = Field(description="Title of the clause")
    content: str = Field(description="Content of the clause")


class ClauseExtractionResponse(BaseModel):
    """Response model for clause extraction."""

    clauses: List[Clause] = Field(description="List of extracted clauses")
