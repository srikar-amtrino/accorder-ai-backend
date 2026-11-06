"""The policy file, the criteria taxonomy it is generated from, and the vector DB seed that mirrors it."""

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.services.clause_mapping.policy_store import POLICIES_PATH, get_clause_policy_store
from src.services.clause_mapping.resilient_classifier import HEADING_TO_CANONICAL_TYPE, LEGACY_CANONICAL_TYPES, current_canonical_type
from src.services.general_review.taxonomy_gates import TAXONOMY_PATH, load_criteria, load_overrides, taxonomy_slug

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_qdrant_archetypes.py"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_policies_from_taxonomy.py"


def _rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = json.loads(POLICIES_PATH.read_text(encoding="utf-8"))
    return rows


def _taxonomy() -> Dict[str, Any]:
    taxonomy: Dict[str, Any] = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    return taxonomy


def _all_types() -> Dict[str, Any]:
    """The taxonomy's types followed by the overrides' extra types, as the generator merges them."""

    return {**_taxonomy()["types"], **load_overrides().get("extra_types", {})}


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy_file_is_generated_from_the_taxonomy():
    build = _load(BUILD_SCRIPT, "build_policies_from_taxonomy")
    assert POLICIES_PATH.read_text(encoding="utf-8") == build.render(build.build_policies(_taxonomy(), load_overrides())), "run scripts/build_policies_from_taxonomy.py"


def test_one_policy_per_taxonomy_clause_type():
    clause_types = [name for name, entry in _all_types().items() if entry["group"] == "Clause"]
    slugs = [row["canonical_type"] for row in _rows()]
    assert slugs == [taxonomy_slug(name) for name in clause_types]
    assert len(slugs) == len(set(slugs)) == 45  # the workbook's 43 clause types, plus Non-Compete and Non-Solicitation
    assert len(get_clause_policy_store()) == 45
    assert slugs[-2:] == ["non-compete", "non-solicitation"]
    # Non-clause content (preamble, recitals, signature block, ...) has no policy: it is never reviewed.
    assert get_clause_policy_store().get("signature-block") is None and get_clause_policy_store().get("preamble") is None


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["canonical_type"])
def test_every_policy_row_carries_its_criteria(row):
    assert row["inherent_risk_tier"] in ("High", "Medium", "Low")
    assert row["acceptable_standard"].startswith("The clause should satisfy each of these checks. (1) ")
    assert isinstance(row["unacceptable_triggers"], list)
    assert all(trigger.strip() for trigger in row["unacceptable_triggers"])
    assert row["fallback_guidelines"] == ""  # the taxonomy carries no fallback positions


def test_every_gate_and_core_criterion_is_a_trigger_and_sets_the_tier():
    rows = {row["canonical_type"]: row for row in _rows()}
    criteria = load_criteria()
    extra_triggers = load_overrides()["extra_triggers"]
    for name, entry in _all_types().items():
        if entry["group"] != "Clause":
            continue
        row = rows[taxonomy_slug(name)]
        hard = [
            criterion_id
            for criterion_id, criterion in entry["criteria"].items()
            if criterion["weight"] == "Core" or criterion["gate_if_not_met"] != "None" or criterion_id in extra_triggers.get(name, [])
        ]
        assert row["unacceptable_triggers"] == [criteria[criterion_id].trigger for criterion_id in hard], name
    assert rows["amendments-and-modifications"]["unacceptable_triggers"] == ["No one-sided changes: A party may change terms alone without limit (AMD-02)"]
    assert rows["amendments-and-modifications"]["inherent_risk_tier"] == "High"  # AMD-02 gates at High
    assert rows["fees-and-payment"]["inherent_risk_tier"] == "High"
    assert rows["notices"]["inherent_risk_tier"] == "Low" and rows["notices"]["unacceptable_triggers"] == []  # no Core criterion, no gate
    assert "[Balance] Is any right of one party to change the terms alone" in rows["amendments-and-modifications"]["acceptable_standard"]
    assert "[Only where the clause allows termination without cause (for convenience)]" in rows["term-and-termination"]["acceptable_standard"]


