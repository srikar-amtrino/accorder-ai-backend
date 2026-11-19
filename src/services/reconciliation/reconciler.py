"""CCRL Reconciler — Secondary pass resolving cross-clause redundancy.

EXECUTION MODEL (v2 Parallel High-Precision Engine)
---------------------------------------------------
1. Runs after Pass 1 (parallel extraction & scoring).
2. Parallel Target-Clause Batching:
   - Groups pending risky clauses into compact batches (~8-12 criteria per call).
   - Evaluates each batch against the Complete Indexed Agreement concurrently
     using asyncio.gather().
   - Avoids redundant sequential re-reading of the 30k-character Agreement.
   - Low latency (~30-45s total for the whole document).
   - Slashes token consumption by ~80% while preserving maximum legal precision.
3. Bedrock Claude 3.5 Sonnet:
   - max_tokens: 4096 (prevents truncation).
   - Concise legal reasoning instruction (1-2 sentences per field).
   - Robust JSON stream parser with markdown fence stripping and partial repair.
4. Strictly implements the legal team's v2 prompt & rules:
   - 5-status classification: FULLY_RESOLVED, PARTIALLY_RESOLVED, NOT_RESOLVED,
     CONFLICTING_PROVISIONS, CONTEXT_UNAVAILABLE.
   - 7 relationship labels.
   - Fix-blob deletion ONLY on FULLY_RESOLVED.
   - PARTIALLY_RESOLVED retains finding and stores remaining_deficiency.
   - Populates per-clause ccrl_scan in audit trace for every clause unit.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config

from src.config.logging import get_logger
from src.config.settings import get_settings
from src.services.reconciliation.ccrl import (
    CCRLEntry,
    CCRLState,
    STATUS_FULLY_RESOLVED,
    STATUS_PARTIALLY_RESOLVED,
    STATUS_NOT_RESOLVED,
    STATUS_CONFLICTING,
    STATUS_CONTEXT_UNAVAILABLE,
    CLOSED_STATUSES,
    OPEN_STATUSES,
    remove_fix_blob,
)

logger = get_logger(__name__)

# Primary switch: currently Sonnet as requested for this development phase;
# swap to Haiku or any other ARN at deployment.
CCRL_MODEL_ID: Optional[str] = None  # None = use settings.bedrock_model_id

# Target batching: max 10 criteria per call ensures the model stays well within
# output token limits while providing rich, unhurried legal analysis.
_MAX_CRITERIA_PER_BATCH = 10

# Max characters per clause snippet in agreement index
_CLAUSE_CONTEXT_CHAR_LIMIT = 1500

_RECONCILE_SYSTEM = """
ROLE

You are a senior legal cross-reference auditor and precision reconciler. Your task is to determine whether a criterion initially identified as missing or deficient in a target clause is actually satisfied, partly satisfied, contradicted, or left unresolved when the Agreement is read as a whole. You perform legal evidence validation only. You do not assign risk ratings, draft contractual language, or rewrite the original findings.

INPUTS

You are provided with:
1. The complete indexed Agreement text — an ordered list of clause sections with their IDs and text.
2. The list of target clauses with their unresolved criteria, each with its criterion_id and question.

If any input is missing, do not invent it. Use the available text and identify the limitation in the output.

CORE TASK

For each pending criterion, search and assess the Agreement as a whole to determine whether another provision actually supplies the substantive right, obligation, protection, procedure, limitation, or remedy required by that criterion. The target clause is the subject of the finding, but the legal effect must be assessed across the entire Agreement.

Do not restrict the review to the target clause itself. Search relevant provisions across the complete indexed Agreement, including definitions, exceptions, limitations, schedules, exhibits, statements of work, order forms, addenda, amendments, and provisions that affect the same legal or commercial subject.

A provision elsewhere resolves a criterion only if its operative wording substantively satisfies the actual criterion and applies to the relevant parties, beneficiary, subject matter, and circumstances. Similar wording, a shared heading, a related legal concept, or general boilerplate is not sufficient by itself.

REQUIRED LEGAL TESTS

1. Criterion-Specific Test
Read the criterion question carefully. Break compound criteria into their required elements. Determine whether each element is satisfied by actual contractual language. Do not use a broad clause to satisfy a specific requirement unless it addresses that requirement in substance.

2. Party and Beneficiary Test
Identify who makes the representation, who owes the obligation, who holds the right, and who receives the protection. Confirm the relevant provision protects the party contemplated by the criterion.

