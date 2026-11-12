"""The General Review on the Master Services Agreement fixture, offline.

The first part checks what code decides before any model call: who the
parties are, which paragraphs are clause content, where cross-document
provisions live, and how a blank jurisdiction field is restored.

The second part runs the whole pipeline with the model scripted. It shows that
the outcome — which paragraphs are flagged, and at what risk level — follows
from the majority of the checklist answers and the taxonomy, so answer sets
that disagree at the margin, or a model that names the paragraphs in a
different order, give the same review.

What this cannot show is how the live model answers. For that, run
scripts/eval_general_review_consistency.py, which measures run-to-run
agreement against Bedrock.
"""

import asyncio
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import docx
import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from src.schemas.general_review import GeneralReviewRequest, TextInformation
from src.services.docx_text import paragraph_text
from src.services.general_review import party_resolver as pr, segmentation as sg
from src.services.general_review.clause_cache import ZDRClauseHashCache
from src.services.general_review.document_facts import apply_blank_field_guard, build_document_facts
from src.tools import general_review as gr
from tests.test_general_review_engine import ScriptedModel

FIXTURE = Path(__file__).parent / "fixtures" / "master_services_agreement.json"

HEADINGS = ["para_8", "para_11", "para_15", "para_19", "para_28", "para_34", "para_40", "para_45"]
# Non-clause content: preamble and recitals, the invoice and notice address blocks, the signature block.
NEVER_FLAGGED = [f"para_{n}" for n in [*range(1, 7), *range(22, 28), *range(55, 63), *range(69, 75)]]
# Routine clauses: Notices (and its deemed-receipt paragraph), Headings, Counterparts, Severability, Modification/Waiver.
BOILERPLATE = ["para_54", "para_63", "para_64", "para_65", "para_66", "para_67"]


@pytest.fixture(scope="module")
def msa() -> GeneralReviewRequest:
    return GeneralReviewRequest.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def plan(msa: GeneralReviewRequest) -> gr.ReviewPlan:
    return gr.build_review_plan(msa, ZDRClauseHashCache(version_hash="plan"))


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> ZDRClauseHashCache:
    cache = ZDRClauseHashCache(version_hash="msa")
    monkeypatch.setattr(gr, "clause_cache", cache)
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    return cache


def _text(request: GeneralReviewRequest, para_id: str) -> str:
    return next(para.text for para in request.textinformation if para.paraindetifier == para_id)


def _position(request: GeneralReviewRequest, para_id: str) -> int:
    return next(idx for idx, para in enumerate(request.textinformation) if para.paraindetifier == para_id)


def _pid(request: GeneralReviewRequest, para_id: str) -> str:
    """The id a paragraph carries in the assessment prompt."""

    return f"P{_position(request, para_id) + 1}"


def _review(request: GeneralReviewRequest) -> List[Tuple[str, str]]:
    return [(suggestion.para_identifier, suggestion.risk_level) for suggestion in asyncio.run(gr.general_review_service(request, "msa")).suggestions]


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


def test_prompts_name_the_client_by_its_defined_term(msa: GeneralReviewRequest) -> None:
    request = msa.model_copy(update={"specific_concerns": "ACI wants to keep ownership of everything ACI pays for."})
    plan = gr.build_review_plan(request, ZDRClauseHashCache(version_hash="prompt"))
    assert "ACI" not in plan.reviewer_context
    assert 'Party the client represents: Accordor Inc. — referred to in this agreement as "Accordor"' in plan.reviewer_context
    assert "Accordor wants to keep ownership of everything Accordor pays for." in plan.reviewer_context
    assert 'Represented Party (your client): Accordor Inc. (defined as "Accordor", Role: Client / Customer)' in plan.party_block
    assert 'Counterparty: Amtrino Technologies (defined as "Service Provider", Role: Vendor / Service Provider)' in plan.party_block
    # The shorthand survives only where the block says what it means.
    assert plan.party_block.count("ACI") == 1 and '"ACI" means this same party' in plan.party_block
    # Both blocks are part of the reference every assessment call is given.
    assert plan.reviewer_context in plan.reference and plan.party_block in plan.reference


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


