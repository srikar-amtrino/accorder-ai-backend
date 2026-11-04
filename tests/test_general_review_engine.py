"""Unit and offline end-to-end tests for the deterministic General Review pipeline."""

import asyncio
from typing import Any, Dict, List, Optional, Sequence

import pytest

from src.schemas.clause_mapping import ClauseMatch, ClausePolicy
from src.schemas.general_review import GeneralReviewFindings, GeneralReviewRequest, Suggestion, TextInformation
from src.services.clause_mapping.policy_store import get_clause_policy_store
from src.services.clause_mapping.resilient_classifier import HEADING_TO_CANONICAL_TYPE, classify_clause_resilient, resolve_qdrant_weighted_knn
from src.services.general_review.clause_cache import ZDRClauseHashCache
from src.services.general_review.risk_matrix import compute_deterministic_risk_level, tier_for_unmapped
from src.services.general_review.segmentation import build_smart_embedding_query, detect_clause_start
from src.tools import general_review as gr

PUBLIC_SUGGESTION_FIELDS = {"clause", "risk_level", "reason", "original_text", "suggested_fix", "para_identifier"}


# --- fakes -------------------------------------------------------------------


class FakeModel:
    """Stands in for BedrockModel.generate: returns canned findings and records every call."""

    def __init__(self, findings_by_call: Optional[List[List[Dict[str, Any]]]] = None, default: Optional[List[Dict[str, Any]]] = None) -> None:
        self.findings_by_call = list(findings_by_call or [])
        self.default = default or []
        self.calls: List[Dict[str, Any]] = []

    async def generate(self, prompt: str, context: Dict[str, Any], response_model: Any, session_id: str, system_message: Optional[str] = None, temperature: float = 0.0) -> Any:
        self.calls.append({"context": context, "response_model": response_model, "temperature": temperature})
        findings = self.findings_by_call.pop(0) if self.findings_by_call else self.default
        return response_model.model_validate({"findings": findings})


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


def _finding(original_text: str, **facts: Any) -> Dict[str, Any]:
    base = {
        "clause": "Clause",
        "reason": "Reason.",
        "original_text": original_text,
        "suggested_fix": original_text + " [fixed]",
        "triggered_unacceptable_flags": [],
        "falls_short_of_acceptable_standard": False,
        "is_materially_one_sided_against_client": False,
        "severity_category": "minor_drafting_hygiene",
    }
    base.update(facts)
    return base


LOL_TRIGGER = "no aggregate liability cap specified (unlimited direct damages exposure)"
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


def test_build_batches_cuts_at_inline_lead_in():
    filler = ("the parties acknowledge the recitals above and agree as follows " * 120).strip()
    assert len(filler) > gr.BATCH_CHAR_BUDGET
    paragraphs = [_para("p1", filler), _para("p2", DAMAGES), _para("p3", NOTICES)]
    batches = gr._build_batches(paragraphs)
    assert len(batches) == 2
    assert [para.paraindetifier for _, para in batches[1][0]] == ["p2", "p3"]
    assert batches[1][1] == ""  # a clean cut carries no context tail


def test_smart_query_keeps_definitions_inside_a_definitions_clause():
    query = build_smart_embedding_query("Definitions", ['"Services" means the services described in a Statement of Work.', '"Deliverables" means the work product.'])
    assert '"Services" means' in query


# --- classifier --------------------------------------------------------------


def test_every_router_slug_has_a_policy():
    store = get_clause_policy_store()
    missing = sorted({slug for _, slug in HEADING_TO_CANONICAL_TYPE if store.get(slug) is None})
    assert not missing, missing


def test_weighted_knn_prefers_supported_type_and_is_deterministic():
    hits = [
        ClauseMatch(canonical_type="general-provisions", score=0.81),
        ClauseMatch(canonical_type="confidentiality", score=0.80),
        ClauseMatch(canonical_type="confidentiality", score=0.79),
        ClauseMatch(canonical_type="confidentiality", score=0.78),
        ClauseMatch(canonical_type="data-protection-and-privacy", score=0.69),
    ]
    assert resolve_qdrant_weighted_knn(hits)[0] == "confidentiality"
    assert resolve_qdrant_weighted_knn(hits, heading_boost_slug="general-provisions")[0] == "general-provisions"
    assert resolve_qdrant_weighted_knn([ClauseMatch(canonical_type="notices", score=0.5)]) == (None, 0.0)


