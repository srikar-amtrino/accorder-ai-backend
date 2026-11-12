"""Clause segmentation for the general review: titles, clause units, embedding queries, paragraph roles.

Everything here is pure string work on the paragraphs the frontend sends; no
model or network call. The title heuristics decide where one clause ends and
the next begins, which drives the clause units a checklist is answered
against, the batches, and the clause names on the suggestions.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Pattern, Sequence, Tuple

from src.schemas.general_review import TextInformation
from src.services.clause_mapping.resilient_classifier import HEADING_MATCH_CONFIDENCE, MISCELLANEOUS_TYPE, classify_layer1_heading

# Short paragraphs with few words are treated as headings and kept with their body.
_HEADING_MAX_CHARS = 80
_HEADING_MAX_WORDS = 8

# Numbered clause starts ("5. Confidentiality...", "Section 3 ...") count as cut points too.
_NUMBERED_START_RE = re.compile(r"^\s*(?:(?:section|article|clause)\s+)?\d+(?:\.\d+)*[.)]\s+\S", re.IGNORECASE)

# Clause-title extraction: a leading "5.2 " / "Section 3." style prefix (at most
# two leading digits, so street numbers stay put), and the short "Title.  Body..."
# lead-in many contracts open their clauses with.
_TITLE_NUMBER_PREFIX_RE = re.compile(r"^(?:(?:section|article|clause)\s+)?\d{1,2}(?:\.\d+)*[.)]?\s+", re.IGNORECASE)
_INLINE_TITLE_RE = re.compile(r"^([^.:]{1,70})[.:](?:\s|$)")
# The same prefix with its number captured. A sub-clause ("5.2 Invoices.") under a numbered, titled clause
# ("5. Fees and Payment.") belongs to that clause: its lead-in names a sub-topic, not a new clause.
_LEADING_NUMBER_RE = re.compile(r"^(?:(?:section|article|clause)\s+)?(\d{1,2}(?:\.\d+)*)[.)]?\s+", re.IGNORECASE)

# Lowercase connectors allowed inside an otherwise capitalized clause title.
TITLE_STOP_WORDS = {"a", "an", "and", "by", "for", "in", "of", "on", "or", "the", "to", "with"}

# Sentence-case lead-ins ("Overdue charges.", "Termination for cause.") count as
# titles only when short and free of words that mark a sentence ("Provider shall
# pay.", "Each Party bears its own costs.", "“Annex” means an annex.").
_SENTENCE_TITLE_MAX_WORDS = 6
_TITLE_VERB_WORDS = {
    "shall",
    "will",
    "may",
    "must",
    "should",
    "can",
    "cannot",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "has",
    "have",
    "had",
    "does",
    "do",
    "did",
    "not",
    "hereby",
    "means",
    "agrees",
    "agree",
    "includes",
    "include",
    "bears",
    "pays",
    "provides",
    "grants",
    "retains",
    "acknowledges",
    "represents",
    "warrants",
    "covenants",
    "undertakes",
    "desire",
    "desires",
    "intends",
    "applies",
    "apply",
    "governs",
    "survives",
    "survive",
    "terminates",
    "indemnifies",
    "indemnify",
}

# Trailing company suffixes mark a party-name line, not a clause heading.
_TITLE_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "co"}

# An opening sentence that merely defines a term ('"Intellectual Property" means
# all worldwide patents...'). Skipped when building a clause's embedding query so
# the clause maps by what it does, not by the term it happens to define first.
_DEFINITION_SENTENCE_RE = re.compile(
    r"^\s*(?:[\(\[a-z0-9\)\]]+\s*)?[\"“]?[A-Z][A-Za-z0-9\s\-\/]{1,50}[\"”]?\s+(?:means|shall\s+mean|has\s+the\s+meaning|refers\s+to)\b",
    re.IGNORECASE,
)
_DEFINITIONS_HEADING_RE = re.compile(r"\b(definitions?|defined\s+terms)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+")

# Words of a clause embedded for clause-type mapping. The embedding service's
# cost grows with every word and the collection holds sentence-level chunks, so
# the heading plus the first operative sentences match as well as the full text.
EMBED_QUERY_WORDS = 65


def is_heading(text: str) -> bool:
    """Heuristic for standalone heading paragraphs (e.g. 'Non-Compete.')."""

    stripped = text.strip()
    return 0 < len(stripped) <= _HEADING_MAX_CHARS and len(stripped.split()) <= _HEADING_MAX_WORDS


def looks_like_title(segment: str) -> bool:
    """True when a segment reads as a clause title: short capitalized words, or a short sentence-case phrase.

    Title Case ("Limitation of Liability") always qualifies. Sentence case
    ("Overdue charges", "Termination for cause") qualifies when nothing in it
    reads as a verb, so a short opening sentence ("Provider shall pay") does not.

    Address and party-name lines ("San Ramon, CA 94583", "XYZInc Inc.",
    "To Amtrino:") also look short and capitalized, so digits, internal
    punctuation, a leading connector, and company suffixes all disqualify.
    """

    if "." in segment or ":" in segment:
        return False
    if re.search(r",\s*[A-Z]{2}$", segment):
        return False
    words = [word.strip(",;()'\"‘’“”") for word in segment.split()]
    words = [word for word in words if any(char.isalnum() for char in word)]
    if not words or len(words) > _HEADING_MAX_WORDS:
        return False
    if words[0].lower() in TITLE_STOP_WORDS or not words[0][0].isupper():
        return False
    if words[-1].lower() in _TITLE_COMPANY_SUFFIXES:
        return False
    if any(any(char.isdigit() for char in word) for word in words):
        return False
    if all(word[0].isupper() or word.lower() in TITLE_STOP_WORDS for word in words):
        return True
    return len(words) <= _SENTENCE_TITLE_MAX_WORDS and not any(word.lower() in _TITLE_VERB_WORDS for word in words)


def clause_title(text: str) -> Optional[str]:
    """Extract this paragraph's own clause title, or None when it has none.

    Handles both a standalone heading paragraph ('Non-Compete.') and an inline
    lead-in ('Return of Advances.  In the event...', '7.3 Security Measures. ...').
    """

    # Signature-block lines ("Name: ______") are never clause titles.
    if "___" in text:
        return None
    stripped = _TITLE_NUMBER_PREFIX_RE.sub("", text.strip(), count=1)
    if is_heading(stripped):
        candidate = stripped.rstrip(".:").strip()
        if looks_like_title(candidate):
            return candidate
    # A short paragraph may still open with a lead-in ("Liability cap. See Clause 13.").
    match = _INLINE_TITLE_RE.match(stripped)
    if match and looks_like_title(match.group(1).strip()):
        return match.group(1).strip()
    return None


def build_clause_titles(paragraphs: Sequence[TextInformation]) -> Dict[str, str]:
    """Map each paragraph id to its clause title, extracted from the document itself.

    A paragraph's title is its own heading/lead-in when it has one, otherwise
    the most recent titled paragraph above it. A numbered sub-clause keeps the
    title of the numbered clause it sits under, so "5.1 Fees." to "5.4 Disputed
    Amounts." are all part of "5. Fees and Payment". Paragraphs with no determinable
    title (e.g. a lone selected body paragraph whose heading was not sent) are
    omitted so the model's own label stays as the fallback.
    """

    titles: Dict[str, str] = {}
    current = ""
    # Top-level number of the clause `current` names ("5" for "5. Fees and Payment"), when it carries one.
    current_root: Optional[str] = None
    previous = ""
    for para in paragraphs:
        if not para.text.strip():
            continue
        number = clause_number(para.text)
        # A paragraph that carries on the sentence the one above broke off has no title of its own, whatever its opening words look like.
        own = None if runs_on_into(previous, para.text) else clause_title(para.text)
        is_subclause = number is not None and len(number) > 1 and current_root == number[0] and bool(current)
        if own and not is_subclause:
            current = own
            current_root = number[0] if number is not None and len(number) == 1 else None
        if current:
            titles[para.paraindetifier] = current
        previous = para.text
    return titles


# Words a sentence cannot end on: a paragraph that stops on one was cut where the document broke the line.
_NEVER_LAST_WORDS = frozenset(
    "a an the of in on at to by for from with under and or nor but that which who whose if unless until as than is are was were be been has have had "
    "shall will may must can not its their his her any all each such this these those".split()
)


def breaks_off_mid_sentence(text: str) -> bool:
    """True when a paragraph of running text stops on a word no sentence ends with ("... EXCEPT WHERE COREWEAVE IS IN").

    A paragraph break can fall inside a sentence on the way from Word. The
    words that follow belong to the same sentence and the same clause.
    """

    words = text.split()
    if len(words) <= _HEADING_MAX_WORDS:
        return False
    # "...; or" and "...; and" close an item of a list: the sentence is whole, and the list goes on.
    if words[-2][-1] in ";,:":
        return False
    return words[-1].isalpha() and words[-1].casefold() in _NEVER_LAST_WORDS


# How a paragraph opens when it carries on a sentence: with a lower-case word.
_CONTINUATION_START_RE = re.compile(r"^\s*[a-z]{2,}\b")
_LIST_ITEM_START_RE = re.compile(r"^\s*\(?(?:[a-z]|[ivx]{1,4}|\d{1,2})[.)]\s")
# "twelve" / "(12) months' notice": a number written out at the end of one paragraph and in figures at the start of the next.
_FIGURE_START_RE = re.compile(r"^\s*\(\d+\)\s")
_NUMBER_WORDS = frozenset(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty forty-five fifty sixty ninety hundred".split()
)


def runs_on_into(text: str, following: str) -> bool:
    """True when the sentence a paragraph ends with carries on in the paragraph after it.

    Either the paragraph stops on a word no sentence ends with, or it stops
    without closing punctuation and the next paragraph opens the way the
    middle of a sentence does ("... with twelve" / "(12) months' notice ...").
    A list item ("(a) ...", "(ii) ...") opens a new item, not the same sentence.
    """

    if not text.strip() or not following.strip():
        return False
    if breaks_off_mid_sentence(text):
        return True
    words = text.split()
    if len(words) <= _HEADING_MAX_WORDS or not words[-1][-1].isalnum():
        return False
    if words[-1].casefold() in _NUMBER_WORDS and _FIGURE_START_RE.match(following):
        return True
    return bool(_CONTINUATION_START_RE.match(following)) and not _LIST_ITEM_START_RE.match(following)


def clause_number(text: str) -> Optional[Tuple[str, ...]]:
    """The components of a paragraph's leading clause number: "5.2 Invoices." -> ("5", "2"); None when it has none."""

    match = _LEADING_NUMBER_RE.match(text.strip())
    return tuple(match.group(1).split(".")) if match else None


