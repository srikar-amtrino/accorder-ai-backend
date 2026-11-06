"""Deterministic risk matrix for the general review.

The model reports facts about a clause — which of its policy's unacceptable
triggers it contains, whether it falls short of the acceptable standard,
whether it is one-sided against the client, and how severe the exposure is.
This module turns those facts and the clause type's inherent risk tier into
the High / Medium / Low risk level in plain code, so the same facts always
give the same level.
"""

import re
from typing import Iterable, List, Literal, Optional

from src.services.general_review.taxonomy_gates import is_balance_trigger

RiskLevel = Literal["High", "Medium", "Low"]

# A policy trigger worded as a burden on the client or a benefit to the other
# side ("restriction preventing client from...", "power of attorney granted to
# counterparty", "one-sided ... protecting only counterparty's information").
_AGAINST_CLIENT_TRIGGER_RE = re.compile(
    r"\bone-sided\b|\b(?:preventing|restricting|binding|prohibits?|against)\s+(?:the\s+)?client\b|\b(?:granted\s+to|allowing|protecting\s+only)\s+(?:the\s+)?(?:counterparty|vendor)\b",
    re.IGNORECASE,
)

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


def apply_gate_floor(level: RiskLevel, gate: Optional[str]) -> RiskLevel:
    """Raise a computed risk level to the floor a failed taxonomy gate sets.

    A High (or Critical) gate makes the finding High; a Medium gate makes it
    at least Medium. No gate leaves the level as computed.
    """

    if gate in ("High", "Critical"):
        return "High"
    if gate == "Medium" and level == "Low":
        return "Medium"
    return level


def drop_direction_contradicted_flags(triggered_unacceptable_flags: Iterable[str]) -> List[str]:
    """Remove the triggers a clause that runs in the client's favor cannot have set off.

    Call this only for a clause that binds the counterparty alone and gives
    it no right of its own. Such a clause puts no restriction on the client
    and hands nothing to the other side, so a Balance criterion of the
    taxonomy (ASG-01, AMD-02, LOL-02, ...), or a trigger worded as a burden on
    the client, is the model reading the parties backwards and carries no weight.
    """

    return [flag for flag in triggered_unacceptable_flags if flag and not _AGAINST_CLIENT_TRIGGER_RE.search(flag) and not is_balance_trigger(flag)]


def compute_deterministic_risk_level(
    inherent_risk_tier: str,
    triggered_unacceptable_flags: Iterable[str],
    falls_short_of_acceptable_standard: bool,
    is_materially_one_sided_against_client: bool,
    severity_category: str,
    clause_favors_client: bool = False,
) -> RiskLevel:
    """Compute the final risk level from the model's structured findings and the clause type's inherent tier.

    Directionality rule: when the clause is one-sided in the client's favor
    (clause_favors_client), it cannot be one-sided against the client, so that
    fact is false whatever the model reported, and triggers worded as a
    burden on the client are discarded.

    1. Any unacceptable trigger: High on a High-tier clause or with severe
       exposure, otherwise Medium.
    2. Short of the acceptable standard without a trigger: High on a High-tier
       clause that is one-sided or severe; Medium on a High/Medium-tier clause
       unless the issue is mere drafting hygiene; otherwise Low.
    3. Anything else is a Low drafting/hygiene point.
    """

    tier = _normalize_tier(inherent_risk_tier)
    flags = [flag for flag in triggered_unacceptable_flags if flag]
    if clause_favors_client:
        is_materially_one_sided_against_client = False
        flags = drop_direction_contradicted_flags(flags)

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