def test_generic_heading_does_not_lock_and_yields_to_vector_vote():
    body = ["Miscellaneous. Each party shall keep the other's non-public information secret, use it only for this engagement, and return it on request; these duties last five years."]
    query = build_smart_embedding_query("Miscellaneous", body)
    words = len(" ".join(body).split())
    with_hits = classify_clause_resilient("Miscellaneous", query, words, [ClauseMatch(canonical_type="confidentiality", score=0.79)])
    assert (with_hits.canonical_type, with_hits.source) == ("confidentiality", "layer2_qdrant")
    without = classify_clause_resilient("Miscellaneous", query, words, None)
    assert (without.canonical_type, without.source) == ("general-provisions", "layer3_fallback")
    specific = classify_clause_resilient(
        "Severability", "Severability. If any provision is held invalid, the remainder continues in force and effect.", 16, [ClauseMatch(canonical_type="confidentiality", score=0.79)]
    )
    assert (specific.canonical_type, specific.source) == ("general-provisions", "layer1_heading")


# --- risk matrix -------------------------------------------------------------


@pytest.mark.parametrize(
    "tier, flags, short, one_sided, severity, expected",
    [
        ("High", ["x"], True, False, "uncapped_or_severe_exposure", "High"),
        ("Medium", ["x"], False, False, "uncapped_or_severe_exposure", "High"),
        ("Medium", ["x"], False, False, "material_commercial_deviation", "Medium"),
        ("Low", ["x"], False, False, "minor_drafting_hygiene", "Medium"),
        ("High", [], True, True, "material_commercial_deviation", "High"),
        ("High", [], True, False, "uncapped_or_severe_exposure", "High"),
        ("High", [], True, False, "material_commercial_deviation", "Medium"),
        ("Medium", [], True, False, "material_commercial_deviation", "Medium"),
        ("Medium", [], True, False, "minor_drafting_hygiene", "Low"),
        ("Low", [], True, True, "material_commercial_deviation", "Low"),
        ("High", [], False, True, "uncapped_or_severe_exposure", "Low"),
        ("nonsense", [], True, False, "material_commercial_deviation", "Medium"),
    ],
)
def test_risk_matrix(tier, flags, short, one_sided, severity, expected):
    assert compute_deterministic_risk_level(tier, flags, short, one_sided, severity) == expected


def test_tier_for_unmapped():
    assert tier_for_unmapped("uncapped_or_severe_exposure") == "High"
    assert tier_for_unmapped("material_commercial_deviation") == "Medium"
    assert tier_for_unmapped("minor_drafting_hygiene") == "Low"


def test_only_verbatim_policy_triggers_count():
    policy = _policy("limitation-of-liability")
    reported = [LOL_TRIGGER.upper(), "the cap is missing", LOL_TRIGGER + "."]
    assert gr._matching_triggers(reported, policy, "Damages") == [LOL_TRIGGER]
    assert gr._matching_triggers(reported, None, "Damages") == []


def test_schema_tolerates_model_quirks():
    parsed = GeneralReviewFindings.model_validate(
        {
            "findings": (
                '[{"clause": "A", "reason": "r", "original_text": "o", "suggested_fix": "s", "triggered_unacceptable_flags": "one flag", '
                '"falls_short_of_acceptable_standard": "true", "is_materially_one_sided_against_client": false, "severity_category": "Material Commercial Deviation"}]'
            )
        }
    )
    finding = parsed.findings[0]
    assert finding.triggered_unacceptable_flags == ["one flag"]
    assert finding.falls_short_of_acceptable_standard is True
    assert finding.severity_category == "material_commercial_deviation"
    assert "risk_level" not in GeneralReviewFindings.model_json_schema()["$defs"]["ReviewFinding"]["properties"]


