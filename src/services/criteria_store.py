"""
Criteria store for the deterministic general review pipeline.

Loads MSA_Criteria_Final_Taxonomy_Calibrated.json once at import and exposes
two look-up functions:

    get_criteria_by_slug(slug)   ->  List[dict]   (type-specific criteria)
    slug_to_display(slug)        ->  Optional[str] (kebab slug → display name)

The slug→display map bridges the vector DB's kebab-case canonical_type values
(e.g. "limitation-of-liability") to the taxonomy's display names
(e.g. "Limitation of Liability") that key the criteria JSON.

Slugs that have no matching criteria type resolve to None — those paragraphs
fall through to the general criteria path in the evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from src.config.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Slug → display-name mapping
# Vector DB canonical_type (kebab-case) → Criteria JSON key (display name)
# 35 vector DB slugs mapped to their taxonomy counterparts.
# Slugs absent from the taxonomy (no criteria defined) map to None.
# ---------------------------------------------------------------------------
CANONICAL_TYPE_MAP: Dict[str, Optional[str]] = {
    # Core operational clauses
    "definitions-and-interpretation":           "Definitions & Interpretation",
    "purpose":                                  "Purpose",
    "scope-of-services":                        "Scope of Services",
    "statements-of-work-and-work-orders":       "Statements of Work & Work Orders",
    "customer-obligations":                     "Customer Obligations",
    "service-provider-obligations":             "Service Provider Obligations",
    "suspension-and-termination":               "Term & Termination",
    "term-and-termination":                     "Term & Termination",
    "term":                                     "Term & Termination",
    "termination":                              "Term & Termination",
    "deliverables-and-acceptance":              "Deliverables & Acceptance",
    "service-levels":                           "Service Levels",
    "support-and-maintenance":                  "Support & Maintenance",
    "change-control":                           "Change Control",
    "fees-and-payment":                         "Fees & Payment",
    # Risk & protection clauses
    "confidentiality":                          "Confidentiality",
    "data-protection-and-privacy":              "Data Protection & Privacy",
    "intellectual-property":                    "Intellectual Property",
    "representations-and-warranties":           "Representations & Warranties",
    "indemnification":                          "Indemnification",
    "limitation-of-liability":                  "Limitation of Liability",
    "insurance":                                "Insurance",
    "compliance":                               "Compliance",
    # Governance & boilerplate
    "audit-and-records":                        "Audit & Records",
    "subcontracting-and-sub-processors":        "Subcontractors",
    "assignment":                               "Assignment",
    "force-majeure":                            "Force Majeure",
    "governing-law-and-dispute-resolution":     "Governing Law & Dispute Resolution",
    "notices":                                  "Notices",
    "independent-contractor":                   "Independent Contractor",
    "order-of-precedence":                      "Order of Precedence",
    "general-provisions":                       "Miscellaneous / Other Provisions",
    # Document structure / metadata slugs (no criteria — resolve to None)
    "exit-and-transition-assistance":           "Term & Termination",   # TER-09–TER-17 live there
    "business-continuity-and-disaster-recovery": None,                  # covered by FOR-09 applies_when
    "governance-and-project-management":        None,                   # no dedicated criteria type
    "information-security-and-data-management": "Data Protection & Privacy",
    "publicity-and-use-of-name":                "Publicity and Use of Names",
    "personnel":                                None,                   # no dedicated criteria type
}


# ---------------------------------------------------------------------------
# JSON loading (singleton — loaded once at module import)
# ---------------------------------------------------------------------------
_CRITERIA_PATH = (
    Path(__file__).resolve().parent
    / "clause_mapping"
    / "MSA_Criteria_Final_Taxonomy_Calibrated.json"
)

_taxonomy: Optional[Dict] = None


def _load() -> Dict:
    global _taxonomy
    if _taxonomy is None:
        try:
            with open(_CRITERIA_PATH, encoding="utf-8") as fh:
                _taxonomy = json.load(fh)
            logger.info(
                "Criteria store: loaded %d types, %d criteria from %s",
                len(_taxonomy.get("types", {})),
                _taxonomy.get("metadata", {}).get("criteria_count", "?"),
                _CRITERIA_PATH.name,
            )
        except FileNotFoundError:
            logger.error(
                "Criteria store: taxonomy file not found at %s — all clauses will use general criteria.",
                _CRITERIA_PATH,
            )
            _taxonomy = {"types": {}, "metadata": {}}
    return _taxonomy


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def slug_to_display(slug: str) -> Optional[str]:
    """Convert a vector DB canonical_type slug to its taxonomy display name.

    Returns None when the slug has no defined criteria type (the evaluator
    will fall back to general criteria for that paragraph group).
    """
    return CANONICAL_TYPE_MAP.get(slug)


def display_to_slug(display_name: str) -> Optional[str]:
    """Convert a taxonomy display name back to its primary canonical slug."""
    for slug, disp in CANONICAL_TYPE_MAP.items():
        if disp == display_name:
            return slug
    return None


def get_criteria(display_name: str) -> List[dict]:
    """Return the list of criterion dicts for a taxonomy display name.

    Returns an empty list when the display name is unknown or has no criteria
    so callers can safely fall back to general criteria without crashing.
    """
    taxonomy = _load()
    type_entry = taxonomy.get("types", {}).get(display_name)
    if type_entry is None:
        return []
    criteria_dict = type_entry.get("criteria", {})
    return list(criteria_dict.values())


def get_criteria_by_slug(slug: str) -> List[dict]:
    """Convenience wrapper: slug → display name → criteria list.

    Returns an empty list for unknown or unmapped slugs.
    """
    display = slug_to_display(slug)
    if display is None:
        return []
    return get_criteria(display)


def get_answer_penalties() -> Dict[str, Optional[float]]:
    """Return the penalty map from the taxonomy metadata."""
    return _load().get("metadata", {}).get("answer_penalties", {
        "Met": 0.0,
        "Partly met": 0.5,
        "Not met": 1.0,
        "Unclear": 0.5,
        "Not applicable": None,
    })

