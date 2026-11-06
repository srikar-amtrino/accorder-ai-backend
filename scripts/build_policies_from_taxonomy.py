"""Build src/services/clause_mapping/clause_policies.json from the MSA criteria taxonomy.

The taxonomy (MSA_Criteria_Final_Taxonomy.json) is the source of truth for
clause types and what each must satisfy. It scores criteria; the general
review works from one policy row per clause type. This script is the mapping
between the two, so the policy file is never edited by hand:

    poetry run python scripts/build_policies_from_taxonomy.py          # rewrite the policy file
    poetry run python scripts/build_policies_from_taxonomy.py --check  # fail if the file is out of date

One row per taxonomy type in the "Clause" group, then one per extra type in
src/services/clause_mapping/taxonomy_overrides.json (types the workbook does not
have yet, such as Non-Compete, written in the taxonomy's own schema). Non-clause
types (preamble, recitals, party identification, signature block, ...) get no
row: the general review never flags non-clause content.

    canonical_type        the type's name as a slug ("Term & Termination" -> "term-and-termination").
    inherent_risk_tier    from the type's gates: High when failing a criterion gates at High or Critical,
                          Medium when the strongest gate is Medium. A type with no gate is Medium when it has
                          a Core criterion and Low when it has none.
    acceptable_standard   the type's criteria as numbered checks, each in the taxonomy's own question wording;
                          a conditional criterion is prefixed with the condition it applies under, and a
                          Balance criterion is marked [Balance] (the review prompt treats it as met whenever
                          the imbalance favors the client).
    unacceptable_triggers the criteria whose failure is a hard defect — every gate, every Core criterion, and
                          any criterion the overrides' extra_triggers name (LAW-03) — worded
                          "<criterion>: <not met when> (<id>)".
    fallback_guidelines   empty: the taxonomy carries no fallback positions.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.clause_mapping.policy_store import POLICIES_PATH  # noqa: E402
from src.services.general_review.taxonomy_gates import BALANCE, GATE_LEVELS, TAXONOMY_PATH, load_overrides, taxonomy_slug, trigger_text  # noqa: E402

CLAUSE_GROUP = "Clause"
ALWAYS = "Always"
CORE = "Core"


def _tier(criteria: List[Dict[str, Any]]) -> str:
    gates = [criterion["gate_if_not_met"] for criterion in criteria if criterion["gate_if_not_met"] in GATE_LEVELS]
    if gates:
        strongest = max(gates, key=GATE_LEVELS.index)
        return "Medium" if strongest == "Medium" else "High"
    return "Medium" if any(criterion["weight"] == CORE for criterion in criteria) else "Low"


def _check(number: int, criterion: Dict[str, Any]) -> str:
    condition = criterion["applies_when"].strip()
    scope = "" if condition == ALWAYS else f"[Only where {condition[0].lower()}{condition[1:].rstrip('.')}] "
    balance = "[Balance] " if criterion["issue_category"] == BALANCE else ""
    return f"({number}) {balance}{scope}{criterion['question_for_llm'].strip()}"


def build_policies(taxonomy: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """One policy row per clause type of the taxonomy, in the taxonomy's own order, then the overrides' extra types."""

    overrides = overrides or {}
    extra_triggers: Dict[str, List[str]] = overrides.get("extra_triggers", {})
    types = {**taxonomy["types"], **overrides.get("extra_types", {})}
    unknown = sorted(set(extra_triggers) - set(types))
    if unknown:
        raise SystemExit(f"taxonomy_overrides.json names extra_triggers for unknown types: {unknown}")

    rows: List[Dict[str, Any]] = []
    for name, entry in types.items():
        if entry["group"] != CLAUSE_GROUP:
            continue
        criteria = list(entry["criteria"].values())
        missing = sorted(set(extra_triggers.get(name, [])) - set(entry["criteria"]))
        if missing:
            raise SystemExit(f"taxonomy_overrides.json names criteria {name} does not have: {missing}")
        hard = [criterion for criterion in criteria if criterion["gate_if_not_met"] in GATE_LEVELS or criterion["weight"] == CORE or criterion["criterion_id"] in extra_triggers.get(name, [])]
        rows.append(
            {
                "canonical_type": taxonomy_slug(name),
                "inherent_risk_tier": _tier(criteria),
                "acceptable_standard": "The clause should satisfy each of these checks. " + " ".join(_check(number, criterion) for number, criterion in enumerate(criteria, start=1)),
                "unacceptable_triggers": [trigger_text(criterion["criterion_id"], criterion["criterion"], criterion["not_met_when"]) for criterion in hard],
                "fallback_guidelines": "",
            }
        )
    return rows


def render(rows: List[Dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Exit non-zero if clause_policies.json differs from what the taxonomy produces; write nothing.")
    args = parser.parse_args()

    rendered = render(build_policies(json.loads(TAXONOMY_PATH.read_text(encoding="utf-8")), load_overrides()))
    if args.check:
        if POLICIES_PATH.read_text(encoding="utf-8") != rendered:
            raise SystemExit(f"{POLICIES_PATH} is out of date with {TAXONOMY_PATH.name}; run this script without --check.")
        print("clause_policies.json is up to date with the taxonomy.")
        return
    POLICIES_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {len(json.loads(rendered))} clause policies to {POLICIES_PATH}.")


if __name__ == "__main__":
    main()
