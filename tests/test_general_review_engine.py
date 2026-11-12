"""Unit and offline end-to-end tests for the General Review pipeline: plan, assessment, findings, drafting."""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Sequence

import pytest

from src.schemas.clause_mapping import ClauseMatch, ClausePolicy
from src.schemas.general_review import BatchAssessment, CriterionAnswer, DraftedRevision, GeneralReviewRequest, Suggestion, TextInformation
from src.services.clause_mapping.policy_store import get_clause_policy_store
from src.services.clause_mapping.resilient_classifier import HEADING_TO_CANONICAL_TYPE, classify_clause_resilient, resolve_qdrant_weighted_knn
from src.services.general_review import assessment as asm
from src.services.general_review.clause_cache import NO_FINDING, ZDRClauseHashCache
from src.services.general_review.document_facts import find_attachments_not_supplied
from src.services.general_review.party_resolver import FAVORS_REPRESENTED, clause_direction, parties_by_usage, resolve_party_context
from src.services.general_review.risk_matrix import criterion_risk_level, defect_risk_level, highest
from src.services.general_review.segmentation import breaks_off_mid_sentence, build_clause_titles, build_clause_units, build_smart_embedding_query, detect_clause_start, runs_on_into
from src.services.general_review.taxonomy_gates import BALANCE, CORE, MAJOR, MINOR, Criterion, criteria_for_type, load_legal_standards, provider_protective_criteria
from src.services.general_review.typography import restore_typography
from src.tools import general_review as gr

PUBLIC_SUGGESTION_FIELDS = {"clause", "risk_level", "reason", "original_text", "suggested_fix", "para_identifier"}


# --- fakes -------------------------------------------------------------------

_CLAUSE_HEADER_RE = re.compile(r'^=== (C\d+)(?: — "(.*)")? ===$', re.MULTILINE)
_ITEM_RE = re.compile(r"^- ([A-Z]{2,4}-\d{2}) \|", re.MULTILINE)


class ScriptedModel:
    """Stands in for BedrockModel.generate.

    Assessment: answers every checklist item it is shown "met", except the
    items scripted for that call. scripts holds one dict per answer set and
    is cycled: {"FEE-07": "not_met", "LOL-01": ("not_met", ["P1"])}; a key
    may be scoped to a clause as "Clause Title|FEE-07". defects are returned
    with every answer set.

    Drafting: returns the paragraph with revise() applied (by default a
    marker sentence appended) and a two-part note.
    """

    def __init__(self, scripts: Optional[List[Dict[str, Any]]] = None, defects: Optional[List[Dict[str, str]]] = None, revise: Any = None, fail_assessments: Sequence[int] = ()) -> None:
        self.scripts = scripts or [{}]
        self.defects = defects or []
        self.revise = revise or (lambda paragraph: paragraph + " The Parties shall act in good faith.")
        self.fail_assessments = set(fail_assessments)
        self.assess_calls: List[Dict[str, Any]] = []
        self.draft_calls: List[Dict[str, Any]] = []
        self.warmups = 0

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Any, session_id: str, system_message: Optional[str] = None, temperature: float = 0.0, **options: Any) -> Any:
        assert temperature == 0.0
        if response_model is BatchAssessment:
            number = len(self.assess_calls)
            self.assess_calls.append({"context": context, "reference": options.get("cached_prefix"), "system": system_message})
            if number in self.fail_assessments:
                raise RuntimeError("throttled")
            script = self.scripts[number % len(self.scripts)]
            return BatchAssessment.model_validate({"clauses": self._answers(context["clauses"], script), "drafting_defects": self.defects})
        assert response_model is DraftedRevision
        self.draft_calls.append({"context": context, "system": system_message})
        return DraftedRevision(exposure="The paragraph leaves the client exposed.", change="The revision closes the gap.", revised_paragraph=self.revise(context["paragraph"]))

    async def warm_prompt_cache(self, cached_prefix: str, response_model: Any, session_id: str, system_message: Optional[str] = None) -> None:
        self.warmups += 1

    @staticmethod
    def _answers(clauses_text: str, script: Dict[str, Any]) -> List[Dict[str, Any]]:
        headers = list(_CLAUSE_HEADER_RE.finditer(clauses_text))
        clauses = []
        for index, header in enumerate(headers):
            body = clauses_text[header.end() : headers[index + 1].start() if index + 1 < len(headers) else len(clauses_text)]
            answers = []
            for criterion_id in _ITEM_RE.findall(body):
                scripted = script.get(f"{header.group(2)}|{criterion_id}", script.get(criterion_id, "met"))
                verdict, paragraphs = scripted if isinstance(scripted, tuple) else (scripted, [])
                answers.append({"criterion_id": criterion_id, "evidence": "No provision", "verdict": verdict, "paragraphs": paragraphs})
            clauses.append({"clause_id": header.group(1), "answers": answers, "clause_type_fits": script.get(f"{header.group(2)}|fits", script.get("fits", True))})
        return clauses


