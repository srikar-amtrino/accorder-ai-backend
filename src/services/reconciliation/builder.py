"""CCRL Builder — Constructs the initial CCRLState from Pass 1 parallel evaluations.

Only Medium and High risk clauses are registered (Q2).
Stores the atomic suggested_fix per criterion (Q1 Option C).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.config.logging import get_logger
from src.schemas.general_review import Suggestion
from src.services.reconciliation.ccrl import CCRLCriterionFragment, CCRLEntry, CCRLState

logger = get_logger(__name__)


def build_ccrl_state(
    suggestions: List[Suggestion],
    audit_trace_entries: Optional[List[Dict[str, Any]]] = None,
) -> CCRLState:
    """Build a CCRLState from the initial parallel evaluation suggestions and audit traces.

    Only Medium and High risk clauses are registered (Q2).
    """
    state = CCRLState()

    # Create mapping from para_identifier to Suggestion object for in-place mutation
    sugg_by_para: Dict[str, Suggestion] = {s.para_identifier: s for s in suggestions if s.para_identifier}

    if not audit_trace_entries:
        # Fallback if no audit trace: build entries directly from suggestions
        for idx, s in enumerate(suggestions):
            if s.risk_level not in ("Medium", "High"):
                continue
            entry = CCRLEntry(
                clause_id=f"clause_{idx + 1}",
                clause_title=s.clause or f"Clause {idx + 1}",
                para_identifier=s.para_identifier,
                risk_level=s.risk_level,
                clause_summary=s.reason[:300],
                pending_fragments=[
                    CCRLCriterionFragment(
                        criterion_id=f"CRIT_{idx + 1}",
                        criterion_question=s.reason[:150],
                        answer="Not met",
                        fix_blob=s.suggested_fix,
                    )
                ],
                _suggestion_ref=s,
            )
            state.register(entry)
        return state

    # When audit trace is available, we have exact criterion-level breakdown
    for item in audit_trace_entries:
        risk_level = item.get("conclusive_risk_level")
        if risk_level not in ("Medium", "High"):
            continue

        unit_id = item.get("clause_unit_id", 0)
        clause_id = f"unit_{unit_id}" if unit_id else item.get("para_identifier", "unknown")
        clause_title = item.get("clause_title", "Untitled Clause")
        para_id = item.get("para_identifier", "")

        # Find matching live Suggestion reference
        sugg_ref = sugg_by_para.get(para_id)
        current_fix = sugg_ref.suggested_fix if sugg_ref else item.get("explanation_call", {}).get("suggested_fix", "")
        reason = sugg_ref.reason if sugg_ref else item.get("explanation_call", {}).get("reason", "")

        failed_criteria = item.get("failed_criteria", []) or []
        fragments: List[CCRLCriterionFragment] = []

        for fc in failed_criteria:
            crit_id = fc.get("criterion_id", "")
            crit_q = fc.get("question", "") or fc.get("criterion", "")
            ans = fc.get("answer", "Not met")
            if not crit_id:
                continue

            fragments.append(
                CCRLCriterionFragment(
                    criterion_id=crit_id,
                    criterion_question=crit_q,
                    answer=ans,
                    fix_blob=current_fix,
                )
            )

        # If no explicit failed_criteria listed (e.g. fragment defect), make a fallback fragment
        if not fragments and current_fix:
            fragments.append(
                CCRLCriterionFragment(
                    criterion_id="DEFECT-01",
                    criterion_question=reason[:150] or "Clause defect",
                    answer="Not met",
                    fix_blob=current_fix,
                )
            )

        if fragments:
            entry = CCRLEntry(
                clause_id=clause_id,
                clause_title=clause_title,
                para_identifier=para_id,
                risk_level=risk_level,
                clause_summary=reason[:300] or clause_title,
                pending_fragments=fragments,
                _suggestion_ref=sugg_ref,
            )
            state.register(entry)

    logger.info("CCRL Builder: registered %d risky clauses (Medium/High) into ledger.", len(state))
    return state
