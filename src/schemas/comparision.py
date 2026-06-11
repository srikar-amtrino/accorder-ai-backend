from typing import List, Literal, Optional

from pydantic import BaseModel, Field

ChangeType = Literal["added", "removed", "modified", "reordered"]
RiskLevel = Literal["high", "medium", "low"]


class HolisticChange(BaseModel):
    """One change emitted by the single holistic document-comparison LLM call.

    Internal plumbing — mapped onto the public ChangeEntry before it reaches the API
    response. The model produces these by reading both full documents and aligning them
    by meaning (so renumbered/restructured sections are matched, not reported as a
    remove-plus-add).
    """

    clause_name: str = Field(description="Short title of the clause/section where the change occurs (e.g. 'Integration Testing', 'Term').")
    section: Optional[str] = Field(None, description="Parent section grouping if clear, else null.")
    change_type: ChangeType = Field(description="added | removed | modified | reordered")
    modification_type: Optional[str] = Field(None, description="For 'modified' only: value, language, scope, structural, rewritten. Null otherwise.")
    risk_level: RiskLevel = Field(description="high | medium | low")
    affected_party: Optional[str] = Field(None, description="Party whose position materially changes, 'Both', or null.")
    text_from_doc_a: Optional[str] = Field(None, description="The specific original excerpt that changed or was removed. Null for additions.")
    text_from_doc_b: Optional[str] = Field(None, description="The specific revised excerpt that changed or was added. Null for removals.")
    summary: str = Field(description="One concrete sentence: what changed, stated as before -> after.")
    is_substantive: bool = Field(description="True for meaningful changes; false for purely cosmetic ones.")


class HolisticCompareResponse(BaseModel):
    """Structured output schema for the single holistic comparison call."""

    changes: List[HolisticChange] = Field(description="Every change between the original and revised documents.")


class ChangeEntry(BaseModel):
    """A single change between two documents (public response item)."""

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
    llm_calls_made: int = Field(description="Number of LLM calls made for the comparison")
    llm_calls_skipped: int = Field(default=0, description="Number of changes for which LLM analysis was skipped")


class CompareResponse(BaseModel):
    """Top-level response for the compare endpoint."""

    success: bool = Field(description="Indicates whether the comparison was successful")
    error: Optional[str] = Field(None, description="Error message if the comparison failed")
    message: Optional[str] = Field(None, description="Additional information about the comparison")
    summary: Optional[CompareSummary] = Field(None, description="Summary statistics for the comparison")
    sections: List[SectionGroup] = Field(description="List of section groups containing the changes")
