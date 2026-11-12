"""Global document facts for the general review.

A clause's checklist asks whether the agreement provides for something, and
the answer may sit in another clause — a survival sentence inside
Term/Termination, an order-of-precedence rule inside Consulting Services.
This module scans every paragraph once and records where such provisions
live, so the assessment is told what exists elsewhere, and the drafting is
told the governing law.

It also restores blank fields: a jurisdiction held in a Word content control
can reach the backend as "the State of ," with the value gone. When the same
paragraph still names the jurisdiction ("California law"), that is the value,
and the model is shown the paragraph with the value back in place.

Everything here is string work on the paragraphs the frontend sends; no model
or network call.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Match, Optional, Pattern, Sequence, Tuple

from src.schemas.general_review import TextInformation
from src.services.general_review.defined_terms import UndefinedTerm, find_undefined_terms
from src.services.general_review.segmentation import ClauseStructure, build_clause_structure

US_STATES = (
    "Alabama",
    "Alaska",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "Florida",
    "Georgia",
    "Hawaii",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
    "District of Columbia",
)

# Longest first, so "West Virginia" is never read as "Virginia".
_STATE_NAMES = "|".join(re.escape(state) for state in sorted(US_STATES, key=lambda state: (-len(state), state)))
_STATE_RE = re.compile(rf"\b({_STATE_NAMES})\b")

# "Sections 2, 4, 6 and 19 will survive the expiration or other termination of this Agreement."
_SURVIVAL_SECTIONS_RE = re.compile(
    r"\b(?:Sections?|Clauses?|Articles?|Paragraphs?)\s+((?:\d+(?:\.\d+)*[\s,;&]*(?:and|through|to|-|–)?[\s,;&]*)+)(?:shall|will|are\s+to|is\s+to)?\s*survive\b",
    re.IGNORECASE,
)
_SECTION_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")
_SURVIVAL_TITLE_RE = re.compile(r"^\s*survival\b", re.IGNORECASE)

_COMPLIANCE_RE = re.compile(r"\bcompl(?:y|ies|iance)\s+with\s+(?:all\s+applicable|applicable|all)\s+(?:\w+\s+){0,3}?(?:laws?|statutes|regulations)\b", re.IGNORECASE)

_RETURN_ADVANCES_TITLE_RE = re.compile(r"\breturn\s+of\s+(?:advances?|deposits?|prepayments?|prepaid|unused)\b", re.IGNORECASE)
_RETURN_ADVANCES_RE = re.compile(
    r"\b(?:return|refund)\w*\b[^.]{0,120}\b(?:unused\s+(?:payments|fees|funds|monies|amounts)|monies\s+advanced|advance\s+payments?|prepaid\s+(?:fees|amounts))\b", re.IGNORECASE
)
_RETURN_MATERIALS_TITLE_RE = re.compile(r"\breturn\s+of\s+(?:materials?|property|documents?|confidential)\b", re.IGNORECASE)
_RETURN_MATERIALS_RE = re.compile(r"\breturn\b[^.]{0,60}\b(?:materials|documents|property|Confidential\s+Information)\b", re.IGNORECASE)

_GOVERNING_LAW_RE = re.compile(r"\bgoverned\s+by\b[^.]{0,120}\blaws?\s+of\b", re.IGNORECASE)
_GOVERNING_STATE_RE = re.compile(rf"\blaws?\s+of\s+(?:the\s+)?(?:(?:State|Commonwealth)\s+of\s+)?({_STATE_NAMES})\b")

# A jurisdiction field with its value gone: "the State of ," / "courts of ."
_BLANK_FIELD_RE = re.compile(r"\b(State|Commonwealth|Province|laws|courts?)\s+of\s*(?=[,.;:)]|$)", re.IGNORECASE)
# Placeholders a rewrite may leave where the value belongs.
_PLACEHOLDER_AFTER_OF_RE = re.compile(r"\b((?:State|Commonwealth|Province|laws|courts?)\s+of)\s*(?:\[[^\]]{0,40}\]|_{2,}|\s(?=[,.;:)])|(?=[,.;:)])|$)", re.IGNORECASE)

# "In the event of a conflict between ... this Agreement ... a Statement of Work, the terms of this Agreement shall prevail."
_PRECEDENCE_RE = re.compile(
    r"\b(?:in\s+the\s+event\s+of\s+(?:a|any)\s+conflict|in\s+case\s+of\s+(?:a|any)\s+conflict|if\s+there\s+is\s+(?:a|any)\s+conflict|conflicts?\s+between)\b"
    r"[^.]{0,220}\b(?:shall|will|do(?:es)?)\s+(?:prevail|govern|control|take\s+precedence)\b",
    re.IGNORECASE,
)
_ORDER_DOCUMENT_RE = re.compile(r"\b(?:statements?\s+of\s+work|sows?|order\s+forms?|purchase\s+orders?)\b", re.IGNORECASE)

# "Exhibit A", "Schedule 2", "Annex II": an attachment the agreement refers to by its label.
_ATTACHMENT_RE = re.compile(r"\b(Exhibit|EXHIBIT|Schedule|SCHEDULE|Annex|ANNEX|Appendix|APPENDIX|Attachment|ATTACHMENT)\s+([A-Z]|\d{1,2}|[IVX]{2,4})\b")
# An attachment's own heading is a short paragraph that opens with its label.
_ATTACHMENT_HEADING_MAX_WORDS = 12
_ATTACHMENT_MENTION_MAX_CHARS = 220
_SENTENCE_END_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z“\"(])")


def find_attachments_not_supplied(paragraphs: Sequence[TextInformation]) -> Tuple[str, ...]:
    """The attachments the agreement refers to whose own text is not among the paragraphs, in order of first mention.

    The add-in sends the body of the agreement; its exhibits and schedules
    often are separate documents. A referenced attachment is taken to be
    supplied only when a paragraph is its heading ("EXHIBIT A", "Schedule 2 —
    Fees").
    """

    return tuple(name for name, _, _ in _attachments_and_mentions(paragraphs))


# A term the sentence defines: “Maintenance Policy”, "Security Standards".
_QUOTED_TERM_RE = re.compile(r"[“\"]([A-Z][^”\"]{1,50})[”\"]")


def _attachments_and_mentions(paragraphs: Sequence[TextInformation]) -> List[Tuple[str, str, Tuple[str, ...]]]:
    """Each attachment not supplied, with the sentence in which the agreement first refers to it and the terms that sentence defines.

    '“Maintenance Policy” means CoreWeave’s maintenance policy, in the form
    attached as Exhibit D' makes "Maintenance Policy" the agreement's name
    for Exhibit D.
    """

    def label(match: Match[str]) -> str:
        return f"{match.group(1).capitalize()} {match.group(2).upper()}"

    supplied = set()
    mentioned: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
    for para in paragraphs:
        text = para.text.strip()
        opening = _ATTACHMENT_RE.match(text)
        if opening and len(text.split()) <= _ATTACHMENT_HEADING_MAX_WORDS:
            supplied.add(label(opening))
            continue
        for sentence in _SENTENCE_END_RE.split(text):
            for match in _ATTACHMENT_RE.finditer(sentence):
                mentioned.setdefault(label(match), (" ".join(sentence.split())[:_ATTACHMENT_MENTION_MAX_CHARS], tuple(_QUOTED_TERM_RE.findall(sentence))))
    return [(name, mention, terms) for name, (mention, terms) in mentioned.items() if name not in supplied]


@dataclass(frozen=True)
class ProvisionLocation:
    """Where a cross-document provision lives: the paragraph and the clause it belongs to."""

    para_identifier: str
    clause: str


@dataclass(frozen=True)
class BlankField:
    """A field that arrived blank, and the value the paragraph's own wording gives for it."""

    para_identifier: str
    clause: str
    blanks: Tuple[str, ...]
    inferred_value: str
    evidence: str


@dataclass(frozen=True)
class DocumentFacts:
    """Cross-document provisions found by scanning the whole agreement."""

    survival: Optional[ProvisionLocation] = None
    survival_sections: Tuple[str, ...] = ()
    compliance_with_laws: Tuple[ProvisionLocation, ...] = ()
    return_of_advances: Tuple[ProvisionLocation, ...] = ()
    return_of_materials: Tuple[ProvisionLocation, ...] = ()
    governing_law: Optional[ProvisionLocation] = None
    governing_state: Optional[str] = None
    blank_fields: Tuple[BlankField, ...] = ()
    order_of_precedence: Tuple[ProvisionLocation, ...] = ()
    precedence_covers_orders: bool = False
    undefined_terms: Tuple[UndefinedTerm, ...] = ()
    # Exhibits and schedules the agreement refers to whose text was not sent for review.
    attachments_not_supplied: Tuple[str, ...] = ()
    # For each of them, in the same order, the sentence in which the agreement first refers to it: what the attachment is.
    attachment_mentions: Tuple[str, ...] = ()
    # And the agreement's own names for it ("Maintenance Policy" for Exhibit D), when that sentence defines any.
    attachment_terms: Tuple[Tuple[str, ...], ...] = ()

    def _attachment_names(self) -> List[Tuple[str, Tuple[str, ...], str]]:
        """(label, the agreement's names for it, how to refer to it) for each attachment not supplied."""

        named = []
        for index, label in enumerate(self.attachments_not_supplied):
            terms = self.attachment_terms[index] if index < len(self.attachment_terms) else ()
            named.append((label, terms, f"the {terms[0]} ({label})" if terms else label))
        return named

    def attachments_relied_on(self, text: str) -> List[str]:
        """The attachments not supplied that a piece of text refers to, by label or by the agreement's name for them."""

        return [reference for label, terms, reference in self._attachment_names() if any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) for name in (label, *terms))]

    def attachments_reminder(self) -> str:
        """One line, placed beside the checklists, naming the documents the assessment cannot see; empty when there are none."""

        if not self.attachments_not_supplied:
            return ""
        return (
            "Documents that are part of the agreement but whose text you have not been given: "
            + "; ".join(reference for _, _, reference in self._attachment_names())
            + ". A checklist item that turns on what one of these documents contains is not_applicable (rule 4).\n\n"
        )

    def attachments_note(self) -> str:
        """What both prompts are told about attachments whose text was not sent; empty when there are none."""

        if not self.attachments_not_supplied:
            return ""
        note = (
            f"Attachments: the agreement refers to {_join(self.attachments_not_supplied)}. Their text was not provided for this review. "
            "They are part of the agreement and are taken to say what the agreement says they contain; they are not missing."
        )
        described = [f'  {name}: "{mention}"' for name, mention in zip(self.attachments_not_supplied, self.attachment_mentions) if mention]
        return note + (" The agreement refers to them in these words:\n" + "\n".join(described) if described else "")

    def blank_field_for(self, para_identifier: str) -> Optional[BlankField]:
        return next((blank for blank in self.blank_fields if blank.para_identifier == para_identifier), None)

    def render(self) -> str:
        """The prompt's notes on provisions located in the agreement; empty when nothing was found."""

        lines: List[str] = []
        if self.survival is not None:
            covered = f", covering Sections {_join(self.survival_sections)}" if self.survival_sections else ""
            lines.append(f'- Survival: the agreement has a survival provision in its "{self.survival.clause}" clause{covered}.')
        if self.compliance_with_laws:
            lines.append(f"- Compliance with laws: a general obligation to comply with applicable laws is in {_clauses(self.compliance_with_laws)}.")
        if self.return_of_advances:
            lines.append(f"- Return of advances: unused payments and advances are returned on termination under {_clauses(self.return_of_advances)}.")
        if self.return_of_materials:
            lines.append(f"- Return of materials: materials, documents and Confidential Information are returned on termination under {_clauses(self.return_of_materials)}.")
        if self.order_of_precedence:
            documents = "this Agreement and a Statement of Work" if self.precedence_covers_orders else "the agreement's documents"
            lines.append(f"- Order of precedence: {_clauses(self.order_of_precedence)} says which terms prevail when {documents} conflict.")
        if self.governing_law is not None:
            state = f" The governing state is {self.governing_state}." if self.governing_state else ""
            lines.append(f'- Governing law and venue: set out in the "{self.governing_law.clause}" clause.{state}')
        if self.attachments_not_supplied:
            lines.append(f"- {self.attachments_note()}")
        if not lines:
            return ""
        return "Provisions located by scanning the whole agreement:\n\n" + "\n".join(lines) + "\n\n"

    @property
    def digest(self) -> str:
        """Content hash of the facts: two documents with the same facts share cached paragraph outcomes."""

        undefined = "|".join(f"{item.para_identifier}:{item.term}:{item.defined_term}" for item in self.undefined_terms)
        return hashlib.sha256(f"{self.render()}#{undefined}".encode("utf-8")).hexdigest()