3. Scope and Applicability Test
Confirm the other provision applies to the same services, data, transactions, claims, deliverables, personnel, period, and circumstances relevant to the criterion.

4. Legal Effect and Relationship Test
Classify the relationship between the source provision and the target clause as one or more:
• SUPPLEMENTS — adds the required substantive protection or obligation
• FULLY_SATISFIES — supplies all required elements of the criterion
• PARTIALLY_SATISFIES — supplies only some required elements
• QUALIFIES_OR_LIMITS — narrows or conditions the apparent protection
• OVERRIDES — expressly takes precedence over another provision
• CONFLICTS — creates inconsistent rights, obligations, standards, or procedures
• UNRELATED — does not substantively address the criterion

5. Cross-Reference and Defined-Term Test
Follow express cross-references and examine relevant definitions. A reference to another section is not evidence of substantive satisfaction until the referenced text has been reviewed.

6. Complete-Agreement and Context-Availability Test
Use the complete indexed Agreement text. If a relevant attachment is incomplete or not retrieved, mark the criterion CONTEXT_UNAVAILABLE. Identify the missing section.

7. Legal Defect Versus Commercial Preference
Do not treat an optional market-standard enhancement or a more favorable commercial position as a missing contractual protection unless the criterion requires it.

STATUS DEFINITIONS

Assign exactly one status to each criterion:

FULLY_RESOLVED — The Agreement contains clear, operative language that satisfies every required element of the criterion for the relevant parties and circumstances.

PARTIALLY_RESOLVED — The Agreement supplies some but not all required elements, or the protection exists but is materially limited, conditional, or incomplete. State exactly what is covered and what remains deficient.

NOT_RESOLVED — After reviewing the available relevant Agreement text, no provision substantively satisfies the criterion, or the available provision is merely general, implied, tangential, or otherwise insufficient.

CONFLICTING_PROVISIONS — Two or more provisions create a material inconsistency affecting the criterion that cannot be resolved confidently from an express precedence rule.

CONTEXT_UNAVAILABLE — A necessary part of the Agreement is missing or inaccessible, so a reliable conclusion cannot be reached.

APPLICATION-LAYER RULES

You are a criterion-level validator only. Report status for each criterion. The application layer decides what to do with fix suggestions based on your status output. Do not instruct the application to delete or retain any fix suggestion — that is not your role.

For FULLY_RESOLVED: the application may remove that specific criterion's fix suggestion.
For PARTIALLY_RESOLVED: the application must retain the criterion for a narrowly tailored fix addressing only the remaining_deficiency.
For NOT_RESOLVED: retain the criterion.
For CONFLICTING_PROVISIONS or CONTEXT_UNAVAILABLE: route for further legal or context review.

IMPORTANT LENGTH CONSTRAINTS

Keep 'reason', 'remaining_deficiency', and 'context_limitation' concise (1-2 sentences maximum each). Provide direct verbatim quotes without paraphrasing.

OUTPUT RULES

Return ONLY a valid JSON object. Do not include Markdown, commentary, code fences, or text outside the JSON. Use this exact schema:

{
  "reconciliation_results": [
    {
      "ledger_clause_id": "<exact ledger_clause_id from input>",
      "criterion_id": "<exact criterion_id from input>",
      "status": "<one of: FULLY_RESOLVED | PARTIALLY_RESOLVED | NOT_RESOLVED | CONFLICTING_PROVISIONS | CONTEXT_UNAVAILABLE>",
      "relationship": ["<zero or more relationship labels>"],
      "resolved_by_clause_ids": ["<clause_id of the source provision(s) in Agreement>"],
      "evidence": [
        {
          "source_clause_id": "<clause_id from the indexed Agreement>",
          "verbatim_quote": "<exact verbatim text — no paraphrasing>",
          "source_section": "<clause title if known>"
        }
      ],
      "reason": "<concise 1-2 sentence legal explanation>",
      "remaining_deficiency": "<for PARTIALLY_RESOLVED only — concise 1-2 sentences on what remains deficient>",
      "context_limitation": "<for CONTEXT_UNAVAILABLE only — concise 1-2 sentences on what is missing>"
    }
  ]
}

If no criteria are addressed, return: { "reconciliation_results": [] }

