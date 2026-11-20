"""Validation script for calibrated general review pipeline:
1. Prompts Calibration check
2. Scorer Directional Asymmetry check
3. Deduplication check
4. Reviewer Context identity injection check
"""

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from src.schemas.general_review import Suggestion, GeneralReviewRequest
from src.services.party_resolver.models import EntityPersona, ClientInterests, PartyContext
from src.services.scorer import (
    AnswerRecord,
    is_client_favorable_asymmetry,
    compute_gap_score,
    score_group,
)
from src.tools.general_review import (
    _build_reviewer_context,
    _deduplicate_suggestions,
)


def test_scorer_directional_asymmetry():
    print("\n--- Test 1: Scorer Directional Asymmetry Filter ---")
    crit = {
        "criterion_id": "GEN-01",
        "question": "Are the obligations in this clause mutual or balanced?",
        "issue_category": "Balance",
        "weight_value": 3,
        "gate_if_not_met": "Medium",
    }

    # Case A: Client ("Consultant") holds the unilateral right
    ans_client_favorable = AnswerRecord(
        criterion_id="GEN-01",
        answer="Not met",
        quote="Consultant may terminate this Agreement at its sole discretion upon 10 days notice.",
        source_para_index=0,
    )
    is_fav = is_client_favorable_asymmetry(
        ans_client_favorable,
        crit,
        client_defined_term="Consultant",
        counterparty_defined_term="City",
    )
    assert is_fav, "Expected True: Consultant's unilateral right is a client asset!"
    print("✅ Consultant unilateral right identified as client-favorable asymmetry.")

    # Compute score in advocacy mode
    gap_score_adv = compute_gap_score(
        [ans_client_favorable],
        [crit],
        review_mode="advocacy",
        client_defined_term="Consultant",
        counterparty_defined_term="City",
    )
    assert gap_score_adv == 0.0, f"Expected gap_score 0.0 for client-favorable asset, got {gap_score_adv}"
    print("✅ Client-favorable asset contributed 0.0 penalty to gap_score in advocacy mode.")

    # Score group check: risk level should be Low (not Medium/High)
    res = score_group(
        [ans_client_favorable],
        [crit],
        review_mode="advocacy",
        client_defined_term="Consultant",
        counterparty_defined_term="City",
    )
    assert res.final_level == "Low", f"Expected Low risk level, got {res.final_level}"
    assert len(res.failed_answers) == 0, f"Expected 0 failed answers, got {len(res.failed_answers)}"
    print("✅ score_group correctly suppressed risk inflation and gate triggering for client-favorable asset.")

    # Case B: Counterparty ("City") holds the unilateral right (Adverse to client!)
    ans_counterparty_favorable = AnswerRecord(
        criterion_id="GEN-01",
        answer="Not met",
        quote="City may terminate this Agreement at its sole discretion upon 10 days notice.",
        source_para_index=0,
    )
    is_fav_cp = is_client_favorable_asymmetry(
        ans_counterparty_favorable,
        crit,
        client_defined_term="Consultant",
        counterparty_defined_term="City",
    )
    assert not is_fav_cp, "Expected False: Counterparty unilateral right is adverse to client!"
    gap_score_cp = compute_gap_score(
        [ans_counterparty_favorable],
        [crit],
        review_mode="advocacy",
        client_defined_term="Consultant",
        counterparty_defined_term="City",
    )
    assert gap_score_cp > 0.0, "Expected positive penalty for counterparty-favorable asymmetry!"
    res_cp = score_group(
        [ans_counterparty_favorable],
        [crit],
        review_mode="advocacy",
        client_defined_term="Consultant",
        counterparty_defined_term="City",
    )
    assert res_cp.final_level in ("Medium", "High"), f"Expected Medium/High risk, got {res_cp.final_level}"
    print("✅ Counterparty-favorable asymmetry correctly penalized and gated to Medium/High.")


def test_deduplication():
    print("\n--- Test 2: Deduplication Engine ---")
    s1 = Suggestion(
        clause="Notice of Events",
        reason="The clause lacks an objective financial standard for insolvency notification.",
        risk_level="High",
        original_text="Consultant shall give notice...",
        suggested_fix="Consultant shall give written notice to City within five (5) Business Days upon becoming aware of any material adverse change...",
        para_identifier="P1",
    )
    # Duplicate fix in another paragraph
    s2 = Suggestion(
        clause="Notice of Events",
        reason="The clause lacks an objective financial standard for insolvency notification.",
        risk_level="High",
        original_text="Consultant shall give notice...",
        suggested_fix="Consultant shall give written notice to City within five (5) Business Days upon becoming aware of any material adverse change...",
        para_identifier="P2",
    )
    # Different fix
    s3 = Suggestion(
        clause="Representations",
        reason="Authority representation is unilateral.",
        risk_level="Medium",
        original_text="City represents...",
        suggested_fix="Each party represents and warrants that it has full authority...",
        para_identifier="P3",
    )

    deduped = _deduplicate_suggestions([s1, s2, s3])
    assert len(deduped) == 2, f"Expected 2 suggestions after deduplication, got {len(deduped)}"
    assert deduped[0].para_identifier == "P1"
    assert deduped[1].para_identifier == "P3"
    print("✅ Deduplication successfully suppressed duplicate suggestion.")


def test_reviewer_context_identity_injection():
    print("\n--- Test 3: Reviewer Context Identity Rule ---")
    party_ctx = PartyContext(
        is_active=True,
        client=EntityPersona(
            name="Columbia Capital Management, LLC",
            defined_term="Consultant",
            transactional_position="Financial advisory firm",
        ),
        counterparty=EntityPersona(
            name="City of Westwood, Kansas",
            defined_term="City",
            transactional_position="Municipal corporation",
        ),
        client_interests=ClientInterests(
            assets_to_protect=["Compensation"],
            burdens_to_limit=["Indemnities"],
        ),
        agreement_nature="PSA",
        hired_counsel_directive="directive text",
    )
    req = GeneralReviewRequest(
        party_represented="Columbia Capital Management",
        agreement_type="Professional Services Agreement",
        textinformation=[],
    )
    ctx_str = _build_reviewer_context(req, party_context=party_ctx)
    assert 'Client Defined Term in Agreement: "Consultant" (SAME LEGAL ENTITY AS Columbia Capital Management)' in ctx_str
    assert 'MANDATORY IDENTITY RULE' in ctx_str
    assert 'Never treat "Consultant" as a separate company' in ctx_str
    print("✅ Reviewer context successfully injected mandatory entity identity rule.")


def main():
    test_scorer_directional_asymmetry()
    test_deduplication()
    test_reviewer_context_identity_injection()
    print("\n🎉 ALL CALIBRATION VALIDATION TESTS PASSED! 🎉\n")


if __name__ == "__main__":
    main()
