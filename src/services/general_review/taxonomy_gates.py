"""The MSA criteria taxonomy, as the general review uses it at runtime.

The taxonomy lists, for every clause type, the criteria a clause of that type
is judged by. Each criterion carries the question to answer, the tests for
Met / Partly met / Not met, the condition it applies under, its weight (Core,
Major, Minor), its gate, and its issue category. The review asks the model
those questions as they stand (criteria_for_type) and computes each finding's
risk level from the weight and gate (risk_matrix).

scripts/build_policies_from_taxonomy.py reads the same file to generate
clause_policies.json, which supplies each clause type's inherent risk tier.

The taxonomy file is optional: without it every clause is reviewed against
the general checklist.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple

from src.config.logging import get_logger

logger = get_logger(__name__)

TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "MSA_Criteria_Final_Taxonomy.json"

# Types and triggers the workbook does not have yet, in the taxonomy's own schema.
OVERRIDES_PATH = Path(__file__).resolve().parents[1] / "clause_mapping" / "taxonomy_overrides.json"

# The legal team's clause-by-clause risk classification: each clause type's risk allocation and fallback position.
LEGAL_STANDARDS_PATH = Path(__file__).resolve().parents[1] / "clause_mapping" / "legal_clause_standards.json"

# gate_if_not_met values, weakest first. "None" is not a gate.
GATE_LEVELS = ("Medium", "High", "Critical")

BALANCE = "Balance"

# applies_when of a criterion that applies to every clause of its type.
ALWAYS = "Always"

# Criterion weights, strongest first.
CORE = "Core"
MAJOR = "Major"
MINOR = "Minor"


@dataclass(frozen=True)
class Criterion:
    """The facts about one taxonomy criterion the review needs."""

    criterion_id: str
    clause_type: str
    criterion: str
    not_met_when: str
    gate: Optional[str]
    issue_category: str
    question: str = ""
    met_when: str = ""
    partly_met_when: str = ""
    applies_when: str = ALWAYS
    weight: str = MAJOR
    why_it_matters: str = ""

    @property
    def conditional(self) -> bool:
        """True when the criterion applies only to a clause that does what applies_when describes."""

        return self.applies_when.strip().casefold() != ALWAYS.casefold()

    @property
    def trigger(self) -> str:
        return trigger_text(self.criterion_id, self.criterion, self.not_met_when)


def trigger_text(criterion_id: str, criterion: str, not_met_when: str) -> str:
    """A taxonomy criterion worded as an unacceptable trigger: what it checks, how it fails, and its id.

    The taxonomy's not_met_when texts are elliptical ("It is not required."),
    so the criterion's title leads: "Assistance is required: It is not
    required (TER-10)". clause_policies.json uses exactly this wording.
    """

    return f"{criterion.strip().rstrip('.')}: {not_met_when.strip().rstrip('.')} ({criterion_id})"


def taxonomy_slug(name: str) -> str:
    """The canonical_type slug of a taxonomy clause type: "Term & Termination" -> "term-and-termination"."""

    return "-".join(re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).split())


def load_overrides(path: Path = OVERRIDES_PATH) -> Dict[str, Any]:
    """The taxonomy overrides (extra types and extra triggers); empty when the file is absent."""

    if not path.is_file():
        return {}
    overrides: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return overrides


@lru_cache(maxsize=4)
def provider_protective_criteria(path: Path = OVERRIDES_PATH) -> FrozenSet[str]:
    """Ids of the criteria whose missing term protects only the service provider.

    "Exclusions are stated" asks for limits on what the provider must deliver;
    "Timing for customer actions" asks for deadlines on the customer. A customer's
    counsel does not ask for either, so the review does not raise them when
    the represented party is the customer.
    """

    return frozenset(load_overrides(path).get("provider_protective", []))


@lru_cache(maxsize=4)
def relocation_criteria(path: Path = OVERRIDES_PATH) -> FrozenSet[str]:
    """Ids of the criteria that can only be resolved by moving wording into another clause.

    A suggestion replaces one paragraph. It cannot take an obligation out of
    a definition and put it where it belongs, and striking it would give the
    obligation up, so these criteria are not raised.
    """

    return frozenset(load_overrides(path).get("needs_relocation", []))


def load_legal_standards(path: Path = LEGAL_STANDARDS_PATH) -> Dict[str, Dict[str, str]]:
    """The legal team's standard for each clause type it classifies, by canonical_type slug; empty when the file is absent."""

    if not path.is_file():
        return {}
    clauses: Dict[str, Dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))["clauses"]
    return {taxonomy_slug(name): entry for name, entry in clauses.items()}


@lru_cache(maxsize=4)
def load_criteria(path: Path = TAXONOMY_PATH, overrides_path: Path = OVERRIDES_PATH) -> Dict[str, Criterion]:
    """Every criterion by id, from the taxonomy and its overrides' extra types. Empty when the taxonomy file is absent or unreadable."""

    if not path.is_file():
        logger.warning("Criteria taxonomy not found; no trigger is treated as a gate or a balance criterion.", path=str(path))
        return {}
    try:
        overrides = load_overrides(overrides_path)
        types = {**json.loads(path.read_text(encoding="utf-8"))["types"], **overrides.get("extra_types", {})}
    except (ValueError, KeyError):
        logger.exception("Criteria taxonomy could not be read; no trigger is treated as a gate or a balance criterion.")
        return {}
    # Criteria the legal team treats as hard defects although the workbook weights them lower (LAW-03): weighted Core.
    hard = {criterion_id for ids in overrides.get("extra_triggers", {}).values() for criterion_id in ids}
    criteria: Dict[str, Criterion] = {}
    for clause_type, entry in types.items():
        for criterion_id, criterion in entry.get("criteria", {}).items():
            gate = criterion.get("gate_if_not_met")
            partly = criterion.get("partly_met_when") or ""
            criteria[criterion_id] = Criterion(
                criterion_id,
                clause_type,
                criterion.get("criterion", ""),
                criterion.get("not_met_when", ""),
                gate if gate in GATE_LEVELS else None,
                criterion.get("issue_category", ""),
                question=criterion.get("question_for_llm", ""),
                met_when=criterion.get("met_when", ""),
                partly_met_when="" if partly.strip().casefold() in ("", "none") else partly,
                applies_when=criterion.get("applies_when") or ALWAYS,
                weight=CORE if criterion_id in hard else (criterion.get("weight") or MAJOR),
                why_it_matters=criterion.get("why_it_matters", ""),
            )
    return criteria


@lru_cache(maxsize=4)
def _criteria_by_slug(path: Path = TAXONOMY_PATH, overrides_path: Path = OVERRIDES_PATH) -> Dict[str, Tuple[Criterion, ...]]:
    by_slug: Dict[str, Tuple[Criterion, ...]] = {}
    for criterion in load_criteria(path, overrides_path).values():
        slug = taxonomy_slug(criterion.clause_type)
        by_slug[slug] = (*by_slug.get(slug, ()), criterion)
    return by_slug


def criteria_for_type(canonical_type: Optional[str], path: Path = TAXONOMY_PATH) -> Tuple[Criterion, ...]:
    """The checklist of a clause type, by its canonical_type slug, in the taxonomy's own order; empty for an unknown type."""

    return _criteria_by_slug(path).get(canonical_type or "", ())
