"""CCRL Reconciler — Secondary pass resolving cross-clause redundancy.

EXECUTION MODEL
---------------
1. Runs sequentially after Pass 1 (asyncio.gather).
2. Uses Bedrock model specified by CCRL_MODEL_ID (currently Sonnet 4.6 as requested;
   can be swapped to Haiku or any other model by changing CCRL_MODEL_ID).
3. Produces granular audit data for every scanned clause (Q4).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.services.reconciliation.ccrl import CCRLEntry, CCRLState, remove_fix_blob

logger = get_logger(__name__)

# Primary switch: currently Sonnet as requested for this development phase;
# swap to Haiku or any other ARN at deployment.
CCRL_MODEL_ID: Optional[str] = None  # None = use settings.bedrock_model_id

_RECONCILE_SYSTEM = """You are a legal document cross-reference auditor and precision reconciler.
You are given:
1. The full text of a current contract clause.
2. A list of unresolved criteria and missing obligations from other clauses in the same agreement.

Your job is to determine whether the current clause text already expressly contains language that satisfies any of the pending criteria.

A criterion is "satisfied" only if the current clause explicitly provides the substantive right, protection, obligation, or procedure in clear, legally enforceable language. General, implied, or tangential text does NOT count.

Return ONLY a valid JSON object matching this schema:
{
  "resolved": [
    {
      "ledger_clause_id": "<exact ledger_clause_id from input>",
      "criterion_id": "<exact criterion_id from input>",
      "evidence_quote": "<exact verbatim quote from current clause text that satisfies it>"
    }
  ]
}

