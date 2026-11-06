"""Determinism suite for the General Review on the Master Services Agreement fixture.

Three live runs on this agreement gave 23, 23 and 22 suggestions, with risk
levels and sub-paragraph anchors that moved between runs. Everything below
runs offline: the model is replaced by a script that reproduces the drift seen
in those runs (a different anchor paragraph, the parties read backwards, a
survival clause reported missing, address typos, a jurisdiction swapped), and
the pipeline has to return the same paragraphs and risk levels every time.

What this proves is that the code settles those cases whatever the model
does. It cannot prove the live model never drifts in a way not scripted here.
"""

import asyncio
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import docx
import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from src.schemas.general_review import GeneralReviewFindings, GeneralReviewRequest, TextInformation
from src.services.docx_text import paragraph_text
from src.services.general_review import party_resolver as pr, segmentation as sg, taxonomy_gates
from src.services.general_review.clause_cache import ZDRClauseHashCache
from src.services.general_review.document_facts import apply_blank_field_guard, build_document_facts
from src.services.general_review.risk_matrix import compute_deterministic_risk_level, drop_direction_contradicted_flags
from src.tools import general_review as gr

FIXTURE = Path(__file__).parent / "fixtures" / "master_services_agreement.json"

HEADINGS = ["para_8", "para_11", "para_15", "para_19", "para_28", "para_34", "para_40", "para_45"]
# Non-clause content: preamble and recitals, the invoice and notice address blocks, the signature block.
NEVER_FLAGGED = [f"para_{n}" for n in [*range(1, 7), *range(22, 28), *range(55, 63), *range(69, 75)]]
# Routine clauses: Notices (and its deemed-receipt paragraph), Headings, Counterparts, Severability, Modification/Waiver.
BOILERPLATE = ["para_54", "para_63", "para_64", "para_65", "para_66", "para_67"]
# Triggers from clause_policies.json, generated from the criteria taxonomy.
AMENDMENT_GATE = "No one-sided changes: A party may change terms alone without limit (AMD-02)"  # High gate, Balance
FEE_INCREASE_TRIGGER = "Fee changes are limited: The change is unrestricted (FEE-06)"  # High gate, Balance
WITHHOLD_TRIGGER = "Disputed invoices: No right to dispute is stated (FEE-07)"  # Medium gate
ASSIGNMENT_TRIGGER = "Consent for assignment: It is not addressed (ASG-01)"  # Core, Balance
COUNTERPARTS_TRIGGER = "Counterparts are allowed: This is not stated (CTP-01)"  # Core
PERFORMANCE_TRIGGER = "Performance standard: No standard is stated (REP-02)"  # Core
LIABILITY_BALANCE_TRIGGER = "The limit applies fairly: It protects one party only (LOL-02)"  # Core, Balance

# Wordings from the policy set the taxonomy replaced. The model may still produce them; none is a trigger now.
URL_AMENDMENT_TRIGGER = "unilateral amendment right allowing vendor to modify agreement terms by posting updated terms at a URL"
EXPENSE_TRIGGER = "reimbursement of travel or out-of-pocket expenses without prior written approval by client"
NON_COMPETE_TRIGGER = "restriction preventing client from engaging other vendors or building similar solutions internally"
ONE_WAY_CONFIDENTIALITY_TRIGGER = "one-sided confidentiality protecting only counterparty's information"
POWER_OF_ATTORNEY_TRIGGER = "broad power of attorney granted to counterparty without notice or failure-to-sign precondition"
DEEMED_RECEIPT_TRIGGER = "deemed receipt immediately upon mailing rather than actual delivery/courier confirmation"

CARVE_OUT = "Notwithstanding the foregoing, each Party retains its Background IP. Service Provider grants Accordor a perpetual, royalty-free license to any Background IP embedded in the Deliverables."
WITHHOLD = "Accordor may withhold any amount it disputes in good faith, without interest, until the dispute is resolved."
SURVIVAL_REASON = "The Entire Agreement clause does not include a survival provision identifying which obligations survive expiration or termination of the Agreement."


@pytest.fixture(scope="module")
def msa() -> GeneralReviewRequest:
    return GeneralReviewRequest.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def plan(msa: GeneralReviewRequest) -> gr.ReviewPlan:
    return gr.build_review_plan(msa, len(gr._build_batches(msa.textinformation)), ZDRClauseHashCache(version_hash="plan"))


def _text(request: GeneralReviewRequest, para_id: str) -> str:
    return next(para.text for para in request.textinformation if para.paraindetifier == para_id)


def _position(request: GeneralReviewRequest, para_id: str) -> int:
    return next(idx for idx, para in enumerate(request.textinformation) if para.paraindetifier == para_id)


def _finding(text: str, append: Optional[str] = None, **facts: Any) -> Dict[str, Any]:
    """A model finding on a paragraph: by default a small edit to its wording, or with append, a sentence added to it."""

    base: Dict[str, Any] = {
        "clause": "Clause",
        "reason": "Reason.",
        "original_text": text,
        "suggested_fix": f"{text} {append}" if append else text.replace(" the ", " that ", 1),
        "triggered_unacceptable_flags": [],
        "falls_short_of_acceptable_standard": True,
        "is_materially_one_sided_against_client": False,
        "severity_category": "material_commercial_deviation",
    }
    base.update(facts)
    return base


def _shown(request: GeneralReviewRequest, plan: gr.ReviewPlan, para_id: str) -> str:
    """The paragraph as the model is shown it — what a real finding's original_text is copied from."""

    return plan.shown_text.get(_position(request, para_id), _text(request, para_id))