class FailingSearch:
    """A clause search whose vector path is down."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, texts: Sequence[str], agreement_type: Optional[str], session_id: str, top_k: int = 10) -> List[List[ClauseMatch]]:
        self.calls += 1
        raise RuntimeError("embedding service timed out")


class CannedSearch:
    """A clause search returning fixed hits for every text."""

    def __init__(self, hits: List[ClauseMatch]) -> None:
        self.hits = hits
        self.queries: List[str] = []

    async def search(self, texts: Sequence[str], agreement_type: Optional[str], session_id: str, top_k: int = 10) -> List[List[ClauseMatch]]:
        self.queries.extend(texts)
        return [list(self.hits) for _ in texts]


def _policy(slug: str) -> ClausePolicy:
    policy = get_clause_policy_store().get(slug)
    assert policy is not None, slug
    return policy


def _para(identifier: str, text: str) -> TextInformation:
    return TextInformation(text=text, paraindetifier=identifier)


def _request(*paragraphs: TextInformation, party: Optional[str] = None) -> GeneralReviewRequest:
    return GeneralReviewRequest(textinformation=list(paragraphs), party_represented=party, agreement_type="MSA")


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> ZDRClauseHashCache:
    """Empty caches for one test."""

    cache = ZDRClauseHashCache(version_hash="test")
    monkeypatch.setattr(gr, "clause_cache", cache)
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    return cache


DAMAGES = (
    "Damages. In no event will either Party be liable, whether arising in contract, tort, warranty or otherwise, "
    "for any special, indirect, incidental, exemplary, punitive or consequential damages, including, but not limited to loss of anticipated profits."
)
NOTICES = "Notices. All notices under this Agreement shall be in writing and delivered by email to the addresses set out in the signature block, effective upon sending."
PAYMENT = "Fees and Payment. Client shall pay all invoices within fifteen (15) days of the invoice date; amounts not paid when due accrue interest at two percent (2%) per month."


# --- segmentation ------------------------------------------------------------


def test_detect_clause_start_negatives_and_numbered():
    assert detect_clause_start("Name: ______________________") == (False, None)
    assert detect_clause_start("Provider shall pay all fees within thirty days.") == (False, None)
    assert detect_clause_start("San Ramon, CA 94583") == (False, None)
    assert detect_clause_start("5.2. The Provider shall maintain insurance throughout the Term.") == (True, None)
    assert detect_clause_start("7.3 Security Measures. Provider shall maintain safeguards.") == (True, "Security Measures")


def test_numbered_sub_clauses_belong_to_their_numbered_clause():
    paragraphs = [
        _para("p0", "5. Fees and Payment."),
        _para("p1", "5.1 Fees. Customer shall pay the fees set out in each Statement of Work."),
        _para("p2", "5.2 Invoices. Provider shall invoice Customer monthly in arrears."),
        _para("p3", "5.3 Disputed Amounts. Customer may withhold amounts disputed in good faith."),
        _para("p4", "6. Confidentiality."),
        _para("p5", "6.1 Exclusions. Confidential Information does not include public information."),
    ]
    titles = build_clause_titles(paragraphs)
    assert [titles[f"p{n}"] for n in range(6)] == ["Fees and Payment"] * 4 + ["Confidentiality"] * 2
    assert build_clause_units(paragraphs, titles) == [[0, 1, 2, 3], [4, 5]]
    # Without the numbered parent in view (a selection), a sub-clause keeps its own lead-in as its title.
    assert build_clause_titles(paragraphs[2:3]) == {"p2": "Invoices"}


def _units(*texts: str) -> List[List[int]]:
    paragraphs = [_para(f"p{number}", text) for number, text in enumerate(texts)]
    return build_clause_units(paragraphs, build_clause_titles(paragraphs))


def test_sub_clauses_belong_to_the_section_heading_they_sit_under():
    # A heading in capitals is a level above its lead-ins: every sub-clause stays in the section, whatever its own title says.
    assert _units(
        "LIMITATION OF LIABILITY",
        "Exclusion of indirect loss. Neither Party is liable for indirect or consequential loss.",
        "Liability cap. Each Party's total liability is limited to the fees paid in the prior twelve months.",
        "Excluded Claims. The above limits do not apply to payment obligations or to fraud.",
        "FORCE MAJEURE",
        "Excused performance. Neither Party is liable for delay caused by events beyond its control.",
        "Notice. The affected Party shall notify the other Party promptly of the event.",
        "GENERAL PROVISIONS",
        "Assignment. Neither Party may assign this Agreement without the other Party's consent.",
        "Language. This Agreement is made in the English language only.",
    ) == [
        [0, 1, 2, 3],
        [4, 5, 6],
        [7],
        [8],
        [9],
    ]  # a heading that names no clause type gathers nothing
    # A heading typed like the lead-ins may be their sibling: a lead-in naming another clause type is its own clause.
    assert _units(
        "Term/Termination.",
        "This Agreement continues for one year from the Effective Date unless terminated earlier.",
        "Return of Materials. Provider shall return all materials on termination.",
        "Wind-up costs. Each Party bears its own costs of winding up the engagement.",
        "Indemnity. Provider shall indemnify Client against third-party claims.",
        "Notices. All notices shall be in writing.",
    ) == [[0, 1, 2, 3], [4], [5]]
    # With no standalone heading, every lead-in is a clause.
    assert _units("Assignment. Neither Party may assign this Agreement.", "Notices. All notices shall be in writing.") == [[0], [1]]
    # Word types apostrophes curly; the heading still names its clause type.
    assert classify_clause_resilient("SERVICES AND PROVIDER’S OBLIGATIONS", "", 40).canonical_type == "service-provider-obligations"


def test_batches_hold_whole_clauses_and_keep_one_clause_type_together():
    filler = ("the parties acknowledge the recitals above and agree as follows " * 120).strip()
    assert len(filler) > gr.BATCH_CHAR_BUDGET
    plan = gr.build_review_plan(_request(_para("p1", filler), _para("p2", DAMAGES), _para("p3", NOTICES)), ZDRClauseHashCache(version_hash="b"))
    assert plan.batches == [[(0,)], [(1,), (2,)]]
    # Consecutive clauses of one type are never cut apart, however long the first is.
    termination = "Term/Termination. " + ("Either Party may terminate this Agreement on material breach. " * 110).strip()
    request = _request(_para("p1", termination), _para("p2", "Return of Materials. Provider shall return all materials on termination of this Agreement."), _para("p3", NOTICES))
    plan = gr.build_review_plan(request, ZDRClauseHashCache(version_hash="b"))
    assert plan.batches == [[(0,), (1,)], [(2,)]]


def test_smart_query_keeps_definitions_inside_a_definitions_clause():
    query = build_smart_embedding_query("Definitions", ['"Services" means the services described in a Statement of Work.', '"Deliverables" means the work product.'])
    assert '"Services" means' in query


def test_embedding_batch_size_respects_service_limit():
    from src.services.clause_mapping.qdrant_search import EMBED_BATCH_SIZE

    assert 1 <= EMBED_BATCH_SIZE <= 16  # the embedding service rejects more than 16 texts per request


# --- classifier --------------------------------------------------------------


def test_every_router_slug_has_a_policy_and_a_checklist():
    store = get_clause_policy_store()
    slugs = {slug for _, slug in HEADING_TO_CANONICAL_TYPE}
    assert not sorted(slug for slug in slugs if store.get(slug) is None)
    assert not sorted(slug for slug in slugs if not criteria_for_type(slug))


def test_weighted_knn_prefers_supported_type_and_is_deterministic():
    hits = [
        ClauseMatch(canonical_type="miscellaneous-other-provisions", score=0.81),
        ClauseMatch(canonical_type="confidentiality", score=0.80),
        ClauseMatch(canonical_type="confidentiality", score=0.79),
        ClauseMatch(canonical_type="confidentiality", score=0.78),
        ClauseMatch(canonical_type="data-protection-and-privacy", score=0.69),
    ]
    assert resolve_qdrant_weighted_knn(hits)[0] == "confidentiality"
    assert resolve_qdrant_weighted_knn(hits, heading_boost_slug="miscellaneous-other-provisions")[0] == "miscellaneous-other-provisions"
    assert resolve_qdrant_weighted_knn([ClauseMatch(canonical_type="notices", score=0.5)]) == (None, 0.0)


def test_generic_heading_does_not_lock_and_yields_to_vector_vote():
    body = ["Miscellaneous. Each party shall keep the other's non-public information secret, use it only for this engagement, and return it on request; these duties last five years."]
    query = build_smart_embedding_query("Miscellaneous", body)
    words = len(" ".join(body).split())
    with_hits = classify_clause_resilient("Miscellaneous", query, words, [ClauseMatch(canonical_type="confidentiality", score=0.79)])
    assert (with_hits.canonical_type, with_hits.source) == ("confidentiality", "layer2_qdrant")
    without = classify_clause_resilient("Miscellaneous", query, words, None)
    assert (without.canonical_type, without.source) == ("miscellaneous-other-provisions", "layer3_fallback")
    specific = classify_clause_resilient(
        "Severability", "Severability. If any provision is held invalid, the remainder continues in force and effect.", 16, [ClauseMatch(canonical_type="confidentiality", score=0.79)]
    )
    assert (specific.canonical_type, specific.source) == ("severability", "layer1_heading")


# --- the checklist -------------------------------------------------------------


def test_the_checklist_comes_from_the_taxonomy_as_written():
    fees = {criterion.criterion_id: criterion for criterion in criteria_for_type("fees-and-payment")}
    assert list(fees)[:2] == ["FEE-01", "FEE-02"] and len(fees) == 10
    disputed = fees["FEE-07"]
    assert (disputed.weight, disputed.gate, disputed.conditional) == (MAJOR, "Medium", False)
    assert disputed.question.startswith("Does the clause let the paying party dispute an invoice")
    assert fees["FEE-02"].partly_met_when == ""  # the workbook's "None" is no Partly met test
    assert fees["FEE-06"].conditional and fees["FEE-06"].issue_category == BALANCE
    assert fees["FEE-08"].weight == MINOR and not asm.is_reportable(fees["FEE-08"])
    # A criterion the overrides name as an extra trigger is a hard defect: weighted Core.
    law = {criterion.criterion_id: criterion for criterion in criteria_for_type("governing-law-and-dispute-resolution")}
    assert law["LAW-03"].weight == CORE
    # Types the workbook does not have yet come from the overrides.
    assert [criterion.criterion_id for criterion in criteria_for_type("non-compete")] == ["NCP-01", "NCP-02", "NCP-03", "NCP-04"]
    assert criteria_for_type("no-such-type") == () and criteria_for_type(None) == ()


def test_a_checklist_item_is_rendered_with_its_tests_and_condition():
    fees = {criterion.criterion_id: criterion for criterion in criteria_for_type("fees-and-payment")}
    assert asm.render_checklist_item(fees["FEE-06"]).splitlines() == [
        "- FEE-06 | Fee changes are limited | [Balance]",
        "  Applies only where: the clause lets one party change fees without the other's agreement",
        "  Question: Is that change limited (by a cap, an index, notice or a right to object)?",
        "  Met: A limit is stated and notice is required.",
        "  Partly met: Only a limit or only notice is stated.",
        "  Not met: The change is unrestricted.",
    ]
    assert "Partly met" not in asm.render_checklist_item(fees["FEE-02"])


# --- risk ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "tier, gate, expected",
    [
        ("High", None, "High"),  # a finding takes the risk allocation of its clause
        ("Medium", None, "Medium"),
        ("Low", None, "Low"),
        ("Low", "High", "High"),  # a High gate decides, whatever the clause
        ("Medium", "Critical", "High"),
        ("Low", "Medium", "Medium"),  # a Medium gate is a floor
        ("High", "Medium", "High"),  # a gate never lowers the level
        ("nonsense", None, "Medium"),  # an unknown tier counts as Medium
    ],
)
def test_a_finding_takes_its_clauses_risk_allocation_unless_a_gate_raises_it(tier, gate, expected):
    assert criterion_risk_level(tier, gate) == expected


def test_clause_risk_tiers_are_the_legal_teams_risk_allocations():
    standards = load_legal_standards()
    assert len(standards) == 43
    for slug, standard in standards.items():
        policy = _policy(slug)
        assert (policy.inherent_risk_tier, policy.fallback_guidelines) == (standard["risk_allocation"], standard["fallback_guidelines"]), slug
    assert [_policy(slug).inherent_risk_tier for slug in ("fees-and-payment", "service-levels", "notices", "taxes", "waiver")] == ["High", "Medium", "Low", "High", "Medium"]


def test_defects_are_low_and_a_paragraph_takes_its_highest_point():
    assert defect_risk_level("typographical_error") == "Low"
    assert highest(["Low", "High", "Medium"]) == "High"
    assert highest([]) == "Low"


# --- schemas ---------------------------------------------------------------------


def test_assessment_schema_tolerates_model_quirks():
    parsed = BatchAssessment.model_validate(
        {
            "clauses": '[{"clause_id": "C1", "answers": [{"criterion_id": "FEE-07", "evidence": null, "verdict": "Not Met", "paragraphs": "P2, P3"},'
            ' {"criterion_id": "FEE-04", "verdict": "partly-met", "paragraphs": "[\\"P2\\"]"}, {"criterion_id": "FEE-01", "verdict": "who knows"}]}]',
            "drafting_defects": [{"paragraph": "P2", "defect_type": "Typographical Error", "evidence": "teh"}, {"paragraph": "P2", "defect_type": "something_else", "evidence": "x"}],
        }
    )
    answers = parsed.clauses[0].answers
    assert [(answer.criterion_id, answer.verdict, answer.paragraphs) for answer in answers] == [("FEE-07", "not_met", ["P2", "P3"]), ("FEE-04", "partly_met", ["P2"]), ("FEE-01", "not_applicable", [])]
    assert [defect.defect_type for defect in parsed.drafting_defects] == ["typographical_error"]  # only the kinds the review asks for
    # The model is never asked for a risk level or a severity.
    properties = json.dumps(BatchAssessment.model_json_schema()) + json.dumps(DraftedRevision.model_json_schema())
    assert "risk" not in properties and "severity" not in properties


def test_answers_and_defects_run_together_in_one_string_are_separated():
    answers = '[{"clause_id": "C1", "answers": [{"criterion_id": "LOL-01", "verdict": "not_met", "evidence": "x"}]}]'
    defects = '[{"paragraph": "P1", "defect_type": "typographical_error", "evidence": "9(A)Y", "note": "Should read 9(A)(Y)."}]'
    both = BatchAssessment.model_validate({"clauses": answers + " " + defects})
    assert (len(both.clauses), [defect.evidence for defect in both.drafting_defects]) == (1, ["9(A)Y"])
    # Anything else after the answers is dropped rather than failing the whole answer set.
    assert len(BatchAssessment.model_validate({"clauses": answers + " }"}).clauses) == 1


def test_public_suggestion_shape_is_unchanged():
    assert set(Suggestion.model_fields) == PUBLIC_SUGGESTION_FIELDS


# --- votes to findings -----------------------------------------------------------


def _criterion(criterion_id: str, weight: str = MAJOR, gate: Optional[str] = None, category: str = "Completeness", partly: str = "") -> Criterion:
    return Criterion(
        criterion_id,
        "Test Type",
        f"Title {criterion_id}",
        f"Not met text {criterion_id}.",
        gate,
        category,
        question="Q?",
        met_when="Met.",
        partly_met_when=partly,
        weight=weight,
        why_it_matters="Why.",
    )


def _clause(*criteria: Criterion, tier: str = "High") -> asm.ClauseView:
    return asm.ClauseView("C1", "Test Clause", "Test Type", (0, 1, 2), frozenset({1, 2}), 1, tuple(criteria), tier)


def _votes(*answer_sets: Dict[str, Any]) -> List[Dict[str, CriterionAnswer]]:
    votes = []
    for answers in answer_sets:
        vote = {}
        for criterion_id, scripted in answers.items():
            verdict, paragraphs = scripted if isinstance(scripted, tuple) else (scripted, [])
            vote[criterion_id] = CriterionAnswer(criterion_id=criterion_id, verdict=verdict, paragraphs=paragraphs, evidence="quoted words")
        votes.append(vote)
    return votes


def test_a_criterion_fails_only_on_a_majority_of_the_answer_sets():
    clause = _clause(_criterion("T-01"), _criterion("T-02"))
    points = asm.tally_criteria(clause, _votes({"T-01": "not_met", "T-02": "not_met"}, {"T-01": "not_met", "T-02": "met"}, {"T-01": "met", "T-02": "met"}), majority=2)
    assert [(point.key, point.position, point.risk_level, point.finding) for point in points] == [("T-01", 1, "High", "Not met text T-01.")]  # the clause is High-tier
    # An answer set that left the item out counts as no finding from that set.
    assert asm.tally_criteria(clause, _votes({"T-01": "not_met"}, {}, {}), majority=2) == []
    assert asm.tally_criteria(clause, _votes({"T-01": "not_applicable"}, {"T-01": "not_applicable"}, {"T-01": "not_met"}), majority=2) == []


def test_the_review_requires_every_answer_set_to_report_the_shortfall():
    clause = _clause(_criterion("T-01", weight=CORE, partly="Partly text."), _criterion("T-02"))
    two_of_three = _votes({"T-01": "not_met", "T-02": "not_met"}, {"T-01": "not_met", "T-02": "not_met"}, {"T-01": "met", "T-02": "not_applicable"})
    assert asm.tally_criteria(clause, two_of_three, majority=2, required=3) == []
    # Unanimous on a shortfall, split on how far short: not met when most say so, otherwise met in part.
    mostly_failed = _votes({"T-01": "not_met"}, {"T-01": "partly_met"}, {"T-01": "not_met"})
    assert [(point.key, point.finding) for point in asm.tally_criteria(clause, mostly_failed, majority=2, required=3)] == [("T-01", "Not met text T-01.")]
    mostly_partly = _votes({"T-01": "partly_met"}, {"T-01": "partly_met"}, {"T-01": "not_met"})
    assert [(point.key, point.finding) for point in asm.tally_criteria(clause, mostly_partly, majority=2, required=3)] == [("T-01", "Partly text.")]


def test_met_in_part_is_a_finding_only_for_a_core_or_gating_criterion_with_a_partly_test():
    core = _criterion("T-01", weight=CORE, partly="Partly text.")
    major = _criterion("T-02", weight=MAJOR, partly="Partly text.")
    gated = _criterion("T-03", weight=MAJOR, gate="Medium", partly="Partly text.")
    untested = _criterion("T-04", weight=CORE)
    partly = {"T-01": "partly_met", "T-02": "partly_met", "T-03": "partly_met", "T-04": "partly_met"}
    points = asm.tally_criteria(_clause(core, major, gated, untested), _votes(partly, partly, {"T-01": "not_met"}), majority=2)
    assert [(point.key, point.risk_level, point.finding) for point in points] == [("T-01", "High", "Partly text."), ("T-03", "High", "Partly text.")]


def test_a_minor_criterion_is_neither_asked_nor_reported():
    clause = _clause(_criterion("T-01", weight=MINOR), _criterion("T-02", weight=MINOR, gate="Medium"), _criterion("T-03"))
    assert [criterion.criterion_id for criterion in clause.asked] == ["T-02", "T-03"]
    failed = {"T-01": "not_met", "T-02": "not_met", "T-03": "met"}
    assert [point.key for point in asm.tally_criteria(clause, _votes(failed, failed, failed), majority=2)] == ["T-02"]


def test_a_finding_is_anchored_where_the_answers_agree_else_on_the_primary_paragraph():
    clause = _clause(_criterion("T-01"))

    def anchor(*named: List[str]) -> int:
        return asm.tally_criteria(clause, _votes(*[{"T-01": ("not_met", paragraphs)} for paragraphs in named]), majority=2)[0].position

    assert anchor(["P3"], ["P3"], ["P2"]) == 2  # a majority name P3 (position 2)
    assert anchor(["P3"], ["P2"], []) == 1  # no majority: the clause's primary paragraph
    assert anchor([], [], []) == 1  # the clause as a whole lacks the term
    assert anchor(["P1"], ["P1"], ["P1"]) == 1  # P1 is a heading, not assessable: ignored
    assert anchor(["P99"], ["nonsense"], ["P3"]) == 1
    # A conflict between two paragraphs lands on the first of them, whichever an answer leads with.
    assert anchor(["P3", "P2"], ["P2", "P3"], ["P3"]) == 1
    assert anchor(["[P3]"], ["p3"], []) == 2  # id spelling variants


def test_a_balance_criterion_cannot_fail_on_a_clause_that_runs_in_the_clients_favor():
    clause = _clause(_criterion("T-01", category=BALANCE), _criterion("T-02"))
    failed = {"T-01": "not_met", "T-02": "not_met"}
    votes = _votes(failed, failed, failed)
    assert [point.key for point in asm.tally_criteria(clause, votes, 2)] == ["T-01", "T-02"]
    assert [point.key for point in asm.tally_criteria(clause, votes, 2, favors_client=lambda position: position == 1)] == ["T-02"]


def test_a_drafting_defect_needs_enough_answer_sets_and_must_quote_the_paragraph():
    text_of = {1: "Supplier shall pay teh invoice within [__] days.", 2: "Notices are sent by e-mail."}

    def vote(*defects: Dict[str, str]) -> BatchAssessment:
        return BatchAssessment.model_validate({"clauses": [], "drafting_defects": list(defects)})

    typo = {"paragraph": "P2", "defect_type": "typographical_error", "evidence": "teh invoice", "note": "'teh' should read 'the'."}
    blank = {"paragraph": "P2", "defect_type": "blank_or_placeholder", "evidence": "[__] days", "note": "The period is blank."}
    invented = {"paragraph": "P3", "defect_type": "typographical_error", "evidence": "words that are not there", "note": "Invented."}
    points = asm.tally_defects([vote(typo, blank, invented), vote(typo, invented), vote(typo, typo, invented)], text_of, frozenset({1, 2}), required=2)
    assert [(point.key, point.position, point.risk_level, point.evidence) for point in points] == [("defect:typographical_error", 1, "Low", "teh invoice")]
    # A defect on a paragraph that is not under assessment is ignored.
    assert asm.tally_defects([vote(typo), vote(typo)], text_of, frozenset({2}), required=2) == []


def test_points_are_grouped_by_paragraph_most_serious_first_and_capped():
    def point(key: str, level: str, order: int, position: int = 1) -> asm.Point:
        return asm.Point(key=key, position=position, title=key, finding="f", risk_level=level, order=order)  # type: ignore[arg-type]

    points = [
        point("A", "Low", 0),
        point("B", "High", 5),
        point("C", "Medium", 2),
        point("B", "High", 5),
        point("D", "High", 1),
        point("E", "Low", 3),
        point("F", "Low", 4),
        point("Z", "Low", 0, position=4),
    ]
    selected = asm.select_points(points)
    assert [item.key for item in selected[1]] == ["D", "B", "C", "A", "E"]  # five at most; a repeated key once
    assert [item.key for item in selected[4]] == ["Z"]
    rendered = asm.render_points(
        [
            asm.Point(
                key="FEE-07",
                position=1,
                title="Disputed invoices",
                finding="No right to dispute is stated.",
                risk_level="Medium",
                why="Pay first, argue later.",
                resolved_when="A right to dispute is stated.",
            )
        ]
    )
    assert rendered.splitlines() == ["1. Disputed invoices: No right to dispute is stated.", "   Resolved when: A right to dispute is stated.", "   Why it matters: Pay first, argue later."]
    assert "FEE-07" not in rendered  # the drafter never sees a criterion id, so it cannot quote one


# --- cache expiry ----------------------------------------------------------------


def test_cached_outcomes_are_discarded_after_their_time_to_live(monkeypatch):
    now = [1000.0]
    cache = ZDRClauseHashCache(version_hash="ttl", ttl_seconds=60.0, clock=lambda: now[0])
    cache.set("old", NO_FINDING)
    now[0] += 59.0
    assert cache.get("old") is NO_FINDING
    cache.set("new", NO_FINDING)
    now[0] += 1.0
    assert cache.get("old") is None and cache.get("new") is NO_FINDING
    # Writing sweeps out everything that has expired, asked for or not.
    now[0] += 60.0
    cache.set("newest", NO_FINDING)
    assert len(cache) == 1
    # A time to live of zero keeps nothing at all.
    off = ZDRClauseHashCache(version_hash="ttl", ttl_seconds=0.0)
    off.set("key", NO_FINDING)
    assert len(off) == 0 and off.get("key") is None

    # The document-level cache follows the same setting.
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    clock = [5000.0]
    monkeypatch.setattr(gr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gr, "cache_ttl_seconds", lambda: 60.0)
    response = gr.GeneralReviewResponse(suggestions=[])
    gr._cache_put("doc", response)
    clock[0] += 59.0
    assert gr._cache_get("doc") is response
    clock[0] += 1.0
    assert gr._cache_get("doc") is None and len(gr._response_cache) == 0


# --- typography ------------------------------------------------------------------


def test_untouched_wording_keeps_the_source_paragraphs_own_characters():
    source = "Service Provider’s  work – per the “Budget” – is due."
    redraft = 'Service Provider\'s work - per the "Budget" and the "Plan" - is due within thirty (30) days of Accordor\'s notice.'
    assert restore_typography(source, redraft, True, True) == "Service Provider’s  work – per the “Budget” and the “Plan” – is due within thirty (30) days of Accordor’s notice."
    # An agreement typed with straight quotes keeps them in the new wording.
    assert restore_typography("Provider's fee is due.", "Provider's fee is due on Customer's request.") == "Provider's fee is due on Customer's request."
    assert restore_typography(source, source, True, True) == source


# --- mapping -----------------------------------------------------------------


def _run_mapping(request: GeneralReviewRequest, search: Any) -> List[Any]:
    async def go() -> List[Any]:
        plan = gr.build_review_plan(request, ZDRClauseHashCache(version_hash="m"))
        contexts, task = gr._start_policy_mapping(request, plan, "s")
        await task
        return [await context for context in contexts]

    gr.get_clause_search = lambda: search
    try:
        return asyncio.run(go())
    finally:
        gr.get_clause_search = lambda: None


def test_clause_types_survive_a_vector_path_outage():
    request = _request(
        _para("p1", DAMAGES),
        _para("p2", "Protection of Accordor. Service Provider shall indemnify and hold harmless Accordor from and against all third-party claims arising out of the Services."),
    )
    search = FailingSearch()
    (policies,) = _run_mapping(request, search)
    assert search.calls == 1  # only the clause Layer 1 could not lock went to the vector path
    assert policies.policies[0].canonical_type == "limitation-of-liability"
    assert policies.policies[1].canonical_type == "indemnification"
    # "Damages" names its type; "Protection of Accordor" does not, so its type was inferred and awaits confirmation.
    assert policies.named_by_heading == {0}


def test_clause_types_without_any_clause_search():
    (policies,) = _run_mapping(_request(_para("p1", DAMAGES)), None)
    assert policies.policies[0].canonical_type == "limitation-of-liability"


def test_vector_path_gets_smart_query_and_layer2_resolves():
    body = "Protection of Accordor. The Parties shall keep each other's non-public information secret and use it only for this Agreement, for five years after termination."
    search = CannedSearch([ClauseMatch(canonical_type="confidentiality", score=0.83), ClauseMatch(canonical_type="miscellaneous-other-provisions", score=0.74)])
    (policies,) = _run_mapping(_request(_para("p1", body)), search)
    assert search.queries == [body]
    assert policies.policies[0].canonical_type == "confidentiality" and policies.named_by_heading == set()


# --- end to end ------------------------------------------------------------------


def _review(request: GeneralReviewRequest) -> List[Suggestion]:
    return asyncio.run(gr.general_review_service(request, "s")).suggestions


def test_a_failed_criterion_becomes_one_redrafted_suggestion(container_stub, fresh):
    model = ScriptedModel([{"LOL-01": "not_met", "LOL-04": "not_met"}])
    container_stub(model=model, search=None)
    (suggestion,) = _review(_request(_para("p1", DAMAGES), _para("p2", NOTICES)))

    assert set(suggestion.model_dump()) == PUBLIC_SUGGESTION_FIELDS
    assert (suggestion.para_identifier, suggestion.clause, suggestion.risk_level) == ("p1", "Damages", "High")
    assert suggestion.original_text == DAMAGES
    assert suggestion.suggested_fix == DAMAGES + " The Parties shall act in good faith."  # the model's redraft, as written
    assert suggestion.reason == "The paragraph leaves the client exposed. The revision closes the gap."

    # Three answer sets for the one batch, each given the whole agreement as reference and both clauses' checklists.
    assert len(model.assess_calls) == gr.CONSENSUS_VOTES and len(model.draft_calls) == 1
    call = model.assess_calls[0]
    assert f"<agreement>\n{DAMAGES}\n\n{NOTICES}\n</agreement>" in call["reference"]
    assert '=== C1 — "Damages" ===\n[P1] Damages. In no event' in call["context"]["clauses"]
    assert "Checklist for C1 (Limitation of Liability):\n- LOL-01 | " in call["context"]["clauses"]
    assert "Checklist for C2 (Notices):" in call["context"]["clauses"]
    # The drafter is given the settled points in words, without ids, and the paragraph to revise.
    draft = model.draft_calls[0]["context"]
    assert draft["paragraph"] == DAMAGES and draft["clause_title"] == "Damages"
    assert draft["points"].startswith("1. The limit is clear: It is not stated, or the wording is contradictory.")
    assert "LOL-0" not in draft["points"] and "2. " in draft["points"]


def test_a_point_the_answer_sets_disagree_on_never_becomes_a_suggestion(container_stub, fresh):
    # Two answer sets in three fail the cap; one in three fails the notice method. Neither is unanimous.
    model = ScriptedModel([{"LOL-01": "not_met"}, {"LOL-01": "not_met", "NOT-01": "not_met"}, {}])
    container_stub(model=model, search=None)
    assert _review(_request(_para("p1", DAMAGES), _para("p2", NOTICES))) == []
    assert model.draft_calls == []


def test_unchanged_paragraphs_keep_their_outcome_without_another_model_call(container_stub, fresh):
    model = ScriptedModel([{"LOL-01": "not_met"}])
    container_stub(model=model, search=None)
    first = _review(_request(_para("p1", DAMAGES), _para("p2", NOTICES)))
    assert [(s.para_identifier, s.risk_level) for s in first] == [("p1", "High")]
    assert (len(model.assess_calls), len(model.draft_calls)) == (3, 1)

    # The same request again, with the document-level cache emptied and a model that would now answer differently.
    gr._response_cache.clear()
    model.scripts = [{"NOT-01": "not_met"}]
    assert _review(_request(_para("p1", DAMAGES), _para("p2", NOTICES))) == first
    assert (len(model.assess_calls), len(model.draft_calls)) == (3, 1)


def test_an_outcome_is_not_remembered_when_an_answer_set_was_lost(container_stub, fresh):
    # The first call and its retry both fail: the batch is decided by the two remaining answer sets.
    model = ScriptedModel([{"LOL-01": "not_met"}], fail_assessments=(0, 3))
    container_stub(model=model, search=None)
    request = _request(_para("p1", DAMAGES))
    assert [s.para_identifier for s in _review(request)] == ["p1"]
    assert len(fresh) == 0 and len(gr._response_cache) == 0
    # The next run reviews the paragraph again, in full, and that outcome is kept.
    model.fail_assessments = set()
    assert [s.para_identifier for s in _review(request)] == ["p1"]
    assert len(fresh) == 1 and len(gr._response_cache) == 1


def test_a_redraft_that_changes_nothing_is_not_a_suggestion(container_stub, fresh):
    container_stub(model=ScriptedModel([{"LOL-01": "not_met"}], revise=lambda paragraph: "[P1] " + paragraph), search=None)
    assert _review(_request(_para("p1", DAMAGES))) == []
    assert len(fresh) == 0  # nothing remembered: the paragraph is drafted again on the next run


def test_consecutive_clauses_of_one_type_are_assessed_as_one_clause(container_stub, fresh):
    term = "Term/Termination. Either Party may terminate this Agreement on thirty (30) days' written notice if the other Party is in material breach."
    materials = "Return of Materials. On termination of this Agreement, Provider shall promptly return all materials and documents belonging to Client."
    model = ScriptedModel([{"TER-09": "not_met"}])
    container_stub(model=model, search=None)
    suggestions = _review(_request(_para("p1", term), _para("p2", materials), _para("p3", NOTICES)))
    clauses = model.assess_calls[0]["context"]["clauses"]
    assert clauses.count("Checklist for") == 2 and clauses.count("- TER-09 |") == 1  # one Term checklist for both paragraphs
    assert '=== C1 — "Term/Termination" ===\n[P1] Term/Termination. Either Party' in clauses and "\n[P2] Return of Materials." in clauses
    # The finding is raised once, on the clause's first operative paragraph; each paragraph keeps its own clause name.
    assert [(s.para_identifier, s.clause) for s in suggestions] == [("p1", "Term/Termination")]


def test_an_inferred_clause_type_counts_only_when_the_model_confirms_it(container_stub, fresh):
    # The heading names no clause type; the opening words ("fees") suggest Fees & Payment. It is a licence clause.
    licence = "Licenses to Customers. You may license the Vendor Software to Customers on the fees and terms You apply to other customers for similar use of the Vendor Software."
    failed = {"FEE-04": "not_met", "FEE-07": "not_met", "GEN-01": "not_met"}

    model = ScriptedModel([{**failed, "fits": False}])
    container_stub(model=model, search=None)
    (suggestion,) = _review(_request(_para("p1", licence)))
    clauses = model.assess_calls[0]["context"]["clauses"]
    assert "Checklist for C1 (Fees & Payment) — it counts only if this clause is, in substance, a Fees & Payment clause" in clauses
    assert "General checklist for C1 — answer it as well" in clauses and "- GEN-01 | " in clauses
    # Not a fees clause: the fee criteria are set aside and only the general point is put to the drafter.
    points = model.draft_calls[0]["context"]["points"]
    assert points.startswith("1. Rights and duties are definite") and "Payment timing" not in points and "2. " not in points
    assert suggestion.risk_level == "Medium"

    # One answer set in three doubting the type is enough to set its checklist aside.
    gr.clause_cache.clear()
    gr._response_cache.clear()
    model = ScriptedModel([{**failed}, {**failed, "fits": False}, {**failed}])
    container_stub(model=model, search=None)
    _review(_request(_para("p1", licence)))
    assert "Payment timing" not in model.draft_calls[0]["context"]["points"]

    # Confirmed by every answer set, the type's checklist is the one that counts.
    gr.clause_cache.clear()
    gr._response_cache.clear()
    model = ScriptedModel([failed])
    container_stub(model=model, search=None)
    (suggestion,) = _review(_request(_para("p1", licence)))
    points = model.draft_calls[0]["context"]["points"]
    assert "Payment timing" in points and "Disputed invoices" in points and "Rights and duties are definite" not in points
    assert suggestion.risk_level == "High"

    # A type the clause's own heading names needs no confirmation and gets one checklist.
    gr.clause_cache.clear()
    gr._response_cache.clear()
    model = ScriptedModel([{"fits": False, "FEE-07": "not_met"}])
    container_stub(model=model, search=None)
    assert [s.risk_level for s in _review(_request(_para("p1", PAYMENT)))] == ["High"]
    assert "General checklist" not in model.assess_calls[0]["context"]["clauses"]


def test_a_clause_of_no_known_type_gets_the_general_checklist(container_stub, fresh):
    odd = "Each Party shall bear its own costs of negotiating this Agreement and shall not seek them from the other Party in any circumstances whatever."
    model = ScriptedModel([{"GEN-01": "not_met"}])
    container_stub(model=model, search=None)
    (suggestion,) = _review(_request(_para("p1", odd)))
    assert "Checklist for C1 (General provision):\n- GEN-01 | " in model.assess_calls[0]["context"]["clauses"]
    assert (suggestion.clause, suggestion.risk_level) == ("General provision", "Medium")
    # A fragment of a few words is not assessed at all.
    model = ScriptedModel([{"GEN-01": "not_met"}])
    container_stub(model=model, search=None)
    assert _review(_request(_para("p1", "Costs lie where they fall."))) == []
    assert model.assess_calls == []


def test_a_numbered_cross_reference_is_not_reported_broken_when_the_numbers_did_not_arrive(container_stub, fresh):
    text = "Damages. Subject to Section 9.2, in no event will either Party be liable for any indirect or consequential damages arising out of this Agreement."
    broken = [{"paragraph": "P1", "defect_type": "broken_cross_reference", "evidence": "Section 9.2", "note": "There is no Section 9.2."}]
    container_stub(model=ScriptedModel(defects=broken), search=None)
    assert _review(_request(_para("p1", text))) == []
    # Nor is a reference to an exhibit whose text was not sent: the exhibit is attached to the agreement, not missing from it.
    exhibit = [{"paragraph": "P1", "defect_type": "broken_cross_reference", "evidence": "Exhibit C", "note": "The agreement has no Exhibit C."}]
    model = ScriptedModel(defects=exhibit)
    container_stub(model=model, search=None)
    assert _review(_request(_para("p1", text.replace("Section 9.2", "Exhibit C")))) == []
    assert "the agreement refers to Exhibit C. Their text was not provided" in model.assess_calls[0]["reference"]
    # Any other broken reference the paragraph's own words show is still raised.
    other = [{"paragraph": "P1", "defect_type": "broken_cross_reference", "evidence": "the Fees clause", "note": "The agreement has no Fees clause."}]
    container_stub(model=ScriptedModel(defects=other), search=None)
    (suggestion,) = _review(_request(_para("p1", text.replace("Section 9.2", "the Fees clause"))))
    assert suggestion.risk_level == "Low"


def test_attachments_are_supplied_only_when_their_own_heading_is_among_the_paragraphs():
    body = [
        _para("a", "Service Levels. Provider shall meet the service levels in Exhibit A and the security measures in Exhibit E (the “Security Standards”)."),
        _para("b", "Fees are set out in Schedule 2."),
    ]
    assert find_attachments_not_supplied(body) == ("Exhibit A", "Exhibit E", "Schedule 2")
    assert find_attachments_not_supplied([*body, _para("c", "EXHIBIT A"), _para("d", "Schedule 2 — Fees")]) == ("Exhibit E",)
    assert find_attachments_not_supplied([_para("a", "Provider may schedule a maintenance window.")]) == ()


def test_parties_are_read_from_usage_when_the_preamble_introduces_none():
    paragraphs = [
        _para("a", "Customer Full Legal Name:"),
        _para("b", "Microsoft Corporation"),
        *[_para(f"v{n}", f"Duty {n}. CoreWeave shall perform obligation {n}, and Coreweave will report on it.") for n in range(3)],
        *[_para(f"c{n}", f"Use {n}. Customer shall pay fee {n}. Customer may dispute it. This Agreement shall govern. Each Party shall cooperate.") for n in range(3)],
    ]
    assert parties_by_usage(paragraphs) == [("CoreWeave", "CoreWeave"), ("Customer", "Customer")]
    context = resolve_party_context("Customer (Microsoft Corporation)", paragraphs)
    assert context is not None and context.resolved and (context.represented.alias, context.counterparty.alias) == ("Customer", "CoreWeave")
    # The counterparty's name is not always typed the same way; either spelling is the counterparty.
    assert clause_direction("No Publicity. Coreweave shall not disclose the identity of Customer.", context) == FAVORS_REPRESENTED
    # Two terms must stand out; one actor alone resolves nothing.
    assert parties_by_usage([_para("x", "Provider shall deliver. Provider shall invoice. Provider will support. Provider may subcontract.")]) == []


def test_a_paragraph_that_carries_on_a_broken_sentence_takes_no_title_of_its_own():
    paragraphs = [
        _para("a", "Customer Responsibilities. COREWEAVE SHALL BEAR NO LIABILITY WITH RESPECT TO DATA THAT IS LOST AS A RESULT OF THE ACTIONS OF CUSTOMER EXCEPT WHERE COREWEAVE IS IN"),
        _para("b", "BREACH OF ITS OBLIGATIONS UNDER THIS AGREEMENT. For the sake of clarity, nothing in this Section shall relieve CoreWeave of its express obligations."),
        _para("c", "Deletion of Data. CoreWeave will implement a zero retention policy."),
    ]
    assert build_clause_titles(paragraphs) == {"a": "Customer Responsibilities", "b": "Customer Responsibilities", "c": "Deletion of Data"}
    assert breaks_off_mid_sentence("Payment Terms and") is False  # a heading, however it ends
    assert breaks_off_mid_sentence("(a) Customer has not cured the breach before the expiration of the notice period; or") is False  # a list item
    assert breaks_off_mid_sentence("Customer will pay the fees and approved expenses on the terms the parties agreed.") is False


def test_a_finding_lands_on_the_paragraph_that_holds_the_words_it_quotes():
    clause = _clause(_criterion("T-01"))
    text_of = {1: "“Services” means the products and solutions provided.", 2: "“Hardware Failure” means breakage of a node that causes that node to fail."}

    def anchor(*answers: Any) -> int:
        votes = [{"T-01": CriterionAnswer(criterion_id="T-01", verdict="not_met", paragraphs=paragraphs, evidence=evidence)} for paragraphs, evidence in answers]
        return asm.tally_criteria(clause, votes, majority=2, text_of=text_of)[0].position

    # Every answer quotes the Hardware Failure definition but two write the id of the paragraph above it.
    assert anchor((["P2"], "breakage of a node that causes that node to fail"), (["P2"], "causes that node to fail"), (["P3"], "breakage of a node")) == 2
    # A quotation found in no paragraph, or in more than one, leaves the listed ids to decide.
    assert anchor((["P2"], "words that are elsewhere"), (["P2"], "means"), (["P3"], "No provision")) == 1


def test_an_empty_placeholder_is_not_raised_and_one_that_holds_a_value_is():
    text_of = {1: "Net 10 days less a [*]% discount, following the [45 day cure period], payable to ______."}

    def points(evidence: str) -> List[asm.Point]:
        vote = BatchAssessment.model_validate({"clauses": [], "drafting_defects": [{"paragraph": "P2", "defect_type": "blank_or_placeholder", "evidence": evidence, "note": "Open."}]})
        return asm.tally_defects([vote, vote], text_of, frozenset({1}), required=2)

    assert points("[*]%") == [] and points("payable to ______") == []
    assert [point.evidence for point in points("[45 day cure period]")] == ["[45 day cure period]"]


def test_a_sentence_broken_across_two_paragraphs_is_marked_for_the_assessment_and_the_drafter(container_stub, fresh):
    assert runs_on_into("CoreWeave will use best efforts to provide Customer with twelve", "(12) months’ notice prior to removing any features.")
    assert not runs_on_into("After Customer receives a proper invoice, it will pay Fees and approved expenses", "(a) Net 10 days less a discount; or")
    assert not runs_on_into("Customer will pay the fees within thirty (30) days.", "the Parties agree otherwise.")
    first = "Service Features. Supplier reserves the right to remove features of the Services and will use best efforts to provide Customer with twelve"
    second = "(12) months’ notice prior to removing any feature that could have a material effect on Customer’s use of the Services."
    model = ScriptedModel([{"GEN-01": ("not_met", ["P1"]), "fits": False}])
    container_stub(model=model, search=None)
    _review(_request(_para("p1", first), _para("p2", second)))
    assert f"with twelve {gr._RUNS_ON_MARK}\n[P2] (12) months" in model.assess_calls[0]["context"]["clauses"]
    notes = {call["context"]["paragraph"][:7]: call["context"]["paragraph_notes"] for call in model.draft_calls}
    assert "stops in the middle of a sentence" in notes["Service"]


def test_the_drafter_is_told_whom_the_paragraph_binds_and_the_legal_teams_fallback_position(container_stub, fresh):
    preamble = _para("p0", "This Agreement is made between Acme Inc. (“Customer”) and Beta LLC (“Supplier”).")
    publicity = _para("p1", "No Publicity. Supplier shall not use the name or logo of Customer in any advertising or publicity whatever, in any medium, at any time during the Term.")
    model = ScriptedModel([{"PUB-02": "not_met"}])
    container_stub(model=model, search=None)
    _review(_request(preamble, publicity, party="Customer"))
    context = model.draft_calls[0]["context"]
    assert "places obligations on Supplier alone" in context["paragraph_notes"]
    assert context["fallback"] == _policy("publicity-and-use-of-names").fallback_guidelines != ""
    assert context["added_words"] == gr._ADDED_WORDS_BASE + gr._ADDED_WORDS_PER_POINT


def test_a_redraft_that_fills_a_blank_or_only_strikes_a_provision_is_not_offered(container_stub, fresh):
    blank = "Fees and Payment. Client shall pay each invoice within thirty (30) days, less a discount of [*]% where payment is made within ten (10) days of the invoice date."
    container_stub(model=ScriptedModel([{"FEE-07": "not_met"}], revise=lambda paragraph: paragraph.replace("[*]%", "two percent (2%)") + " Late amounts bear interest."), search=None)
    assert _review(_request(_para("p1", blank))) == []
    container_stub(model=ScriptedModel([{"FEE-07": "not_met"}], revise=lambda paragraph: paragraph + " Late amounts bear interest."), search=None)
    assert len(_review(_request(_para("p1", blank)))) == 1
    # Striking a sentence and writing nothing in its place gives the provision up; that is not offered as a fix.
    two = PAYMENT + " By accepting an invoice, an Affiliate agrees to be bound by the terms of this Agreement as if it were a party."
    gr.clause_cache.clear()
    gr._response_cache.clear()
    container_stub(model=ScriptedModel([{"FEE-07": "not_met"}], revise=lambda paragraph: PAYMENT), search=None)
    assert _review(_request(_para("p1", two))) == []


def test_a_term_that_protects_only_the_provider_is_not_raised_for_the_customer(container_stub, fresh):
    preamble = _para("p0", "This Agreement is made between Acme Inc. (“Customer”) and Beta LLC (“Supplier”).")
    scope = _para("p1", "Scope of Services. Supplier shall provide to Customer the hosting services and the related support services that are described in each Order Form.")
    failed = {"SCO-02": "not_met"}
    container_stub(model=ScriptedModel([failed]), search=None)
    assert _review(_request(preamble, scope, party="Customer")) == []  # "Exclusions are stated": the customer's counsel does not ask for exclusions
    container_stub(model=ScriptedModel([failed]), search=None)
    assert len(_review(_request(preamble, scope, party="Supplier"))) == 1
    assert provider_protective_criteria() == {"SCO-02", "CUS-02"}
    # An obligation sitting inside a definition can only be moved, which a one-paragraph suggestion cannot do: not raised for anyone.
    definition = _para("p1", "Definitions. “Order Form” means an ordering document entered into between the Parties, and by entering into one an Affiliate agrees to be bound by this Agreement.")
    model = ScriptedModel([{"DEF-07": "not_met"}])
    container_stub(model=model, search=None)
    assert _review(_request(definition)) == [] and model.draft_calls == []


def test_a_paragraph_that_relies_on_an_attachment_is_redrafted_by_reference_to_it(container_stub, fresh):
    definition = _para("p0", "Definitions. “Maintenance Policy” means Supplier’s maintenance policy, in the form attached as Exhibit D.")
    support = _para("p1", "Support and Maintenance. Supplier will provide maintenance for the Services in accordance with the Maintenance Policy and will respond to support requests.")
    model = ScriptedModel([{"SUP-04": "not_met"}])
    container_stub(model=model, search=None)
    _review(_request(definition, support))
    assert "the Maintenance Policy (Exhibit D)" in model.assess_calls[0]["context"]["attachments"]
    (draft,) = [call["context"] for call in model.draft_calls]
    assert "relies on the Maintenance Policy (Exhibit D), whose text was not provided" in draft["paragraph_notes"]


def test_a_redraft_far_over_its_allowance_is_sent_back_once(container_stub, fresh):
    assert [gr._added_words_allowed([None] * count) for count in (1, 3, 5)] == [55, 85, 100]
    long_tail = " " + " ".join(f"word{number}" for number in range(200))
    drafts = iter([long_tail, " Late amounts bear interest."])
    model = ScriptedModel([{"FEE-07": "not_met"}], revise=lambda paragraph: paragraph + next(drafts))
    container_stub(model=model, search=None)
    (suggestion,) = _review(_request(_para("p1", PAYMENT)))
    assert suggestion.suggested_fix == PAYMENT + " Late amounts bear interest."
    assert [call["context"].get("over_length") for call in model.draft_calls] == [None, 200]


def test_a_suspension_heading_does_not_make_a_clause_a_termination_clause():
    assert classify_clause_resilient("Suspension of Service", "", 40).canonical_type != "term-and-termination"
    assert classify_clause_resilient("Suspension and Termination", "", 40).canonical_type == "term-and-termination"


def test_a_redraft_that_is_not_a_revision_of_its_paragraph_is_not_offered(container_stub, fresh):
    another = "“Hardware Failure” means a physical malfunction of a GPU node that renders that node unable to perform its intended compute functions."
    container_stub(model=ScriptedModel([{"LOL-01": "not_met"}], revise=lambda paragraph: another), search=None)
    assert _review(_request(_para("p1", DAMAGES))) == []
    assert len(fresh) == 0 and len(gr._response_cache) == 0  # an answer was lost: nothing is remembered


def test_streaming_service_emits_public_shape(container_stub, fresh):
    container_stub(model=ScriptedModel([{"FEE-07": "not_met"}]), search=None)

    async def collect() -> str:
        chunks = []
        async for frame in gr.general_review_streaming_service(_request(_para("p1", PAYMENT)), "s"):
            chunks.append(frame)
        return "".join(chunks)

    stream = asyncio.run(collect())
    payload = "".join(json.loads(line[len("data: ") :]) for line in stream.split("\n\n") if line.startswith("data: ") and line != "data: [DONE]")
    suggestions = json.loads(payload)["suggestions"]
    assert len(suggestions) == 1
    assert set(suggestions[0]) == PUBLIC_SUGGESTION_FIELDS
    assert suggestions[0]["risk_level"] == "High"  # a fees clause is High in the legal team's classification
    assert stream.endswith("data: [DONE]\n\n")
    # The streamed review is stored like any other: the same request again is a cache hit.
    assert len(gr._response_cache) == 1
