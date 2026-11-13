"""
Criteria Management API — `src/api/endpoints/criteria/router.py`

Provides full CRUD for review_criteria rows, surfaced so a legal team
can build and maintain evaluation criteria for any agreement type
through a UI without touching code or the database directly.

Endpoints:
    GET  /criteria/agreement-types
    GET  /criteria/agreement-types/{atid}/canonical-types
    GET  /criteria/canonical-types/{canonical_type_id}/criteria
    POST /criteria/canonical-types/{canonical_type_id}/criteria
    PUT  /criteria/criteria/{criterion_id}
    DELETE /criteria/criteria/{criterion_id}
    POST /criteria/cache/invalidate/{atid}
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.config.logging import get_logger
from src.services.criteria_store import invalidate_cache

logger = get_logger(__name__)

router = APIRouter(tags=["Criteria Management"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AgreementTypeOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None


class CanonicalTypeOut(BaseModel):
    id: Any
    key: str
    name: str
    criterion_group: Optional[str] = None
    sort_order: Optional[int] = None


class CriterionOut(BaseModel):
    criterion_id: str
    canonical_type_id: Any
    canonical_type_name: str
    type_key: str
    criterion_group: Optional[str]
    criterion: Optional[str]
    question_for_llm: Optional[str]
    met_when: Optional[str]
    partly_met_when: Optional[str]
    not_met_when: Optional[str]
    applies_when: Optional[str]
    issue_category: Optional[str]
    why_it_matters: Optional[str]
    weight: Optional[str]
    weight_value: Optional[float]
    gate_if_not_met: Optional[bool]
    gate_rank: Optional[int]
    answer_options: Optional[str]
    seq: Optional[int]
    review_status: Optional[str]
    is_active: bool


class CriterionCreate(BaseModel):
    criterion: str = Field(..., description="Short internal label, e.g. 'Liability cap stated'")
    question_for_llm: str = Field(..., description="Full review question sent to the AI model")
    met_when: str = Field(..., description="Plain-English condition for a passing answer")
    partly_met_when: Optional[str] = Field(None, description="Condition for a partial pass")
    not_met_when: str = Field(..., description="Condition for a failing answer")
    applies_when: Optional[str] = Field(None, description="Leave blank for universal criteria")
    issue_category: Optional[str] = Field(None, description="Risk | Ambiguity | Missing Provision | Compliance")
    why_it_matters: Optional[str] = Field(None, description="1–2 sentence plain-English explanation")
    # Importance — legal team uses friendly label; system maps to weight_value
    weight: Optional[str] = Field(
        None,
        description="Critical | High | Medium | Low — controls scoring impact"
    )
    weight_value: Optional[float] = Field(
        None,
        ge=0.0, le=1.0,
        description="Override numeric weight (0.0–1.0). Computed from 'weight' label if omitted."
    )
    gate_if_not_met: Optional[bool] = Field(
        False,
        description="If True, failing this criterion alone forces High Risk on the entire clause"
    )
    gate_rank: Optional[int] = Field(None, description="Priority order when multiple gates are active")
    answer_options: Optional[str] = Field(None, description="JSON array of allowed LLM answer strings")
    seq: Optional[int] = Field(10, description="Display/evaluation order (multiples of 10 recommended)")
    review_status: Optional[str] = Field("draft", description="draft | active")


class CriterionUpdate(BaseModel):
    criterion: Optional[str] = None
    question_for_llm: Optional[str] = None
    met_when: Optional[str] = None
    partly_met_when: Optional[str] = None
    not_met_when: Optional[str] = None
    applies_when: Optional[str] = None
    issue_category: Optional[str] = None
    why_it_matters: Optional[str] = None
    weight: Optional[str] = None
    weight_value: Optional[float] = Field(None, ge=0.0, le=1.0)
    gate_if_not_met: Optional[bool] = None
    gate_rank: Optional[int] = None
    answer_options: Optional[str] = None
    seq: Optional[int] = None
    review_status: Optional[str] = None


# ---------------------------------------------------------------------------
# Weight label → numeric value mapping
# ---------------------------------------------------------------------------
_WEIGHT_VALUE_MAP: Dict[str, float] = {
    "critical": 1.0,
    "high":     0.7,
    "medium":   0.4,
    "low":      0.2,
}


def _resolve_weight_value(weight_label: Optional[str], explicit_value: Optional[float]) -> Optional[float]:
    if explicit_value is not None:
        return explicit_value
    if weight_label:
        return _WEIGHT_VALUE_MAP.get(weight_label.lower())
    return None


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _get_conn():
    try:
        from src.services.db import get_db_connection  # noqa: PLC0415
        return get_db_connection()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# GET  /agreement-types
# ---------------------------------------------------------------------------

@router.get("/agreement-types", response_model=List[AgreementTypeOut])
async def list_agreement_types() -> List[AgreementTypeOut]:
    """Return all agreement types available in the system."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description FROM public.agreement_types ORDER BY name"
            )
            rows = cur.fetchall()
    return [AgreementTypeOut(id=r[0], name=r[1], description=r[2]) for r in rows]


