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

# In client advocacy mode, partial coverage and ambiguity leave the client
# materially exposed, so higher deficiency factors are applied deterministically.
ANSWER_PENALTIES_ADVOCACY: Dict[str, Optional[float]] = {
    "Met":             0.0,
    "Partly met":      0.70,
    "Not met":         1.0,
    "Unclear":         0.65,
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
    # Advocacy comparison metadata
    gap_score_neutral: Optional[float] = None
    gap_score_advocacy: Optional[float] = None
    level_neutral: Optional[str] = None
    level_advocacy: Optional[str] = None
    review_mode: str = "neutral"


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

def is_client_favorable_asymmetry(
    ans: AnswerRecord,
    crit: dict,
    client_defined_term: Optional[str] = None,
    counterparty_defined_term: Optional[str] = None,
) -> bool:
    """Check if a failed answer actually reflects a commercial asymmetry in favor of the client.

    If a clause gives the client a unilateral right or discretionary power, or imposes a sole
    restriction on the counterparty, tests for 'mutuality' or 'balance' may be answered
    'Not met' or 'Partly met'. In advocacy mode, this is an asset for the client and should
    not be penalized.
    """
    if not client_defined_term:
        return False

    q_text = (crit.get("question") or crit.get("question_for_llm") or crit.get("criterion", "")).lower()
    cat = (crit.get("issue_category") or "").lower()
    is_balance_criterion = any(
        kw in q_text or kw in cat
        for kw in ("balance", "mutual", "bilateral", "reciprocal", "one-sided", "symmetry")
    )
    if not is_balance_criterion:
        return False

    quote = (ans.quote or "").lower()
    client_term = client_defined_term.lower()
    cp_term = (counterparty_defined_term or "").lower()

    # Evidence shows client holds the unilateral right/power:
    client_right_patterns = (
        f"{client_term} may",
        f"{client_term} shall have the right",
        f"{client_term} reserves the right",
        f"at {client_term}'s sole discretion",
        f"in {client_term}'s sole discretion",
        f"at {client_term}'s option",
        f"to {client_term}'s satisfaction",
    )
    if any(p in quote for p in client_right_patterns):
        return True

    # Evidence shows burden/restriction is exclusively on counterparty:
    if cp_term:
        cp_burden_patterns = (
            f"{cp_term} shall not",
            f"{cp_term} represents",
            f"{cp_term} warrants",
            f"{cp_term} agrees to indemnify",
        )
        if any(p in quote for p in cp_burden_patterns) and f"{client_term} shall" not in quote:
            return True

    return False


def compute_gap_score(
    answers: List[AnswerRecord],
    criteria: List[dict],
    review_mode: str = "neutral",
    client_defined_term: Optional[str] = None,
    counterparty_defined_term: Optional[str] = None,
) -> float:
    """Weighted gap score in [0, 1].

    gap_score = Σ(weight_value × penalty) / Σ(weight_value)

    Criteria answered "Not applicable" are excluded from both numerator
    and denominator.  Returns 0.0 when every criterion is N/A (nothing
    to score → clean clause).
    """
    index = _build_criteria_index(criteria)
    penalties = ANSWER_PENALTIES_ADVOCACY if review_mode == "advocacy" else ANSWER_PENALTIES
    numerator = 0.0
    denominator = 0.0

    for ans in answers:
        crit = index.get(ans.criterion_id)
        if crit is None:
            continue

        # In advocacy mode, do not penalize asymmetries that favor our client
        if review_mode == "advocacy" and is_client_favorable_asymmetry(
            ans, crit, client_defined_term, counterparty_defined_term
        ):
            continue

        pen = penalties.get(ans.answer, 0.5)
        if pen is None:
            continue    # Not applicable — skip
        weight = float(crit.get("weight_value", 1))
        numerator += weight * pen
        denominator += weight

    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def apply_gates(
    answers: List[AnswerRecord],
    criteria: List[dict],
    client_defined_term: Optional[str] = None,
    counterparty_defined_term: Optional[str] = None,
) -> Optional[str]:
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

        # Skip gate if this "Not met" is a client-favorable asymmetry
        if is_client_favorable_asymmetry(ans, crit, client_defined_term, counterparty_defined_term):
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
    review_mode: str = "neutral",
    client_defined_term: Optional[str] = None,
    counterparty_defined_term: Optional[str] = None,
) -> ScoringResult:
    """Compute the final risk level for a clause group.

    1. Gap score → score-based level (computed under both neutral and advocacy lenses).
    2. Gate check → gate floor (only from "Not met" answers).
    3. Final level = max(score level, gate floor).
    4. Collect failed answers and risky paragraph indices.

    The LLM never sees or influences the final_level returned here.
    """
    index = _build_criteria_index(criteria)

    # Collect failed answers (anything that contributes a penalty)
    failed = [
        ans for ans in answers
        if ans.answer not in ("Met", "Not applicable")
        and not (
            review_mode == "advocacy"
            and is_client_favorable_asymmetry(
                ans, index.get(ans.criterion_id, {}), client_defined_term, counterparty_defined_term
            )
        )
    ]

    # Compute both neutral and advocacy gap scores and levels for comparative audit
    gap_score_neutral = compute_gap_score(answers, criteria, review_mode="neutral")
    score_level_neutral = _score_to_level(gap_score_neutral, failed_answers=failed, criteria_index=index)

    gap_score_advocacy = compute_gap_score(
        answers, criteria, review_mode="advocacy",
        client_defined_term=client_defined_term,
        counterparty_defined_term=counterparty_defined_term,
    )
    score_level_advocacy = _score_to_level(gap_score_advocacy, failed_answers=failed, criteria_index=index)

    gate = apply_gates(
        answers, criteria,
        client_defined_term=client_defined_term if review_mode == "advocacy" else None,
        counterparty_defined_term=counterparty_defined_term if review_mode == "advocacy" else None,
    )
    gate_level = _GATE_TO_LEVEL.get(gate or "None", "Low")

    final_level_neutral = _max_level(score_level_neutral, gate_level)
    final_level_advocacy = _max_level(score_level_advocacy, gate_level)

    if review_mode == "advocacy":
        gap_score = gap_score_advocacy
        final_level = final_level_advocacy
    else:
        gap_score = gap_score_neutral
        final_level = final_level_neutral

    # Collect paragraph indices that carry evidence of failure
    risky_indices: List[int] = []
    seen: set = set()
    for ans in failed:
        if ans.source_para_index is not None and ans.source_para_index not in seen:
            seen.add(ans.source_para_index)
            risky_indices.append(ans.source_para_index)

    logger.debug(
        "Scorer: mode=%s gap_score=%.3f (neu=%.3f, adv=%.3f) final_level=%s (neu=%s, adv=%s) gate=%s "
        "failed=%d/%d risky_paras=%s",
        review_mode, gap_score, gap_score_neutral, gap_score_advocacy,
        final_level, final_level_neutral, final_level_advocacy, gate,
        len(failed), len(answers), risky_indices,
    )

    return ScoringResult(
        gap_score=gap_score,
        final_level=final_level,  # type: ignore[arg-type]
        failed_answers=failed,
        risky_para_indices=sorted(risky_indices),
        gap_score_neutral=gap_score_neutral,
        gap_score_advocacy=gap_score_advocacy,
        level_neutral=final_level_neutral,
        level_advocacy=final_level_advocacy,
        review_mode=review_mode,
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