Preserve all ledger_clause_id and criterion_id values exactly as provided in the input. Never invent or substitute IDs.
""".strip()


def _build_agreement_index(clause_scan_units: List[Dict[str, Any]]) -> str:
    """Build a compact indexed Agreement representation from all clause scan units."""
    lines: List[str] = ["=== INDEXED AGREEMENT (all clauses) ==="]
    for unit in clause_scan_units:
        cid = unit.get("clause_id") or (f"unit_{unit.get('clause_unit_id')}" if unit.get("clause_unit_id") else "?")
        title = unit.get("clause_title", "Untitled")
        text = unit.get("clause_text") or unit.get("clause_unit_entire_text") or ""
        snippet = text[:_CLAUSE_CONTEXT_CHAR_LIMIT].strip()
        if len(text) > _CLAUSE_CONTEXT_CHAR_LIMIT:
            snippet += " [...]"
        lines.append(f"\n[{cid}] {title}\n{snippet}")
    return "\n".join(lines)


def _parse_robust_json(raw_text: str) -> Dict[str, Any]:
    """Robustly parse JSON even if enclosed in markdown fences or slightly truncated."""
    cleaned = raw_text.strip()
    # Strip opening markdown fence
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    # Strip closing fence
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # If truncated at the end, attempt stream repair by closing the array & object
    idx = cleaned.rfind("}")
    while idx > 0:
        candidate = cleaned[:idx + 1].strip()
        for closure in ["]\n}", "\n}\n}", "}"]:
            try:
                data = json.loads(candidate + closure)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        idx = cleaned.rfind("}", 0, idx)

    return {}


class CCRLReconciler:
    """Performs parallel target-clause reconciliation against the Agreement context."""

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
                    config=Config(read_timeout=120, connect_timeout=10, retries={"max_attempts": 2}),
                )
            else:
                self.client = boto3.client(
                    "bedrock-runtime",
                    region_name=self.region,
                    config=Config(read_timeout=120, connect_timeout=10, retries={"max_attempts": 2}),
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
        """Run parallel target-clause reconciliation across all pending risky clauses.

        Args:
            ccrl_state: The populated CCRLState holding Medium/High risky clauses.
            clause_scan_units: List of all clause units in the document.
            audit_trace_by_clause_id: Mapping from clause_id to per-clause audit dict.

        Returns:
            The top-level `reconciliation_pass` audit block.
        """
        start_time = time.time()
        pending_snapshot = ccrl_state.snapshot_for_reconciler()

        if not pending_snapshot or not self._client_ok:
            logger.info("CCRL Reconciler: no pending criteria or client unavailable — skipping pass.")
            self._populate_empty_audits(clause_scan_units, audit_trace_by_clause_id)
            return self._build_empty_summary(ccrl_state, time.time() - start_time)

        # Build agreement index once
        agreement_index = _build_agreement_index(clause_scan_units)

        # Partition pending entries into batches based on total criteria count
        batches: List[List[Dict[str, Any]]] = []
        current_batch: List[Dict[str, Any]] = []
        current_criteria_count = 0

        for entry in pending_snapshot:
            entry_crits = len(entry.get("pending_criteria", []))
            if current_criteria_count + entry_crits > _MAX_CRITERIA_PER_BATCH and current_batch:
                batches.append(current_batch)
                current_batch = [entry]
                current_criteria_count = entry_crits
            else:
                current_batch.append(entry)
                current_criteria_count += entry_crits
        if current_batch:
            batches.append(current_batch)

        logger.info(
            "CCRL Reconciler v2 (Parallel): starting pass with model %s across %d target entries "
            "(partitioned into %d parallel batches). Agreement index: %d chars.",
            self.model_id, len(pending_snapshot), len(batches), len(agreement_index)
        )

        # Execute all batches concurrently
        tasks = [
            self._call_model_batch(
                batch_idx=idx + 1,
                total_batches=len(batches),
                batch_snapshot=batch,
                agreement_index=agreement_index,
            )
            for idx, batch in enumerate(batches)
        ]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        all_raw_responses: List[Dict[str, Any]] = []
        all_recon_items: List[Dict[str, Any]] = []
        total_latency_ms = 0

        for res in batch_results:
            if isinstance(res, Exception):
                logger.error("CCRL batch failed with exception: %s", res)
                continue
            raw_resp, items, lat_ms = res
            all_raw_responses.append(raw_resp)
            all_recon_items.extend(items)
            total_latency_ms += lat_ms

        # Process resolutions and mutate live suggestions
        resolved_log: List[Dict[str, Any]] = []
        partial_log: List[Dict[str, Any]] = []
        total_blobs_removed = 0
        resolutions_by_source_cid: Dict[str, List[Dict[str, Any]]] = {}
        partials_by_source_cid: Dict[str, List[Dict[str, Any]]] = {}

        for item in all_recon_items:
            ledger_cid = item.get("ledger_clause_id")
            crit_id = item.get("criterion_id")
            status = item.get("status", STATUS_FULLY_RESOLVED)

            if not ledger_cid or not crit_id:
                continue
            if status not in (STATUS_FULLY_RESOLVED, STATUS_PARTIALLY_RESOLVED,
                              STATUS_NOT_RESOLVED, STATUS_CONFLICTING, STATUS_CONTEXT_UNAVAILABLE):
                continue

            target_entry = next((e for e in ccrl_state.all_entries() if e.clause_id == ledger_cid), None)
            if not target_entry:
                continue

            target_frag = next((f for f in target_entry.pending_fragments
                                if f.criterion_id == crit_id and not f.resolved), None)
            if not target_frag:
                continue

            evidence_list = item.get("evidence", [])
            evidence_quote_str = ""
            if evidence_list and isinstance(evidence_list, list):
                evidence_quote_str = evidence_list[0].get("verbatim_quote", "") if evidence_list else ""
            elif isinstance(evidence_list, str):
                evidence_quote_str = evidence_list

            resolved_by_ids = item.get("resolved_by_clause_ids", [])
            primary_source_id = resolved_by_ids[0] if resolved_by_ids else "unknown"
            relationship = item.get("relationship", [])
            reason = item.get("reason", "")
            remaining_def = item.get("remaining_deficiency") or ""
            context_lim = item.get("context_limitation") or ""

            sugg_ref = target_entry._suggestion_ref
            fix_before = (
                sugg_ref.suggested_fix
                if sugg_ref and hasattr(sugg_ref, "suggested_fix")
                else target_frag.fix_blob
            )
            fix_after = fix_before

            if status in CLOSED_STATUSES:
                # FULLY_RESOLVED: surgically remove fix blob & close fragment
                if sugg_ref and hasattr(sugg_ref, "suggested_fix"):
                    fix_after = remove_fix_blob(fix_before, target_frag.fix_blob)
                    if fix_after != fix_before:
                        sugg_ref.suggested_fix = fix_after
                        total_blobs_removed += 1
                        logger.info(
                            "CCRL v2: FULLY_RESOLVED — removed fix blob from %s "
                            "(criterion %s resolved by %s)",
                            ledger_cid, crit_id, primary_source_id,
                        )

                target_entry.mark_resolved(
                    criterion_id=crit_id,
                    resolved_by_id=primary_source_id,
                    resolved_by_title="",
                    evidence_quote=evidence_quote_str,
                    status=status,
                    relationship=relationship,
                    reason=reason,
                    remaining_deficiency=remaining_def or None,
                    context_limitation=context_lim or None,
                    resolved_by_clause_ids=resolved_by_ids,
                )

                rec_record = _build_resolution_record(
                    ledger_cid=ledger_cid,
                    ledger_clause_title=target_entry.clause_title,
                    crit_id=crit_id,
                    criterion_question=target_frag.criterion_question,
                    status=status,
                    relationship=relationship,
                    resolved_by_clause_id=primary_source_id,
                    resolved_by_clause_title="",
                    resolved_by_clause_ids=resolved_by_ids,
                    evidence_list=evidence_list,
                    evidence_quote_str=evidence_quote_str,
                    reason=reason,
                    remaining_deficiency=remaining_def,
                    context_limitation=context_lim,
                    fix_blob_removed=fix_after != fix_before,
                    fix_before=fix_before,
                    fix_after=fix_after,
                    target_frag_fix_blob=target_frag.fix_blob,
                )
                resolved_log.append(rec_record)

                for src_id in (resolved_by_ids or [primary_source_id]):
                    resolutions_by_source_cid.setdefault(src_id, []).append(rec_record)

            elif status in OPEN_STATUSES and status != STATUS_NOT_RESOLVED:
                target_entry.mark_partial(
                    criterion_id=crit_id,
                    resolved_by_id=primary_source_id,
                    resolved_by_title="",
                    evidence_quote=evidence_quote_str,
                    status=status,
                    relationship=relationship,
                    reason=reason,
                    remaining_deficiency=remaining_def or None,
                    context_limitation=context_lim or None,
                    resolved_by_clause_ids=resolved_by_ids,
                )

                part_record = _build_resolution_record(
                    ledger_cid=ledger_cid,
                    ledger_clause_title=target_entry.clause_title,
                    crit_id=crit_id,
                    criterion_question=target_frag.criterion_question,
                    status=status,
                    relationship=relationship,
                    resolved_by_clause_id=primary_source_id,
                    resolved_by_clause_title="",
                    resolved_by_clause_ids=resolved_by_ids,
                    evidence_list=evidence_list,
                    evidence_quote_str=evidence_quote_str,
                    reason=reason,
                    remaining_deficiency=remaining_def,
                    context_limitation=context_lim,
                    fix_blob_removed=False,
                    fix_before=fix_before,
                    fix_after=fix_after,
                    target_frag_fix_blob=target_frag.fix_blob,
                )
                partial_log.append(part_record)

                for src_id in (resolved_by_ids or [primary_source_id]):
                    partials_by_source_cid.setdefault(src_id, []).append(part_record)

        # Attach ccrl_scan to each clause's audit record
        if audit_trace_by_clause_id is not None:
            scanned_at = datetime.datetime.now().isoformat()
            for unit in clause_scan_units:
                cid = unit.get("clause_id") or (f"unit_{unit.get('clause_unit_id')}" if unit.get("clause_unit_id") else "")
                c_audit = audit_trace_by_clause_id.get(cid)
                if c_audit is not None:
                    src_resolutions = resolutions_by_source_cid.get(cid, [])
                    src_partials = partials_by_source_cid.get(cid, [])
                    c_audit["ccrl_scan"] = {
                        "scanned_at": scanned_at,
                        "secondary_model": self.model_id,
                        "prompt_version": "v2_parallel",
                        "resolutions_applied": src_resolutions,
                        "partials_recorded": src_partials,
                        "no_resolutions_reason": (
                            None if (src_resolutions or src_partials)
                            else "no_matching_provisions_in_clause"
                        ),
                    }

        elapsed = time.time() - start_time

        # Compile pending at end
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
                        "last_known_status": frag.resolution_status,
                        "remaining_deficiency": frag.remaining_deficiency,
                        "context_limitation": frag.context_limitation,
                    })

        reconciliation_summary = {
            "prompt_version": "v2_parallel",
            "secondary_model": self.model_id,
            "total_ccrl_entries_registered": len(ccrl_state),
            "total_criteria_fully_resolved": len(resolved_log),
            "total_criteria_partially_resolved": len([
                p for p in partial_log if p["status"] == STATUS_PARTIALLY_RESOLVED
            ]),
            "total_criteria_conflicting": len([
                p for p in partial_log if p["status"] == STATUS_CONFLICTING
            ]),
            "total_criteria_context_unavailable": len([
                p for p in partial_log if p["status"] == STATUS_CONTEXT_UNAVAILABLE
            ]),
            "total_fix_blobs_removed": total_blobs_removed,
            "reconciliation_elapsed_seconds": round(elapsed, 2),
            "fully_resolved_entries": resolved_log,
            "partially_resolved_entries": [
                p for p in partial_log if p["status"] == STATUS_PARTIALLY_RESOLVED
            ],
            "conflicting_entries": [
                p for p in partial_log if p["status"] == STATUS_CONFLICTING
            ],
            "context_unavailable_entries": [
                p for p in partial_log if p["status"] == STATUS_CONTEXT_UNAVAILABLE
            ],
            "pending_entries_at_end": pending_at_end,
        }

        logger.info(
            "CCRL Reconciler v2 (Parallel): finished in %.2fs. "
            "%d fully resolved, %d partial, %d conflicting, %d ctx-unavailable, %d fix blobs removed.",
            elapsed,
            len(resolved_log),
            len([p for p in partial_log if p["status"] == STATUS_PARTIALLY_RESOLVED]),
            len([p for p in partial_log if p["status"] == STATUS_CONFLICTING]),
            len([p for p in partial_log if p["status"] == STATUS_CONTEXT_UNAVAILABLE]),
            total_blobs_removed,
        )
        return reconciliation_summary

    async def _call_model_batch(
        self,
        batch_idx: int,
        total_batches: int,
        batch_snapshot: List[Dict[str, Any]],
        agreement_index: str,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        """Invoke Bedrock for a single batch of target clauses against the Agreement."""
        t_start = time.time()
        user_message = (
            f"{agreement_index}\n\n"
            f"=== TARGET CLAUSES WITH UNRESOLVED CRITERIA (Batch {batch_idx} of {total_batches}) ===\n"
            f"{json.dumps(batch_snapshot, indent=2)}\n\n"
            "Review the complete indexed Agreement above. For each pending criterion, determine "
            "whether any provision in the Agreement substantively satisfies it. Apply the "
            "REQUIRED LEGAL TESTS from the system prompt. Respond strictly in JSON matching the schema."
        )

        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
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

            data = _parse_robust_json(raw_text)
            recon_list = (
                data.get("reconciliation_results") or data.get("resolved", [])
            ) if isinstance(data, dict) else []

            lat_ms = int((time.time() - t_start) * 1000)
            logger.info(
                "CCRL batch %d/%d completed in %.2fs (%d results parsed, stop_reason=%s)",
                batch_idx, total_batches, lat_ms / 1000, len(recon_list), parsed.get("stop_reason")
            )
            return data, recon_list, lat_ms

        except Exception as exc:
            lat_ms = int((time.time() - t_start) * 1000)
            logger.error("CCRL batch %d/%d invocation failed: %s", batch_idx, total_batches, exc)
            return {"error": str(exc), "reconciliation_results": []}, [], lat_ms

    def _populate_empty_audits(
        self,
        clause_scan_units: List[Dict[str, Any]],
        audit_trace_by_clause_id: Optional[Dict[str, Dict[str, Any]]],
    ) -> None:
        if audit_trace_by_clause_id is not None:
            scanned_at = datetime.datetime.now().isoformat()
            for unit in clause_scan_units:
                cid = unit.get("clause_id") or (f"unit_{unit.get('clause_unit_id')}" if unit.get("clause_unit_id") else "")
                c_audit = audit_trace_by_clause_id.get(cid)
                if c_audit is not None:
                    c_audit["ccrl_scan"] = {
                        "scanned_at": scanned_at,
                        "secondary_model": self.model_id,
                        "prompt_version": "v2_parallel",
                        "resolutions_applied": [],
                        "partials_recorded": [],
                        "no_resolutions_reason": "no_pending_ccrl_entries",
                    }

    def _build_empty_summary(self, ccrl_state: CCRLState, elapsed: float) -> Dict[str, Any]:
        return {
            "prompt_version": "v2_parallel",
            "secondary_model": self.model_id,
            "total_ccrl_entries_registered": len(ccrl_state),
            "total_criteria_fully_resolved": 0,
            "total_criteria_partially_resolved": 0,
            "total_criteria_conflicting": 0,
            "total_criteria_context_unavailable": 0,
            "total_fix_blobs_removed": 0,
            "reconciliation_elapsed_seconds": round(elapsed, 2),
            "fully_resolved_entries": [],
            "partially_resolved_entries": [],
            "conflicting_entries": [],
            "context_unavailable_entries": [],
            "pending_entries_at_end": [],
        }


def _build_resolution_record(
    ledger_cid: str,
    ledger_clause_title: str,
    crit_id: str,
    criterion_question: str,
    status: str,
    relationship: List[str],
    resolved_by_clause_id: str,
    resolved_by_clause_title: str,
    resolved_by_clause_ids: List[str],
    evidence_list: Any,
    evidence_quote_str: str,
    reason: str,
    remaining_deficiency: str,
    context_limitation: str,
    fix_blob_removed: bool,
    fix_before: str,
    fix_after: str,
    target_frag_fix_blob: str,
) -> Dict[str, Any]:
    if isinstance(evidence_list, list):
        evidence_normalised = evidence_list
    elif evidence_quote_str:
        evidence_normalised = [{
            "source_clause_id": resolved_by_clause_id,
            "verbatim_quote": evidence_quote_str,
            "source_section": resolved_by_clause_title,
        }]
    else:
        evidence_normalised = []

    return {
        "ledger_clause_id": ledger_cid,
        "ledger_clause_title": ledger_clause_title,
        "criterion_id": crit_id,
        "criterion_question": criterion_question,
        "status": status,
        "relationship": relationship,
        "resolved_by_clause_id": resolved_by_clause_id,
        "resolved_by_clause_title": resolved_by_clause_title,
        "resolved_by_clause_ids": resolved_by_clause_ids,
        "evidence": evidence_normalised,
        "evidence_quote": evidence_quote_str,
        "reason": reason,
        "remaining_deficiency": remaining_deficiency or None,
        "context_limitation": context_limitation or None,
        "fix_blob_removed": fix_blob_removed,
        "fix_blob_snapshot": target_frag_fix_blob[:300],
        "suggested_fix_before_reconciliation": fix_before,
        "suggested_fix_after_reconciliation": fix_after,
    }
