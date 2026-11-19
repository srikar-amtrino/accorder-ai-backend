"""Cross-Clause Resolution Ledger (CCRL) — Core Data Structures.

DESIGN SPECIFICATION (Locked):
------------------------------
1. Q1: Option C (Atomic Blob) — Stores the entire suggested_fix blob associated with
   an unmet criterion. If another clause satisfies the criterion, that entire blob
   is cleanly removed from the earlier clause's suggested_fix.
2. Q2: Medium and High only — Low-risk clauses are never registered in the CCRL.
3. Concurrency: Built sequentially after parallel Pass 1.
4. Status Model (Legal Team Prompt v2): FULLY_RESOLVED, PARTIALLY_RESOLVED,
   NOT_RESOLVED, CONFLICTING_PROVISIONS, CONTEXT_UNAVAILABLE.
   Only FULLY_RESOLVED triggers fix-blob removal and closes the fragment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Resolution status constants (legal team prompt v2)
STATUS_FULLY_RESOLVED = "FULLY_RESOLVED"
STATUS_PARTIALLY_RESOLVED = "PARTIALLY_RESOLVED"
STATUS_NOT_RESOLVED = "NOT_RESOLVED"
STATUS_CONFLICTING = "CONFLICTING_PROVISIONS"
STATUS_CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"

# Statuses that fully close a criterion and allow fix-blob removal
CLOSED_STATUSES = {STATUS_FULLY_RESOLVED}

# Statuses that keep the criterion open but attach partial audit info
OPEN_STATUSES = {STATUS_PARTIALLY_RESOLVED, STATUS_NOT_RESOLVED,
                 STATUS_CONFLICTING, STATUS_CONTEXT_UNAVAILABLE}


@dataclass
class CCRLCriterionFragment:
    """One unmet/partly-met criterion and the fix blob it generated."""

    criterion_id: str
    criterion_question: str
    answer: str                        # "Not met" | "Partly met" | "Unclear"
    fix_blob: str                      # The suggested_fix text associated with this finding
    resolved: bool = False
    resolved_by_clause_id: Optional[str] = None
    resolved_by_clause_title: Optional[str] = None
    evidence_quote: Optional[str] = None

    # --- Legal-team prompt v2 enrichments (populated on partial/conflicting) ---
    resolution_status: Optional[str] = None          # one of the STATUS_* constants
    relationship: List[str] = field(default_factory=list)  # e.g. ["SUPPLEMENTS"]
    reason: Optional[str] = None                     # model's legal reasoning
    remaining_deficiency: Optional[str] = None       # only for PARTIALLY_RESOLVED
    context_limitation: Optional[str] = None         # only for CONTEXT_UNAVAILABLE
    resolved_by_clause_ids: List[str] = field(default_factory=list)  # explicit list from model


@dataclass
class CCRLEntry:
    """The CCRL record for a single risky (Medium or High) clause."""

    clause_id: str                              # e.g. "clause_3" or "unit_3"
    clause_title: str                           # e.g. "Term and Termination"
    para_identifier: str                        # Target paragraph identifier
    risk_level: str                             # "High" | "Medium"
    clause_summary: str                         # Summary of what is missing or reason
    pending_fragments: List[CCRLCriterionFragment] = field(default_factory=list)
    fully_resolved: bool = False

    # Back-reference to the live Suggestion object so we can mutate it in place
    # after reconciliation. NOT serialised into audit traces directly.
    _suggestion_ref: Any = field(default=None, repr=False, compare=False)

    def any_pending(self) -> bool:
        """Return True if at least one criterion fragment is still unresolved."""
        return any(not f.resolved for f in self.pending_fragments)

    def pending_criterion_ids(self) -> List[str]:
        """Return list of unresolved criterion IDs."""
        return [f.criterion_id for f in self.pending_fragments if not f.resolved]

    def mark_resolved(
        self,
        criterion_id: str,
        resolved_by_id: str,
        resolved_by_title: str,
        evidence_quote: str,
        status: str = STATUS_FULLY_RESOLVED,
        relationship: Optional[List[str]] = None,
        reason: Optional[str] = None,
        remaining_deficiency: Optional[str] = None,
        context_limitation: Optional[str] = None,
        resolved_by_clause_ids: Optional[List[str]] = None,
    ) -> bool:
        """Mark a criterion as fully resolved (FULLY_RESOLVED only).

        For PARTIALLY_RESOLVED / CONFLICTING / CONTEXT_UNAVAILABLE use mark_partial().
        Returns True if the fragment was found and updated.
        """
        for frag in self.pending_fragments:
            if frag.criterion_id == criterion_id and not frag.resolved:
                frag.resolved = True
                frag.resolved_by_clause_id = resolved_by_id
                frag.resolved_by_clause_title = resolved_by_title
                frag.evidence_quote = evidence_quote
                frag.resolution_status = status
                frag.relationship = relationship or []
                frag.reason = reason
                frag.remaining_deficiency = remaining_deficiency
                frag.context_limitation = context_limitation
                frag.resolved_by_clause_ids = resolved_by_clause_ids or [resolved_by_id]
                self.fully_resolved = all(f.resolved for f in self.pending_fragments)
                return True
        return False

    def mark_partial(
        self,
        criterion_id: str,
        resolved_by_id: str,
        resolved_by_title: str,
        evidence_quote: str,
        status: str = STATUS_PARTIALLY_RESOLVED,
        relationship: Optional[List[str]] = None,
        reason: Optional[str] = None,
        remaining_deficiency: Optional[str] = None,
        context_limitation: Optional[str] = None,
        resolved_by_clause_ids: Optional[List[str]] = None,
    ) -> bool:
        """Attach partial resolution data to a criterion WITHOUT closing it.

        The fragment stays open (resolved=False) so it remains in the CCRL.
        Used for PARTIALLY_RESOLVED, CONFLICTING_PROVISIONS, CONTEXT_UNAVAILABLE.
        Returns True if the fragment was found.
        """
        for frag in self.pending_fragments:
            if frag.criterion_id == criterion_id and not frag.resolved:
                # Keep resolved=False — criterion stays open
                frag.resolved_by_clause_id = resolved_by_id
                frag.resolved_by_clause_title = resolved_by_title
                frag.evidence_quote = evidence_quote
                frag.resolution_status = status
                frag.relationship = relationship or []
                frag.reason = reason
                frag.remaining_deficiency = remaining_deficiency
                frag.context_limitation = context_limitation
                frag.resolved_by_clause_ids = resolved_by_clause_ids or [resolved_by_id]
                return True
        return False


class CCRLState:
    """In-memory state machine holding all registered risky clause entries for a document."""

    def __init__(self) -> None:
        self._entries: List[CCRLEntry] = []

    def register(self, entry: CCRLEntry) -> None:
        """Add a new risky clause entry (Medium or High only)."""
        if entry.risk_level in ("Medium", "High") and entry.pending_fragments:
            self._entries.append(entry)

    def pending_entries(self) -> List[CCRLEntry]:
        """Return all entries that still have unresolved criteria."""
        return [e for e in self._entries if e.any_pending()]

    def all_entries(self) -> List[CCRLEntry]:
        """Return all registered entries."""
        return list(self._entries)

    def snapshot_for_reconciler(self, exclude_clause_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Serialise pending entries into the JSON structure fed to the reconciliation model.

        v2: Includes resolution_status of any prior partial pass for context continuity.
        """
        result = []
        for entry in self.pending_entries():
            if exclude_clause_id and entry.clause_id == exclude_clause_id:
                continue
            criteria_list = []
            for f in entry.pending_fragments:
                if f.resolved:
                    continue
                crit_item: Dict[str, Any] = {
                    "criterion_id": f.criterion_id,
                    "question": f.criterion_question,
                    "answer_so_far": f.answer,
                }
                # Include partial resolution context so the model knows prior pass status
                if f.resolution_status and f.resolution_status != STATUS_FULLY_RESOLVED:
                    crit_item["prior_pass_status"] = f.resolution_status
                if f.remaining_deficiency:
                    crit_item["remaining_deficiency_from_prior_pass"] = f.remaining_deficiency
                criteria_list.append(crit_item)
            if criteria_list:
                result.append(
                    {
                        "ledger_clause_id": entry.clause_id,
                        "ledger_clause_title": entry.clause_title,
                        "para_identifier": entry.para_identifier,
                        "missing_summary": entry.clause_summary,
                        "pending_criteria": criteria_list,
                    }
                )
        return result

    def __len__(self) -> int:
        return len(self._entries)


def remove_fix_blob(original_fix: str, blob_to_remove: str) -> str:
    """Cleanly remove a fix blob or its representative sentences from a suggested_fix string.

    Never destroys the surrounding text or leaves orphan punctuation.
    """
    if not blob_to_remove or not original_fix:
        return original_fix

    # 1. Exact match removal
    if blob_to_remove in original_fix:
        cleaned = original_fix.replace(blob_to_remove, "")
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # 2. Line-by-line / sentence-level match for slightly formatted differences
    blob_lines = [line.strip() for line in blob_to_remove.splitlines() if len(line.strip()) > 15]
    result = original_fix
    for line in blob_lines:
        if line in result:
            result = result.replace(line, "")

    # Clean double spaces or double blank lines
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
