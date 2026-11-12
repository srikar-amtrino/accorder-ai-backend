"""From checklist answers to the points a paragraph must resolve.

The assessment call asks the model a closed set of questions: for each
clause, the criteria the taxonomy lists for that clause type, each with its
own tests for Met / Partly met / Not met. Several independent sets of answers
are taken. This module renders the clauses and their checklists for the
prompt, and turns the answers into findings in plain code:

- a criterion is failed only when every answer set reports a shortfall on it,
  so a point the model is in two minds about is left out on every run rather
  than raised on some and not on others;
- the paragraph a finding is anchored on is the one a majority point to, and
  otherwise the clause's primary operative paragraph;
- a Balance criterion is never failed on a clause that runs in the client's
  favor;
- each finding's risk level is the legal team's risk allocation for its type
  of clause (risk_matrix), never the model's.

Nothing here reads the model's free-text wording to make a decision.
"""

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from src.config.logging import get_logger
from src.schemas.general_review import NOT_MET, PARTLY_MET, BatchAssessment, CriterionAnswer
from src.services.general_review.defined_terms import UndefinedTerm
from src.services.general_review.risk_matrix import RiskLevel, criterion_risk_level, defect_risk_level, rank
from src.services.general_review.taxonomy_gates import BALANCE, CORE, MAJOR, Criterion

logger = get_logger(__name__)

# Criteria weights that can raise a finding on their own. A Minor criterion is a refinement, not a defect;
# it is neither asked nor reported unless the taxonomy makes it a gate.
REPORTABLE_WEIGHTS = (CORE, MAJOR)

# A paragraph's redraft resolves at most this many points, the most serious first, so one rewrite stays one coherent edit.
MAX_POINTS_PER_PARAGRAPH = 5

# The checklist for a clause whose type the taxonomy does not name. Same schema as the taxonomy's own criteria.
GENERIC_TYPE_LABEL = "General provision"
GENERIC_TIER = "Medium"
GENERIC_CRITERIA: Tuple[Criterion, ...] = (
    Criterion(
        "GEN-01",
        GENERIC_TYPE_LABEL,
        "Rights and duties are definite",
        "Most rights or duties are vague or open-ended.",
        None,
        "Clarity",
        question="Does each right or duty the clause creates say who must do what, and when or to what standard?",
        met_when="Each states who, what, and when or to what standard.",
        partly_met_when="Some lack one of these.",
        weight=CORE,
        why_it_matters="A vague right or duty cannot be applied or enforced.",
    ),
    Criterion(
        "GEN-02",
        GENERIC_TYPE_LABEL,
        "Terms are even-handed",
        "The rights are one-sided without limit.",
        None,
        BALANCE,
        question="Do the rights, discretions and remedies in the clause apply to both parties in the same way?",
        met_when="They are mutual, or the clause creates none.",
        partly_met_when="They are one-sided but limited.",
        weight=MAJOR,
        why_it_matters="A one-sided right leaves the other party exposed with no equivalent protection.",
    ),
    Criterion(
        "GEN-03",
        GENERIC_TYPE_LABEL,
        "Exposure is bounded",
        "The exposure has no limit.",
        None,
        "Enforceability",
        question="Is the payment, liability, indemnity or open-ended commitment limited by amount, time or scope?",
        met_when="A limit by amount, time or scope is stated.",
        partly_met_when="A limit is stated but leaves a material part of the exposure open.",
        applies_when="The clause imposes a payment, liability, indemnity or open-ended commitment",
        weight=MAJOR,
        why_it_matters="An unbounded commitment cannot be priced or insured.",
    ),
    Criterion(
        "GEN-04",
        GENERIC_TYPE_LABEL,
        "Consistent with the rest of the agreement",
        "The clause conflicts with another clause of the agreement.",
        None,
        "Consistency",
        question="Is the clause consistent with the other terms of the agreement?",
        met_when="It does not conflict with any other clause.",
        weight=MAJOR,
        why_it_matters="Conflicting terms leave it uncertain which one applies.",
    ),
)

DEFECT_TITLES = {
    "blank_or_placeholder": "Blank or placeholder",
    "broken_cross_reference": "Incorrect cross-reference",
    "typographical_error": "Typographical error",
    # Found in code (defined_terms), never asked of the model.
    "undefined_term": "Undefined term",
}

