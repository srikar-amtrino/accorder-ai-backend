"""The MSA criteria taxonomy, as the general review uses it at runtime.

clause_policies.json is generated from the taxonomy (see
scripts/build_policies_from_taxonomy.py): every criterion whose failure is a
hard defect becomes an unacceptable trigger of its clause type, worded
"<criterion>: <not met when> (<id>)". This module reads the same file to
answer two questions about a trigger the model reported:

- its gate level (``gate_if_not_met`` of Medium or above), which sets a floor
  under the computed risk level — AMD-02, a party's right to change the terms
  alone, always makes a finding High;
- whether it is a Balance criterion (one-sidedness), which cannot have been
  set off by a clause that runs in the client's favor.

The taxonomy file is optional: without it no trigger is a gate or a Balance
criterion, and risk levels come from the risk matrix alone.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from src.config.logging import get_logger

logger = get_logger(__name__)

TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "MSA_Criteria_Final_Taxonomy.json"

# Types and triggers the workbook does not have yet, in the taxonomy's own schema.
OVERRIDES_PATH = Path(__file__).resolve().parents[1] / "clause_mapping" / "taxonomy_overrides.json"

# gate_if_not_met values, weakest first. "None" is not a gate.
GATE_LEVELS = ("Medium", "High", "Critical")

BALANCE = "Balance"

# The criterion id a trigger ends with: "... (AMD-02)".
_TRIGGER_ID_RE = re.compile(r"\(([A-Z]{2,4}-\d{2})\)\s*\.?\s*$")


@dataclass(frozen=True)
class Criterion:
    """The facts about one taxonomy criterion the review needs."""

    criterion_id: str
    clause_type: str
    criterion: str
    not_met_when: str
    gate: Optional[str]
    issue_category: str

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
def load_criteria(path: Path = TAXONOMY_PATH, overrides_path: Path = OVERRIDES_PATH) -> Dict[str, Criterion]:
    """Every criterion by id, from the taxonomy and its overrides' extra types. Empty when the taxonomy file is absent or unreadable."""

    if not path.is_file():
        logger.warning("Criteria taxonomy not found; no trigger is treated as a gate or a balance criterion.", path=str(path))
        return {}
    try:
        types = {**json.loads(path.read_text(encoding="utf-8"))["types"], **load_overrides(overrides_path).get("extra_types", {})}
    except (ValueError, KeyError):
        logger.exception("Criteria taxonomy could not be read; no trigger is treated as a gate or a balance criterion.")
        return {}
    criteria: Dict[str, Criterion] = {}
    for clause_type, entry in types.items():
        for criterion_id, criterion in entry.get("criteria", {}).items():
            gate = criterion.get("gate_if_not_met")
            criteria[criterion_id] = Criterion(
                criterion_id, clause_type, criterion.get("criterion", ""), criterion.get("not_met_when", ""), gate if gate in GATE_LEVELS else None, criterion.get("issue_category", "")
            )
    return criteria


def load_gates(path: Path = TAXONOMY_PATH) -> Dict[str, Tuple[Criterion, ...]]:
    """Gating criteria by taxonomy clause type."""

    gates: Dict[str, Tuple[Criterion, ...]] = {}
    for criterion in load_criteria(path).values():
        if criterion.gate is not None:
            gates[criterion.clause_type] = (*gates.get(criterion.clause_type, ()), criterion)
    return gates


def criterion_id_of(flag: str) -> Optional[str]:
    """The criterion id a trigger ends with ("... (LAW-03)" -> "LAW-03"), or None."""

    match = _TRIGGER_ID_RE.search(flag or "")
    return match.group(1) if match else None


def _criterion_of(flag: str, path: Path) -> Optional[Criterion]:
    criterion_id = criterion_id_of(flag)
    return load_criteria(path).get(criterion_id) if criterion_id else None


def gate_level(flag: str, path: Path = TAXONOMY_PATH) -> Optional[str]:
    """The gate level of a triggered flag, or None when the flag is not a taxonomy gate."""

    criterion = _criterion_of(flag, path)
    return criterion.gate if criterion is not None else None


def strongest_gate(flags: Iterable[str], path: Path = TAXONOMY_PATH) -> Optional[str]:
    """The strongest gate level among triggered flags, or None when none of them is a gate."""

    levels = [level for level in (gate_level(flag, path) for flag in flags) if level is not None]
    return max(levels, key=GATE_LEVELS.index) if levels else None


def is_balance_trigger(flag: str, path: Path = TAXONOMY_PATH) -> bool:
    """True for a trigger from a Balance criterion — one that complains a clause favors one party."""

    criterion = _criterion_of(flag, path)
    return criterion is not None and criterion.issue_category == BALANCE