def test_public_suggestion_shape_is_unchanged():
    assert set(Suggestion.model_fields) == PUBLIC_SUGGESTION_FIELDS


# --- validation + risk -------------------------------------------------------


def test_validate_batch_computes_risk_from_policy_tier():
    batch = [(0, _para("p1", DAMAGES)), (1, _para("p2", NOTICES))]
    titles = {"p1": "Damages", "p2": "Notices"}
    policies = gr.BatchPolicies(block="x", policy_of={0: _policy("limitation-of-liability"), 1: _policy("notices")})
    findings = GeneralReviewFindings.model_validate(
        {
            "findings": [
                _finding(DAMAGES, triggered_unacceptable_flags=[LOL_TRIGGER], falls_short_of_acceptable_standard=True, severity_category="uncapped_or_severe_exposure"),
                _finding("effective upon sending", falls_short_of_acceptable_standard=True, severity_category="minor_drafting_hygiene"),
            ]
        }
    ).findings
    valid = gr._validate_batch(findings, batch, set(), titles, policies)
    assert [(pos, s.clause, s.risk_level) for pos, s in valid] == [(0, "Damages", "High"), (1, "Notices", "Low")]
    assert valid[1][1].original_text == NOTICES  # expanded to the whole paragraph
    assert set(valid[0][1].model_dump()) == PUBLIC_SUGGESTION_FIELDS


def test_validate_batch_unmapped_clause_uses_severity():
    batch = [(0, _para("p1", DAMAGES))]
    findings = GeneralReviewFindings.model_validate(
        {"findings": [_finding(DAMAGES, triggered_unacceptable_flags=[LOL_TRIGGER], falls_short_of_acceptable_standard=True, severity_category="uncapped_or_severe_exposure")]}
    ).findings
    ((_, suggestion),) = gr._validate_batch(findings, batch, set(), {}, gr.BatchPolicies())
    assert suggestion.risk_level == "High"  # flags dropped (no policy); High tier from severity + falls short + severe


# --- mapping -----------------------------------------------------------------


def _run_mapping(request: GeneralReviewRequest, search: Any) -> List[gr.BatchPolicies]:
    async def go() -> List[gr.BatchPolicies]:
        batches = gr._build_batches(request.textinformation)
        titles = gr.build_clause_titles(request.textinformation)
        contexts, task = gr._start_policy_mapping(request, batches, titles, "s")
        await task
        return [await context for context in contexts]

    gr.get_clause_search = lambda: search
    try:
        return asyncio.run(go())
    finally:
        gr.get_clause_search = lambda: None


def test_policy_block_survives_vector_path_outage():
    request = GeneralReviewRequest(
        textinformation=[
            _para("p1", DAMAGES),
            _para("p2", "Protection of Accordor. Service Provider shall indemnify and hold harmless Accordor from and against all third-party claims arising out of the Services."),
        ]
    )
    search = FailingSearch()
    (policies,) = _run_mapping(request, search)
    assert search.calls == 1  # only the clause Layer 1 could not lock went to the vector path
    assert "Limitation of Liability" in policies.block and "Indemnification" in policies.block
    assert LOL_TRIGGER in policies.block
    assert policies.policy_of[0].canonical_type == "limitation-of-liability"
    assert policies.policy_of[1].canonical_type == "indemnification"


def test_policy_block_without_any_clause_search():
    request = GeneralReviewRequest(textinformation=[_para("p1", DAMAGES)])
    (policies,) = _run_mapping(request, None)
    assert policies.policy_of[0].canonical_type == "limitation-of-liability"


def test_vector_path_gets_smart_query_and_layer2_resolves():
    body = "Protection of Accordor. The Parties shall keep each other's non-public information secret and use it only for this Agreement, for five years after termination."
    request = GeneralReviewRequest(textinformation=[_para("p1", body)])
    search = CannedSearch([ClauseMatch(canonical_type="confidentiality", score=0.83), ClauseMatch(canonical_type="general-provisions", score=0.74)])
    (policies,) = _run_mapping(request, search)
    assert search.queries == ["Protection of Accordor. The Parties shall keep each other's non-public information secret and use it only for this Agreement, for five years after termination."]
    assert policies.policy_of[0].canonical_type == "confidentiality"