If no pending criteria are satisfied by the current clause, return:
{ "resolved": [] }
"""


class CCRLReconciler:
    """Performs the sequential reconciliation pass across all clauses."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.region = self.settings.aws_region or "us-west-1"
        self.model_id = CCRL_MODEL_ID or self.settings.bedrock_model_id or "anthropic.claude-3-5-sonnet-20241022-v2:0"

        try:
            if self.settings.env == "development":
                self.client = boto3.client(
                    "bedrock-runtime",
                    region_name=self.region,
                    aws_access_key_id=self.settings.aws_access_key_id,
                    aws_secret_access_key=self.settings.aws_secret_access_key,
                    config=Config(read_timeout=120, connect_timeout=10, retries={"max_attempts": 1}),
                )
            else:
                self.client = boto3.client(
                    "bedrock-runtime",
                    region_name=self.region,
                    config=Config(read_timeout=120, connect_timeout=10, retries={"max_attempts": 1}),
                )
            self._client_ok = True
        except Exception as exc:
            logger.warning("CCRL Reconciler: Bedrock client initialization failed: %s", exc)
            self._client_ok = False

    async def reconcile(
        self,
        ccrl_state: CCRLState,
        clause_scan_units: List[Dict[str, Any]],
        audit_trace_by_clause_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Run sequential reconciliation across all clause units.

        Args:
            ccrl_state: The populated CCRLState holding Medium/High risky clauses.
            clause_scan_units: List of dicts:
                [{
                   "clause_id": "unit_1" or "para_1",
                   "clause_title": "...",
                   "clause_text": "...",
                   "para_identifier": "..."
                }, ...]
            audit_trace_by_clause_id: Mapping from clause_id to the per-clause audit dict
                                      to attach `ccrl_scan` blocks.

        Returns:
            The top-level `reconciliation_pass` audit block.
        """
        start_time = time.time()
        resolved_log: List[Dict[str, Any]] = []
        total_blobs_removed = 0

        logger.info(
            "CCRL Reconciler: starting pass with model %s across %d scan units; %d entries in ledger.",
            self.model_id, len(clause_scan_units), len(ccrl_state)
        )

        for unit in clause_scan_units:
            cid = unit["clause_id"]
            ctitle = unit["clause_title"]
            ctext = unit["clause_text"]
            para_id = unit.get("para_identifier", "")
            scanned_at = datetime.datetime.now().isoformat()

            clause_audit = None
            if audit_trace_by_clause_id is not None:
                clause_audit = audit_trace_by_clause_id.get(cid)

            # Check if there are any pending entries in CCRL from OTHER clauses
            pending_snapshot = ccrl_state.snapshot_for_reconciler(exclude_clause_id=cid)

            if not pending_snapshot or not self._client_ok or not ctext.strip():
                # No entries to reconcile against
                if clause_audit is not None:
                    clause_audit["ccrl_scan"] = {
                        "scanned_at": scanned_at,
                        "secondary_model": self.model_id,
                        "pending_ccrl_entries_read": [],
                        "secondary_model_raw_response": {"resolved": []},
                        "secondary_model_latency_ms": 0,
                        "resolutions_applied": [],
                        "no_resolutions_reason": "no_pending_ccrl_entries" if not pending_snapshot else "client_unavailable",
                    }
                continue

            # Call the reconciliation model
            t_call = time.time()
            raw_response, resolved_items = await self._call_model(ctext, pending_snapshot)
            latency_ms = int((time.time() - t_call) * 1000)

            applied_resolutions: List[Dict[str, Any]] = []

            for res in resolved_items:
                ledger_cid = res.get("ledger_clause_id")
                crit_id = res.get("criterion_id")
                quote = res.get("evidence_quote", "")

                if not ledger_cid or not crit_id:
                    continue

                # Locate the CCRL entry
                target_entry = next((e for e in ccrl_state.all_entries() if e.clause_id == ledger_cid), None)
                if not target_entry:
                    continue

                # Find the target fragment
                target_frag = next((f for f in target_entry.pending_fragments if f.criterion_id == crit_id and not f.resolved), None)
                if not target_frag:
                    continue

                # Cleanly remove the fix blob from the target Suggestion
                sugg_ref = target_entry._suggestion_ref
                fix_before = sugg_ref.suggested_fix if sugg_ref and hasattr(sugg_ref, "suggested_fix") else target_frag.fix_blob
                fix_after = fix_before

                if sugg_ref and hasattr(sugg_ref, "suggested_fix"):
                    fix_after = remove_fix_blob(fix_before, target_frag.fix_blob)
                    if fix_after != fix_before:
                        sugg_ref.suggested_fix = fix_after
                        total_blobs_removed += 1
                        logger.info(
                            "CCRL: removed fix blob from %s (criterion %s satisfied by %s: %s)",
                            ledger_cid, crit_id, cid, ctitle
                        )

                # Mark fragment resolved in ledger
                target_entry.mark_resolved(
                    criterion_id=crit_id,
                    resolved_by_id=cid,
                    resolved_by_title=ctitle,
                    evidence_quote=quote,
                )

                resolution_record = {
                    "ledger_clause_id": ledger_cid,
                    "ledger_clause_title": target_entry.clause_title,
                    "criterion_id": crit_id,
                    "criterion_question": target_frag.criterion_question,
                    "resolved_by_clause_id": cid,
                    "resolved_by_clause_title": ctitle,
                    "evidence_quote": quote,
                    "fix_blob_removed": target_frag.fix_blob[:300],
                    "suggested_fix_before_reconciliation": fix_before,
                    "suggested_fix_after_reconciliation": fix_after,
                }
                applied_resolutions.append(resolution_record)
                resolved_log.append(resolution_record)

            # Record per-clause audit scan
            if clause_audit is not None:
                clause_audit["ccrl_scan"] = {
                    "scanned_at": scanned_at,
                    "secondary_model": self.model_id,
                    "pending_ccrl_entries_read": pending_snapshot,
                    "secondary_model_raw_response": raw_response,
                    "secondary_model_latency_ms": latency_ms,
                    "resolutions_applied": applied_resolutions,
                    "no_resolutions_reason": None if applied_resolutions else "no_matching_provisions_in_clause",
                }

        elapsed = time.time() - start_time

        # Compile pending entries remaining at the end
        pending_at_end = []
        for entry in ccrl_state.pending_entries():
            for frag in entry.pending_fragments:
                if not frag.resolved:
                    pending_at_end.append({
                        "ledger_clause_id": entry.clause_id,
                        "ledger_clause_title": entry.clause_title,
                        "criterion_id": frag.criterion_id,
                        "criterion_question": frag.criterion_question,
                        "answer": frag.answer,
                        "clause_summary": entry.clause_summary,
                    })

        reconciliation_summary = {
            "secondary_model": self.model_id,
            "total_ccrl_entries_registered": len(ccrl_state),
            "total_criteria_resolved": len(resolved_log),
            "total_fix_blobs_removed": total_blobs_removed,
            "reconciliation_elapsed_seconds": round(elapsed, 2),
            "resolved_entries": resolved_log,
            "pending_entries_at_end": pending_at_end,
        }

        logger.info(
            "CCRL Reconciler: finished in %.2fs. %d criteria resolved, %d fix blobs removed.",
            elapsed, len(resolved_log), total_blobs_removed
        )
        return reconciliation_summary

    async def _call_model(
        self,
        clause_text: str,
        pending_snapshot: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Invoke the reconciliation Bedrock model."""
        truncated_text = clause_text[:3000] if len(clause_text) > 3000 else clause_text
        user_message = (
            f"=== CURRENT CLAUSE TEXT ===\n{truncated_text}\n\n"
            f"=== PENDING CRITERIA FROM EARLIER CLAUSES ===\n"
            f"{json.dumps(pending_snapshot, indent=2)}\n\n"
            "Does the current clause text expressly satisfy any of these pending criteria? "
            "Respond strictly in JSON matching the schema."
        )

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "temperature": 0.0,
            "system": _RECONCILE_SYSTEM,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": user_message}]}
            ],
        }

        try:
            response = await asyncio.to_thread(
                self.client.invoke_model,
                modelId=self.model_id,
                body=json.dumps(native_request),
            )
            body = await asyncio.to_thread(response["body"].read)
            parsed = json.loads(body)
            content_blocks = parsed.get("content", [])
            raw_text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

            # Extract json
            data = {}
            try:
                data = json.loads(raw_text.strip())
            except json.JSONDecodeError:
                import re
                m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw_text)
                if m:
                    data = json.loads(m.group(1).strip())

            resolved_list = data.get("resolved", []) if isinstance(data, dict) else []
            return data, resolved_list
        except Exception as exc:
            logger.warning("CCRL Reconciler: LLM invocation failed: %s", exc)
            return {"error": str(exc), "resolved": []}, []