# --- 2. document facts --------------------------------------------------------


def test_document_facts_locate_the_cross_document_provisions(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    facts = plan.facts
    assert facts.survival is not None and facts.survival.para_identifier == "para_16"
    assert facts.survival_sections == ("2", "4", "6", "7", "8", "9", "10", "11", "12", "14", "16", "19")
    assert [location.para_identifier for location in facts.compliance_with_laws] == ["para_7"]
    assert [location.para_identifier for location in facts.return_of_advances] == ["para_17"]
    assert "para_18" in [location.para_identifier for location in facts.return_of_materials]
    assert [location.para_identifier for location in facts.order_of_precedence] == ["para_14"] and facts.precedence_covers_orders
    assert facts.governing_law is not None and facts.governing_law.para_identifier == "para_48"
    assert facts.governing_state == "California"
    rendered = facts.render()
    assert 'a survival provision in its "Term/Termination" clause, covering Sections 2, 4, 6, 7, 8, 9, 10, 11, 12, 14, 16 and 19' in rendered
    assert '- Order of precedence: the "Consulting Services" clause says which terms prevail when this Agreement and a Statement of Work conflict.' in rendered
    assert "The governing state is California." in rendered
    # The notes say where a provision is; they give the model no instruction.
    assert "never" not in rendered.lower() and "do not" not in rendered.lower()
    assert rendered in plan.reference


def test_document_facts_are_the_same_on_every_scan(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    again = build_document_facts(msa.textinformation, sg.build_clause_titles(msa.textinformation))
    assert again == plan.facts and again.digest == plan.facts.digest


def test_no_survival_note_without_a_survival_provision(msa: GeneralReviewRequest) -> None:
    without = [para for para in msa.textinformation if para.paraindetifier != "para_16"]
    facts = build_document_facts(without, sg.build_clause_titles(without))
    assert facts.survival is None and "Survival" not in facts.render()


# --- 3. paragraph roles ---------------------------------------------------------


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


def test_the_modification_clause_is_an_amendments_clause(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    amendments = gr.policy_for_clause("amendments-and-modifications", "MSA")
    assert amendments is not None
    assert gr.policy_for_clause(sg.clause_type_for_title("Modification, Waiver and Authorized Representative"), "MSA") == amendments
    assert plan.structure.clause_type_of[_position(msa, "para_9")] == "non-compete"


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


def test_the_model_only_ever_sees_the_governing_law_paragraph_with_the_state_restored(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    raw = _text(msa, "para_48")
    restored = raw.replace("State of ,", "State of California,").replace("courts of .", "courts of California.")
    assert plan.shown_text == {_position(msa, "para_48"): restored}
    assert restored in plan.reference
    assert "State of ," not in plan.reference and "courts of ." not in plan.reference
    # Nothing about a blank reaches the model, so there is nothing for it to report.
    assert "blank" not in plan.facts.render().lower()


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
    assert apply_blank_field_guard(fix, text, blank) == text.replace("State of ,", "State of California,").replace("courts of .", "courts of California.")


def test_no_value_is_inferred_without_exactly_one_jurisdiction() -> None:
    def facts_for(text: str) -> Any:
        return build_document_facts([TextInformation(text=text, paraindetifier="p1")], {"p1": "Governing Law"})

    assert facts_for("Governing Law. This Agreement is governed by the laws of the State of , without regard to conflicts of laws.").blank_fields == ()
    assert facts_for("Governing Law. This Agreement is governed by the laws of the State of , and not Texas law or Ohio law.").blank_fields == ()
    assert facts_for("Governing Law. This Agreement is governed by the laws of the State of Texas.").blank_fields == ()


# --- 5. undefined terms ---------------------------------------------------------


def test_undefined_capitalised_terms_are_found_in_code(msa: GeneralReviewRequest, plan: gr.ReviewPlan) -> None:
    # Only the mismatch the agreement itself resolves. "Purchase Order" (para_12) has no defined counterpart, and
    # "Proprietary Information" in para_43 is what Service Provider owes a third party, not Accordor's Confidential Information.
    found = [(item.para_identifier, item.term, item.defined_term) for item in plan.facts.undefined_terms]
    assert found == [("para_10", "Accordor Proprietary Information", "Confidential Information")]
    # The agreement's defined terms are listed for the drafter, so a redraft uses them as defined.
    assert '"Confidential Information"' in plan.agreement_context and '"Statement of Work"' in plan.agreement_context
    assert "Governing law: California" in plan.agreement_context


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
    # "Customer Records" is undefined, but no definition covers "records": no mismatch the agreement resolves, so nothing is reported.
    assert build_document_facts(paragraphs, titles).undefined_terms == ()
    # "Customer Confidential Data" stands for "Customer Data", whose definition covers "confidential data".
    paragraphs[1] = TextInformation(text='Services. "Customer Data" means all confidential data Customer provides.', paraindetifier="p1")
    paragraphs[3] = TextInformation(text="Vendor shall keep the Customer Confidential Data and the Customer Records secure.", paraindetifier="p3")
    facts = build_document_facts(paragraphs, sg.build_clause_titles(paragraphs))
    assert [(item.term, item.defined_term) for item in facts.undefined_terms] == [("Customer Confidential Data", "Customer Data")]


# --- 6. cache keys --------------------------------------------------------------


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
    as_acronym = gr.build_review_plan(msa, cache)
    as_name = gr.build_review_plan(msa.model_copy(update={"party_represented": "Accordor"}), cache)
    other_side = gr.build_review_plan(msa.model_copy(update={"party_represented": "Amtrino"}), cache)
    assert as_acronym.cache_keys == as_name.cache_keys
    assert set(as_acronym.cache_keys.values()).isdisjoint(other_side.cache_keys.values())


# --- 7. the pipeline on the whole agreement -------------------------------------

# What a scripted answer set fails. Keys may be scoped to a clause by its title.
CAP = {"LOL-01": "not_met", "LOL-04": "not_met"}
DISPUTES = {"FEE-07": "not_met"}
EXPECTED_BASE = [("para_10", "Low"), ("para_20", "High"), ("para_50", "High")]  # para_10: the undefined term, found in code


def _clauses_text(model: ScriptedModel) -> str:
    return "\n\n".join(call["context"]["clauses"] for call in model.assess_calls)


def test_only_clause_content_is_put_to_the_model(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    model = ScriptedModel()
    container_stub(model=model, search=None)
    _review(msa)
    shown = _clauses_text(model)
    # Preamble, recitals, address blocks and the signature block carry no id, so no answer can point at them; nor can a heading.
    for para_id in [*NEVER_FLAGGED, *HEADINGS]:
        assert f"[{_pid(msa, para_id)}]" not in shown, para_id
    assert "[heading] Intellectual Property Rights." in shown and "[context] Attn.: Accounts Payable" in shown
    for para_id in ("para_9", "para_20", "para_37", "para_48", "para_54", "para_68"):
        assert f"[{_pid(msa, para_id)}] " in shown, para_id
    # Every call works from the same reference: the client, the located provisions, and the whole agreement with the state restored.
    references = {call["reference"] for call in model.assess_calls}
    assert len(references) == 1
    (reference,) = references
    assert "Represented Party (your client): Accordor Inc." in reference and "ACI" not in reference.split("Parties to this agreement")[0]
    assert "laws of the State of California," in reference and "State of ," not in reference
    assert model.warmups == 1  # the reference is written to the prompt cache once, before the calls fan out


def test_sub_clauses_under_one_heading_type_share_one_checklist(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    model = ScriptedModel()
    container_stub(model=model, search=None)
    _review(msa)
    shown = _clauses_text(model)
    one_set = "\n\n".join(call["context"]["clauses"] for call in model.assess_calls[:: gr.CONSENSUS_VOTES])
    # Term/Termination, Return of Advances and Return of Materials: one clause, one Term checklist.
    assert one_set.count("- TER-01 |") == 1
    term = shown.split('"Term/Termination" ===')[1].split("Checklist for")[0]
    assert f"[{_pid(msa, 'para_17')}] Return of Advances." in term and f"[{_pid(msa, 'para_18')}] Return of Materials." in term
    # Governing law and the arbitration clause likewise, so a conflict between them is one finding.
    assert one_set.count("- LAW-03 |") == 1
    assert one_set.count("- CMP-01 |") == 1  # Compliance with Laws + Anti-Corruption Compliance


def test_three_cold_runs_with_drifting_answers_give_one_review(msa: GeneralReviewRequest, container_stub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each run's three answer sets agree on the cap and the dispute right; around them, single answer sets drift.
    runs = [
        [{**CAP, **DISPUTES}, {**CAP, **DISPUTES, "IND-05": "not_met"}, {**CAP, **DISPUTES, "NOT-01": "not_met"}],
        [{**CAP, **DISPUTES, "CON-03": "not_met"}, {**CAP, **DISPUTES}, {**CAP, "FEE-07": ("not_met", [_pid(msa, "para_21")]), "ENT-02": "partly_met"}],
        [{**CAP, "FEE-07": ("not_met", ["P999"])}, {**CAP, **DISPUTES, "FOR-02": "not_met", "ASG-02": "not_met"}, {"LOL-01": ("not_met", [_pid(msa, "para_50")]), "LOL-04": "not_met", **DISPUTES}],
    ]
    outcomes = []
    for number, scripts in enumerate(runs):
        # Cold: nothing carried over from the previous run.
        monkeypatch.setattr(gr, "clause_cache", ZDRClauseHashCache(version_hash=f"cold-{number}"))
        monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
        # The batches run concurrently, so each answer set is scripted per batch rather than per call.
        container_stub(model=_PerBatchModel(scripts), search=None)
        outcomes.append(_review(msa))
    assert outcomes[0] == outcomes[1] == outcomes[2] == EXPECTED_BASE


class _PerBatchModel(ScriptedModel):
    """A scripted model whose n-th answer set for any one batch uses the n-th script, whatever order the batches' calls arrive in."""

    def __init__(self, scripts: List[Dict[str, Any]]) -> None:
        super().__init__(scripts)
        self._seen: Dict[str, int] = {}

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Any, session_id: str, system_message: Any = None, temperature: float = 0.0, **options: Any) -> Any:
        if response_model.__name__ != "BatchAssessment":
            return await super().generate(prompt, context, response_model, session_id, system_message, temperature, **options)
        number = self._seen.get(context["clauses"], 0)
        self._seen[context["clauses"]] = number + 1
        self.assess_calls.append({"context": context, "reference": options.get("cached_prefix"), "system": system_message})
        return response_model.model_validate({"clauses": self._answers(context["clauses"], self.scripts[number % len(self.scripts)]), "drafting_defects": []})


def test_a_warm_run_replays_the_first_outcome_even_when_the_model_changes_its_mind(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    model = ScriptedModel([{**CAP, **DISPUTES}])
    container_stub(model=model, search=None)
    cold = asyncio.run(gr.general_review_service(msa, "cold"))
    calls = (len(model.assess_calls), len(model.draft_calls))
    assert [(s.para_identifier, s.risk_level) for s in cold.suggestions] == EXPECTED_BASE

    # Same document, the client named differently, the document-level cache emptied, a model that now answers differently.
    gr._response_cache.clear()
    model.scripts = [{"IND-05": "not_met"}]
    warm = asyncio.run(gr.general_review_service(msa.model_copy(update={"party_represented": "Accordor"}), "warm"))
    assert (len(model.assess_calls), len(model.draft_calls)) == calls  # every paragraph replayed from the clause cache
    assert warm == cold


def test_the_undefined_term_is_raised_on_every_run_whatever_the_model_notices(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    model = ScriptedModel(revise=lambda paragraph: paragraph.replace("Accordor Proprietary Information", "Confidential Information"))  # a model that fails nothing
    container_stub(model=model, search=None)
    response = asyncio.run(gr.general_review_service(msa, "terms"))
    assert [(s.para_identifier, s.risk_level, s.clause) for s in response.suggestions] == [("para_10", "Low", "Non-Compete")]
    suggestion = response.suggestions[0]
    assert suggestion.original_text == _text(msa, "para_10")
    assert suggestion.suggested_fix == _text(msa, "para_10").replace("Accordor Proprietary Information", "Confidential Information")
    (draft,) = model.draft_calls
    assert '"Accordor Proprietary Information" is used as a defined term but the agreement never defines it' in draft["context"]["points"]
    assert 'the agreement\'s defined term for this is "Confidential Information"' in draft["context"]["points"]


def test_a_one_sidedness_point_is_not_raised_on_a_clause_that_favors_the_client(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    # ASG-01 and ASG-04 are Balance criteria. Assignment binds only Service Provider, in Accordor's favor.
    one_sided = [{"ASG-01": "not_met", "ASG-04": "not_met"}]
    container_stub(model=ScriptedModel(one_sided), search=None)
    assert _review(msa) == [("para_10", "Low")]
    # Acting for Service Provider, the same answers are a finding: the clause now runs against the client.
    gr.clause_cache.clear()
    gr._response_cache.clear()
    container_stub(model=ScriptedModel(one_sided), search=None)
    assert ("para_52", "Medium") in _review(msa.model_copy(update={"party_represented": "Amtrino"}))


def test_a_forum_conflict_is_high_and_lands_on_the_governing_law_paragraph(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    law, arbitration = _pid(msa, "para_48"), _pid(msa, "para_49")
    # The answer sets name the two conflicting paragraphs in different orders; the finding lands on the first of them.
    scripts = [{"LAW-03": ("not_met", [law, arbitration])}, {"LAW-03": ("not_met", [arbitration, law])}, {"LAW-03": ("not_met", [arbitration, law])}]
    model = ScriptedModel(scripts, revise=lambda paragraph: paragraph.replace("California law", "Delaware law") + " Arbitration under the Mediation and Arbitration clause prevails.")
    container_stub(model=model, search=None)
    response = asyncio.run(gr.general_review_service(msa, "forum"))
    by_id = {s.para_identifier: s for s in response.suggestions}
    assert set(by_id) == {"para_10", "para_48"} and by_id["para_48"].risk_level == "High"
    suggestion = by_id["para_48"]
    # The add-in gets the paragraph exactly as it sent it; the redraft keeps the state the paragraph itself names.
    assert suggestion.original_text == _text(msa, "para_48") and "State of ," in suggestion.original_text
    assert "State of California," in suggestion.suggested_fix and "courts of California." in suggestion.suggested_fix
    assert "Delaware" not in suggestion.suggested_fix
    # The drafter was shown the paragraph with the state restored, and the clause's other paragraph for context.
    (draft,) = [call["context"] for call in model.draft_calls if call["context"]["clause_title"] == "Governing Law and Choice of Venue"]
    assert "State of California," in draft["paragraph"] and "Mediation and Arbitration." in draft["clause_text"]


def test_a_redraft_keeps_the_agreements_own_typography(msa: GeneralReviewRequest, container_stub: Any, fresh: ZDRClauseHashCache) -> None:
    original = _text(msa, "para_37")
    assert "Service Provider’s" in original
    # The model returns the paragraph retyped with straight apostrophes, plus one new sentence.
    retyped = lambda paragraph: paragraph.replace("’", "'") + " Service Provider waives all moral rights in Accordor's favor."  # noqa: E731
    container_stub(model=ScriptedModel([{"IPR-03": "not_met"}], revise=retyped), search=None)
    response = asyncio.run(gr.general_review_service(msa, "typography"))
    suggestion = next(s for s in response.suggestions if s.para_identifier == "para_37")
    assert suggestion.suggested_fix == original + " Service Provider waives all moral rights in Accordor’s favor."
