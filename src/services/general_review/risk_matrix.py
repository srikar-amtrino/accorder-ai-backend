"""Risk levels for the general review, computed in code.

A finding is a checklist criterion its clause fails. Its risk level is the
risk allocation the legal team gives that type of clause (the clause's risk
tier): a shortfall in a limitation of liability clause is High, one in a
notices clause is Low. A criterion the taxonomy marks as a gate can raise the
level above the clause's tier, never lower it. The model supplies none of
this, so the same failed criterion always carries the same level.
"""

from typing import Iterable, Literal, Optional, cast

RiskLevel = Literal["High", "Medium", "Low"]

_TIERS = ("High", "Medium", "Low")
_ORDER = {"Low": 0, "Medium": 1, "High": 2}


def normalize_tier(tier: Optional[str]) -> str:
    folded = (tier or "").strip().capitalize()
    return folded if folded in _TIERS else "Medium"


def criterion_risk_level(inherent_risk_tier: Optional[str], gate: Optional[str] = None) -> RiskLevel:
    """The risk level of one failed criterion.

    1. The finding takes the risk tier of its clause.
    2. A High or Critical gate makes it High whatever the clause.
    3. A Medium gate sets a floor of Medium.

    Which criteria can raise a finding at all (their weight, and whether one
    met only in part counts) is decided before this, in assessment.
    """

    tier = normalize_tier(inherent_risk_tier)
    if gate in ("High", "Critical"):
        return "High"
    if gate == "Medium" and tier == "Low":
        return "Medium"
    return cast(RiskLevel, tier)


def defect_risk_level(defect_type: str) -> RiskLevel:
    """The risk level of a drafting defect: a correction, whatever its kind."""

    return "Low"


def rank(level: str) -> int:
    """Sort key: higher is more serious."""

    return _ORDER.get(level, 0)


def highest(levels: Iterable[RiskLevel]) -> RiskLevel:
    """The most serious of several levels; Low when there are none."""

    best: RiskLevel = "Low"
    for level in levels:
        if rank(level) > rank(best):
            best = level
    return best