# ---------------------------------------------------------------------------
# GET  /agreement-types/{atid}/canonical-types
# ---------------------------------------------------------------------------

@router.get(
    "/agreement-types/{atid}/canonical-types",
    response_model=List[CanonicalTypeOut],
)
async def list_canonical_types(atid: int) -> List[CanonicalTypeOut]:
    """Return all clause types (canonical_types) for a given agreement type."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, key, name, applies_to AS criterion_group, sort_order
                FROM public.canonical_types
                WHERE agreement_type_id = %s AND is_active = true
                ORDER BY sort_order
                """,
                (atid,),
            )
            rows = cur.fetchall()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No canonical types found for agreement_type_id={atid}",
        )
    return [
        CanonicalTypeOut(id=r[0], key=r[1], name=r[2], criterion_group=r[3], sort_order=r[4])
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET  /canonical-types/{canonical_type_id}/criteria
# ---------------------------------------------------------------------------

@router.get(
    "/canonical-types/{canonical_type_id}/criteria",
    response_model=List[CriterionOut],
)
async def list_criteria(canonical_type_id: str) -> List[CriterionOut]:
    """Return all criteria for a specific clause type."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    rc.criterion_id,
                    rc.canonical_type_id,
                    ct.name            AS canonical_type_name,
                    ct.key             AS type_key,
                    ct.applies_to      AS criterion_group,
                    rc.criterion,
                    rc.question_for_llm,
                    rc.met_when,
                    rc.partly_met_when,
                    rc.not_met_when,
                    rc.applies_when,
                    rc.issue_category,
                    rc.why_it_matters,
                    rc.weight,
                    rc.weight_value,
                    rc.gate_if_not_met,
                    rc.gate_rank,
                    rc.answer_options,
                    rc.seq,
                    rc.review_status,
                    rc.is_active
                FROM public.review_criteria rc
                JOIN public.canonical_types ct ON rc.canonical_type_id = ct.id
                WHERE rc.canonical_type_id = %s
                ORDER BY rc.seq
                """,
                (canonical_type_id,),
            )
            rows = cur.fetchall()

    return [
        CriterionOut(
            criterion_id=r[0],
            canonical_type_id=r[1],
            canonical_type_name=r[2],
            type_key=r[3],
            criterion_group=r[4],
            criterion=r[5],
            question_for_llm=r[6],
            met_when=r[7],
            partly_met_when=r[8],
            not_met_when=r[9],
            applies_when=r[10],
            issue_category=r[11],
            why_it_matters=r[12],
            weight=r[13],
            weight_value=r[14],
            gate_if_not_met=r[15],
            gate_rank=r[16],
            answer_options=r[17],
            seq=r[18],
            review_status=r[19],
            is_active=r[20],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# POST /canonical-types/{canonical_type_id}/criteria
# ---------------------------------------------------------------------------

@router.post(
    "/canonical-types/{canonical_type_id}/criteria",
    response_model=CriterionOut,
    status_code=201,
)
async def create_criterion(
    canonical_type_id: str,
    body: CriterionCreate,
) -> CriterionOut:
    """Create a new review criterion for a clause type."""

    # Resolve numeric weight from friendly label
    resolved_weight_value = _resolve_weight_value(body.weight, body.weight_value)
    new_id = str(uuid.uuid4())

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Fetch agreement_type_id and type details from canonical_types
            cur.execute(
                "SELECT agreement_type_id, key, name, applies_to AS criterion_group FROM public.canonical_types WHERE id = %s",
                (canonical_type_id,),
            )
            ct_row = cur.fetchone()
            if not ct_row:
                raise HTTPException(status_code=404, detail=f"canonical_type_id={canonical_type_id} not found")

            atid, ct_key, ct_name, ct_group = ct_row

            cur.execute(
                """
                INSERT INTO public.review_criteria (
                    criterion_id, canonical_type_id, agreement_type_id,
                    criterion, question_for_llm,
                    met_when, partly_met_when, not_met_when, applies_when,
                    issue_category, why_it_matters,
                    weight, weight_value, gate_if_not_met, gate_rank,
                    answer_options, seq, review_status, is_active, origin
                ) VALUES (
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, true, 'manual'
                )
                """,
                (
                    new_id, canonical_type_id, atid,
                    body.criterion, body.question_for_llm,
                    body.met_when, body.partly_met_when, body.not_met_when, body.applies_when,
                    body.issue_category, body.why_it_matters,
                    body.weight, resolved_weight_value, body.gate_if_not_met, body.gate_rank,
                    body.answer_options, body.seq, body.review_status,
                ),
            )
            conn.commit()

    # Invalidate in-memory cache so new criterion is picked up immediately
    invalidate_cache(atid)
    logger.info("Criterion created: %s for canonical_type_id=%d", new_id, canonical_type_id)

    return CriterionOut(
        criterion_id=new_id,
        canonical_type_id=canonical_type_id,
        canonical_type_name=ct_name,
        type_key=ct_key,
        criterion_group=ct_group,
        criterion=body.criterion,
        question_for_llm=body.question_for_llm,
        met_when=body.met_when,
        partly_met_when=body.partly_met_when,
        not_met_when=body.not_met_when,
        applies_when=body.applies_when,
        issue_category=body.issue_category,
        why_it_matters=body.why_it_matters,
        weight=body.weight,
        weight_value=resolved_weight_value,
        gate_if_not_met=body.gate_if_not_met,
        gate_rank=body.gate_rank,
        answer_options=body.answer_options,
        seq=body.seq,
        review_status=body.review_status,
        is_active=True,
    )


# ---------------------------------------------------------------------------
# PUT  /criteria/{criterion_id}
# ---------------------------------------------------------------------------

@router.put("/criteria/{criterion_id}", response_model=Dict[str, Any])
async def update_criterion(criterion_id: str, body: CriterionUpdate) -> Dict[str, Any]:
    """Update one or more fields of an existing criterion."""

    updates: Dict[str, Any] = {
        k: v for k, v in body.model_dump(exclude_none=True).items()
    }
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Resolve weight_value from weight label if only label was provided
    if "weight" in updates and "weight_value" not in updates:
        updates["weight_value"] = _resolve_weight_value(updates["weight"], None)

    set_clause = ", ".join(f"{col} = %s" for col in updates)
    values = list(updates.values()) + [criterion_id]

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.review_criteria
                SET {set_clause}
                WHERE criterion_id = %s
                RETURNING agreement_type_id
                """,
                values,
            )
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail=f"criterion_id={criterion_id!r} not found")
            atid = result[0]
            conn.commit()

    invalidate_cache(atid)
    logger.info("Criterion updated: %s, fields: %s", criterion_id, list(updates.keys()))
    return {"status": "updated", "criterion_id": criterion_id, "fields_updated": list(updates.keys())}


# ---------------------------------------------------------------------------
# DELETE /criteria/{criterion_id}  (soft delete)
# ---------------------------------------------------------------------------

@router.delete("/criteria/{criterion_id}", response_model=Dict[str, Any])
async def delete_criterion(criterion_id: str) -> Dict[str, Any]:
    """Soft-delete a criterion (sets is_active = false). The criterion stays in the DB for audit purposes."""

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.review_criteria
                SET is_active = false
                WHERE criterion_id = %s
                RETURNING agreement_type_id
                """,
                (criterion_id,),
            )
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail=f"criterion_id={criterion_id!r} not found")
            atid = result[0]
            conn.commit()

    invalidate_cache(atid)
    logger.info("Criterion soft-deleted: %s", criterion_id)
    return {"status": "deactivated", "criterion_id": criterion_id}


# ---------------------------------------------------------------------------
# POST /cache/invalidate/{atid}
# ---------------------------------------------------------------------------

@router.post("/cache/invalidate/{atid}", response_model=Dict[str, Any])
async def invalidate_criteria_cache(atid: int) -> Dict[str, Any]:
    """Force the criteria_store in-memory cache to refresh for the given agreement type.
    Call this after bulk DB edits so the pipeline picks up the changes immediately."""
    invalidate_cache(atid)
    return {"status": "invalidated", "agreement_type_id": atid}

