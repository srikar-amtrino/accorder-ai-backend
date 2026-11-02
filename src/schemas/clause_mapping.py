from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ClausePolicy(BaseModel):
    """The legal team's review rules for one clause type — one row of the Policy Rule Matrix.

    canonical_type and sub_type must use the exact labels stored on the vector
    DB records, so a vector match leads straight to its policy.
    """

    canonical_type: str = Field(..., min_length=1, description="Clause type label, identical to the vector DB record's canonical_type (e.g. 'personnel')")
    sub_type: Optional[str] = Field(default=None, description="Sub-type label, identical to the vector DB record's sub_type. Omit for a policy that covers every sub-type")
    agreement_type: Optional[str] = Field(default=None, description="Agreement type this policy is specific to (e.g. 'MSA'). Omit for a policy that applies to every agreement type")
    inherent_risk_tier: Literal["High", "Medium", "Low"] = Field(..., description="How risky this clause topic is by nature, before the actual language is read")
    acceptable_standard: str = Field(default="", description="What an acceptable version of this clause looks like")
    unacceptable_triggers: List[str] = Field(default_factory=list, description="Red-flag terms that make the clause unacceptable")
    fallback_guidelines: str = Field(default="", description="Compromise position to propose when the clause misses the standard")

    @field_validator("inherent_risk_tier", mode="before")
    @classmethod
    def _normalize_risk_tier(cls, value: Any) -> Any:
        """Accept any casing ('medium', 'HIGH') from hand-edited policy files."""

        if not isinstance(value, str):
            return value
        return value.strip().capitalize()


class ClauseMatch(BaseModel):
    """One vector DB hit: a known clause type and how closely the clause resembles it."""

    canonical_type: str = Field(..., description="canonical_type of the matched vector DB record")
    sub_type: Optional[str] = Field(default=None, description="sub_type of the matched vector DB record")
    score: float = Field(..., description="Cosine similarity between the clause and the matched record")


class ClassificationStatus(str, Enum):
    """How confidently a clause was mapped to a clause type."""

    AUTO_ACCEPTED = "auto_accepted"
    NEEDS_CONFIRMATION = "needs_confirmation"
    UNCLASSIFIED = "unclassified"


class ClauseClassification(BaseModel):
    """Outcome of mapping one clause to the canonical clause types."""

    status: ClassificationStatus = Field(..., description="auto_accepted, needs_confirmation (LLM picks from matches), or unclassified (manual triage)")
    matches: List[ClauseMatch] = Field(
        default_factory=list,
        description="Accepted clause types when auto_accepted (several for a compound clause); candidates for the LLM when needs_confirmation; empty when unclassified",
    )
