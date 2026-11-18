"""
src/services/tracing/langsmith_tracer.py
----------------------------------------
LangSmith low-level agentic auditing and tracing integration.
Constructs hierarchical span trees for contract reviews:
  - Root: ContractReviewPipeline (chain)
    - ClauseUnit (chain)
      - ReferenceNavigation (tool)
      - BedrockExtraction (llm)
      - BedrockExplanation (llm)

Supports live streaming to LangSmith cloud dashboard when LANGCHAIN_API_KEY
or LANGSMITH_API_KEY is configured, with safe local fallback.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from langsmith.run_trees import RunTree
    _LANGSMITH_AVAILABLE = True
except ImportError:
    RunTree = None  # type: ignore
    _LANGSMITH_AVAILABLE = False

logger = logging.getLogger("accorder.tracing")


def is_langsmith_enabled() -> bool:
    """Check if LangSmith tracing is active via environment variables."""
    if not _LANGSMITH_AVAILABLE:
        return False
    tracing_v2 = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() in ("true", "1")
    has_key = bool(os.getenv("LANGCHAIN_API_KEY") or os.getenv("LANGSMITH_API_KEY"))
    return tracing_v2 or has_key


class ContractReviewTracer:
    """Manages LangSmith RunTree spans for a complete contract review execution."""

    def __init__(self, project_name: Optional[str] = None) -> None:
        self.base_project = project_name or os.getenv("LANGCHAIN_PROJECT", "accorder-contract-review")
        self.enabled = is_langsmith_enabled()

    def start_pipeline_trace(
        self,
        session_id: str,
        agreement_type: Optional[str] = None,
        party_represented: Optional[str] = None,
        review_objective: Optional[str] = None,
        total_clauses: int = 0,
        total_paragraphs: int = 0,
        simulation_mode: bool = False,
    ) -> Optional[Any]:
        """Start the root span for the entire contract review pipeline."""
        if not _LANGSMITH_AVAILABLE:
            return None

        # Dual dashboard routing: separate project for clause-level vs paragraph reviews
        if simulation_mode:
            target_project = os.getenv("LANGSMITH_PROJECT_CLAUSE", f"{self.base_project}-clause")
            mode_tag = "clause-level-review"
        else:
            target_project = os.getenv("LANGSMITH_PROJECT_PARAGRAPH", f"{self.base_project}-paragraph")
            mode_tag = "paragraph-payload-review"

        try:
            root = RunTree(
                name="ContractReviewPipeline",
                run_type="chain",
                inputs={
                    "session_id": session_id,
                    "review_mode": mode_tag,
                    "agreement_type": agreement_type or "General Review",
                    "party_represented": party_represented or "Neutral General Review",
                    "review_objective": review_objective or "Comprehensive Review",
                    "total_clauses": total_clauses,
                    "total_paragraphs": total_paragraphs,
                    "simulation_clause_payload": simulation_mode,
                },
                tags=[
                    mode_tag,
                    f"type:{agreement_type or 'General'}",
                    f"party:{party_represented or 'Neutral'}",
                ],
                extra={
                    "session_id": session_id,
                    "simulation_mode": simulation_mode,
                    "service": "accorder-ai-backend",
                },
                project_name=target_project,
            )
            return root
        except Exception as exc:
            logger.warning("Failed to start LangSmith root run: %s", exc)
            return None

    def create_clause_span(
        self,
        parent_run: Optional[Any],
        unit_id: int,
        clause_title: str,
        canonical_type: Optional[str],
        pipeline_path: str,
        para_identifiers: List[str],
        clause_text: str,
    ) -> Optional[Any]:
        """Create a child span representing the evaluation of a single clause unit."""
        if not parent_run or not hasattr(parent_run, "create_child"):
            return None

        try:
            span = parent_run.create_child(
                name=f"ClauseUnit_{unit_id:02d}_{clause_title[:35]}",
                run_type="chain",
                inputs={
                    "unit_id": unit_id,
                    "clause_title": clause_title,
                    "canonical_type": canonical_type or "Unclassified",
                    "pipeline_path": pipeline_path,
                    "paragraph_count": len(para_identifiers),
                    "paragraph_identifiers": para_identifiers,
                    "clause_text": clause_text,
                },
                extra={
                    "unit_id": unit_id,
                    "clause_title": clause_title,
                    "canonical_type": canonical_type,
                },
            )
            return span
        except Exception as exc:
            logger.debug("Failed to create clause span: %s", exc)
            return None

    def create_reference_span(
        self,
        parent_run: Optional[Any],
        references_found: List[Dict[str, Any]],
        context_bundle: Optional[str] = None,
    ) -> Optional[Any]:
        """Create a child tool span tracking cross-clause reference resolution."""
        if not parent_run or not hasattr(parent_run, "create_child"):
            return None

        try:
            span = parent_run.create_child(
                name="ReferenceNavigation",
                run_type="tool",
                inputs={
                    "references_detected_count": len(references_found),
                    "references": references_found,
                },
                outputs={
                    "context_bundle_injected": bool(context_bundle),
                    "context_bundle_words": len(context_bundle.split()) if context_bundle else 0,
                    "context_bundle_snippet": context_bundle[:300] if context_bundle else None,
                },
            )
            span.end()
            return span
        except Exception as exc:
            logger.debug("Failed to create reference span: %s", exc)
            return None

    def create_llm_span(
        self,
        parent_run: Optional[Any],
        span_name: str,
        prompt: str,
        model_name: str = "global.anthropic.claude-sonnet-4-6",
        thinking_budget: Optional[int] = None,
        criteria_summary: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Any]:
        """Create a child LLM span tracking a Bedrock Claude model generation."""
        if not parent_run or not hasattr(parent_run, "create_child"):
            return None

        try:
            span = parent_run.create_child(
                name=span_name,
                run_type="llm",
                inputs={
                    "prompt": prompt,
                    "criteria_evaluated_count": len(criteria_summary) if criteria_summary else 0,
                    "criteria": criteria_summary or [],
                },
                extra={
                    "model": model_name,
                    "thinking_budget": thinking_budget,
                },
            )
            return span
        except Exception as exc:
            logger.debug("Failed to create LLM span: %s", exc)
            return None

    def end_span(
        self,
        span: Optional[Any],
        outputs: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Safely end a span and set outputs / usage metrics."""
        if not span or not hasattr(span, "end"):
            return

        try:
            if extra and hasattr(span, "extra") and isinstance(span.extra, dict):
                span.extra.update(extra)
            span.end(outputs=outputs, error=error)
        except Exception as exc:
            logger.debug("Failed to end span: %s", exc)

    def finish_pipeline_trace(
        self,
        root_run: Optional[Any],
        total_suggestions: int,
        suppressed_count: int = 0,
        local_save_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """End the root pipeline trace, post to LangSmith if active, and save local trace file."""
        if not root_run:
            return

        try:
            root_run.end(
                outputs={
                    "total_suggestions_issued": total_suggestions,
                    "redundancies_suppressed": suppressed_count,
                    "status": "COMPLETED",
                }
            )

            # 1. Post to LangSmith Cloud if configured
            if self.enabled:
                try:
                    root_run.post()
                    logger.info("LangSmith: successfully posted pipeline trace (trace_id=%s)", getattr(root_run, "trace_id", root_run.id))
                except Exception as exc:
                    logger.warning("LangSmith: post failed (will keep local copy): %s", exc)

            # 2. Save local JSON representation for offline audit & debugging
            if local_save_path:
                try:
                    p = Path(local_save_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    trace_dict = root_run.dict() if hasattr(root_run, "dict") else root_run.model_dump()
                    p.write_text(json.dumps(trace_dict, indent=2, default=str), encoding="utf-8")
                    logger.info("LangSmith: saved local trace tree to %s", p.name)
                except Exception as exc:
                    logger.debug("Failed to save local LangSmith trace file: %s", exc)

        except Exception as exc:
            logger.warning("Error finishing pipeline trace: %s", exc)
