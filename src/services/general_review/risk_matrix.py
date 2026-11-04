"""Deterministic risk matrix for the general review.

The model reports facts about a clause — which of its policy's unacceptable
triggers it contains, whether it falls short of the acceptable standard,
whether it is one-sided against the client, and how severe the exposure is.
This module turns those facts and the clause type's inherent risk tier into
the High / Medium / Low risk level in plain code, so the same facts always
give the same level.
"""

from typing import Iterable, Literal

RiskLevel = Literal["High", "Medium", "Low"]

UNCAPPED_OR_SEVERE = "uncapped_or_severe_exposure"
MATERIAL_DEVIATION = "material_commercial_deviation"
MINOR_HYGIENE = "minor_drafting_hygiene"
SEVERITY_CATEGORIES = (UNCAPPED_OR_SEVERE, MATERIAL_DEVIATION, MINOR_HYGIENE)

_TIERS = ("High", "Medium", "Low")


def _normalize_tier(tier: str) -> str:
    folded = (tier or "").strip().capitalize()
    return folded if folded in _TIERS else "Medium"


def tier_for_unmapped(severity_category: str) -> str:
    """Inherent tier stand-in for a clause that mapped to no policy, read off the reported severity."""

    if severity_category == UNCAPPED_OR_SEVERE:
        return "High"
    if severity_category == MINOR_HYGIENE:
        return "Low"
    return "Medium"


def compute_deterministic_risk_level(
    inherent_risk_tier: str,
    triggered_unacceptable_flags: Iterable[str],
    falls_short_of_acceptable_standard: bool,
    is_materially_one_sided_against_client: bool,
    severity_category: str,
) -> RiskLevel:
    """Compute the final risk level from the model's structured findings and the clause type's inherent tier.

    1. Any unacceptable trigger: High on a High-tier clause or with severe
       exposure, otherwise Medium.
    2. Short of the acceptable standard without a trigger: High on a High-tier
       clause that is one-sided or severe; Medium on a High/Medium-tier clause
       unless the issue is mere drafting hygiene; otherwise Low.
    3. Anything else is a Low drafting/hygiene point.
    """

    tier = _normalize_tier(inherent_risk_tier)
    flags = [flag for flag in triggered_unacceptable_flags if flag]

    if flags:
        if tier == "High" or severity_category == UNCAPPED_OR_SEVERE:
            return "High"
        return "Medium"

    if falls_short_of_acceptable_standard:
        if tier == "High" and (is_materially_one_sided_against_client or severity_category == UNCAPPED_OR_SEVERE):
            return "High"
        if tier in ("High", "Medium") and severity_category != MINOR_HYGIENE:
            return "Medium"
        return "Low"

    return "Low"
