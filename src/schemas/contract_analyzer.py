from typing import List, Literal

from pydantic import BaseModel, Field


class KeyInformationResponse(BaseModel):
    """Response model for key information extracted from a contract."""

    field_name: str = Field(description="Name of the key information field")
    value: str = Field(description="Value of the key information field")


class TimelineMilestone(BaseModel):
    """Model for key milestones and their timelines."""

    milestone_name: str = Field(description="Description of the milestone")
    date_or_trigger: str = Field(description="Timeline associated with the milestone")
    description: str = Field(description="Additional details about the milestone")


class RiskComplianceInsight(BaseModel):
    """Model for identified risks and compliance issues."""

    severity: Literal["Critical", "High", "Medium", "Low"] = Field(description="Severity level of the issue")
    clause_title: str = Field(description="The name of the clause where this issue appears, or the standard clause that is absent")
    issue: str = Field(description="One sentence describing the issue, quoting the specific contract language (or stating a standard protection is absent), the commercial consequence, and which party bears it")


class ContractAnalyzerResponse(BaseModel):
    """Response model for contract analysis results — produced in a single LLM call."""

    summary: str = Field(description="Summary of the contract and analysis")
    key_information: List[KeyInformationResponse] = Field(description="List of key information fields extracted from the contract")
    timeline_and_key_milestones: List[TimelineMilestone] = Field(description="List of key milestones and their timelines")
    risk_and_compliance_insights: List[RiskComplianceInsight] = Field(description="List of identified risks and compliance issues")