_PARAGRAPH_ID_RE = re.compile(r"\bP\s*(\d+)\b", re.IGNORECASE)
# A placeholder with nothing in it: "[*]", "[ ]", "[●]", "$______". What belongs there is a fact or a figure only the
# parties have, so no redraft can supply it and it is not raised; "[45 day cure period]" holds a value and is.
_EMPTY_PLACEHOLDER_RE = re.compile(r"\[[\s*●•_.…-]*\]|_{3,}")
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def paragraph_id(position: int) -> str:
    """The id a paragraph carries in the prompt: position 11 -> "P12"."""

    return f"P{position + 1}"


def position_of(reference: str) -> Optional[int]:
    """The paragraph position a model-written id names ("P12", "[P12]"), or None."""

    match = _PARAGRAPH_ID_RE.search(reference or "")
    return int(match.group(1)) - 1 if match else None


def _fold(text: str) -> str:
    return " ".join((text or "").translate(_FOLD).split()).casefold()


@dataclass(frozen=True)
class ClauseView:
    """One clause as the assessment sees it: its paragraphs, its checklist, and where a clause-wide finding goes."""

    clause_id: str
    title: str
    type_label: str
    positions: Tuple[int, ...]
    assessable: FrozenSet[int]
    anchor: int
    criteria: Tuple[Criterion, ...]
    tier: str
    # False when the clause's type was inferred from its wording or from similar clauses rather than named by its own
    # heading. The model is then asked whether the clause really is of that type, and is given the general checklist
    # as well: the type's checklist counts only if every answer set says the type fits (see settled).
    type_confirmed: bool = True
    # The legal team's fallback position for this type of clause, when it has given one.
    fallback: str = ""

    @property
    def asked(self) -> Tuple[Criterion, ...]:
        """The criteria put to the model: those that can raise a finding."""

        return tuple(criterion for criterion in self.criteria if is_reportable(criterion))

    @property
    def needs_confirmation(self) -> bool:
        return not self.type_confirmed and self.criteria is not GENERIC_CRITERIA

    def settled(self, type_fits: bool) -> "ClauseView":
        """The clause with the checklist that counts: its type's own, or the general one when the type was not confirmed."""

        if not self.needs_confirmation or type_fits:
            return self
        return replace(self, criteria=GENERIC_CRITERIA, tier=GENERIC_TIER, type_label=GENERIC_TYPE_LABEL, type_confirmed=True, fallback="")


@dataclass(frozen=True)
class Point:
    """One thing a paragraph's redraft must resolve."""

    key: str  # the criterion id, or "defect:<type>"
    position: int
    title: str
    finding: str
    risk_level: RiskLevel
    why: str = ""
    # The taxonomy's own test for the criterion being met: what the redraft has to achieve.
    resolved_when: str = ""
    evidence: str = ""
    order: int = 0  # checklist order; keeps sorting stable


def is_reportable(criterion: Criterion) -> bool:
    return criterion.weight in REPORTABLE_WEIGHTS or criterion.gate is not None


def counts_when_partly_met(criterion: Criterion) -> bool:
    """A criterion met in part is a finding only when it is Core or a gate, and the taxonomy states a Partly met test for it."""

    return bool(criterion.partly_met_when) and (criterion.weight == CORE or criterion.gate is not None)


# --- prompt rendering -----------------------------------------------------------


def render_checklist_item(criterion: Criterion) -> str:
    """One checklist item as the model reads it: id, title, condition, question and tests."""

    head = f"- {criterion.criterion_id} | {criterion.criterion.strip().rstrip('.')}" + (" | [Balance]" if criterion.issue_category == BALANCE else "")
    lines = [head]
    if criterion.conditional:
        condition = criterion.applies_when.strip().rstrip(".")
        lines.append(f"  Applies only where: {condition[:1].lower()}{condition[1:]}")
    lines.append(f"  Question: {criterion.question.strip()}")
    lines.append(f"  Met: {criterion.met_when.strip()}")
    if criterion.partly_met_when:
        lines.append(f"  Partly met: {criterion.partly_met_when.strip()}")
    lines.append(f"  Not met: {criterion.not_met_when.strip()}")
    return "\n".join(lines)


