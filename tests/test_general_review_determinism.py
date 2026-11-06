"""Verification & determinism suite for the General Review backend (v3).

1. Inline lead-ins ("Damages. In no event...") are detected as clause starts.
2. "Intellectual Property Rights" maps to intellectual-property even when the
   vector DB says definitions-and-interpretation (the opening-definition bug).
3. Confidentiality, Non-Compete and Consulting Services map with the vector
   path offline.
4. Ten consecutive runs, alternating vector path up / down, give one
   identical (clause type, risk level) outcome.

Runs under pytest or directly: ``python tests/test_general_review_determinism.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.schemas.clause_mapping import ClauseMatch  # noqa: E402
from src.services.clause_mapping.policy_store import get_clause_policy_store  # noqa: E402
from src.services.clause_mapping.resilient_classifier import LAYER1_HEADING, LAYER3_FALLBACK, classify_clause_resilient  # noqa: E402
from src.services.general_review.clause_cache import ZDRClauseHashCache  # noqa: E402
from src.services.general_review.risk_matrix import compute_deterministic_risk_level  # noqa: E402
from src.services.general_review.segmentation import build_smart_embedding_query, detect_clause_start  # noqa: E402


def _policy(slug):
    policy = get_clause_policy_store().get(slug)
    assert policy is not None, slug
    return policy


def _classify(heading, paragraphs, hits=None):
    query = build_smart_embedding_query(heading, paragraphs)
    word_count = len(" ".join(paragraphs).split())
    return classify_clause_resilient(heading, query, word_count, hits)


def test_inline_lead_in_detection():
    samples = {
        "Damages. In no event will either Party be liable, whether arising in contract, tort, warranty or otherwise...": "Damages",
        "Governing Law and Choice of Venue. This Agreement will be governed by and construed in accordance with...": "Governing Law and Choice of Venue",
        "Intellectual Property Rights.": "Intellectual Property Rights",
    }
    for sample, expected in samples.items():
        is_start, heading = detect_clause_start(sample)
        assert is_start, f"Failed to detect clause start for: {sample}"
        assert heading == expected
        print(f"  [PASS] Detected start -> Heading: '{heading}'")


def test_ip_clause_is_not_hijacked_by_its_opening_definition():
    paragraphs = [
        "Intellectual Property Rights.",
        '"Intellectual Property" means all worldwide patents, copyrights, trade secrets, know-how, and mask works. '
        "Service Provider hereby irrevocably assigns to Accordor all right, title, and interest in all Deliverables, "
        "and grants Accordor an irrevocable power of attorney to execute documents.",
    ]
    query = build_smart_embedding_query("Intellectual Property Rights", paragraphs)
    assert "means all worldwide patents" not in query
    assert query.startswith("Intellectual Property Rights. Service Provider hereby irrevocably assigns")

    # The vector DB returning definitions-and-interpretation at 0.935 must not win.
    bad_hits = [ClauseMatch(canonical_type="definitions-and-interpretation", score=0.935)]
    resolved = _classify("Intellectual Property Rights", paragraphs, bad_hits)
    assert resolved.canonical_type == "intellectual-property", resolved
    assert resolved.source == LAYER1_HEADING
    assert _policy(resolved.canonical_type).inherent_risk_tier == "High"
    print(f"  [PASS] IP Clause mapped to '{resolved.canonical_type}' (source: {resolved.source})")


def test_previously_unmapped_clauses_map_with_vector_path_offline():
    cases = [
        ("Confidentiality", ["Confidentiality. Each Party agrees to hold the other Party's Confidential Information in strict confidence..."], "confidentiality"),
        ("Non-Compete", ["Non-Compete. During the term of this Agreement and for two years thereafter, Service Provider shall not engage in competing business..."], "non-compete"),
        ("Consulting Services", ["Consulting Services. Service Provider shall perform the consulting services described in each Statement of Work..."], "statements-of-work-and-work-orders"),
    ]
    for heading, paragraphs, expected in cases:
        resolved = _classify(heading, paragraphs, hits=None)
        assert resolved.canonical_type == expected, f"Expected {expected}, got {resolved}"
        print(f"  [PASS] '{heading}' -> '{resolved.canonical_type}' (tier: {_policy(expected).inherent_risk_tier})")


def test_ten_runs_are_identical_with_vector_path_flapping():
    paragraphs = [
        "Damages. In no event will either Party be liable, whether arising in contract, tort, warranty or "
        "otherwise, for any special, indirect, incidental, exemplary, punitive or consequential damages, "
        "including, but not limited to loss of anticipated profits."
    ]
    facts: dict = {
        "triggered_unacceptable_flags": [
            "The limit is clear: It is not stated, or the wording is contradictory (LOL-01)",
            "Liabilities that cannot be limited: None is addressed, or the clause limits them (LOL-04)",
        ],
        "falls_short_of_acceptable_standard": True,
        "is_materially_one_sided_against_client": False,
        "severity_category": "uncapped_or_severe_exposure",
    }

    outcomes = []
    for run in range(1, 11):
        hits = [ClauseMatch(canonical_type="limitation-of-liability", score=0.846)] if run % 2 else None
        resolved = _classify("Damages", paragraphs, hits)
        policy = _policy(resolved.canonical_type)
        risk = compute_deterministic_risk_level(
            policy.inherent_risk_tier,
            facts["triggered_unacceptable_flags"],
            facts["falls_short_of_acceptable_standard"],
            facts["is_materially_one_sided_against_client"],
            facts["severity_category"],
        )
        outcomes.append((resolved.canonical_type, risk))

    assert len(set(outcomes)) == 1, f"Drift detected! Outcomes: {set(outcomes)}"
    assert outcomes[0] == ("limitation-of-liability", "High")
    print(f"  [PASS] 10/10 runs (vector path up on odd runs, down on even) gave {outcomes[0]}")


def test_layer3_fallback_names_a_type_when_vector_path_is_down():
    # Unrecognised heading, but the opening words say what the clause does.
    resolved = _classify(
        "Protection of Accordor", ["Protection of Accordor. Service Provider shall indemnify and hold harmless Accordor from and against all third-party claims arising from the Services."], hits=None
    )
    assert resolved.canonical_type == "indemnification"
    assert resolved.source == LAYER3_FALLBACK


def test_clause_cache_key_is_stable_and_normalised():
    cache = ZDRClauseHashCache(version_hash="v")
    key_a = cache.compute_key("Damages.  In no event", "Damages", "Accordor", "MSA")
    key_b = cache.compute_key("damages. in no event", "Damages", "Accordor", "MSA")
    key_c = cache.compute_key("Damages. In no event", "Damages", "Amtrino", "MSA")
    assert key_a == key_b
    assert key_a != key_c


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            print(f"=== {name} ===")
            test()
    print("All checks passed.")