def detect_clause_start(text: str) -> Tuple[bool, Optional[str]]:
    """Decide whether a paragraph begins a new clause, and name it when it can.

    A clause starts at a standalone heading ("Intellectual Property Rights."),
    an inline lead-in ("Damages. In no event will either Party..."), or a
    numbered paragraph ("5.2 The Provider shall...", which has no title of its
    own). These are the only safe places to cut a review batch, so a clause's
    paragraphs always stay together.
    """

    stripped = text.strip()
    if not stripped:
        return False, None
    title = clause_title(stripped)
    if title:
        return True, title
    if _NUMBERED_START_RE.match(stripped):
        return True, None
    return False, None


def build_smart_embedding_query(heading: str, paragraph_texts: Sequence[str], max_words: int = EMBED_QUERY_WORDS) -> str:
    """Build the text embedded to map a clause to its type: the heading plus its first operative words.

    Outside a Definitions section, opening sentences that only define a term
    ('"Intellectual Property" means...') are skipped, so an IP assignment
    clause maps as intellectual-property rather than as definitions. When a
    clause is nothing but definitions the full text is used.
    """

    heading = heading.strip().rstrip(".:")
    is_definitions = bool(_DEFINITIONS_HEADING_RE.search(heading)) if heading else False
    operative: List[str] = []
    for raw in paragraph_texts:
        text = raw.strip()
        if not text:
            continue
        if heading and text.rstrip(".:").strip() == heading:
            continue
        if heading and text.lower().startswith(heading.lower()):
            remainder = text[len(heading) :].lstrip()
            if remainder[:1] in ".:":
                text = remainder[1:].strip()
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            if not is_definitions and _DEFINITION_SENTENCE_RE.match(sentence):
                continue
            operative.append(sentence)

    operative_text = " ".join(operative).strip() or " ".join(raw.strip() for raw in paragraph_texts).strip()
    words = " ".join(operative_text.split()[:max_words])
    return f"{heading}. {words}".strip() if heading else words


