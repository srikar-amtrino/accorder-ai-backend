from typing import List

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
    source_clause: str = Field(description="The clause from the contract that this milestone is based on")


class RiskComplianceInsight(BaseModel):
    """Model for identified risks and compliance issues."""

    issue_title: str = Field(description="A short descriptive title for this issue")
    severity: str = Field(description="Severity level of the issue (Critical / High / Medium / Low)")
    clause_title: str = Field(description="The name of the clause where this issue appears (should always be present in the contract)")
    issue_type: str = Field(description="Type of the issue (Missing Clause / Ambiguity / One-Sided Provision / Unusual Obligation / Broad Term / Unenforceable Clause / Jurisdiction Risk / Other)")
    issue: str = Field(description="One to two sentences describing the issue, quoting specific language from the contract and the commercial consequence and which party bears it")
    scenario: str = Field(description="One to two sentences describing a concrete situation where this causes harm, naming the parties and the outcome")
    fix: str = Field(description="Exact sentences describing whether to accept, revise, or reject, plus the specific replacement or added language")


class ContractAnalyzerResponse(BaseModel):
    """Response model for contract analysis results."""

    summary: str = Field(description="Summary of the contract and analysis")
    key_information: List[KeyInformationResponse] = Field(description="List of key information fields extracted from the contract")
    timeline_and_key_milestones: List[TimelineMilestone] = Field(description="List of key milestones and their timelines")
    risk_and_compliance_insights: List[RiskComplianceInsight] = Field(description="List of identified risks and compliance issues")
