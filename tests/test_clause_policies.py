"""The legal team's policy file and the vector DB seed that mirrors it."""

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.services.clause_mapping.policy_store import POLICIES_PATH, get_clause_policy_store

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_qdrant_archetypes.py"


def _rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = json.loads(POLICIES_PATH.read_text(encoding="utf-8"))
    return rows


def test_policy_file_has_37_unique_slugs():
    rows = _rows()
    slugs = [row["canonical_type"] for row in rows]
    assert len(slugs) == 37
    assert len(set(slugs)) == len(slugs)
    assert len(get_clause_policy_store()) == 37


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["canonical_type"])
def test_every_policy_row_is_fully_populated(row):
    assert row["inherent_risk_tier"] in ("High", "Medium", "Low")
    assert row["acceptable_standard"].strip(), "acceptable_standard is blank"
    assert isinstance(row["unacceptable_triggers"], list) and row["unacceptable_triggers"], "unacceptable_triggers is empty"
    assert all(trigger.strip() for trigger in row["unacceptable_triggers"])
    assert row["fallback_guidelines"].strip(), "fallback_guidelines is blank"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_qdrant_archetypes", SEED_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_archetypes_cover_every_policy_slug():
    seed = _load_seed_module()
    slugs = seed.load_policy_slugs()
    rows = seed.validate_archetypes(slugs)  # raises SystemExit on any gap
    assert set(slug for slug, _, _ in rows) == set(slugs)
    assert 150 <= len(rows) <= 220, len(rows)
    agreement_types = {agreement_type for _, agreement_type, _ in rows}
    assert {"MSA", "NDA", "SaaS", "SOW"} <= agreement_types


def test_seed_points_are_deterministic_and_match_search_payload():
    seed = _load_seed_module()
    rows = seed.validate_archetypes(seed.load_policy_slugs())
    ids = [seed.point_id(slug, text) for slug, _, text in rows]
    assert len(set(ids)) == len(ids)
    assert ids == [seed.point_id(slug, text) for slug, _, text in rows]  # stable across calls
    points = seed.build_points(rows[:2], [[0.0] * 4, [1.0] * 4])
    payload = points[0]["payload"]
    assert payload["label"] == "Clause" and payload["is_exhibit"] is False
    assert payload["canonical_type"] == rows[0][0] and payload["sub_type"] is None
    assert "dense" in points[0]["vector"]