def test_overrides_add_the_forum_conflict_trigger_and_the_restrictive_covenant_types():
    rows = {row["canonical_type"]: row for row in _rows()}
    assert rows["governing-law-and-dispute-resolution"]["unacceptable_triggers"][-1] == "No conflicting provisions: There is a conflict (LAW-03)"
    non_compete = rows["non-compete"]
    assert non_compete["inherent_risk_tier"] == "Medium"
    assert non_compete["unacceptable_triggers"] == [
        "Duration is limited: It lasts more than 12 months after the agreement ends, or has no end date (NCP-01)",
        "Client is not restricted: The client is restricted (NCP-04)",
    ]
    assert "(4) [Balance] Does the clause leave the client free to engage other vendors" in non_compete["acceptable_standard"]
    assert "California law generally voids post-termination non-competes" in non_compete["acceptable_standard"]
    assert "minimum volume" not in non_compete["acceptable_standard"]  # that is Non-Exclusivity's check, not a non-compete's
    assert rows["non-solicitation"]["inherent_risk_tier"] == "Medium"
    assert rows["non-solicitation"]["unacceptable_triggers"] == ["Restriction is even-handed: Only the client is restricted (NSO-01)"]
    # The restored types' criteria are known at runtime, so their Balance triggers are recognised.
    assert load_criteria()["NCP-04"].issue_category == "Balance" and load_criteria()["NSO-01"].issue_category == "Balance"


def test_overrides_naming_unknown_types_or_criteria_fail_the_build():
    build = _load(BUILD_SCRIPT, "build_policies_from_taxonomy")
    with pytest.raises(SystemExit, match="unknown types"):
        build.build_policies(_taxonomy(), {"extra_triggers": {"No Such Type": ["X-01"]}})
    with pytest.raises(SystemExit, match="does not have"):
        build.build_policies(_taxonomy(), {"extra_triggers": {"Governing Law & Dispute Resolution": ["LAW-99"]}})


def test_router_and_policies_name_the_same_types():
    slugs = {row["canonical_type"] for row in _rows()}
    assert {slug for _, slug in HEADING_TO_CANONICAL_TYPE} == slugs


def test_legacy_vector_labels_map_to_current_types():
    slugs = {row["canonical_type"] for row in _rows()}
    assert {target for target in LEGACY_CANONICAL_TYPES.values() if target is not None} <= slugs
    assert not set(LEGACY_CANONICAL_TYPES) & slugs
    assert current_canonical_type("non-compete") == "non-compete" and current_canonical_type("non-solicitation") == "non-solicitation"
    assert current_canonical_type("general-provisions") is None  # split into seven types; the label no longer says which
    assert current_canonical_type("confidentiality") == "confidentiality"


def test_seed_archetypes_cover_every_policy_slug():
    seed = _load(SEED_SCRIPT, "seed_qdrant_archetypes")
    slugs = seed.load_policy_slugs()
    rows = seed.validate_archetypes(slugs)  # raises SystemExit on any gap
    assert set(slug for slug, _, _ in rows) == set(slugs)
    assert seed.MIN_PER_SLUG * len(slugs) <= len(rows) <= seed.MAX_PER_SLUG * len(slugs)
    agreement_types = {agreement_type for _, agreement_type, _ in rows}
    assert {"MSA", "NDA", "SaaS", "SOW"} <= agreement_types


def test_seed_points_are_deterministic_and_match_search_payload():
    seed = _load(SEED_SCRIPT, "seed_qdrant_archetypes")
    rows = seed.validate_archetypes(seed.load_policy_slugs())
    ids = [seed.point_id(slug, text) for slug, _, text in rows]
    assert len(set(ids)) == len(ids)
    assert ids == [seed.point_id(slug, text) for slug, _, text in rows]  # stable across calls
    points = seed.build_points(rows[:2], [[0.0] * 4, [1.0] * 4])
    payload = points[0]["payload"]
    assert payload["label"] == "Clause" and payload["is_exhibit"] is False
    assert payload["canonical_type"] == rows[0][0] and payload["sub_type"] is None
    assert "dense" in points[0]["vector"]