def _join(items: Sequence[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _clauses(locations: Sequence[ProvisionLocation]) -> str:
    """'the "A" clause' / 'the "A" and "B" clauses', each clause named once, in document order."""

    names: List[str] = []
    for location in locations:
        if location.clause not in names:
            names.append(location.clause)
    quoted = [f'"{name}"' for name in names]
    return f"the {_join(quoted)} clause{'s' if len(quoted) > 1 else ''}"


def infer_blank_field(para: TextInformation, clause: str) -> Optional[BlankField]:
    """Find blank jurisdiction fields in a paragraph and the value its own wording supplies.

    The value is inferred only when the paragraph names exactly one
    jurisdiction; with none, or with several, the blank is left for the
    review to raise as it stands.
    """

    blanks = tuple(" ".join(match.group(0).split()) + " " + para.text[match.end() : match.end() + 1] for match in _BLANK_FIELD_RE.finditer(para.text))
    if not blanks:
        return None
    mentions = _STATE_RE.findall(para.text)
    if len(set(mentions)) != 1:
        return None
    state = mentions[0]
    match = re.search(rf"\b{re.escape(state)}(?:\s+law)?\b", para.text)
    evidence = f'"{match.group(0)}"' if match else f'"{state}"'
    unique: List[str] = []
    for blank in blanks:
        if blank not in unique:
            unique.append(blank)
    return BlankField(para.paraindetifier, clause, tuple(unique), state, evidence)


def restore_blank_fields(text: str, blank: BlankField) -> str:
    """The paragraph as the model is shown it: each blank field filled with the value the paragraph itself supplies.

    "the laws of the State of ," becomes "the laws of the State of
    California,". The blank is not a drafting defect — the value sits in a
    Word content control the add-in did not read — so the model reviews the
    clause as the document actually reads.
    """

    return _PLACEHOLDER_AFTER_OF_RE.sub(lambda match: f"{match.group(1)} {blank.inferred_value}", text)


def apply_blank_field_guard(suggested_fix: str, original: str, blank: BlankField) -> str:
    """Make a rewrite of a paragraph with a blank field use the inferred value, and only that value.

    Fills blanks and placeholders the rewrite left where the value belongs,
    and replaces any jurisdiction the rewrite introduced that the paragraph
    never named — the paragraph says California, so the fix may not say
    Delaware.
    """

    value = blank.inferred_value
    fixed = _PLACEHOLDER_AFTER_OF_RE.sub(lambda match: f"{match.group(1)} {value}", suggested_fix)
    known = set(_STATE_RE.findall(original))

    def keep_or_replace(match: Match[str]) -> str:
        return match.group(0) if match.group(0) in known else value

    return _STATE_RE.sub(keep_or_replace, fixed)


def _is_heading_only(text: str, clause: str) -> bool:
    return bool(clause) and text.strip().rstrip(".:").strip() == clause


def _find_survival(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> Tuple[Optional[ProvisionLocation], Tuple[str, ...]]:
    for para in paragraphs:
        clause = titles.get(para.paraindetifier, "")
        match = _SURVIVAL_SECTIONS_RE.search(para.text)
        if match:
            return ProvisionLocation(para.paraindetifier, clause), tuple(_SECTION_NUMBER_RE.findall(match.group(1)))
        if _SURVIVAL_TITLE_RE.match(clause) and not _is_heading_only(para.text, clause):
            return ProvisionLocation(para.paraindetifier, clause), ()
    return None, ()


def _find_all(paragraphs: Sequence[TextInformation], titles: Dict[str, str], body: Pattern[str], title: Optional[Pattern[str]] = None) -> Tuple[ProvisionLocation, ...]:
    found: List[ProvisionLocation] = []
    for para in paragraphs:
        clause = titles.get(para.paraindetifier, "")
        if _is_heading_only(para.text, clause):
            continue
        if body.search(para.text) or (title is not None and title.search(clause)):
            found.append(ProvisionLocation(para.paraindetifier, clause))
    return tuple(found)


def build_document_facts(paragraphs: Sequence[TextInformation], titles: Dict[str, str], structure: Optional[ClauseStructure] = None) -> DocumentFacts:
    """Scan every paragraph for the cross-document provisions, blank fields, and undefined terms."""

    structure = structure if structure is not None else build_clause_structure(paragraphs, titles)

    survival, sections = _find_survival(paragraphs, titles)
    governing = next(iter(_find_all(paragraphs, titles, _GOVERNING_LAW_RE)), None)
    precedence = _find_all(paragraphs, titles, _PRECEDENCE_RE)
    blank_fields = tuple(blank for blank in (infer_blank_field(para, titles.get(para.paraindetifier, "")) for para in paragraphs) if blank is not None)

    attachments = _attachments_and_mentions(paragraphs)

    governing_state: Optional[str] = None
    if governing is not None:
        text = next(para.text for para in paragraphs if para.paraindetifier == governing.para_identifier)
        stated = _GOVERNING_STATE_RE.search(text)
        blank = next((item for item in blank_fields if item.para_identifier == governing.para_identifier), None)
        governing_state = stated.group(1) if stated else (blank.inferred_value if blank else None)

    return DocumentFacts(
        survival=survival,
        survival_sections=sections,
        compliance_with_laws=_find_all(paragraphs, titles, _COMPLIANCE_RE),
        return_of_advances=_find_all(paragraphs, titles, _RETURN_ADVANCES_RE, _RETURN_ADVANCES_TITLE_RE),
        return_of_materials=_find_all(paragraphs, titles, _RETURN_MATERIALS_RE, _RETURN_MATERIALS_TITLE_RE),
        governing_law=governing,
        governing_state=governing_state,
        blank_fields=blank_fields,
        order_of_precedence=precedence,
        undefined_terms=find_undefined_terms(paragraphs, titles, structure, (*US_STATES, "United States")),
        precedence_covers_orders=any(_ORDER_DOCUMENT_RE.search(para.text) for para in paragraphs if any(location.para_identifier == para.paraindetifier for location in precedence)),
        attachments_not_supplied=tuple(name for name, _, _ in attachments),
        attachment_mentions=tuple(mention for _, mention, _ in attachments),
        attachment_terms=tuple(terms for _, _, terms in attachments),
    )
