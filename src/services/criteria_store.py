"""
Criteria store for the deterministic general review pipeline.

Loads review criteria DYNAMICALLY from the PostgreSQL RDS database, keyed
by agreement_type_id resolved from the free-text agreement_type string in the
request payload.

Public API (backward-compatible signatures):
    resolve_agreement_type_id(text)         -> int
    slug_to_display(slug, atid)             -> Optional[str]
    display_to_slug(display_name, atid)     -> Optional[str]
    get_criteria(display_name, atid)        -> List[dict]
    get_criteria_by_slug(slug, atid)        -> List[dict]
    get_answer_penalties()                  -> Dict[str, Optional[float]]

The agreement_type_id defaults to 84 (MSA) for callers that have not yet been
updated to pass one, preserving backward compatibility.

Fallback: if the DB is unavailable or returns 0 criteria for a given type,
the store falls back to the static JSON file (MSA only).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default agreement_type_id when no match found — MSA
_DEFAULT_ATI = 84

# In-memory TTL for cached taxonomy per agreement_type_id (seconds)
_CACHE_TTL = 3600

# Fallback JSON (MSA only)
_CRITERIA_PATH = (
    Path(__file__).resolve().parent
    / "clause_mapping"
    / "MSA_Criteria_Final_Taxonomy_Calibrated.json"
)

# Common abbreviations → agreement_type_id shortcuts (augmented from DB lookup)
_ABBREV_ATI: Dict[str, int] = {
    "msa": 84,
    "master service agreement": 84,
    "master services agreement": 84,
    "nda": 85,
    "non-disclosure agreement": 85,
    "non disclosure agreement": 85,
    "saas": 1,
    "software as a service": 1,
}

# Common vector DB slug synonyms bridging alternate or legacy slug formats
# (e.g. from Qdrant vector index) to canonical display names
_VECTOR_SLUG_SYNONYMS: Dict[str, str] = {
    "suspension-and-termination": "Term & Termination",
    "term": "Term & Termination",
    "termination": "Term & Termination",
    "exit-and-transition-assistance": "Term & Termination",
    "subcontracting-and-sub-processors": "Subcontractors",
    "general-provisions": "Miscellaneous / Other Provisions",
    "information-security-and-data-management": "Data Protection & Privacy",
    "publicity-and-use-of-name": "Publicity and Use of Names",
}

# ---------------------------------------------------------------------------
# Cache dataclass
# ---------------------------------------------------------------------------

@dataclass
class _TaxonomyCache:
    agreement_type_id: int
    slug_to_display_map: Dict[str, str]        # canonical_types.key → name
    display_to_slug_map: Dict[str, str]        # name → canonical_types.key (first key)
    criteria_by_display: Dict[str, List[dict]] # display_name → list of criterion dicts
    aliases: Dict[str, str]                    # lowercase alias → slug key
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.fetched_at) > _CACHE_TTL


_CACHE: Dict[int, _TaxonomyCache] = {}


# ---------------------------------------------------------------------------
# Fallback: static JSON (MSA only)
# ---------------------------------------------------------------------------

_fallback_taxonomy: Optional[Dict] = None


def _load_fallback() -> Dict:
    global _fallback_taxonomy
    if _fallback_taxonomy is None:
        try:
            with open(_CRITERIA_PATH, encoding="utf-8") as fh:
                _fallback_taxonomy = json.load(fh)
            logger.info(
                "Criteria store [fallback]: loaded %d types from %s",
                len(_fallback_taxonomy.get("types", {})),
                _CRITERIA_PATH.name,
            )
        except FileNotFoundError:
            logger.error("Criteria store [fallback]: JSON file not found at %s", _CRITERIA_PATH)
            _fallback_taxonomy = {"types": {}, "metadata": {}}
    return _fallback_taxonomy


def _build_fallback_cache(atid: int) -> _TaxonomyCache:
    """Build an in-memory cache entry from the static JSON file."""
    taxonomy = _load_fallback()
    types_dict = taxonomy.get("types", {})

    slug_to_display_map: Dict[str, str] = {}
    display_to_slug_map: Dict[str, str] = {}
    criteria_by_display: Dict[str, List[dict]] = {}

    # Hard-coded slug→display map from the original CANONICAL_TYPE_MAP
    # (preserved here as the fallback mapping for MSA)
    _HARDCODED: Dict[str, Optional[str]] = {
        "definitions-and-interpretation":          "Definitions & Interpretation",
        "purpose":                                 "Purpose",
        "scope-of-services":                       "Scope of Services",
        "statements-of-work-and-work-orders":      "Statements of Work & Work Orders",
        "customer-obligations":                    "Customer Obligations",
        "service-provider-obligations":            "Service Provider Obligations",
        "suspension-and-termination":              "Term & Termination",
        "term-and-termination":                    "Term & Termination",
        "term":                                    "Term & Termination",
        "termination":                             "Term & Termination",
        "exit-and-transition-assistance":          "Term & Termination",
        "deliverables-and-acceptance":             "Deliverables & Acceptance",
        "service-levels":                          "Service Levels",
        "support-and-maintenance":                 "Support & Maintenance",
        "change-control":                          "Change Control",
        "fees-and-payment":                        "Fees & Payment",
        "confidentiality":                         "Confidentiality",
        "data-protection-and-privacy":             "Data Protection & Privacy",
        "information-security-and-data-management":"Data Protection & Privacy",
        "intellectual-property":                   "Intellectual Property",
        "representations-and-warranties":          "Representations & Warranties",
        "indemnification":                         "Indemnification",
        "limitation-of-liability":                 "Limitation of Liability",
        "insurance":                               "Insurance",
        "compliance":                              "Compliance",
        "audit-and-records":                       "Audit & Records",
        "subcontracting-and-sub-processors":       "Subcontractors",
        "assignment":                              "Assignment",
        "force-majeure":                           "Force Majeure",
        "governing-law-and-dispute-resolution":    "Governing Law & Dispute Resolution",
        "notices":                                 "Notices",
        "independent-contractor":                  "Independent Contractor",
        "order-of-precedence":                     "Order of Precedence",
        "general-provisions":                      "Miscellaneous / Other Provisions",
        "publicity-and-use-of-name":               "Publicity and Use of Names",
        "business-continuity-and-disaster-recovery": None,
        "governance-and-project-management":        None,
        "personnel":                               None,
    }

    for slug, display in _HARDCODED.items():
        if display is not None:
            slug_to_display_map[slug] = display
            if display not in display_to_slug_map:
                display_to_slug_map[display] = slug

    for display_name, type_entry in types_dict.items():
        criteria_dict = type_entry.get("criteria", {})
        norm_crits = []
        for cid, cval in criteria_dict.items():
            c_copy = dict(cval)
            c_id = cid or cval.get("criterion_id")
            c_copy.setdefault("id", c_id)
            c_copy.setdefault("criterion_id", c_id)
            c_copy.setdefault("question", cval.get("question_for_llm") or cval.get("criterion") or "")
            norm_crits.append(c_copy)
        criteria_by_display[display_name] = norm_crits

    return _TaxonomyCache(
        agreement_type_id=atid,
        slug_to_display_map=slug_to_display_map,
        display_to_slug_map=display_to_slug_map,
        criteria_by_display=criteria_by_display,
        aliases={},
    )


# ---------------------------------------------------------------------------
# DB loaders
# ---------------------------------------------------------------------------

def _load_from_db(atid: int) -> Optional[_TaxonomyCache]:
    """
    Fetch canonical_types, review_criteria, and aliases from the DB for the
    given agreement_type_id.  Returns None if DB is unavailable or no data
    exists for this type.
    """
    try:
        from src.services.db import get_db_connection  # noqa: PLC0415
    except Exception as exc:
        logger.warning("criteria_store: cannot import db module (%s) — using fallback", exc)
        return None

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # --- 1. canonical_types -----------------------------------------------
                cur.execute(
                    """
                    SELECT key, name, sort_order
                    FROM public.canonical_types
                    WHERE agreement_type_id = %s
                      AND is_active = true
                    ORDER BY sort_order
                    """,
                    (atid,),
                )
                ct_rows = cur.fetchall()

                if not ct_rows:
                    logger.info(
                        "criteria_store: no canonical_types for agreement_type_id=%d — will use fallback",
                        atid,
                    )
                    return None

                slug_to_display_map: Dict[str, str] = {}
                display_to_slug_map: Dict[str, str] = {}
                for key, name, _sort in ct_rows:
                    slug_to_display_map[key] = name
                    if name not in display_to_slug_map:
                        display_to_slug_map[name] = key

                # Bridge vector DB slug synonyms to canonical display names
                for v_slug, v_disp in _VECTOR_SLUG_SYNONYMS.items():
                    if v_disp in display_to_slug_map and v_slug not in slug_to_display_map:
                        slug_to_display_map[v_slug] = v_disp

                # --- 2. review_criteria -----------------------------------------------
                # NOTE: type_key and criterion_group are dropped from review_criteria;
                # they are now derived from canonical_types (ct.key, ct.criterion_group).
                cur.execute(
                    """
                    SELECT
                        rc.criterion_id,
                        ct.key            AS type_key,
                        rc.weight,
                        rc.weight_value,
                        rc.gate_if_not_met,
                        rc.question_for_llm,
                        rc.criterion,
                        rc.met_when,
                        rc.partly_met_when,
                        rc.not_met_when,
                        rc.applies_when,
                        rc.seq,
                        ct.applies_to      AS criterion_group,
                        rc.issue_category,
                        rc.why_it_matters,
                        ct.name            AS canonical_type_name
                    FROM public.review_criteria rc
                    JOIN public.canonical_types ct
                         ON rc.canonical_type_id = ct.id
                    WHERE rc.agreement_type_id = %s
                      AND rc.is_active = true
                    ORDER BY ct.sort_order, rc.seq
                    """,
                    (atid,),
                )
                rc_rows = cur.fetchall()

                criteria_by_display: Dict[str, List[dict]] = {}
                for row in rc_rows:
                    (
                        criterion_id, type_key, weight, weight_value, gate_if_not_met,
                        question_for_llm, criterion_text,
                        met_when, partly_met_when, not_met_when,
                        applies_when, seq, criterion_group, issue_category,
                        why_it_matters, canonical_type_name,
                    ) = row
                    criterion = {
                        "id": criterion_id,
                        "criterion_id": criterion_id,
                        "type_key": type_key,
                        "weight": weight,
                        "weight_value": weight_value,
                        "gate_if_not_met": gate_if_not_met,
                        # pipeline checks .get("question") first, then "question_for_llm", then "criterion"
                        "question": question_for_llm or criterion_text or "",
                        "question_for_llm": question_for_llm or "",
                        "criterion": criterion_text or "",
                        "met_when": met_when or "",
                        "partly_met_when": partly_met_when or "",
                        "not_met_when": not_met_when or "",
                        "applies_when": applies_when or "",
                        "seq": seq,
                        "criterion_group": criterion_group or "",
                        "issue_category": issue_category or "",
                        "why_it_matters": why_it_matters or "",
                    }
                    criteria_by_display.setdefault(canonical_type_name, []).append(criterion)

                # --- 3. canonical_type_aliases ----------------------------------------
                cur.execute(
                    """
                    SELECT a.alias, ct.key
                    FROM public.canonical_type_aliases a
                    JOIN public.canonical_types ct ON a.canonical_type_id = ct.id
                    WHERE a.agreement_type_id = %s
                    """,
                    (atid,),
                )
                alias_rows = cur.fetchall()
                aliases: Dict[str, str] = {
                    alias.lower(): slug for alias, slug in alias_rows
                }

        criteria_count = sum(len(v) for v in criteria_by_display.values())
        logger.info(
            "criteria_store: loaded %d canonical_types, %d criteria, %d aliases from DB for agreement_type_id=%d",
            len(slug_to_display_map), criteria_count, len(aliases), atid,
        )

        return _TaxonomyCache(
            agreement_type_id=atid,
            slug_to_display_map=slug_to_display_map,
            display_to_slug_map=display_to_slug_map,
            criteria_by_display=criteria_by_display,
            aliases=aliases,
        )

    except Exception as exc:
        logger.warning("criteria_store: DB load failed for atid=%d (%s) — using fallback", atid, exc)
        return None


def _get_cache(atid: int) -> _TaxonomyCache:
    """Return (possibly cached) TaxonomyCache for the given agreement_type_id."""
    cached = _CACHE.get(atid)
    if cached is not None and not cached.is_expired:
        return cached

    # Try DB first
    db_cache = _load_from_db(atid)
    if db_cache is not None:
        _CACHE[atid] = db_cache
        return db_cache

    # Fallback: use static JSON (meaningful only for MSA / atid=84)
    fallback = _build_fallback_cache(atid)
    _CACHE[atid] = fallback
    return fallback


# ---------------------------------------------------------------------------
# Agreement type resolution (free-text string → agreement_type_id)
# ---------------------------------------------------------------------------

# Simple LRU for resolved IDs (up to 256 strings)
_ATI_RESOLUTION_CACHE: Dict[str, int] = {}


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation/extra whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fuzzy_match(norm: str, candidates: List[Tuple[str, int]]) -> Optional[int]:
    """
    Score each (name, id) candidate against norm by counting shared tokens.
    Returns the best atid if score >= 50 % of norm tokens, else None.
    """
    norm_tokens = set(norm.split())
    best_score = 0
    best_id: Optional[int] = None
    for name, atid in candidates:
        name_tokens = set(_normalise(name).split())
        common = norm_tokens & name_tokens
        score = len(common) / max(len(norm_tokens), 1)
        if score > best_score:
            best_score = score
            best_id = atid
    if best_score >= 0.5:
        return best_id
    return None


def resolve_agreement_type_id(agreement_type_str: str) -> int:
    """
    Resolve a free-text agreement type string to its DB agreement_type_id.

    Resolution order:
        1. Cache hit
        2. Abbreviation map (msa, nda, saas …)
        3. Exact case-insensitive DB match on agreement_types.name
        4. ILIKE prefix match in DB
        5. Fuzzy token overlap against all agreement_types.name values
        6. Default: 84 (MSA)
    """
    if not agreement_type_str:
        return _DEFAULT_ATI

    cache_key = agreement_type_str.strip()
    if cache_key in _ATI_RESOLUTION_CACHE:
        return _ATI_RESOLUTION_CACHE[cache_key]

    norm = _normalise(cache_key)

    # 2. Abbreviation map
    for abbrev, atid in _ABBREV_ATI.items():
        if norm == abbrev or norm.startswith(abbrev + " "):
            _ATI_RESOLUTION_CACHE[cache_key] = atid
            logger.debug("resolve_agreement_type_id: '%s' → %d (abbrev map)", cache_key, atid)
            return atid

    # 3–5. DB lookup
    try:
        from src.services.db import get_db_connection  # noqa: PLC0415
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Exact match
                cur.execute(
                    "SELECT id FROM public.agreement_types WHERE lower(name) = %s LIMIT 1",
                    (norm,),
                )
                row = cur.fetchone()
                if row:
                    atid = row[0]
                    _ATI_RESOLUTION_CACHE[cache_key] = atid
                    logger.info("resolve_agreement_type_id: '%s' → %d (exact)", cache_key, atid)
                    return atid

                # ILIKE prefix
                cur.execute(
                    "SELECT id FROM public.agreement_types WHERE lower(name) LIKE %s LIMIT 1",
                    (norm[:30] + "%",),
                )
                row = cur.fetchone()
                if row:
                    atid = row[0]
                    _ATI_RESOLUTION_CACHE[cache_key] = atid
                    logger.info("resolve_agreement_type_id: '%s' → %d (prefix)", cache_key, atid)
                    return atid

                # Fuzzy: fetch all names, score
                cur.execute("SELECT id, name FROM public.agreement_types")
                all_types: List[Tuple[str, int]] = [(name, aid) for aid, name in cur.fetchall()]
                atid = _fuzzy_match(norm, all_types)
                if atid is not None:
                    _ATI_RESOLUTION_CACHE[cache_key] = atid
                    logger.info("resolve_agreement_type_id: '%s' → %d (fuzzy)", cache_key, atid)
                    return atid

    except Exception as exc:
        logger.warning(
            "resolve_agreement_type_id: DB lookup failed for '%s' (%s) — defaulting to %d",
            cache_key, exc, _DEFAULT_ATI,
        )

    logger.info(
        "resolve_agreement_type_id: '%s' → %d (default fallback)", cache_key, _DEFAULT_ATI
    )
    _ATI_RESOLUTION_CACHE[cache_key] = _DEFAULT_ATI
    return _DEFAULT_ATI


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def slug_to_display(slug: str, agreement_type_id: int = _DEFAULT_ATI) -> Optional[str]:
    """
    Convert a vector-DB canonical_type slug to its taxonomy display name.

    Returns None when the slug has no defined criteria type (the evaluator
    will fall back to general criteria for that paragraph group).
    """
    cache = _get_cache(agreement_type_id)
    result = cache.slug_to_display_map.get(slug)
    if result is None:
        # Try alias lookup (aliases map heading text → slug; here we reverse)
        # Also check if slug matches an alias value → then resolve its display
        for alias_slug in cache.aliases.values():
            if alias_slug == slug:
                return cache.slug_to_display_map.get(alias_slug)
    return result


def display_to_slug(display_name: str, agreement_type_id: int = _DEFAULT_ATI) -> Optional[str]:
    """Convert a taxonomy display name back to its primary canonical slug."""
    cache = _get_cache(agreement_type_id)
    return cache.display_to_slug_map.get(display_name)


def get_criteria(display_name: str, agreement_type_id: int = _DEFAULT_ATI) -> List[dict]:
    """
    Return the list of criterion dicts for a taxonomy display name.

    Returns an empty list when the display name is unknown or has no criteria.
    """
    cache = _get_cache(agreement_type_id)
    return list(cache.criteria_by_display.get(display_name, []))


def get_criteria_by_slug(slug: str, agreement_type_id: int = _DEFAULT_ATI) -> List[dict]:
    """
    Convenience wrapper: slug → display name → criteria list.

    Returns an empty list for unknown or unmapped slugs.
    """
    display = slug_to_display(slug, agreement_type_id)
    if display is None:
        return []
    return get_criteria(display, agreement_type_id)


def get_answer_penalties() -> Dict[str, Optional[float]]:
    """Return the standard answer penalty map (static — same for all agreement types)."""
    fallback = _load_fallback()
    return fallback.get("metadata", {}).get(
        "answer_penalties",
        {
            "Met": 0.0,
            "Partly met": 0.5,
            "Not met": 1.0,
            "Unclear": 0.5,
            "Not applicable": None,
        },
    )


def get_all_display_names(agreement_type_id: int = _DEFAULT_ATI) -> List[str]:
    """Return all canonical display names for the given agreement type."""
    cache = _get_cache(agreement_type_id)
    return list(cache.slug_to_display_map.values())


def invalidate_cache(agreement_type_id: Optional[int] = None) -> None:
    """Force a re-fetch on next access. Pass None to clear all cached types."""
    if agreement_type_id is None:
        _CACHE.clear()
        logger.info("criteria_store: full cache cleared")
    else:
        _CACHE.pop(agreement_type_id, None)
        logger.info("criteria_store: cache cleared for agreement_type_id=%d", agreement_type_id)