# --- clause cache + end-to-end -------------------------------------------------


def _request(*paragraphs: TextInformation, party: str = "Accordor") -> GeneralReviewRequest:
    return GeneralReviewRequest(textinformation=list(paragraphs), party_represented=party, agreement_type="MSA")


def test_unchanged_paragraph_keeps_its_outcome_and_response_shape(container_stub, monkeypatch):
    cache = ZDRClauseHashCache(version_hash="test")
    monkeypatch.setattr(gr, "clause_cache", cache)
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    first = [
        _finding(DAMAGES, clause="Damages", reason="No cap.", triggered_unacceptable_flags=[LOL_TRIGGER], falls_short_of_acceptable_standard=True, severity_category="uncapped_or_severe_exposure")
    ]
    # Second run: the model changes its mind about Damages and newly flags Payment.
    second = [
        _finding(DAMAGES, clause="Damages", reason="Different reason.", falls_short_of_acceptable_standard=False, severity_category="minor_drafting_hygiene"),
        _finding(PAYMENT, clause="Fees and Payment", reason="Net 15.", falls_short_of_acceptable_standard=True, severity_category="material_commercial_deviation"),
    ]
    model = FakeModel(findings_by_call=[first, second])
    container_stub(model=model, search=None)

    run1 = asyncio.run(gr.general_review_service(_request(_para("p1", DAMAGES), _para("p2", NOTICES)), "s"))
    assert [(s.para_identifier, s.risk_level, s.reason) for s in run1.suggestions] == [("p1", "High", "No cap.")]
    assert all(set(s.model_dump()) == PUBLIC_SUGGESTION_FIELDS for s in run1.suggestions)
    assert len(model.calls) == 1
    assert "Legal team policies" in model.calls[0]["context"]["policy_context"]
    assert model.calls[0]["temperature"] == 0.0

    # Same two paragraphs plus a new one: a different document, but the old paragraphs are unchanged.
    run2 = asyncio.run(gr.general_review_service(_request(_para("p1", DAMAGES), _para("p2", NOTICES), _para("p3", PAYMENT)), "s"))
    assert len(model.calls) == 2
    assert [(s.para_identifier, s.risk_level, s.reason) for s in run2.suggestions] == [("p1", "High", "No cap."), ("p3", "Medium", "Net 15.")]

    # Everything cached now: an identical re-run with a fresh document cache makes no model call.
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    run3 = asyncio.run(gr.general_review_service(_request(_para("p1", DAMAGES), _para("p2", NOTICES), _para("p3", PAYMENT)), "s"))
    assert len(model.calls) == 2
    assert run3 == run2


def test_streaming_service_emits_public_shape(container_stub, monkeypatch):
    monkeypatch.setattr(gr, "clause_cache", ZDRClauseHashCache(version_hash="stream"))
    monkeypatch.setattr(gr, "_response_cache", gr.OrderedDict())
    container_stub(model=FakeModel(default=[_finding(NOTICES, clause="Notices", severity_category="minor_drafting_hygiene", falls_short_of_acceptable_standard=True)]), search=None)

    async def collect() -> str:
        chunks = []
        async for frame in gr.general_review_streaming_service(_request(_para("p1", NOTICES)), "s"):
            chunks.append(frame)
        return "".join(chunks)

    stream = asyncio.run(collect())
    import json

    payload = "".join(json.loads(line[len("data: ") :]) for line in stream.split("\n\n") if line.startswith("data: ") and line != "data: [DONE]")
    suggestions = json.loads(payload)["suggestions"]
    assert len(suggestions) == 1
    assert set(suggestions[0]) == PUBLIC_SUGGESTION_FIELDS
    assert suggestions[0]["risk_level"] == "Low"
    assert stream.endswith("data: [DONE]\n\n")