def render_clause(clause: ClauseView, text_of: Mapping[int, str], heading_positions: FrozenSet[int] = frozenset()) -> str:
    """One clause for the assessment prompt: its paragraphs, each led by its id, then its checklist."""

    lines = [f'=== {clause.clause_id} — "{clause.title}" ===' if clause.title else f"=== {clause.clause_id} ==="]
    for position in clause.positions:
        text = text_of[position].strip()
        if position in clause.assessable:
            lines.append(f"[{paragraph_id(position)}] {text}")
        else:
            lines.append(f"[{'heading' if position in heading_positions else 'context'}] {text}")
    lines.append("")
    if clause.needs_confirmation:
        lines.append(f"Checklist for {clause.clause_id} ({clause.type_label}) — it counts only if this clause is, in substance, a {clause.type_label} clause; say whether it is in clause_type_fits:")
        lines.extend(render_checklist_item(criterion) for criterion in clause.asked)
        lines.append("")
        lines.append(f"General checklist for {clause.clause_id} — answer it as well; it is used when the clause is not a {clause.type_label} clause:")
        lines.extend(render_checklist_item(criterion) for criterion in GENERIC_CRITERIA if is_reportable(criterion))
    else:
        lines.append(f"Checklist for {clause.clause_id} ({clause.type_label}):")
        lines.extend(render_checklist_item(criterion) for criterion in clause.asked)
    return "\n".join(lines)


def render_points(points: Sequence[Point]) -> str:
    """The points to resolve, numbered, as the drafting prompt lists them."""

    blocks: List[str] = []
    for number, point in enumerate(points, start=1):
        lines = [f"{number}. {point.title}: {point.finding}"]
        if point.resolved_when:
            lines.append(f"   Resolved when: {point.resolved_when}")
        if point.why:
            lines.append(f"   Why it matters: {point.why}")
        if point.evidence:
            lines.append(f'   Text concerned: "{point.evidence}"')
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


# --- votes to findings ----------------------------------------------------------


def answers_by_clause(assessment: BatchAssessment) -> Dict[str, Dict[str, CriterionAnswer]]:
    """One answer set, indexed clause id -> criterion id -> answer. A repeated id keeps its first answer."""

    indexed: Dict[str, Dict[str, CriterionAnswer]] = {}
    for clause in assessment.clauses:
        answers = indexed.setdefault(clause.clause_id.strip().upper(), {})
        for answer in clause.answers:
            answers.setdefault(answer.criterion_id.strip().upper(), answer)
    return indexed


def type_fits_by_clause(assessment: BatchAssessment) -> Dict[str, bool]:
    """One answer set's view of whether each clause is of the type its checklist names, by clause id."""

    fits: Dict[str, bool] = {}
    for clause in assessment.clauses:
        fits.setdefault(clause.clause_id.strip().upper(), clause.clause_type_fits)
    return fits


def _paragraphs_pointed_to(answer: CriterionAnswer, clause: ClauseView, text_of: Mapping[int, str]) -> set:
    """The paragraphs of the clause one failing answer puts the shortfall in.

    The words the answer quotes say where the problem is more reliably than
    the ids it lists: when the quotation is found in exactly one paragraph of
    the clause, that paragraph is the one, whatever id was written next to it.
    Otherwise the listed ids stand.
    """

    quoted = _fold(answer.evidence).strip('."')
    if quoted and quoted != "no provision":
        holding = [position for position in sorted(clause.assessable) if quoted in _fold(text_of.get(position, ""))]
        if len(holding) == 1:
            return set(holding)
    return {position for position in (position_of(reference) for reference in answer.paragraphs) if position is not None and position in clause.assessable}


def _agreed_position(supporting: Sequence[CriterionAnswer], clause: ClauseView, majority: int, text_of: Mapping[int, str]) -> int:
    """The paragraph a finding is anchored on.

    The earliest paragraph that a majority of the failing answers point to;
    when no paragraph has a majority — the answers disagree, or the clause
    as a whole simply lacks the term — the clause's primary operative
    paragraph. A conflict between two paragraphs therefore always lands on
    the first of them, whichever one an answer happened to lead with.
    """

    named: Counter = Counter()
    for answer in supporting:
        named.update(_paragraphs_pointed_to(answer, clause, text_of))
    agreed = [position for position, count in named.items() if count >= majority]
    return min(agreed) if agreed else clause.anchor


def _evidence(supporting: Sequence[CriterionAnswer]) -> str:
    for answer in supporting:
        if answer.evidence and _fold(answer.evidence).strip('."') != "no provision":
            return answer.evidence
    return ""


