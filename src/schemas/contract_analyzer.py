from typing import List, Literal

from pydantic import BaseModel, Field

# NOTE: These Field descriptions are not just documentation — Bedrock receives them
# verbatim as the forced tool's input_schema, so they act as per-field prompt
# instructions. They are written to compress every value to the shortest form that
# still conveys the fact, which is the main lever on output tokens (and therefore
# latency). The field NAMES and structure are the locked wire contract the frontend
# depends on; only the guidance text is tuned here.


class KeyInformationResponse(BaseModel):
    """One key fact about the deal."""

    field_name: str = Field(description="Short, unique label for the fact (1-4 words). Choose it yourself from the contract; there is no fixed set.")
    value: str = Field(description="The value only, as the shortest phrase that conveys it (e.g. 'Net 30', '2 years, auto-renews'). Not a sentence. Do not restate the label. Use 'Absent' when a material term is missing.")


class TimelineMilestone(BaseModel):
    """A date or trigger that carries real consequence."""

    milestone_name: str = Field(description="Short name of the milestone (1-4 words).")
    date_or_trigger: str = Field(description="The date or triggering event only, as a short phrase (e.g. '30 days after signing', '2025-12-31').")
    description: str = Field(description="What happens, in one short phrase (<=12 words). Not a sentence.")


class RiskComplianceInsight(BaseModel):
    """A material risk a party would renegotiate before signing."""

    severity: Literal["Critical", "High", "Medium", "Low"] = Field(description="Severity. Emit only Critical or High.")
    clause_title: str = Field(description="The clause where the issue appears, or the standard clause that is absent (1-4 words).")
    issue: str = Field(description="The specific risk and which party it hurts, in <=20 words. No long quotes, no generic advice, no preamble.")


class ContractAnalyzerResponse(BaseModel):
    """Contract analysis for a Word side-panel — terse, high-signal, produced in one LLM call."""

    summary: str = Field(description="<=2 sentences (<=35 words): the parties, what the contract grants, and whether to sign as-is or amend. No preamble.")
    key_information: List[KeyInformationResponse] = Field(description="Only the facts needed to understand the deal. As many or as few as the contract genuinely warrants; omit routine boilerplate. No fixed set.")
    timeline_and_key_milestones: List[TimelineMilestone] = Field(description="Only dates/triggers with real consequence. Empty list if none are material.")
    risk_and_compliance_insights: List[RiskComplianceInsight] = Field(description="Only Critical/High issues worth renegotiating before signing. Fewer, sharper items are better. Empty list if genuinely none.")
