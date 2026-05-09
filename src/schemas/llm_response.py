from pydantic import BaseModel, Field


class QueryLLmResponse(BaseModel):
    """Schema for the Response for the given user query."""

    response: str = Field(..., description="Detailed response for the user query.")


class ChangeAnalysis(BaseModel):
    """Schema for analyzing contract changes."""

    change_type: str = Field(..., description="Type of change (e.g., 'Modified', 'Added', 'Deleted', 'Replaced')")
    clause_title: str = Field(..., description="Title or name of the affected clause")
    old_text: str = Field(..., description="The original text before changes")
    new_text: str = Field(..., description="The new text after changes")
    risk_impact: str = Field(..., description="Assessment of risk impact (e.g., 'High', 'Medium', 'Low', 'Neutral')")
    summary: str = Field(..., description="Brief summary of the change")
    legal_implications: str = Field(default="", description="Any legal implications of the change")
    recommendations: str = Field(default="", description="Recommendations for handling the change")
