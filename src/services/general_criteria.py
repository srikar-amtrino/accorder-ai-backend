"""
General and fragment-defect criteria for the deterministic general review pipeline.

GENERAL_CRITERIA  — 8 questions that apply to any whole clause whose canonical
                    type could not be determined (confidence < 0.70).  Every
                    question has a weight and a gate so the scorer can treat
                    them identically to type-specific criteria.

FRAGMENT_DEFECT_CRITERIA — 2 binary questions for orphaned fragment paragraphs
                    (short, connector-start, no obligation verb).  Fragments are
                    not scored; a single "Yes" answer produces a Low-risk
                    suggestion about the specific defect found.
"""

from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# General criteria — applied to whole unclassified clauses (PATH B)
# Format mirrors the taxonomy JSON criteria dicts so the scorer and
# extraction prompt can treat them identically to type-specific criteria.
# ---------------------------------------------------------------------------
GENERAL_CRITERIA: List[Dict] = [
    {
        "criterion_id": "GEN-01",
        "criterion": "Obligations are balanced",
        "issue_category": "Balance",
        "applies_when": "Always",
        "question_for_llm": (
            "Are the obligations in this clause one-sided in a way that "
            "significantly disadvantages the reviewed party without "
            "commercial justification?"
        ),
        "met_when": "Obligations are mutual or the difference is commercially reasonable.",
        "partly_met_when": "Obligations are somewhat one-sided but not extreme.",
        "not_met_when": "Obligations are entirely one-sided and expose the reviewed party to unreasonable risk.",
        "answer_options": ["Met", "Partly met", "Not met"],
        "weight": "Core",
        "weight_value": 3,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "One-sided obligations create disproportionate risk and are frequently renegotiated.",
        "evidence_required": "Quote the specific one-sided wording.",
    },
    {
        "criterion_id": "GEN-02",
        "criterion": "Timeframes are specific",
        "issue_category": "Clarity",
        "applies_when": "Always",
        "question_for_llm": (
            "Are obligations or rights in this clause tied to vague time "
            "standards (such as 'promptly', 'immediately', 'reasonable time', "
            "'as soon as practicable') where a definite number of days should exist?"
        ),
        "met_when": "All time-sensitive obligations state a definite period.",
        "partly_met_when": "Some obligations have definite periods but others use vague standards.",
        "not_met_when": "Vague time standards are used throughout with no fallback.",
        "answer_options": ["Met", "Partly met", "Not met"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "Vague timeframes are argued differently by each party and may render an obligation unenforceable.",
        "evidence_required": "Quote the vague time standard.",
    },
    {
        "criterion_id": "GEN-03",
        "criterion": "Financial terms are stated",
        "issue_category": "Completeness",
        "applies_when": "Always",
        "question_for_llm": (
            "Where this clause involves financial amounts, caps, thresholds or "
            "payment obligations, are they stated explicitly or by a determinable formula?"
        ),
        "met_when": "All financial amounts are stated or calculable from information in the agreement.",
        "partly_met_when": "Some amounts are stated but others are left to future agreement.",
        "not_met_when": "No amounts or formula are given where they are needed.",
        "answer_options": ["Met", "Partly met", "Not met", "Not applicable"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "High",
        "gate_rank": 2,
        "why_it_matters": "Missing financial terms leave exposure unlimited and disputes almost certain.",
        "evidence_required": "Identify where amounts should appear but do not.",
    },
    {
        "criterion_id": "GEN-04",
        "criterion": "Key terms are defined",
        "issue_category": "Clarity",
        "applies_when": "Always",
        "question_for_llm": (
            "Does this clause rely on terms that are not defined anywhere in the "
            "clause or the broader agreement, leaving their meaning open to dispute?"
        ),
        "met_when": "All key terms used are defined or have a plain commercial meaning.",
        "partly_met_when": "Most terms are defined but one or two critical ones are not.",
        "not_met_when": "Central terms are undefined and their meaning is genuinely ambiguous.",
        "answer_options": ["Met", "Partly met", "Not met"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "Undefined terms are interpreted by courts, often unpredictably.",
        "evidence_required": "Quote the undefined term in context.",
    },
    {
        "criterion_id": "GEN-05",
        "criterion": "Liability is not uncapped",
        "issue_category": "Balance",
        "applies_when": "Always",
        "question_for_llm": (
            "Does this clause create unlimited or uncapped financial liability "
            "for the reviewed party — including through open-ended indemnities, "
            "broad warranties, or unlimited exposure to consequential loss?"
        ),
        "met_when": "Liability is capped, excluded, or proportionate.",
        "partly_met_when": "Liability is partially limited but significant exposure remains.",
        "not_met_when": "Liability is unlimited and exposure is material.",
        "answer_options": ["Met", "Partly met", "Not met"],
        "weight": "Core",
        "weight_value": 3,
        "gate_if_not_met": "High",
        "gate_rank": 2,
        "why_it_matters": "Uncapped liability can exceed the value of the entire contract.",
        "evidence_required": "Quote the wording creating unlimited exposure.",
    },
    {
        "criterion_id": "GEN-06",
        "criterion": "No unfilled placeholders",
        "issue_category": "Completeness",
        "applies_when": "Always",
        "question_for_llm": (
            "Does this clause contain unfilled placeholder text such as [INSERT], "
            "[●], ___, TBD, or bracketed blanks where actual values should appear?"
        ),
        "met_when": "No unfilled placeholders are present.",
        "partly_met_when": None,
        "not_met_when": "One or more placeholders are present that should have been filled.",
        "answer_options": ["Met", "Not met"],
        "weight": "Minor",
        "weight_value": 1,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "Placeholders indicate incomplete drafting and may void the obligation.",
        "evidence_required": "Quote the placeholder text.",
    },
    {
        "criterion_id": "GEN-07",
        "criterion": "No internal contradiction",
        "issue_category": "Consistency",
        "applies_when": "Always",
        "question_for_llm": (
            "Does this clause contradict itself or directly conflict with another "
            "clause visible in the same document batch?"
        ),
        "met_when": "The clause is internally consistent and does not contradict other provisions in the batch.",
        "partly_met_when": None,
        "not_met_when": "A direct contradiction exists between this clause and another provision.",
        "answer_options": ["Met", "Not met"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "Medium",
        "gate_rank": 1,
        "why_it_matters": "Contradictions create enforceability disputes and require costly litigation to resolve.",
        "evidence_required": "Quote both the conflicting provisions.",
    },
    {
        "criterion_id": "GEN-08",
        "criterion": "Rights and obligations are paired",
        "issue_category": "Balance",
        "applies_when": "Always",
        "question_for_llm": (
            "Does this clause grant a right to one party without a corresponding "
            "obligation, or impose an obligation without a corresponding benefit or right?"
        ),
        "met_when": "Rights and obligations are reasonably paired.",
        "partly_met_when": "Minor imbalance exists but the overall structure is reasonable.",
        "not_met_when": "A party has a right with no obligation or an obligation with no right.",
        "answer_options": ["Met", "Partly met", "Not met"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "Unpaired obligations create leverage imbalances and are frequently disputed.",
        "evidence_required": "Quote the unpaired right or obligation.",
    },
    {
        "criterion_id": "GEN-09",
        "criterion": "Operative effect is unambiguous",
        "issue_category": "Clarity",
        "applies_when": "Always",
        "question_for_llm": (
            "Could the operative effect of this clause — what it requires, permits, or prohibits — "
            "be determined by a court without recourse to extrinsic evidence? "
            "Or is the clause so ambiguous that two reasonable lawyers would read it differently?"
        ),
        "met_when": "The clause has a clear, singular operative meaning.",
        "partly_met_when": "The clause is mostly clear but contains one or more ambiguous phrases.",
        "not_met_when": "The clause is drafted so ambiguously that its effect is genuinely uncertain.",
        "answer_options": ["Met", "Partly met", "Not met"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "An ambiguous clause is as dangerous as no clause — it will be argued differently by each party.",
        "evidence_required": "Quote the ambiguous wording.",
    },
    {
        "criterion_id": "GEN-10",
        "criterion": "Trigger conditions are identified",
        "issue_category": "Completeness",
        "applies_when": "Always",
        "question_for_llm": (
            "Where this clause creates an obligation or right that only arises on the occurrence "
            "of a condition, event, or milestone, is that condition, event, or milestone "
            "clearly identified within this clause or by a cross-reference?"
        ),
        "met_when": "Every condition or trigger for an obligation or right is identified.",
        "partly_met_when": "Some triggers are identified but others are left vague or unidentified.",
        "not_met_when": "An obligation or right exists with no stated trigger or condition.",
        "answer_options": ["Met", "Partly met", "Not met", "Not applicable"],
        "weight": "Major",
        "weight_value": 2,
        "gate_if_not_met": "None",
        "gate_rank": 0,
        "why_it_matters": "An obligation with no stated trigger is unenforceable until the dispute arises.",
        "evidence_required": "Identify the obligation and the missing trigger.",
    },
]


# ---------------------------------------------------------------------------
# Fragment defect criteria — PATH C (2 questions only, no scorer)
# A "Yes" answer on either → Low-risk suggestion with a plain-language reason.
# ---------------------------------------------------------------------------
FRAGMENT_DEFECT_CRITERIA: List[Dict] = [
    {
        "criterion_id": "FRG-01",
        "criterion": "No unfilled placeholders",
        "question": (
            "Does this text contain a blank, placeholder, or obviously missing "
            "value such as [INSERT], [●], ___, TBD, or a blank line?"
        ),
        "risk_level_if_yes": "Low",
        "reason_template": (
            "This paragraph contains an unfilled placeholder ({quote}). "
            "The document appears to be an incomplete draft and this value must "
            "be inserted before the agreement is executed."
        ),
    },
    {
        "criterion_id": "FRG-02",
        "criterion": "No direct contradiction",
        "question": (
            "Does this text explicitly contradict a provision visible elsewhere "
            "in the same review — for example, naming a different party, date, "
            "amount or governing law than another clause states?"
        ),
        "risk_level_if_yes": "Low",
        "reason_template": (
            "This paragraph appears to conflict with another provision in the "
            "agreement ({quote}). The contradiction should be resolved before "
            "execution to avoid disputes about which term governs."
        ),
    },
]

