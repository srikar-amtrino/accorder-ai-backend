"""
Deterministic scorer for the general review pipeline.

The LLM answers closed criteria questions (Met / Partly met / Not met /
Unclear / Not applicable).  This module converts those answers into a
numeric gap score and a final risk level — the LLM never sees or decides
the risk level.

Public API
----------
score_group(answers, criteria)
    -> ScoringResult(gap_score, final_level, failed_answers)

is_risky(final_level)
    -> bool  (Medium or High → risky; Low → no suggestion unless fragment defect)

identify_risky_para_indices(answers)
    -> List[int]   (paragraph positions that contain evidence of failure)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

from src.config.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Penalty applied to each answer option when computing the gap score.
# "Not applicable" maps to None — that criterion is excluded from both
# numerator and denominator so genuinely absent topics don't inflate risk.
ANSWER_PENALTIES: Dict[str, Optional[float]] = {
    "Met":             0.0,
    "Partly met":      0.5,
    "Not met":         1.0,
    "Unclear":         0.5,
    "Not applicable":  None,
}

# Gap score thresholds → risk level.
# Critical is used internally to trigger the High gate; the output schema
# only allows Low / Medium / High so Critical is capped before returning.
_THRESHOLDS: List[Tuple[float, str]] = [
    (0.15, "Low"),
    (0.40, "Medium"),
    (0.75, "High"),
    (1.01, "High"),   # Critical → capped to High in output
]

# Gate level ordering for max() comparisons.
_GATE_ORDER: Dict[str, int] = {
    "None":     0,
    "Medium":   1,
    "High":     2,
    "Critical": 3,
}

# Map gate level strings to risk level strings (Critical capped to High).
_GATE_TO_LEVEL: Dict[str, str] = {
    "None":     "Low",
    "Medium":   "Medium",
    "High":     "High",
    "Critical": "High",
}

RiskLevel = Literal["Low", "Medium", "High"]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AnswerRecord:
    """A single criterion answer as returned by the extraction LLM call."""

    criterion_id: str
    answer: str                       # one of the five answer strings
    quote: Optional[str] = None       # verbatim evidence from clause text
    source_para_id: Optional[str] = None   # paraindetifier of the source para
    source_para_index: Optional[int] = None  # resolved integer position


@dataclass
class ScoringResult:
    """Output of score_group()."""

    gap_score: float
    final_level: RiskLevel
    failed_answers: List[AnswerRecord] = field(default_factory=list)
    # para indices that have at least one failed criterion's evidence
    risky_para_indices: List[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_criteria_index(criteria: List[dict]) -> Dict[str, dict]:
    """Index criteria by criterion_id for O(1) lookup."""
    return {c["criterion_id"]: c for c in criteria}


def _penalty(answer: str) -> Optional[float]:
    return ANSWER_PENALTIES.get(answer, 0.5)   # unknown → treat as Unclear


def _score_to_level(
    gap_score: float,
    failed_answers: Optional[List[AnswerRecord]] = None,
    criteria_index: Optional[Dict[str, dict]] = None,
) -> RiskLevel:
    """Map numeric gap score to a deterministic risk level with threshold boundary stabilization.

    Uses objective taxonomy criteria weights (Core / Major / Minor) in the ±0.02
    boundary zones around 0.15 and 0.40 to eliminate machine noise without altering
    legal standards.
    """
    failed = failed_answers or []
    index = criteria_index or {}

    # Check for substantive failure floor first:
    # Any Core failure (Not met / Partly met) or 2+ Major failures cannot be Low risk!
    has_core_failure = any(
        index.get(a.criterion_id, {}).get("weight") == "Core"
        for a in failed
    )
    major_failure_count = sum(
        1 for a in failed
        if index.get(a.criterion_id, {}).get("weight") == "Major"
    )
    is_substantive_risk = has_core_failure or major_failure_count >= 2

    # 1. Solid High
    if gap_score >= 0.42:
        return "High"

    # 2. Medium/High Boundary Zone: [0.38, 0.42)
    if gap_score >= 0.38:
        # Resolve deterministically by legal severity: Core failure or 2+ Major failures = High
        if is_substantive_risk:
            return "High"
        return "Medium"

    # 3. Solid Medium: [0.17, 0.38)
    if gap_score >= 0.17:
        return "Medium"

    # 4. Low/Medium Boundary Zone: [0.13, 0.17)
    if gap_score >= 0.13:
        # Core or Major failure = Medium; only minor/vague failures = Low
        has_substantive_failure = is_substantive_risk or any(
            index.get(a.criterion_id, {}).get("weight") == "Major"
            for a in failed
        )
        return "Medium" if has_substantive_failure else "Low"

    # 5. Low Score (< 0.13): If a Core criterion or 2+ Major criteria failed, enforce Medium floor to prevent denominator dilution in large taxonomies
    if is_substantive_risk:
        return "Medium"

    return "Low"


def _max_level(a: str, b: str) -> str:
    """Return the higher of two risk/gate level strings."""
    order = {"Low": 0, "Medium": 1, "High": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_gap_score(answers: List[AnswerRecord], criteria: List[dict]) -> float:
    """Weighted gap score in [0, 1].

    gap_score = Σ(weight_value × penalty) / Σ(weight_value)

    Criteria answered "Not applicable" are excluded from both numerator
    and denominator.  Returns 0.0 when every criterion is N/A (nothing
    to score → clean clause).
    """
    index = _build_criteria_index(criteria)
    numerator = 0.0
    denominator = 0.0

    for ans in answers:
        crit = index.get(ans.criterion_id)
        if crit is None:
            continue
        pen = _penalty(ans.answer)
        if pen is None:
            continue    # Not applicable — skip
        weight = float(crit.get("weight_value", 1))
        numerator += weight * pen
        denominator += weight

    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def apply_gates(answers: List[AnswerRecord], criteria: List[dict]) -> Optional[str]:
    """Return the highest gate level triggered by any "Not met" answer.

    Gates only fire on "Not met", never on "Partly met" or "Unclear".
    Returns None when no gate is triggered.
    """
    index = _build_criteria_index(criteria)
    max_gate_rank = 0
    max_gate_name: Optional[str] = None

    for ans in answers:
        if ans.answer != "Not met":
            continue
        crit = index.get(ans.criterion_id)
        if crit is None:
            continue
        gate = crit.get("gate_if_not_met", "None")
        rank = _GATE_ORDER.get(gate, 0)
        if rank > max_gate_rank:
            max_gate_rank = rank
            max_gate_name = gate

    return max_gate_name if max_gate_rank > 0 else None


def score_group(
    answers: List[AnswerRecord],
    criteria: List[dict],
) -> ScoringResult:
    """Compute the final risk level for a clause group.

    1. Gap score → score-based level.
    2. Gate check → gate floor (only from "Not met" answers).
    3. Final level = max(score level, gate floor).
    4. Collect failed answers and risky paragraph indices.

    The LLM never sees or influences the final_level returned here.
    """
    # Collect failed answers (anything that contributes a penalty)
    failed = [
        ans for ans in answers
        if ans.answer not in ("Met", "Not applicable")
    ]
    index = _build_criteria_index(criteria)

    gap_score = compute_gap_score(answers, criteria)
    score_level = _score_to_level(gap_score, failed_answers=failed, criteria_index=index)

    gate = apply_gates(answers, criteria)
    gate_level = _GATE_TO_LEVEL.get(gate or "None", "Low")
    final_level = _max_level(score_level, gate_level)

    # Collect paragraph indices that carry evidence of failure
    risky_indices: List[int] = []
    seen: set = set()
    for ans in failed:
        if ans.source_para_index is not None and ans.source_para_index not in seen:
            seen.add(ans.source_para_index)
            risky_indices.append(ans.source_para_index)

    logger.debug(
        "Scorer: gap_score=%.3f score_level=%s gate=%s final_level=%s "
        "failed=%d/%d risky_paras=%s",
        gap_score, score_level, gate, final_level,
        len(failed), len(answers), risky_indices,
    )

    return ScoringResult(
        gap_score=gap_score,
        final_level=final_level,  # type: ignore[arg-type]
        failed_answers=failed,
        risky_para_indices=sorted(risky_indices),
    )


def is_risky(final_level: str, has_failed_answers: bool = False) -> bool:
    """True when a clause group has findings worth surfacing to the user.

    Medium and High groups are always surfaced. Low-risk groups are surfaced
    if they contain at least one identified failed criterion (e.g. minor gap,
    ambiguity, or missing ancillary provision). Clean clauses (zero failed criteria)
    return False.
    """
    if final_level in ("Medium", "High"):
        return True
    return bool(has_failed_answers)


def identify_risky_para_indices(answers: List[AnswerRecord]) -> List[int]:
    """Indices of paragraphs that contain evidence of at least one failure.

    Used by the orchestrator to know which paragraphs need an explanation
    call after scoring.  Paragraphs whose answers are all Met / N/A produce
    no Suggestion and are never included in the output.
    """
    seen: set = set()
    result: List[int] = []
    for ans in answers:
        if ans.answer in ("Met", "Not applicable"):
            continue
        if ans.source_para_index is not None and ans.source_para_index not in seen:
            seen.add(ans.source_para_index)
            result.append(ans.source_para_index)
    return sorted(result)