def tally_criteria(
    clause: ClauseView,
    votes: Sequence[Mapping[str, CriterionAnswer]],
    majority: int,
    favors_client: Callable[[int], bool] = lambda position: False,
    required: Optional[int] = None,
    text_of: Optional[Mapping[int, str]] = None,
) -> List[Point]:
    """The criteria a clause fails, each anchored on one paragraph.

    text_of gives the text of the clause's paragraphs by position, so a
    finding lands on the paragraph that holds the words it quotes.

    votes holds each answer set's answers for this clause, by criterion id.
    A criterion is failed when at least `required` answer sets report a
    shortfall on it (not met, or met in part); the review passes the number
    of answer sets, so the answers must be unanimous. It counts as not met
    when a majority say not met, and otherwise as met in part. required
    defaults to majority. favors_client says whether the paragraph at a
    position binds only the counterparty: a Balance criterion cannot fail there.
    """

    required = majority if required is None else required

    points: List[Point] = []
    for order, criterion in enumerate(clause.criteria):
        if not is_reportable(criterion):
            continue
        answers = [vote[criterion.criterion_id] for vote in votes if criterion.criterion_id in vote]
        failed = [answer for answer in answers if answer.verdict == NOT_MET]
        partial = [answer for answer in answers if answer.verdict == PARTLY_MET]
        if len(failed) + len(partial) < required:
            continue
        if len(failed) >= majority:
            supporting, partly = failed, False
        elif counts_when_partly_met(criterion):
            supporting, partly = [*failed, *partial], True
        else:
            continue
        position = _agreed_position(supporting, clause, majority, text_of or {})
        if criterion.issue_category == BALANCE and (favors_client(position) or favors_client(clause.anchor)):
            logger.info("Balance criterion %s not raised for clause '%s': the clause runs in the client's favor.", criterion.criterion_id, clause.title)
            continue
        points.append(
            Point(
                key=criterion.criterion_id,
                position=position,
                title=criterion.criterion.strip().rstrip("."),
                finding=(criterion.partly_met_when if partly else criterion.not_met_when).strip(),
                risk_level=criterion_risk_level(clause.tier, criterion.gate),
                why=criterion.why_it_matters.strip(),
                resolved_when=criterion.met_when.strip(),
                evidence=_evidence(supporting),
                order=order,
            )
        )
    return points


def tally_defects(votes: Sequence[BatchAssessment], text_of: Mapping[int, str], assessable: FrozenSet[int], required: int) -> List[Point]:
    """Drafting defects at least `required` answer sets report on the same paragraph, each grounded in that paragraph's own words.

    A defect whose quoted evidence is not in the paragraph is discarded from
    its answer set before the count, so an invented defect never gathers votes.
    """

    seen: Dict[Tuple[int, str], List[Tuple[str, str]]] = {}
    for vote in votes:
        counted: set = set()
        for defect in vote.drafting_defects:
            position = position_of(defect.paragraph)
            key = (position, defect.defect_type)
            if position is None or position not in assessable or key in counted:
                continue
            if not defect.evidence or _fold(defect.evidence) not in _fold(text_of.get(position, "")):
                continue
            if defect.defect_type == "blank_or_placeholder" and _EMPTY_PLACEHOLDER_RE.search(defect.evidence):
                continue
            counted.add(key)
            seen.setdefault((position, defect.defect_type), []).append((defect.evidence, defect.note))

    points: List[Point] = []
    for (position, defect_type), reports in sorted(seen.items()):
        if len(reports) < required:
            continue
        evidence, note = reports[0]
        points.append(
            Point(
                key=f"defect:{defect_type}",
                position=position,
                title=DEFECT_TITLES.get(defect_type, "Drafting defect"),
                finding=note or "The quoted wording is defective.",
                risk_level=defect_risk_level(defect_type),
                evidence=evidence,
                order=1000,
            )
        )
    return points


def undefined_term_points(terms: Sequence[UndefinedTerm], position_of_id: Mapping[str, int]) -> List[Point]:
    """Undefined terms found in code (defined_terms), as points: these are raised on every run, whatever the model noticed."""

    points: List[Point] = []
    for item in terms:
        position = position_of_id.get(item.para_identifier)
        if position is None:
            continue
        points.append(
            Point(
                key="defect:undefined_term",
                position=position,
                title=DEFECT_TITLES["undefined_term"],
                finding=(
                    f'"{item.term}" is used as a defined term but the agreement never defines it; '
                    f'the agreement\'s defined term for this is "{item.defined_term}". Replace it with the defined term.'
                ),
                risk_level=defect_risk_level("undefined_term"),
                evidence=item.term,
                order=999,
            )
        )
    return points


def select_points(points: Sequence[Point], limit: int = MAX_POINTS_PER_PARAGRAPH) -> Dict[int, List[Point]]:
    """Group points by paragraph, one per key, the most serious first, capped at limit per paragraph."""

    grouped: Dict[int, List[Point]] = {}
    for point in points:
        bucket = grouped.setdefault(point.position, [])
        if all(existing.key != point.key for existing in bucket):
            bucket.append(point)
    return {position: sorted(bucket, key=lambda point: (-rank(point.risk_level), point.order, point.key))[:limit] for position, bucket in sorted(grouped.items())}
