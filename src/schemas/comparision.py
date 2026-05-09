from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field


class ClauseUnit(BaseModel):
    """A single clause extracted from a document's chunks."""

    clause_id: str = Field(..., description="Unique identifier for this clause unit")
    heading: Optional[str] = Field(None, description="The section heading this clause belongs to, if any")
    content: str = Field(..., description="The full text content of this clause")
    position: int = Field(..., description="The position of this clause in the original document (e.g., clause number or order index)")
    doc_order: int = Field(0, description="The order of this clause in the document, used for reordering detection")
    embedding: List[float] = Field(default_factory=list)
    section_heading: Optional[str] = None


class MatchResult(BaseModel):
    """Output of the clause matching stage."""

    matched_pairs: List[Tuple[int, int, float]] = Field(
        ..., description="List of matched clause pairs with their similarity scores. Each tuple contains (index_in_doc_a, index_in_doc_b, similarity_score)"
    )
    unmatched_a: List[int] = Field(..., description="List of clause indices in Document A that were not matched to any clause in Document B")
    unmatched_b: List[int] = Field(..., description="List of clause indices in Document B that were not matched to any clause in Document A")


class CompareRequest(BaseModel):
    """Request body for the compare endpoint."""

    session_id: str = Field(..., description="Unique identifier for the user's session")
    document_id_a: str = Field(..., description="Unique identifier for Document A (e.g., version 1)")
    document_id_b: str = Field(..., description="Unique identifier for Document B (e.g., version 2)")


ChangeType = Literal["added", "removed", "modified", "reordered"]
RiskLevel = Literal["high", "medium", "low"]

# class ClauseComparisonLLMResponse(BaseModel):
#     """Structured output schema for the per-pair LLM comparison call."""

#     change_type: str = Field(description="One of: modified, reordered")
#     modification_type: Optional[str] = Field(None, description="Sub-type: value, language, scope, structural, rewritten")
#     risk_level: str = Field(description="One of: high, medium, low")
#     affected_party: Optional[str] = Field(None, description="Which party is affected by this change")
#     old_text: str = Field(description="Exact text from Document A that changed")
#     new_text: str = Field(description="Exact text from Document B that changed")
#     legal_implication: str = Field(description="Brief note on legal meaning of this change")
#     is_substantive: bool = Field(description="Whether this is a meaningful change vs cosmetic")


class ClauseComparisonLLMResponse(BaseModel):
    """Structured output schema for the per-pair LLM comparison call."""

    change_type: ChangeType = Field(description="One of: added, removed, modified, reordered")
    modification_type: Optional[str] = Field(None, description="Sub-type: value, language, scope, structural, rewritten")
    summary: str = Field(description="Brief one-sentence summary of what changed between the two versions")
    risk_level: RiskLevel = Field(description="One of: high, medium, low")
    affected_party: Optional[str] = Field(None, description="Which party is affected by this change")
    is_substantive: bool = Field(description="Whether this is a meaningful change vs cosmetic")


# class ChangeEntry(BaseModel):
#     """A single change between two document clauses."""

#     clause_name: str = Field(description="Name of the clause or section where the change occurred")
#     change_type: str = Field(description="One of: added, removed, modified, reordered")
#     modification_type: Optional[str] = Field(None, description="Sub-type: value, language, scope, structural, rewritten")
#     risk_level: str = Field(description="One of: high, medium, low")
#     affected_party: Optional[str] = Field(None, description="Which party is affected by this change")
#     confidence: str = Field("high", description="Confidence level of the change detection")
#     text_from_doc_a: Optional[str] = Field(None, description="Text from Document A related to this change")
#     text_from_doc_b: Optional[str] = Field(None, description="Text from Document B related to this change")
#     legal_implication: Optional[str] = Field(None, description="Brief note on legal meaning of this change")
#     is_substantive: bool = Field(True, description="Whether this is a meaningful change vs cosmetic")


class ChangeEntry(BaseModel):
    """A single change between two document clauses."""

    clause_name: str
    section: Optional[str] = None
    change_type: str
    modification_type: Optional[str] = None
    risk_level: Optional[str] = None
    affected_party: Optional[str] = None
    confidence: str = "high"
    text_from_doc_a: Optional[str] = None
    text_from_doc_b: Optional[str] = None
    summary: Optional[str] = None
    is_substantive: bool = True


class SectionGroup(BaseModel):
    """A group of changes under the same section heading."""

    section_name: str = Field(description="Name of the section or heading that groups these changes")
    changes: List[ChangeEntry] = Field(description="List of changes that fall under this section")


class CompareSummary(BaseModel):
    """Summary statistics for the comparison."""

    total_changes: int = Field(description="Total number of changes detected between the two documents")
    added: int = Field(description="Number of clauses that were added in Document B compared to Document A")
    removed: int = Field(description="Number of clauses that were removed in Document B compared to Document A")
    modified: int = Field(description="Number of clauses that were modified between Document A and Document B")
    reordered: int = Field(description="Number of clauses that were reordered between Document A and Document B")
    overall_risk: str = Field(description="Overall risk level based on the changes")
    high_risk_count: int = Field(description="Number of changes classified as high risk")
    llm_calls_made: int = Field(description="Number of calls made to the LLM for detailed change analysis")
    llm_calls_skipped: int = Field(default=0, description="Number of changes for which LLM analysis was skipped due to low similarity or other heuristics")


class CompareResponse(BaseModel):
    """Top-level response for the compare endpoint."""

    success: bool = Field(description="Indicates whether the comparison was successful")
    error: Optional[str] = Field(None, description="Error message if the comparison failed")
    message: Optional[str] = Field(None, description="Additional information about the comparison")
    summary: Optional[CompareSummary] = Field(None, description="Summary statistics for the comparison")
    sections: List[SectionGroup] = Field(description="List of section groups containing the changes")
    # document_id_a: Optional[str] = Field(None, description="Unique identifier for Document A")
    # document_id_b: Optional[str] = Field(None, description="Unique identifier for Document B")