def _validate(request: GeneralReviewRequest, plan: gr.ReviewPlan, script: Dict[str, Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Run scripted findings through grounding and the deterministic rules, with offline clause policies, over the whole document."""

    return [(para_id, suggestion.risk_level) for para_id, suggestion in _suggestions(request, plan, script).items()]


def _suggestions(request: GeneralReviewRequest, plan: gr.ReviewPlan, script: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    batch = [(idx, para) for idx, para in enumerate(request.textinformation) if para.text.strip()]
    findings = GeneralReviewFindings.model_validate({"findings": [_finding(_shown(request, plan, para_id), **facts) for para_id, facts in script.items()]}).findings
    return {suggestion.para_identifier: suggestion for _, suggestion in gr._validate_batch(findings, batch, set(), plan.titles, _offline_policies(request, plan), plan)}


def _offline_policies(request: GeneralReviewRequest, plan: gr.ReviewPlan) -> gr.BatchPolicies:
    """The policy each paragraph gets from its clause heading alone, with the vector path down."""

    policy_of = {}
    for position, clause_type in plan.structure.clause_type_of.items():
        policy = gr.policy_for_clause(clause_type, request.agreement_type, plan.titles.get(request.textinformation[position].paraindetifier, ""))
        if policy is not None:
            policy_of[position] = policy
    return gr.BatchPolicies(block="", policy_of=policy_of)


# --- 1. party alias and role resolution ---------------------------------------


def test_aci_resolves_to_accordor(msa: GeneralReviewRequest) -> None:
    assert msa.party_represented == "ACI"
    parties = pr.resolve_party_context(msa.party_represented, msa.textinformation)
    assert parties is not None and parties.resolved
    assert parties.method == pr.METHOD_ACRONYM
    assert parties.represented is not None and parties.counterparty is not None
    assert parties.represented.alias == "Accordor"
    assert parties.represented.describe() == 'Accordor Inc. (defined as "Accordor", Role: Client / Customer)'
    assert parties.counterparty.describe() == 'Amtrino Technologies (defined as "Service Provider", Role: Vendor / Service Provider)'


def test_party_resolution_is_the_same_however_the_client_is_named(msa: GeneralReviewRequest) -> None:
    tokens = set()
    for wording in ("ACI", "aci", "Accordor", "Accordor Inc.", "accordor inc", "Customer", "Client"):
        parties = pr.resolve_party_context(wording, msa.textinformation)
        assert parties is not None and parties.represented is not None, wording
        assert parties.represented.alias == "Accordor", wording
        tokens.add(parties.cache_token)
    assert len(tokens) == 1
    for wording in ("Amtrino", "Service Provider", "Vendor", "AT"):
        parties = pr.resolve_party_context(wording, msa.textinformation)
        assert parties is not None and parties.represented is not None and parties.represented.alias == "Service Provider", wording


def test_an_acronym_never_matches_the_wrong_party(msa: GeneralReviewRequest) -> None:
    # "ACI" is a subsequence of "Amtrino TeChnologIes", but it is not an acronym of it.
    assert pr.is_acronym_of("ACI", "Accordor Inc.")
    assert not pr.is_acronym_of("ACI", "Amtrino Technologies")
    assert not pr.is_acronym_of("ACI", "Service Provider")
    unresolved = pr.resolve_party_context("XYZ", msa.textinformation)
    assert unresolved is not None and not unresolved.resolved
    assert unresolved.render_block() == ""
    assert pr.resolve_party_context(None, msa.textinformation) is None


def test_prompt_names_the_client_by_its_defined_term(msa: GeneralReviewRequest) -> None:
    request = msa.model_copy(update={"specific_concerns": "ACI wants to keep ownership of everything ACI pays for."})
    plan = gr.build_review_plan(request, 5, ZDRClauseHashCache(version_hash="prompt"))
    assert "ACI" not in plan.reviewer_context
    assert 'Party the client represents: Accordor Inc. — referred to in this agreement as "Accordor"' in plan.reviewer_context
    assert "Accordor wants to keep ownership of everything Accordor pays for." in plan.reviewer_context
    assert 'Represented Party (your client): Accordor Inc. (defined as "Accordor", Role: Client / Customer)' in plan.party_block
    assert 'Counterparty: Amtrino Technologies (defined as "Service Provider", Role: Vendor / Service Provider)' in plan.party_block
    # The shorthand survives only where the block says what it means.
    assert plan.party_block.count("ACI") == 1 and '"ACI" means this same party' in plan.party_block


# --- 1b. directionality -------------------------------------------------------


@pytest.mark.parametrize("para_id", ["para_9", "para_29", "para_38", "para_52", "para_44"])
def test_clauses_binding_only_the_service_provider_favor_accordor(msa: GeneralReviewRequest, plan: gr.ReviewPlan, para_id: str) -> None:
    assert pr.clause_direction(_text(msa, para_id), plan.parties) == pr.FAVORS_REPRESENTED


def test_direction_reverses_with_the_represented_party(msa: GeneralReviewRequest) -> None:
    amtrino = pr.resolve_party_context("Amtrino", msa.textinformation)
    assert pr.clause_direction(_text(msa, "para_9"), amtrino) == pr.BURDENS_REPRESENTED
    accordor = pr.resolve_party_context("ACI", msa.textinformation)
    assert pr.clause_direction(_text(msa, "para_20"), accordor) == pr.BURDENS_REPRESENTED  # Accordor shall pay
    assert pr.clause_direction(_text(msa, "para_47"), accordor) == pr.MUTUAL  # Neither party shall
    assert pr.clause_direction(_text(msa, "para_9"), None) == pr.NEUTRAL


def test_risk_matrix_enforces_the_directionality_rule() -> None:
    assert compute_deterministic_risk_level("High", [], True, True, "material_commercial_deviation") == "High"
    assert compute_deterministic_risk_level("High", [], True, True, "material_commercial_deviation", clause_favors_client=True) == "Medium"
    assert compute_deterministic_risk_level("High", [NON_COMPETE_TRIGGER], True, True, "material_commercial_deviation", clause_favors_client=True) == "Medium"
    # A trigger that is not about which party is bound still counts.
    assert compute_deterministic_risk_level("High", [WITHHOLD_TRIGGER], True, False, "material_commercial_deviation", clause_favors_client=True) == "High"
    # Balance criteria of the taxonomy (and the old against-the-client wordings) cannot be set off by a clause in the client's favor.
    kept = drop_direction_contradicted_flags([NON_COMPETE_TRIGGER, ASSIGNMENT_TRIGGER, AMENDMENT_GATE, LIABILITY_BALANCE_TRIGGER, FEE_INCREASE_TRIGGER, WITHHOLD_TRIGGER, PERFORMANCE_TRIGGER])
    assert kept == [WITHHOLD_TRIGGER, PERFORMANCE_TRIGGER]


@pytest.mark.parametrize(
    "para_id, trigger",
    # The Assignment trigger is a real Balance criterion; the others are wordings the model may produce that no policy carries.
    [("para_9", NON_COMPETE_TRIGGER), ("para_29", ONE_WAY_CONFIDENTIALITY_TRIGGER), ("para_38", POWER_OF_ATTORNEY_TRIGGER), ("para_52", ASSIGNMENT_TRIGGER)],
)
def test_a_clause_in_the_clients_favor_scores_the_same_however_the_model_reads_the_parties(msa: GeneralReviewRequest, plan: gr.ReviewPlan, para_id: str, trigger: str) -> None:
    backwards = _validate(msa, plan, {para_id: {"triggered_unacceptable_flags": [trigger], "is_materially_one_sided_against_client": True}})
    correct = _validate(msa, plan, {para_id: {}})
    assert backwards == correct == [(para_id, "Medium")]


# --- 2. global document facts -------------------------------------------------


def test_document_facts_locate_the_cross_document_provisions(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    facts = plan.facts
    assert facts.survival is not None and facts.survival.para_identifier == "para_16"
    assert facts.survival_sections == ("2", "4", "6", "7", "8", "9", "10", "11", "12", "14", "16", "19")
    assert [location.para_identifier for location in facts.compliance_with_laws] == ["para_7"]
    assert [location.para_identifier for location in facts.return_of_advances] == ["para_17"]
    assert "para_18" in [location.para_identifier for location in facts.return_of_materials]
    assert facts.governing_law is not None and facts.governing_law.para_identifier == "para_48"
    assert facts.governing_state == "California"
    rendered = facts.render()
    assert 'survival provision, in its "Term/Termination" clause, covering Sections 2, 4, 6, 7, 8, 9, 10, 11, 12, 14, 16 and 19' in rendered
    assert "Entire Agreement" in rendered


def test_document_facts_are_the_same_on_every_scan(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    again = build_document_facts(msa.textinformation, sg.build_clause_titles(msa.textinformation))
    assert again == plan.facts and again.digest == plan.facts.digest


def test_entire_agreement_is_not_flagged_for_a_survival_clause_that_exists(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    assert plan.facts.contradicted_claim("para_68", SURVIVAL_REASON) == "survival"
    assert _validate(msa, plan, {"para_68": {"reason": SURVIVAL_REASON}}) == []
    # The survival clause's own paragraph, and an Entire Agreement finding about something else, are untouched.
    assert plan.facts.contradicted_claim("para_16", SURVIVAL_REASON) is None
    # Entire Agreement is a Low-tier type in the taxonomy (no Core criterion, no gate).
    assert _validate(msa, plan, {"para_68": {"reason": "The clause does not state that no reliance is placed on pre-contractual statements."}}) == [("para_68", "Low")]


def test_survival_is_only_suppressed_when_the_document_has_one(msa: GeneralReviewRequest) -> None:
    without = [para for para in msa.textinformation if para.paraindetifier != "para_16"]
    facts = build_document_facts(without, sg.build_clause_titles(without))
    assert facts.survival is None
    assert facts.contradicted_claim("para_68", SURVIVAL_REASON) is None


# --- 3. anchor locking ----------------------------------------------------------


def test_paragraph_roles(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    role = {para.paraindetifier: plan.structure.role(idx) for idx, para in enumerate(msa.textinformation)}
    assert all(role[para_id] == sg.ROLE_HEADING for para_id in HEADINGS)
    assert all(role[para_id] == sg.ROLE_CONTEXT_ONLY for para_id in NEVER_FLAGGED)
    assert all(role[para_id] == sg.ROLE_BOILERPLATE for para_id in BOILERPLATE)
    assert [para_id for para_id in ("para_20", "para_29", "para_37", "para_9", "para_41", "para_68") if role[para_id] != sg.ROLE_PRIMARY] == []
    assert [para_id for para_id in ("para_21", "para_36", "para_38", "para_43", "para_10") if role[para_id] != sg.ROLE_SECONDARY] == []
    assert [para_id for para_id in ("para_30", "para_31", "para_35") if role[para_id] != sg.ROLE_DEFINITION] == []


def test_every_reviewable_paragraph_has_one_primary_anchor_in_its_own_clause(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    structure = plan.structure
    for position in range(len(msa.textinformation)):
        if structure.role(position) == sg.ROLE_CONTEXT_ONLY:
            assert position not in structure.anchor_of
            continue
        anchor = structure.anchor_of[position]
        assert structure.role(anchor) in (sg.ROLE_PRIMARY, sg.ROLE_BOILERPLATE)
        assert structure.unit_of[anchor] == structure.unit_of[position]
    anchors = {msa.textinformation[position].paraindetifier: msa.textinformation[anchor].paraindetifier for position, anchor in structure.anchor_of.items()}
    assert (anchors["para_34"], anchors["para_35"], anchors["para_36"], anchors["para_38"], anchors["para_39"]) == ("para_37",) * 5
    assert (anchors["para_19"], anchors["para_21"]) == ("para_20", "para_20")


def test_roles_do_not_depend_on_the_vector_path(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    for _ in range(5):
        again = sg.build_clause_structure(msa.textinformation, sg.build_clause_titles(msa.textinformation))
        assert again == plan.structure


def test_role_tags_reach_the_prompt_and_never_the_output(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    batch = [(idx, para) for idx, para in enumerate(msa.textinformation)]
    document = gr._format_document(batch, plan.structure)
    assert "[HEADING] Intellectual Property Rights." in document
    assert "[PRIMARY_OPERATIVE_ANCHOR] In exchange for valuable consideration" in document
    assert "[SECONDARY] Service Provider agrees that any and all Intellectual Property" in document
    assert "[CONTEXT_ONLY] MASTER SERVICES AGREEMENT" in document
    # A tag the model copies back is stripped before grounding and from the fix.
    tagged = "[PRIMARY_OPERATIVE_ANCHOR] " + _text(msa, "para_50")
    suggestions = _suggestions(msa, plan, {"para_50": {"original_text": tagged, "suggested_fix": tagged + " Liability is capped at fees paid."}})
    assert suggestions["para_50"].original_text == _text(msa, "para_50")
    assert "[" not in suggestions["para_50"].suggested_fix


IP_CARVE_OUT_REASON = "The assignment has no carve-out for Service Provider's pre-existing intellectual property or Background IP, and grants no license to it."
DISPUTE_REASON = "The clause gives Accordor no right to withhold payment of invoice amounts it disputes in good faith."


@pytest.mark.parametrize("raised_on", ["para_36", "para_37", "para_38", "para_39", "para_35"])
def test_ip_clause_wide_finding_always_lands_on_para_37(msa: GeneralReviewRequest, plan: gr.ReviewPlan, raised_on: str) -> None:
    script = {raised_on: {"reason": IP_CARVE_OUT_REASON, "append": CARVE_OUT, "severity_category": "uncapped_or_severe_exposure"}}
    suggestions = _suggestions(msa, plan, script)
    assert list(suggestions) == ["para_37"]
    suggestion = suggestions["para_37"]
    assert suggestion.risk_level == "High"
    assert suggestion.original_text == _text(msa, "para_37")
    assert suggestion.suggested_fix == _text(msa, "para_37") + " " + CARVE_OUT


def test_ip_anchor_is_stable_when_both_paragraphs_are_flagged(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    on_secondary = {"reason": IP_CARVE_OUT_REASON, "append": CARVE_OUT, "severity_category": "uncapped_or_severe_exposure"}
    on_anchor = {"reason": "The assignment omits a moral rights waiver.", "append": "Service Provider waives all moral rights.", "severity_category": "uncapped_or_severe_exposure"}
    both = _suggestions(msa, plan, {"para_36": on_secondary, "para_37": on_anchor})
    reverse = _suggestions(msa, plan, {"para_37": on_anchor, "para_36": on_secondary})
    assert list(both) == list(reverse) == ["para_37"]
    assert both["para_37"] == reverse["para_37"]
    assert both["para_37"].reason == "The assignment omits a moral rights waiver."  # the anchor's own finding wins


@pytest.mark.parametrize("raised_on", ["para_20", "para_21"])
def test_fees_dispute_withholding_always_lands_on_para_20(msa: GeneralReviewRequest, plan: gr.ReviewPlan, raised_on: str) -> None:
    script = {raised_on: {"reason": DISPUTE_REASON, "append": WITHHOLD, "triggered_unacceptable_flags": [WITHHOLD_TRIGGER]}}
    assert _validate(msa, plan, script) == [("para_20", "High")]


def test_model_declared_clause_wide_omission_is_locked_to_the_anchor(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    # No topic keyword here: the model's own finding_scope moves it.
    reason = "The clause has no warranty that the Services will be performed in a workmanlike manner."
    script = {"para_43": {"reason": reason, "append": "Service Provider warrants workmanlike performance.", "finding_scope": "clause_wide_omission"}}
    assert [para_id for para_id, _ in _validate(msa, plan, script)] == ["para_41"]


def test_secondary_paragraph_keeps_a_defect_in_its_own_wording(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    text = _text(msa, "para_13")
    script = {"para_13": {"reason": "Thirty days to price a change is too long for the client.", "suggested_fix": text.replace("thirty (30) days", "ten (10) business days")}}
    assert [para_id for para_id, _ in _validate(msa, plan, script)] == ["para_13"]


def test_clause_wide_finding_with_nothing_to_carry_is_dropped_not_misplaced(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    text = _text(msa, "para_36")
    script = {"para_36": {"reason": IP_CARVE_OUT_REASON, "suggested_fix": text.replace("any and all Intellectual Property", "any and all Intellectual Property (other than Background IP)")}}
    assert _validate(msa, plan, script) == []


@pytest.mark.parametrize("heading", HEADINGS)
def test_a_heading_is_never_an_anchor(msa: GeneralReviewRequest, plan: gr.ReviewPlan, heading: str) -> None:
    edited = {heading: {"suggested_fix": _text(msa, heading).rstrip(".") + " and Related Matters."}}
    assert _validate(msa, plan, edited) == []
    added = _validate(msa, plan, {heading: {"append": "The Parties shall act in good faith under this clause."}})
    assert [para_id for para_id, _ in added] not in ([heading],)
    assert all(para_id not in HEADINGS for para_id, _ in added)


def test_overlapping_suggestions_in_one_clause_collapse_onto_the_anchor(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    reason = "The confidentiality obligations have no fixed survival period after termination, leaving the duration of protection uncertain."
    script = {"para_32": {"reason": reason}, "para_29": {"reason": reason + " This creates uncertainty."}, "para_33": {"reason": "Destruction is not required to use a secure method."}}
    assert [para_id for para_id, _ in _validate(msa, plan, script)] == ["para_29", "para_33"]


# --- 4. content controls and blank fields --------------------------------------


def test_inline_content_control_text_is_extracted() -> None:
    document = docx.Document()
    paragraph = document.add_paragraph("governed by the laws of the State of ")
    sdt = parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtPr><w:alias w:val="Field"/></w:sdtPr><w:sdtContent><w:r><w:t>California</w:t></w:r></w:sdtContent></w:sdt>')
    paragraph._p.append(sdt)
    paragraph.add_run(", without regard to conflicts of laws.")
    assert paragraph_text(paragraph) == "governed by the laws of the State of California, without regard to conflicts of laws."
    # Round-trips through a saved file, and plain paragraphs are read exactly as python-docx reads them.
    buffer = io.BytesIO()
    document.add_paragraph("No content control here.\tTabbed.")
    document.save(buffer)
    reopened = docx.Document(io.BytesIO(buffer.getvalue()))
    assert "State of California," in paragraph_text(reopened.paragraphs[0])
    assert paragraph_text(reopened.paragraphs[1]) == reopened.paragraphs[1].text


def test_blank_state_is_inferred_from_the_paragraph_itself(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    text = _text(msa, "para_48")
    assert "State of ," in text and "super cede California law" in text
    blank = plan.facts.blank_field_for("para_48")
    assert blank is not None and blank.inferred_value == "California"
    assert blank.blanks == ("State of ,", "courts of .")
    assert "The governing state is California." in plan.facts_block


def test_model_is_shown_the_governing_law_paragraph_with_the_state_restored(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    raw = _text(msa, "para_48")
    restored = raw.replace("State of ,", "State of California,").replace("courts of .", "courts of California.")
    assert plan.shown_text == {_position(msa, "para_48"): restored}
    batch = [(idx, para) for idx, para in enumerate(msa.textinformation)]
    document = gr._format_document(batch, plan.structure, plan.shown_text)
    assert "laws of the State of California, without regard" in document and "state and federal courts of California." in document
    assert "State of ," not in document and "courts of ." not in document
    # Nothing about a blank reaches the model, so there is nothing for it to flag.
    assert "blank" not in plan.facts_block.lower()


def test_blank_governing_law_is_not_a_finding_and_a_real_finding_keeps_the_raw_paragraph(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    blank_reason = "The governing law clause contains a blank where the state name should appear, and the venue clause likewise contains a blank."
    assert _validate(msa, plan, {"para_48": {"reason": blank_reason}}) == []
    assert _validate(msa, plan, {"para_48": {"reason": "The governing state is not specified and the venue is left unspecified."}}) == []
    # A finding about something else is raised; the add-in still gets the paragraph exactly as it sent it.
    injunctive = "Either Party may seek injunctive relief in any court of competent jurisdiction."
    suggestions = _suggestions(msa, plan, {"para_48": {"reason": "The clause has no carve-out for urgent injunctive relief.", "append": injunctive}})
    assert suggestions["para_48"].original_text == _text(msa, "para_48")
    assert suggestions["para_48"].suggested_fix == _shown(msa, plan, "para_48") + " " + injunctive
    assert "State of California," in suggestions["para_48"].suggested_fix


@pytest.mark.parametrize(
    "state_blank, law, court_blank",
    [
        ("State of Delaware,", "Delaware law", "courts of Delaware."),
        ("State of [State],", "California law", "courts of [●]."),
        ("State of ______,", "California law", "courts of ______."),
        ("State of ,", "California law", "courts of ."),
        ("State of New York,", "New York law", "courts of New York."),
    ],
)
def test_blank_field_guard_never_lets_another_jurisdiction_in(msa: GeneralReviewRequest, plan: gr.ReviewPlan, state_blank: str, law: str, court_blank: str) -> None:
    text = _text(msa, "para_48")
    fix = text.replace("State of ,", state_blank).replace("California law", law).replace("courts of .", court_blank)
    blank = plan.facts.blank_field_for("para_48")
    assert blank is not None
    guarded = apply_blank_field_guard(fix, text, blank)
    assert guarded == text.replace("State of ,", "State of California,").replace("courts of .", "courts of California.")
    suggestions = _suggestions(msa, plan, {"para_48": {"suggested_fix": fix}})
    assert suggestions["para_48"].suggested_fix == guarded
    assert suggestions["para_48"].original_text == text


def test_no_value_is_inferred_without_exactly_one_jurisdiction() -> None:
    def facts_for(text: str) -> Any:
        return build_document_facts([TextInformation(text=text, paraindetifier="p1")], {"p1": "Governing Law"})

    assert facts_for("Governing Law. This Agreement is governed by the laws of the State of , without regard to conflicts of laws.").blank_fields == ()
    assert facts_for("Governing Law. This Agreement is governed by the laws of the State of , and not Texas law or Ohio law.").blank_fields == ()
    assert facts_for("Governing Law. This Agreement is governed by the laws of the State of Texas.").blank_fields == ()


# --- 5. noise -----------------------------------------------------------------


@pytest.mark.parametrize("para_id", NEVER_FLAGGED)
def test_preamble_recitals_address_blocks_and_signature_block_are_never_flagged(msa: GeneralReviewRequest, plan: gr.ReviewPlan, para_id: str) -> None:
    script = {para_id: {"reason": "Drafting defect.", "append": "Corrected.", "severity_category": "uncapped_or_severe_exposure", "finding_scope": "clause_wide_omission"}}
    assert all(flagged != para_id for flagged, _ in _validate(msa, plan, script))
    assert _validate(msa, plan, {para_id: {"reason": "Drafting defect.", "severity_category": "uncapped_or_severe_exposure"}}) == []
    # Not even a trigger gets non-clause content flagged.
    triggered = {"reason": "Drafting defect.", "triggered_unacceptable_flags": [URL_AMENDMENT_TRIGGER, AMENDMENT_GATE, DEEMED_RECEIPT_TRIGGER, WITHHOLD_TRIGGER]}
    assert _validate(msa, plan, {para_id: triggered}) == []


# --- 5b. boilerplate gating ---------------------------------------------------


def test_taxonomy_gates_and_balance_criteria_are_read_from_the_taxonomy() -> None:
    gates = taxonomy_gates.load_gates()
    assert [(gate.criterion_id, gate.gate, gate.trigger) for gate in gates["Amendments and Modifications"]] == [("AMD-02", "High", AMENDMENT_GATE)]
    assert "Severability" not in gates and "Notices" not in gates
    assert taxonomy_gates.gate_level(AMENDMENT_GATE.upper() + ".") == "High"
    assert taxonomy_gates.gate_level(WITHHOLD_TRIGGER) == "Medium"
    assert taxonomy_gates.gate_level(URL_AMENDMENT_TRIGGER) is None and taxonomy_gates.gate_level(COUNTERPARTS_TRIGGER) is None
    assert taxonomy_gates.is_balance_trigger(AMENDMENT_GATE) and not taxonomy_gates.is_balance_trigger(WITHHOLD_TRIGGER)
    # Without the taxonomy file there are simply no gates.
    assert taxonomy_gates.load_criteria(Path("no-such-taxonomy.json")) == {}


def test_amendment_gate_is_a_trigger_of_the_amendments_clause_only(msa: GeneralReviewRequest) -> None:
    amendments = gr.policy_for_clause("amendments-and-modifications", "MSA")
    severability = gr.policy_for_clause("severability", "MSA")
    entire = gr.policy_for_clause("entire-agreement", "MSA")
    assert amendments is not None and severability is not None and entire is not None
    assert amendments.unacceptable_triggers == [AMENDMENT_GATE]
    assert severability.unacceptable_triggers == [] and entire.unacceptable_triggers == []
    assert f"  * {AMENDMENT_GATE}" in "\n".join(gr._describe_policy(amendments))
    # The MSA's "Modification, Waiver and Authorized Representative" clause is an amendments clause.
    assert gr.policy_for_clause(sg.clause_type_for_title("Modification, Waiver and Authorized Representative"), "MSA") == amendments


@pytest.mark.parametrize("para_id", BOILERPLATE)
@pytest.mark.parametrize("severity", ["minor_drafting_hygiene", "material_commercial_deviation", "uncapped_or_severe_exposure"])
def test_routine_clauses_are_silent_without_a_trigger_or_gate(msa: GeneralReviewRequest, plan: gr.ReviewPlan, para_id: str, severity: str) -> None:
    # Completeness, clarity and drafting points never surface on a routine clause, whatever severity the model gives them.
    assert _validate(msa, plan, {para_id: {"reason": "The clause does not address electronic signatures.", "severity_category": severity, "is_materially_one_sided_against_client": True}}) == []
    assert _validate(msa, plan, {para_id: {"reason": "Incomplete.", "severity_category": severity, "append": "Added.", "finding_scope": "clause_wide_omission"}}) == []
    # A trigger the model invents, or one that belongs to another clause, does not open the gate.
    assert _validate(msa, plan, {para_id: {"reason": "Defect.", "triggered_unacceptable_flags": ["the clause is badly drafted", WITHHOLD_TRIGGER]}}) == []


def test_routine_clause_is_flagged_on_a_policy_trigger_or_a_taxonomy_gate(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    # AMD-02 is a High gate: a unilateral right to change the terms is High, whatever else the model reports.
    gate = {"reason": "Accordor's CEO may amend alone.", "triggered_unacceptable_flags": [AMENDMENT_GATE], "severity_category": "minor_drafting_hygiene", "falls_short_of_acceptable_standard": False}
    assert _validate(msa, plan, {"para_67": gate}) == [("para_67", "High")]
    # A Core criterion without a gate follows the risk matrix, and is never Low.
    missing = {"reason": "Counterparts are not allowed for.", "triggered_unacceptable_flags": [COUNTERPARTS_TRIGGER], "severity_category": "minor_drafting_hygiene"}
    assert _validate(msa, plan, {"para_65": missing}) == [("para_65", "Medium")]
    # Wordings of the replaced policies are no longer triggers; Notices has no Core criterion or gate in the taxonomy, so it is never flagged.
    assert _validate(msa, plan, {"para_67": {"reason": "Terms may be changed by posting.", "triggered_unacceptable_flags": [URL_AMENDMENT_TRIGGER]}}) == []
    deemed = {"reason": "Notice is deemed received on mailing.", "triggered_unacceptable_flags": [DEEMED_RECEIPT_TRIGGER], "severity_category": "minor_drafting_hygiene"}
    assert _validate(msa, plan, {"para_63": deemed}) == [] and _validate(msa, plan, {"para_54": deemed}) == []
    # The amendment gate belongs to the amendments clause; on Severability it is not a trigger at all.
    assert _validate(msa, plan, {"para_66": {"reason": "A party may change terms alone.", "triggered_unacceptable_flags": [AMENDMENT_GATE]}}) == []


def _roles(*texts: str) -> List[str]:
    paragraphs = [TextInformation(text=text, paraindetifier=f"p{number}") for number, text in enumerate(texts)]
    structure = sg.build_clause_structure(paragraphs, sg.build_clause_titles(paragraphs))
    return [structure.role(position) for position in range(len(paragraphs))]


def test_suppression_stops_where_substance_starts() -> None:
    body = "Provider shall perform the Services described in this Exhibit with qualified personnel and in a professional manner."
    # An exhibit after the signature block is still reviewed.
    roles = _roles(
        "Payment. Client shall pay each invoice within thirty days of receipt.", "IN WITNESS WHEREOF, the Parties have executed this Agreement.", "By: ________", "Name: ________", "EXHIBIT A", body
    )
    assert roles[1:4] == [sg.ROLE_CONTEXT_ONLY] * 3
    assert roles[0] == sg.ROLE_PRIMARY and roles[5] != sg.ROLE_CONTEXT_ONLY
    # "agree as follows" inside an operative clause does not turn the clauses above it into preamble.
    roles = _roles("Confidentiality. Each Party shall hold the other's Confidential Information in confidence.", "Payment. In consideration of the Services, the Parties agree as follows:", body)
    assert sg.ROLE_CONTEXT_ONLY not in roles
    # Clauses of substance that merely share a word with boilerplate stay reviewable; a sentence is not an address.
    assert _roles("Waiver of Jury Trial. Each Party waives trial by jury in any proceeding arising out of this Agreement.") == [sg.ROLE_PRIMARY]
    assert _roles("Amendments to the Agreement. Section 4 of the Agreement is deleted and replaced with the following text.") == [sg.ROLE_PRIMARY]
    assert _roles("Support. Provider shall provide support during business hours.", "100 hours of support are included each month.")[1] == sg.ROLE_SECONDARY
    assert _roles("Severability. If any provision is held invalid, the remainder continues in full force and effect.") == [sg.ROLE_BOILERPLATE]


def test_notices_typo_and_address_points_are_suppressed(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    typo = {"reason": "'suit 200' should read 'Suite 200' and the Amtrino address has no zip code.", "severity_category": "minor_drafting_hygiene", "falls_short_of_acceptable_standard": False}
    assert _validate(msa, plan, {"para_54": typo}) == []
    # Nor is a Notices point raised for completeness alone, at any severity.
    material = {"reason": "Legal notices are not required to be copied to the legal department.", "severity_category": "material_commercial_deviation"}
    assert _validate(msa, plan, {"para_54": material}) == []


# --- 5c. fees, confidentiality and warranties: the remaining drift ---------------

EXPENSE_REASON = "The expense provision requires prior written approval but does not limit reimbursement to actual costs with receipts."


def test_expense_paragraph_that_already_meets_the_standard_is_not_flagged(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    assert [location.para_identifier for location in plan.facts.expense_terms] == ["para_21"]
    assert "Do not raise a finding on that expense paragraph" in plan.facts_block
    text = _text(msa, "para_21")
    in_place = {"reason": EXPENSE_REASON, "suggested_fix": text.replace("all reasonable documented", "the actual cost, supported by receipts, of all reasonable")}
    assert _validate(msa, plan, {"para_21": in_place}) == []
    assert _validate(msa, plan, {"para_21": {"reason": EXPENSE_REASON, "append": "Expenses are billed at cost."}}) == []
    # The old expense-approval wording is not a trigger, and could not apply to a paragraph that requires approval anyway.
    assert _validate(msa, plan, {"para_21": {"reason": EXPENSE_REASON, "triggered_unacceptable_flags": [EXPENSE_TRIGGER]}}) == []
    # A different hard defect in the same paragraph is still raised, and the dispute right still goes to the payment paragraph.
    assert _validate(msa, plan, {"para_21": {"reason": "Fees may be raised at will.", "triggered_unacceptable_flags": [FEE_INCREASE_TRIGGER]}}) == [("para_21", "High")]
    assert _validate(msa, plan, {"para_21": {"reason": DISPUTE_REASON, "append": WITHHOLD, "triggered_unacceptable_flags": [WITHHOLD_TRIGGER]}}) == [("para_20", "High")]


def test_expense_paragraph_without_prior_approval_is_still_reviewed() -> None:
    paragraphs = [
        TextInformation(text="Fees and Expenses.", paraindetifier="p0"),
        TextInformation(text="Client shall pay invoices within thirty (30) days of receipt.", paraindetifier="p1"),
        TextInformation(text="Client shall reimburse Provider for all reasonable documented expenses incurred in performing the Services.", paraindetifier="p2"),
    ]
    assert build_document_facts(paragraphs, sg.build_clause_titles(paragraphs)).expense_terms == ()


# Reasons the model gave for para_29 in the live runs.
ONE_WAY_RUN_1 = (
    "The confidentiality obligations in this clause are entirely one-sided: they protect only Accordor's Confidential Information and impose obligations solely on Service Provider. "
    "ACI receives no reciprocal protection for any of its own confidential information that it may share with Service Provider in the course of the engagement. "
    "In a mutual services relationship, market standard requires mutual confidentiality obligations."
)
ONE_WAY_RUN_2 = (
    "The confidentiality obligations run only one way: they bind Service Provider to protect Accordor's Confidential Information, but impose no reciprocal obligation on Accordor to protect any "
    "confidential information that Service Provider may disclose in the course of performing the services. While ACI is the client and the primary disclosing party, a fully one-sided clause may be "
    "challenged as unenforceable in some jurisdictions and does not reflect market standard for a mutual services relationship. Additionally, the definition of Confidential Information and the "
    "exclusions apply only to Accordor's information, leaving no framework for any Service Provider confidential information that may be shared."
)
MISSING_EXCLUSION = (
    "The definition of Confidential Information and its exclusions apply only to Accordor's information. "
    "The exclusions list omits the standard 'received from a third party without restriction' exclusion. Only four exclusions are enumerated."
)


def test_a_reason_is_a_sole_mutuality_complaint_only_when_it_names_no_other_defect() -> None:
    assert pr.is_sole_mutuality_complaint(ONE_WAY_RUN_1) and pr.is_sole_mutuality_complaint(ONE_WAY_RUN_2)
    assert pr.is_sole_mutuality_complaint("The clause is one-sided.")
    assert not pr.is_sole_mutuality_complaint(MISSING_EXCLUSION)
    assert not pr.is_sole_mutuality_complaint("The obligations run one way. The clause also states no period for which the duty lasts.")
    assert not pr.is_sole_mutuality_complaint("The clause states no period for which the duty lasts.")


@pytest.mark.parametrize("reason", [ONE_WAY_RUN_1, ONE_WAY_RUN_2])
def test_one_way_confidentiality_in_the_clients_favor_is_not_a_finding(msa: GeneralReviewRequest, plan: gr.ReviewPlan, reason: str) -> None:
    for facts in ({}, {"triggered_unacceptable_flags": [ONE_WAY_CONFIDENTIALITY_TRIGGER], "is_materially_one_sided_against_client": True, "severity_category": "uncapped_or_severe_exposure"}):
        assert _validate(msa, plan, {"para_29": {"reason": reason, **facts}}) == []
        # The same complaint raised on another paragraph of the clause is the same non-finding.
        assert _validate(msa, plan, {"para_32": {"reason": reason, **facts}}) == []
    # The clause's real findings are untouched: the missing exclusion and the return window stay where they are.
    kept = _validate(msa, plan, {"para_29": {"reason": reason}, "para_31": {"reason": MISSING_EXCLUSION}, "para_33": {"reason": "No deadline is set for return or destruction."}})
    assert [para_id for para_id, _ in kept] == ["para_31", "para_33"]
    assert [para_id for para_id, _ in _validate(msa, plan, {"para_29": {"reason": "The clause states no period for which the duty lasts."}})] == ["para_29"]


def test_one_way_complaint_stands_when_the_clause_binds_the_client(msa: GeneralReviewRequest) -> None:
    # Acting for Amtrino, the same clause binds the client alone: one-sidedness is then exactly the finding.
    other_side = msa.model_copy(update={"party_represented": "Amtrino"})
    plan = gr.build_review_plan(other_side, 4, ZDRClauseHashCache(version_hash="other-side"))
    assert [para_id for para_id, _ in _validate(other_side, plan, {"para_29": {"reason": ONE_WAY_RUN_1}})] == ["para_29"]


# Representations & Warranties is a Medium-tier type: the anchor is High only with its Core criterion (REP-02) and severe exposure.
WORKMANLIKE = {
    "reason": "There is no warranty that the Services will be performed in a workmanlike manner.",
    "severity_category": "uncapped_or_severe_exposure",
    "triggered_unacceptable_flags": [PERFORMANCE_TRIGGER],
}
VAGUE = {"reason": "'Reasonably related to the Services' is vague.", "severity_category": "minor_drafting_hygiene", "falls_short_of_acceptable_standard": False}


def test_low_finding_on_a_secondary_paragraph_is_dropped_when_the_anchor_is_high(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    assert _validate(msa, plan, {"para_41": WORKMANLIKE, "para_43": VAGUE}) == [("para_41", "High")]
    assert _validate(msa, plan, {"para_43": VAGUE, "para_41": WORKMANLIKE}) == [("para_41", "High")]
    # It stays when the anchor is not High, when the anchor is not flagged, and when it is itself more than Low.
    assert _validate(msa, plan, {"para_41": {"reason": "The warranty is narrow."}, "para_43": VAGUE}) == [("para_41", "Medium"), ("para_43", "Low")]
    assert _validate(msa, plan, {"para_43": VAGUE}) == [("para_43", "Low")]
    assert _validate(msa, plan, {"para_41": WORKMANLIKE, "para_43": {"reason": "The warranty excludes the employer's rights."}}) == [("para_41", "High"), ("para_43", "Medium")]


# --- 5d. the four diffs and two mapping issues of the taxonomy runs ---------------

# Reasons the model gave in the three runs saved as GR_output_1..3.json.
PROMPTLY_ADVANCES = (
    'The obligation to return unused advances is expressed as "promptly" without specifying a definite timeframe. "Promptly" is legally ambiguous and, in practice, '
    'could allow Service Provider to delay repayment indefinitely while disputing what constitutes an "unused" amount. '
    "Accordor should have a fixed deadline for repayment to protect its cash position."
)
PROMPTLY_MATERIALS = (
    'The obligation to return materials and Confidential Information is expressed as "promptly" without a definite deadline, creating the same ambiguity as in the Return of Advances clause. '
    "Additionally, the clause does not address destruction as an alternative to return, nor does it require certification of return or destruction."
)
ANTI_CORRUPTION = (
    "The clause prohibits corrupt payments but does not identify any specific anti-corruption laws or frameworks by name (such as the U.S. Foreign Corrupt Practices Act or the UK Bribery Act), "
    "leaving the scope of 'Anti-Corruption Laws' uncertain. It also contains no obligation on either party to notify the other if it becomes aware of a breach or suspected breach."
)
NOTIFY = "Each party shall promptly notify the other in writing if it becomes aware of any actual or suspected breach of this section."
FORUM_CONFLICT = (
    "The clause designates California courts as the exclusive litigation forum, but the Mediation and Arbitration clause immediately following requires all disputes to be resolved by binding "
    "arbitration. These two provisions directly conflict: one mandates court litigation, the other mandates arbitration."
)
FORUM_FIX = "Any dispute arising under this Agreement shall be resolved in accordance with the Mediation and Arbitration clause of this Agreement."
FORUM_TRIGGER = "No conflicting provisions: There is a conflict (LAW-03)"
SOW_CONFLICT = (
    "The clause describes what each Statement of Work must contain. However, it does not state that the master agreement prevails when it conflicts with a Statement of Work, "
    "and it does not state whether termination of one Statement of Work ends the master agreement."
)
PRECEDENCE_SENTENCE = "In the event of a conflict between the terms of this Agreement and those of a Statement of Work, the terms of this Agreement shall prevail."
SOW_TERMINATION = "Termination or expiration of any individual Statement of Work shall not, by itself, terminate this Agreement or any other Statement of Work then in effect."
DRAFTING = {"severity_category": "minor_drafting_hygiene", "falls_short_of_acceptable_standard": False}


def _promptly_fix(msa: GeneralReviewRequest, para_id: str) -> str:
    return _text(msa, para_id).replace("shall promptly return", "shall within thirty (30) days return")


@pytest.mark.parametrize("para_id, reason", [("para_17", PROMPTLY_ADVANCES), ("para_18", PROMPTLY_MATERIALS)])
def test_promptly_nitpick_on_a_clause_in_the_clients_favor_is_not_a_finding(msa: GeneralReviewRequest, plan: gr.ReviewPlan, para_id: str, reason: str) -> None:
    assert pr.clause_direction(_text(msa, para_id), plan.parties) == pr.FAVORS_REPRESENTED
    assert _validate(msa, plan, {para_id: {"reason": reason, "suggested_fix": _promptly_fix(msa, para_id), **DRAFTING}}) == []
    # A shortfall the model stands behind, or a fix that adds a protection, is still raised.
    material = {"reason": reason, "suggested_fix": _promptly_fix(msa, para_id), "severity_category": "material_commercial_deviation", "falls_short_of_acceptable_standard": True}
    assert [p for p, _ in _validate(msa, plan, {para_id: material})] == [para_id]
    certified = {"reason": reason, "append": "Service Provider shall certify the return in writing.", **DRAFTING}
    assert [p for p, _ in _validate(msa, plan, {para_id: certified})] == [para_id]


def test_promptly_point_on_a_clause_that_binds_the_client_is_still_raised(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    assert pr.clause_direction(_text(msa, "para_20"), plan.parties) == pr.BURDENS_REPRESENTED
    text = _text(msa, "para_20")
    script = {"para_20": {"reason": "The payment deadline should run from receipt of an undisputed invoice.", "suggested_fix": text.replace("of receipt", "of receipt of an undisputed"), **DRAFTING}}
    assert _validate(msa, plan, script) == [("para_20", "Low")]


def test_a_fix_that_adds_an_obligation_on_a_medium_tier_clause_is_never_low(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    # Run 3 called the same notification duty a clarity point; it scores Medium like runs 1 and 2.
    for facts in (DRAFTING, {"severity_category": "material_commercial_deviation", "falls_short_of_acceptable_standard": True}):
        assert _validate(msa, plan, {"para_47": {"reason": ANTI_CORRUPTION, "append": NOTIFY, **facts}}) == [("para_47", "Medium")]
    # A wording edit that adds nothing stays a drafting point, and a Low-tier clause stays Low.
    in_place = _text(msa, "para_47").replace("local and other anti-corruption laws", "local and other anti-corruption laws, including the FCPA")
    assert _validate(msa, plan, {"para_47": {"reason": ANTI_CORRUPTION, "suggested_fix": in_place, **DRAFTING}}) == [("para_47", "Low")]
    assert _validate(msa, plan, {"para_68": {"reason": "The clause does not exclude reliance.", "append": "Neither Party relies on any statement not set out here.", **DRAFTING}}) == [
        ("para_68", "Low")
    ]


def test_a_trigger_copied_without_its_criterion_name_still_counts(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    # In the runs saved at 12:12-12:16 the model reported every taxonomy trigger in this short form, and all were dropped.
    for para_id, short, level in [
        ("para_48", "There is a conflict (LAW-03)", "High"),
        ("para_51", "No way out is stated (FOR-07)", "High"),
        ("para_50", "It is not stated, or the wording is contradictory (LOL-01)", "High"),
    ]:
        script = {para_id: {"reason": "Defect.", "append": "Corrected.", "triggered_unacceptable_flags": [short], "severity_category": "material_commercial_deviation"}}
        assert _validate(msa, plan, script) == [(para_id, level)], para_id
    policy = gr.policy_for_clause("governing-law-and-dispute-resolution", "MSA")
    assert gr._matching_triggers(["There is a conflict (LAW-03)", "there is a conflict (law-03)."], policy, "Governing Law") == [FORUM_TRIGGER]
    # An id that belongs to another clause type, or no id at all, is still not a trigger of this clause.
    assert gr._matching_triggers(["No right to dispute is stated (FEE-07)", "There is a conflict"], policy, "Governing Law") == []


def test_forum_conflict_is_a_trigger_and_scores_high(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    flagged = {"reason": FORUM_CONFLICT, "append": FORUM_FIX, "triggered_unacceptable_flags": [FORUM_TRIGGER]}
    assert _validate(msa, plan, {"para_48": flagged}) == [("para_48", "High")]
    assert _validate(msa, plan, {"para_48": {"reason": FORUM_CONFLICT, "append": FORUM_FIX}}) == [("para_48", "Medium")]  # the trigger is what makes it High


def test_non_compete_maps_to_its_own_policy_not_non_exclusivity(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    assert plan.structure.clause_type_of[_position(msa, "para_9")] == "non-compete"
    policy = _offline_policies(msa, plan).policy_of[_position(msa, "para_9")]
    assert policy.canonical_type == "non-compete" and "minimum volume" not in policy.acceptable_standard
    # NCP-04 is a Balance criterion: a non-compete that restricts only Service Provider cannot set it off.
    backwards = {"reason": "Restricts the client.", "triggered_unacceptable_flags": ["Client is not restricted: The client is restricted (NCP-04)"], "is_materially_one_sided_against_client": True}
    assert _validate(msa, plan, {"para_9": backwards}) == _validate(msa, plan, {"para_9": {}}) == [("para_9", "Medium")]


def test_undefined_capitalised_terms_are_document_facts(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    found = [(item.para_identifier, item.term, item.defined_term) for item in plan.facts.undefined_terms]
    assert found == [
        ("para_10", "Accordor Proprietary Information", "Confidential Information"),
        ("para_12", "Purchase Order", None),
        ("para_43", "Proprietary Information", None),  # "Inventions and/or Proprietary Information" owed to a third party: no replacement is implied
    ]
    assert (
        '- Undefined terms in operative clauses: "Accordor Proprietary Information" in the "Non-Compete" clause (the defined term is "Confidential Information"); '
        '"Purchase Order" in the "Consulting Services" clause; "Proprietary Information" in the "Representations and Warranties" clause.'
    ) in plan.facts_block


def test_defined_terms_proper_names_and_sentence_openers_are_not_undefined_terms() -> None:
    paragraphs = [
        TextInformation(text='This Agreement ("Agreement") is between Acme Inc. ("Customer") and Beta LLC ("Vendor").', paraindetifier="p0"),
        TextInformation(text='Services. "Statement of Work" means a signed order. "Customer Data" means data Customer provides.', paraindetifier="p1"),
        TextInformation(
            text="Each Statement of Work is governed by this Agreement. Vendor shall comply with the Foreign Corrupt Practices Act, the rules of the American Arbitration Association, "
            "and the laws of the State of New York and the United States, and shall protect Customer Data and all Statements of Work.",
            paraindetifier="p2",
        ),
        TextInformation(text="Vendor shall keep the Customer Records and the CEO Report confidential.", paraindetifier="p3"),
    ]
    titles = sg.build_clause_titles(paragraphs)
    facts = build_document_facts(paragraphs, titles)
    assert [item.term for item in facts.undefined_terms] == ["Customer Records"]
    assert facts.undefined_terms[0].defined_term is None  # "Customer" opens it, but no definition covers "records"


def test_undefined_term_with_a_known_replacement_is_flagged_on_every_run(msa: GeneralReviewRequest, plan: gr.ReviewPlan, container_stub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gr, "clause_cache", ZDRClauseHashCache(version_hash="terms"))
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    container_stub(model=ScriptedModel(msa, plan, {}), search=None)  # a model that raises nothing at all
    response = asyncio.run(gr.general_review_service(msa, "terms"))
    assert [(s.para_identifier, s.risk_level) for s in response.suggestions] == [("para_10", "Low")]
    suggestion = response.suggestions[0]
    assert suggestion.original_text == _text(msa, "para_10")
    assert suggestion.suggested_fix == _text(msa, "para_10").replace("Accordor Proprietary Information", "Confidential Information")
    assert suggestion.clause == "Non-Compete"
    # When the model raises para_10 itself, its own finding is kept and nothing is added.
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    monkeypatch.setattr(gr, "clause_cache", ZDRClauseHashCache(version_hash="terms-2"))
    fix = {"reason": "Undefined term.", "suggested_fix": _text(msa, "para_10").replace("Accordor Proprietary Information", "Confidential Information"), **DRAFTING}
    container_stub(model=ScriptedModel(msa, plan, {"para_10": fix}), search=None)
    response = asyncio.run(gr.general_review_service(msa, "terms-2"))
    assert [(s.para_identifier, s.risk_level, s.reason) for s in response.suggestions] == [("para_10", "Low", "Undefined term.")]


def test_order_of_precedence_is_a_document_fact(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    assert [location.para_identifier for location in plan.facts.order_of_precedence] == ["para_14"]
    assert plan.facts.precedence_covers_orders
    assert '- Order of precedence: the "Consulting Services" clause already says which terms prevail when this Agreement and a Statement of Work conflict.' in plan.facts_block


def test_a_fix_never_re_adds_a_sentence_the_agreement_already_has(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    # Runs 2 and 3 proposed para_14's own sentence for para_12; it is taken out and the real addition stays.
    suggestions = _suggestions(msa, plan, {"para_12": {"reason": SOW_CONFLICT, "append": f"{PRECEDENCE_SENTENCE} {SOW_TERMINATION}"}})
    assert suggestions["para_12"].suggested_fix == _text(msa, "para_12") + " " + SOW_TERMINATION
    assert suggestions["para_12"].risk_level == "Medium"
    # A fix whose only addition is already in the agreement is no finding at all.
    assert _validate(msa, plan, {"para_12": {"reason": SOW_CONFLICT, "append": PRECEDENCE_SENTENCE}}) == []


# --- 6. cache key and end-to-end determinism ------------------------------------


def test_cache_key_uses_text_resolved_parties_and_policy_version(msa: GeneralReviewRequest) -> None:
    cache = ZDRClauseHashCache(version_hash="v1")
    aci = pr.resolve_party_context("ACI", msa.textinformation)
    accordor = pr.resolve_party_context("Accordor Inc.", msa.textinformation)
    amtrino = pr.resolve_party_context("Amtrino", msa.textinformation)
    text = _text(msa, "para_50")
    key = cache.compute_key(text, "Damages", aci, "MSA")
    assert key == cache.compute_key("  " + text.upper().replace(" ", "   "), "damages", accordor, "msa")  # normalised text, same resolved parties
    assert key != cache.compute_key(text, "Damages", amtrino, "MSA")  # the other side of the same agreement
    assert key != cache.compute_key(text + " More.", "Damages", aci, "MSA")
    assert key != cache.compute_key(text, "Damages", aci, "MSA", clause_context="other-clause")
    assert key != cache.compute_key(text, "Damages", aci, "MSA", document_facts="other-facts")
    assert key != ZDRClauseHashCache(version_hash="v2").compute_key(text, "Damages", aci, "MSA")


def test_plan_cache_keys_do_not_depend_on_how_the_client_is_named(msa: GeneralReviewRequest) -> None:
    cache = ZDRClauseHashCache(version_hash="keys")
    as_acronym = gr.build_review_plan(msa, 5, cache)
    as_name = gr.build_review_plan(msa.model_copy(update={"party_represented": "Accordor"}), 5, cache)
    other_side = gr.build_review_plan(msa.model_copy(update={"party_represented": "Amtrino"}), 5, cache)
    assert as_acronym.cache_keys == as_name.cache_keys
    assert set(as_acronym.cache_keys.values()).isdisjoint(other_side.cache_keys.values())


class ScriptedModel:
    """Stands in for the Bedrock model: for each batch, returns the scripted findings whose paragraph is in that batch.

    Like the real model it copies original_text from the document it is shown,
    so a paragraph normalised before the prompt comes back in its shown form.
    """

    def __init__(self, request: GeneralReviewRequest, plan: gr.ReviewPlan, script: Dict[str, Dict[str, Any]]) -> None:
        self.texts = {para.paraindetifier: plan.shown_text.get(idx, para.text) for idx, para in enumerate(request.textinformation)}
        self.script = script
        self.calls: List[Dict[str, Any]] = []

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Any, session_id: str, system_message: Optional[str] = None, temperature: float = 0.0) -> Any:
        self.calls.append(context)
        findings = [_finding(self.texts[para_id], **facts) for para_id, facts in self.script.items() if self.texts[para_id].strip() in context["document"]]
        return response_model.model_validate({"findings": findings})


def _drifting_runs(text_of: Dict[str, str]) -> List[Dict[str, Dict[str, Any]]]:
    """Three model behaviours for the same document, reproducing the drift seen across the three live runs."""

    ip = {"reason": IP_CARVE_OUT_REASON, "append": CARVE_OUT, "severity_category": "uncapped_or_severe_exposure"}
    fees = {"reason": DISPUTE_REASON, "append": WITHHOLD, "triggered_unacceptable_flags": [WITHHOLD_TRIGGER]}
    survival = {"reason": SURVIVAL_REASON, "append": "Sections 6 and 7 survive termination."}
    blank = "The governing law clause contains a blank where the state name should appear."
    typo = {"reason": "'suit 200' should read 'Suite 200'.", "severity_category": "minor_drafting_hygiene", "falls_short_of_acceptable_standard": False}
    exclusion = {"reason": MISSING_EXCLUSION}
    confused = {"triggered_unacceptable_flags": [ONE_WAY_CONFIDENTIALITY_TRIGGER], "is_materially_one_sided_against_client": True, "severity_category": "uncapped_or_severe_exposure"}
    run_1: Dict[str, Dict[str, Any]] = {
        "para_9": {"reason": "Restricts ACI from engaging other vendors.", "triggered_unacceptable_flags": [NON_COMPETE_TRIGGER], "is_materially_one_sided_against_client": True},
        "para_20": fees,
        "para_29": {"reason": ONE_WAY_RUN_1, **confused},
        "para_31": exclusion,
        "para_37": ip,
        "para_41": WORKMANLIKE,
        "para_43": VAGUE,
        "para_48": {"reason": blank},
        "para_52": {"reason": "The clause is one-sided against ACI (the Service Provider).", "triggered_unacceptable_flags": [ASSIGNMENT_TRIGGER], "is_materially_one_sided_against_client": True},
        "para_67": {"reason": "The clause does not say that oral agreements cannot vary the Agreement."},
        "para_68": survival,
    }
    run_2: Dict[str, Dict[str, Any]] = {
        "para_2": {"reason": "The Amtrino address has no zip code."},
        "para_9": {"reason": "No geographic limit; may be unenforceable."},
        "para_21": fees,
        "para_29": {"reason": ONE_WAY_RUN_2},
        "para_31": exclusion,
        "para_36": ip,
        "para_41": WORKMANLIKE,
        "para_43": VAGUE,
        "para_52": {"reason": "The assignment right is not mutual."},
        "para_54": typo,
        "para_57": typo,
        "para_68": survival,
        "para_71": {"reason": "Signature line is malformed."},
    }
    run_3: Dict[str, Dict[str, Any]] = {
        "para_8": {"reason": "Heading should describe the scope.", "append": "The restriction applies worldwide."},
        "para_9": {"reason": "Acceptable in scope and duration, but unlimited in geography."},
        "para_19": {"reason": DISPUTE_REASON, "append": WITHHOLD, "triggered_unacceptable_flags": [WITHHOLD_TRIGGER]},
        "para_20": fees,
        "para_21": {"reason": EXPENSE_REASON},
        "para_31": exclusion,
        "para_32": {"reason": "The obligations run only one way."},
        "para_36": ip,
        "para_37": ip,
        "para_41": WORKMANLIKE,
        "para_48": {"reason": "The venue is left blank."},
        "para_63": {"reason": "No rule for changing an email address is stated."},
        "para_64": {"reason": "Headings clause is redundant."},
        "para_68": survival,
    }
    return [run_1, run_2, run_3]


# para_10 is never raised by the scripted model: the undefined-term backstop adds it on every run.
EXPECTED = [("para_9", "Medium"), ("para_10", "Low"), ("para_20", "High"), ("para_31", "Medium"), ("para_37", "High"), ("para_41", "High")]


def test_three_cold_runs_give_identical_paragraphs_and_risk_levels(msa: GeneralReviewRequest, plan: gr.ReviewPlan, container_stub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    text_of = {para.paraindetifier: para.text for para in msa.textinformation}
    outcomes = []
    fixes = []
    for number, script in enumerate(_drifting_runs(text_of), start=1):
        # Cold: nothing carried over from the previous run.
        monkeypatch.setattr(gr, "clause_cache", ZDRClauseHashCache(version_hash="cold"))
        monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
        model = ScriptedModel(msa, plan, script)
        container_stub(model=model, search=None)
        response = asyncio.run(gr.general_review_service(msa, f"run-{number}"))
        outcomes.append([(suggestion.para_identifier, suggestion.risk_level) for suggestion in response.suggestions])
        fixes.append({suggestion.para_identifier: suggestion.suggested_fix for suggestion in response.suggestions})

        # Every batch was told who the parties are and what the document contains elsewhere, and none was shown a blank state.
        assert model.calls
        for context in model.calls:
            assert "Represented Party (your client): Accordor Inc." in context["party_context"]
            assert 'survival provision, in its "Term/Termination" clause' in context["document_facts"]
            assert "ACI" not in context["reviewer_context"]
            assert "State of ," not in context["document"] and "courts of ." not in context["document"]
        assert any("laws of the State of California," in context["document"] for context in model.calls)
        assert any(f"  * {AMENDMENT_GATE}" in context["policy_context"] for context in model.calls)

    assert outcomes[0] == outcomes[1] == outcomes[2] == EXPECTED
    for fix in fixes:
        assert fix["para_37"].startswith(text_of["para_37"]) and fix["para_37"].endswith(CARVE_OUT)
        assert fix["para_20"].startswith(text_of["para_20"]) and fix["para_20"].endswith(WITHHOLD)


def test_a_warm_run_replays_the_first_outcome_even_when_the_model_changes_its_mind(msa: GeneralReviewRequest, plan: gr.ReviewPlan, container_stub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    text_of = {para.paraindetifier: para.text for para in msa.textinformation}
    first, second, _ = _drifting_runs(text_of)
    monkeypatch.setattr(gr, "clause_cache", ZDRClauseHashCache(version_hash="warm"))
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    model = ScriptedModel(msa, plan, first)
    container_stub(model=model, search=None)
    cold = asyncio.run(gr.general_review_service(msa, "cold"))
    calls = len(model.calls)

    # Same document, the client named differently, the document-level cache emptied, a model that now answers differently.
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    model.script = second
    warm = asyncio.run(gr.general_review_service(msa.model_copy(update={"party_represented": "Accordor"}), "warm"))
    assert len(model.calls) == calls  # every paragraph replayed from the clause cache
    assert warm == cold