def build_clause_units(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> List[List[int]]:
    """Group paragraph positions into clauses, in order of each clause's first paragraph.

    Consecutive paragraphs under the same clause title form one group; a
    paragraph with no title stands alone. Groups are then gathered under
    section headings: a heading standing on its own that names a clause type
    ("LIMITATION OF LIABILITY", "Term/Termination.") opens a section, and the
    titled sub-clauses that follow it ("Liability cap. ...", "Excluded Claims.
    ...", "Time limit for claims. ...") are part of that one clause, up to the
    next standalone heading.

    How firmly the sub-clauses belong depends on how the heading is set. A
    heading in capitals over sentence-case lead-ins is plainly a level above
    them, so every sub-clause stays in its section whatever its own title
    says ("Notice." under FORCE MAJEURE is the force majeure notice, not a
    notices clause). Where the heading is typed like the lead-ins, the two
    may be siblings, so a sub-clause whose own title names a different clause
    type ("Indemnity. ..." after "Representations and Warranties.") is a
    clause of its own, and a document that sets every clause at one level is
    left as it is.

    A clause's positions are in document order but need not be consecutive:
    a clause of another type may sit in the middle of a section.
    """

    groups: List[List[int]] = []
    previous: Optional[str] = None
    for idx, para in enumerate(paragraphs):
        if not para.text.strip():
            continue
        title = titles.get(para.paraindetifier)
        if groups and title is not None and title == previous:
            groups[-1].append(idx)
        else:
            groups.append([idx])
        previous = title

    units: List[List[int]] = []
    # The open section: (its index in units, the clause type its heading names, whether the heading is in capitals).
    section: Optional[Tuple[int, str, bool]] = None
    for group in groups:
        first = paragraphs[group[0]]
        title = titles.get(first.paraindetifier, "")
        clause_type = clause_type_for_title(title)
        if is_standalone_heading(first.text, title):
            section = (len(units), clause_type, title.isupper()) if clause_type is not None and clause_type not in (RECITALS_TYPE, MISCELLANEOUS_TYPE) else None
            units.append(list(group))
        elif section is not None and title and (section[2] or clause_type in (None, section[1])):
            units[section[0]].extend(group)
        else:
            units.append(list(group))
    return units


# --- paragraph roles and the primary anchor -----------------------------------
#
# Every paragraph gets one role, decided in code from the document alone:
#
#   CONTEXT_ONLY              non-clause content: preamble, recitals, party and
#                             address lines, the signature block. Never assessed.
#   HEADING                   a clause heading standing alone. Never assessed.
#   PRIMARY_OPERATIVE_ANCHOR  the paragraph carrying the clause's main operative
#                             obligation. A finding that the clause as a whole
#                             lacks something is anchored here.
#   SECONDARY / DEFINITION    the clause's other paragraphs.
#   BOILERPLATE               a paragraph of a routine clause (headings,
#                             counterparts, severability, waiver, amendments,
#                             notices).

ROLE_CONTEXT_ONLY = "CONTEXT_ONLY"
ROLE_HEADING = "HEADING"
ROLE_PRIMARY = "PRIMARY_OPERATIVE_ANCHOR"
ROLE_SECONDARY = "SECONDARY"
ROLE_DEFINITION = "DEFINITION"
ROLE_BOILERPLATE = "BOILERPLATE"
REVIEWABLE_ROLES = (ROLE_PRIMARY, ROLE_SECONDARY, ROLE_DEFINITION, ROLE_BOILERPLATE)

# Why a paragraph is context only.
ZONE_PREAMBLE = "preamble"
ZONE_RECITALS = "recitals"
ZONE_PARTY_BLOCK = "party_block"
ZONE_SIGNATURE = "signature"

# The preamble and recitals end within the opening paragraphs or not at all.
_PREAMBLE_SCAN_PARAGRAPHS = 20
_RECITALS_END_RE = re.compile(r"\bNOW,?\s+THEREFORE\b|\bagrees?\s+as\s+follows\b", re.IGNORECASE)
_RECITALS_START_RE = re.compile(r"^\s*(?:RECITALS?|BACKGROUND|WITNESSETH|WHEREAS)\b", re.IGNORECASE)
_PARTY_INTRO_RE = re.compile(r"\bby\s+and\s+between\b|\b(?:entered\s+into|made|dated)\b[^.]{0,160}\bbetween\b", re.IGNORECASE)

_WITNESS_RE = re.compile(r"^\s*IN\s+WITNESS\s+WHEREOF\b", re.IGNORECASE)
_SIGNATURE_LINE_RE = re.compile(r"^\s*(?:By|Name|Title|Date|Signature|Signed|Its)\s*:|_{4,}", re.IGNORECASE)
_SIGNATURE_BLOCK_MAX_WORDS = 6

# Address and contact lines: "San Ramon, CA 94583", "2603 Camino Ramon suit 200", "Attn.: Accounts Payable", "To Amtrino:".
_ADDRESS_MAX_WORDS = 12
_COMPANY_LINE_MAX_WORDS = 6
_LEAD_IN_MAX_WORDS = 8
_STATE_ZIP_RE = re.compile(r",\s*[A-Z]{2}\.?(?:\s+\d{5}(?:-\d{4})?)?\s*\.?$")
_STREET_SUFFIX_RE = re.compile(r"^\d+[A-Za-z]?\s+.*\b(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Suite|Ste|Dr|Drive|Ln|Lane|Way|Floor|Plaza|Pkwy|Parkway|Hwy|Highway|Court|Ct|Place|Square)\b\.?")
# "2603 Camino Ramon suit 200": a street number and capitalised names. "100 hours of support are included" is a sentence, not a street.
_STREET_NUMBER_RE = re.compile(r"^\d{3,6}[A-Za-z]?\s+[A-Z]")
_STREET_MAX_WORDS = 6
_STREET_MAX_LOWERCASE_WORDS = 1
_CONTACT_LINE_RE = re.compile(r"^(?:attn|attention|c/o|e-?mail|fax|facsimile|tel|telephone|phone)\b\.?\s*:|^to\s+[^:.]{1,40}:\s*$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Routine boilerplate clauses, matched on the whole clause title: "Headings", "Execution of Counterparts",
# "Severability", "Modification, Waiver and Authorized Representative". A title that says more than that is a
# clause of substance and is reviewed in full: "Waiver of Jury Trial", "Amendments to the Agreement".
# A notices clause is boilerplate too, whatever its title, by its clause type.
_AMENDMENT_WORDS = r"(?:modifications?|amendments?|(?:no\s+)?waivers?|authorized\s+representatives?)"
_BOILERPLATE_TITLE_RE = re.compile(
    rf"^(?:headings|captions|section\s+headings|(?:execution\s+(?:of|in)\s+)?counterparts|severability|{_AMENDMENT_WORDS}(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s+or\s+){_AMENDMENT_WORDS})*)$",
    re.IGNORECASE,
)

# A paragraph that opens by defining a term or stating what the term covers.
_DEFINITION_PARAGRAPH_RE = re.compile(
    r"^\s*(?:\(?[a-z0-9]{1,3}\)\s*)?[\"“]?[A-Z][A-Za-z0-9\s\-/]{1,50}[\"”]?\s+"
    r"(?:means|shall\s+mean|includes?|shall\s+include|shall\s+not,?\s+(?:however,?\s+)?include|does\s+not\s+include|has\s+the\s+meaning|refers\s+to)\b"
)

NOTICES_TYPE = "notices"
# The clause type a "Recitals" / "Background" heading maps to.
RECITALS_TYPE = "purpose"

# The wording that marks a clause type's main operative paragraph, strongest first.
# The first paragraph matching the strongest pattern is the anchor; a clause
# type with no pattern here, or no match, anchors on its first operative paragraph.
PRIMARY_ANCHOR_PATTERNS: Dict[str, Tuple[Pattern[str], ...]] = {
    "intellectual-property": (
        re.compile(r"\bhereby\s+(?:irrevocably\s+)?(?:assigns?|transfers?|conveys?)\b", re.IGNORECASE),
        re.compile(r"\b(?:shall|will|agrees?\s+to)\s+assign\b", re.IGNORECASE),
        re.compile(r"\bsole\s+and\s+exclusive\s+property\b|\bshall\s+(?:own|be\s+owned)\b|\bworks?\s+made\s+for\s+hire\b", re.IGNORECASE),
    ),
    "fees-and-payment": (
        re.compile(r"\bpay\s+(?:all\s+|each\s+|undisputed\s+)?invoices?\s+within\b", re.IGNORECASE),
        re.compile(r"\b(?:shall|will)\s+pay\b|\bpayable\s+within\b|\bdue\s+(?:and\s+payable\s+)?within\b", re.IGNORECASE),
    ),
    "confidentiality": (
        re.compile(r"\bhold\b[^.]{0,60}\bconfiden", re.IGNORECASE),
        re.compile(r"\b(?:shall|will|agrees?)\s+not\s+(?:to\s+)?(?:disclose|use)\b", re.IGNORECASE),
    ),
}


@dataclass(frozen=True)
class ClauseStructure:
    """Role, clause, and anchor of every paragraph, by position in the document."""

    roles: Dict[int, str] = field(default_factory=dict)
    anchor_of: Dict[int, int] = field(default_factory=dict)
    unit_of: Dict[int, int] = field(default_factory=dict)
    clause_type_of: Dict[int, str] = field(default_factory=dict)
    zone_of: Dict[int, str] = field(default_factory=dict)

    def role(self, position: int) -> str:
        return self.roles.get(position, ROLE_PRIMARY)

    def reviewable(self, position: int) -> bool:
        return self.role(position) in REVIEWABLE_ROLES


def is_address_line(text: str) -> bool:
    """True for a line of an address or contact block: a street, a city/state line, a company name, an Attn/To line."""

    stripped = text.strip()
    words = stripped.split()
    if not words or len(words) > _ADDRESS_MAX_WORDS:
        return False
    if _STATE_ZIP_RE.search(stripped) or _STREET_SUFFIX_RE.match(stripped) or _CONTACT_LINE_RE.match(stripped) or _EMAIL_RE.search(stripped):
        return True
    if _STREET_NUMBER_RE.match(stripped):
        lowercase = sum(1 for word in words if word[0].islower())
        return len(words) <= _STREET_MAX_WORDS and lowercase <= _STREET_MAX_LOWERCASE_WORDS
    return len(words) <= _COMPANY_LINE_MAX_WORDS and words[-1].strip(".,").lower() in _TITLE_COMPANY_SUFFIXES and words[0][0].isupper()


def is_definition_paragraph(text: str) -> bool:
    """True when a paragraph opens by defining a term ('“Confidential Information” means...')."""

    return bool(_DEFINITION_PARAGRAPH_RE.match(text))


def is_standalone_heading(text: str, title: str) -> bool:
    """True when a paragraph is nothing but its clause's heading ('Non-Compete.')."""

    if not title:
        return False
    stripped = _TITLE_NUMBER_PREFIX_RE.sub("", text.strip(), count=1)
    return is_heading(stripped) and stripped.rstrip(".:").strip() == title


def _preamble_zones(texts: Dict[int, str], order: Sequence[int], operative: Sequence[int]) -> Dict[int, str]:
    """Preamble and recitals at the top of the document.

    When the opening paragraphs reach "NOW, THEREFORE ... agree as follows",
    everything up to that line is preamble or recitals — unless an operative
    clause (operative: the positions under a heading that names a clause
    type) starts before it, in which case that line sits inside the body and
    proves nothing. Otherwise only the lines that are unmistakably preamble
    or recitals are marked.
    """

    opening = order[:_PREAMBLE_SCAN_PARAGRAPHS]
    zones: Dict[int, str] = {}
    end = next((idx for idx in reversed(opening) if _RECITALS_END_RE.search(texts[idx])), None)
    if end is not None and any(idx <= end for idx in operative):
        end = None
    in_recitals = False
    for idx in opening:
        text = texts[idx]
        in_recitals = in_recitals or bool(_RECITALS_START_RE.match(text))
        if end is not None:
            if idx > end:
                break
            zones[idx] = ZONE_RECITALS if in_recitals else ZONE_PREAMBLE
        elif _RECITALS_START_RE.match(text):
            zones[idx] = ZONE_RECITALS
        elif _PARTY_INTRO_RE.search(text):
            zones[idx] = ZONE_PREAMBLE
    return zones


def _is_signature_line(text: str) -> bool:
    """A line of a signature block: "By: ____", "Name:", a party name standing alone."""

    return bool(_SIGNATURE_LINE_RE.search(text)) or len(text.split()) <= _SIGNATURE_BLOCK_MAX_WORDS


def _signature_zones(texts: Dict[int, str], order: Sequence[int]) -> Dict[int, str]:
    """Signature blocks: each "IN WITNESS WHEREOF" line and the signature lines under it, and the document's closing run of signature lines.

    A block ends at the first paragraph of substance, so an exhibit or
    schedule that follows the signatures is still reviewed.
    """

    zones: Dict[int, str] = {}
    in_block = False
    for idx in order:
        text = texts[idx]
        if _WITNESS_RE.match(text):
            in_block = True
        elif in_block and not _is_signature_line(text):
            in_block = False
        if in_block:
            zones[idx] = ZONE_SIGNATURE

    tail: List[int] = []
    for idx in reversed(order):
        if idx in zones or not _is_signature_line(texts[idx]):
            break
        tail.append(idx)
    if any(_SIGNATURE_LINE_RE.search(texts[idx]) for idx in tail):
        zones.update({idx: ZONE_SIGNATURE for idx in tail})
    return zones


def _party_block_zones(texts: Dict[int, str], order: Sequence[int]) -> Dict[int, str]:
    """Address and contact lines, and the short lead-in that introduces them ("All invoices should be mailed to:")."""

    zones = {idx: ZONE_PARTY_BLOCK for idx in order if is_address_line(texts[idx])}
    for idx, following in zip(order, order[1:]):
        text = texts[idx].strip()
        if following in zones and text.endswith(":") and len(text.split()) <= _LEAD_IN_MAX_WORDS:
            zones[idx] = ZONE_PARTY_BLOCK
    return zones


def _context_zones(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> Dict[int, str]:
    """Positions that are read for context and never reviewed, with the reason."""

    texts = {idx: para.text for idx, para in enumerate(paragraphs) if para.text.strip()}
    order = sorted(texts)
    operative = [idx for idx in order if clause_type_for_title(titles.get(paragraphs[idx].paraindetifier, "")) not in (None, RECITALS_TYPE)]
    zones: Dict[int, str] = {}
    zones.update(_party_block_zones(texts, order))
    zones.update(_preamble_zones(texts, order, operative))
    zones.update(_signature_zones(texts, order))
    return zones


def clause_type_for_title(title: str) -> Optional[str]:
    """The clause type a clause's own heading names, decided offline; None when the heading names none."""

    resolved = classify_layer1_heading(title) if title else None
    return resolved[0] if resolved and resolved[1] >= HEADING_MATCH_CONFIDENCE else None


def pick_primary_anchor(candidates: Sequence[Tuple[int, str]], clause_type: Optional[str]) -> int:
    """Choose a clause's primary operative paragraph from (position, text) candidates in document order."""

    for pattern in PRIMARY_ANCHOR_PATTERNS.get(clause_type or "", ()):
        for position, text in candidates:
            if pattern.search(text):
                return position
    return candidates[0][0]


def is_boilerplate_clause(title: str, clause_type: Optional[str]) -> bool:
    """True for a routine clause: headings, counterparts, severability, waiver, amendments, or notices."""

    return clause_type == NOTICES_TYPE or bool(_BOILERPLATE_TITLE_RE.match(title or ""))


def _assign_content_roles(structure: ClauseStructure, paragraphs: Sequence[TextInformation], content: Sequence[int], title: str, clause_type: Optional[str]) -> int:
    """Give a clause's body paragraphs their roles and return the primary anchor's position."""

    definitions = {idx for idx in content if is_definition_paragraph(paragraphs[idx].text)}
    operative = [idx for idx in content if idx not in definitions] or list(content)
    primary = pick_primary_anchor([(idx, paragraphs[idx].text) for idx in operative], clause_type)
    boilerplate = is_boilerplate_clause(title, clause_type)
    for idx in content:
        if boilerplate:
            structure.roles[idx] = ROLE_BOILERPLATE
        elif idx == primary:
            structure.roles[idx] = ROLE_PRIMARY
        else:
            structure.roles[idx] = ROLE_DEFINITION if idx in definitions else ROLE_SECONDARY
    return primary


def build_clause_structure(paragraphs: Sequence[TextInformation], titles: Dict[str, str]) -> ClauseStructure:
    """Assign every paragraph its role, clause unit, clause type, and primary anchor."""

    structure = ClauseStructure(zone_of=_context_zones(paragraphs, titles))
    for number, unit in enumerate(build_clause_units(paragraphs, titles)):
        title = titles.get(paragraphs[unit[0]].paraindetifier, "")
        clause_type = clause_type_for_title(title)
        content: List[int] = []
        for idx in unit:
            structure.unit_of[idx] = number
            if clause_type:
                structure.clause_type_of[idx] = clause_type
            if idx in structure.zone_of:
                structure.roles[idx] = ROLE_CONTEXT_ONLY
            elif is_standalone_heading(paragraphs[idx].text, title):
                structure.roles[idx] = ROLE_HEADING
            else:
                content.append(idx)
        if not content:
            continue
        primary = _assign_content_roles(structure, paragraphs, content, title, clause_type)
        for idx in unit:
            if structure.roles[idx] != ROLE_CONTEXT_ONLY:
                structure.anchor_of[idx] = primary
    return structure
