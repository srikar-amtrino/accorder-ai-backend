"""Internal drafting mismatches: undefined capitalised terms that stand for a term the agreement does define.

"Accordor Proprietary Information" reads as a defined term, but the agreement
defines the concept as "Confidential Information". That mismatch is a drafting
defect the review should raise on every run, with the defined term as the
fix, not only when the model happens to notice it. This module finds such
phrases in plain code.

Only mismatches the agreement itself resolves are reported. A capitalised
phrase with no defined counterpart ("Purchase Order", or "Proprietary
Information" describing what a party owes a third party) is left alone: the
agreement does not say what it should be, and reporting it crowded out
substantive findings and invited wrong replacements.

Everything that defines a term counts as a definition: a quoted capitalised
phrase anywhere in the agreement ('(the "Budget")', '"Confidential
Information" means'). Phrases are compared singular and case-folded, so
"Statements of Work" is the defined "Statement of Work". Not flagged: party
names, clause headings, words that merely open a sentence ("Each Statement
of Work"), places, all-capital abbreviations, and the names of laws, codes,
courts and organisations.

Where the undefined phrase is a party's name followed by wording that appears
in exactly the definition of a defined term ("Accordor Proprietary
Information": the "Confidential Information" definition covers "proprietary
information ... of Accordor"), that defined term is recorded as the one to use.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.schemas.general_review import TextInformation
from src.services.general_review.party_resolver import extract_defined_parties
from src.services.general_review.segmentation import ROLE_DEFINITION, ROLE_PRIMARY, ROLE_SECONDARY, ClauseStructure

# Roles of the paragraphs scanned: operative text only, never headings, routine clauses or non-clause content.
SCANNED_ROLES = (ROLE_PRIMARY, ROLE_SECONDARY, ROLE_DEFINITION)

_QUOTED_TERM_RE = re.compile(r"[\"“]([A-Z][^\"“”]{0,60}?)[,.]?[\"”]")
_PHRASE_RE = re.compile(r"[A-Z][A-Za-z’'\-]+(?:\s+(?:of\s+)?[A-Z][A-Za-z’'\-]+)+")
_POSSESSIVE_RE = re.compile(r"[’']s$")
_SENTENCE_END_RE = re.compile(r"[.;:!?)]\s*$")

# A word that marks a proper name rather than a defined term: laws, codes, courts, organisations, places.
_PROPER_NAME_WORDS = {
    "act",
    "acts",
    "code",
    "codes",
    "regulation",
    "regulations",
    "rules",
    "statute",
    "court",
    "courts",
    "commission",
    "department",
    "authority",
    "agency",
    "association",
    "institute",
    "bureau",
    "office",
    "nations",
    "union",
    "kingdom",
    "states",
    "state",
    "county",
    "city",
    "republic",
    "inc",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "company",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


@dataclass(frozen=True)
class UndefinedTerm:
    """A capitalised phrase used in an operative paragraph that the agreement never defines, and the defined term it stands for."""

    para_identifier: str
    clause: str
    term: str
    defined_term: str


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def term_key(phrase: str) -> str:
    """A phrase folded for comparison: case, possessives, punctuation and plurals ignored."""

    words = re.findall(r"[a-z0-9\-]+", _POSSESSIVE_RE.sub("", phrase.strip()).casefold())
    return " ".join(_singular(word) for word in words)


def _defined_terms(paragraphs: Sequence[TextInformation]) -> Dict[str, Tuple[str, int]]:
    """Every quoted capitalised phrase, by its key: (the phrase, the position of the paragraph that first quotes it)."""

    defined: Dict[str, Tuple[str, int]] = {}
    for position, para in enumerate(paragraphs):
        for match in _QUOTED_TERM_RE.finditer(para.text):
            phrase = " ".join(match.group(1).split()).rstrip(",.")
            defined.setdefault(term_key(phrase), (phrase, position))
    return defined


def _groups(phrase: str) -> List[Tuple[int, List[str]]]:
    """Split a capitalised run into phrases at possessives: "Accordor’s Confidential Information" -> "Accordor", "Confidential Information".

    Returns (offset of the group's first word within the run, the group's words).
    """

    groups: List[Tuple[int, List[str]]] = []
    current: List[str] = []
    start = 0
    for match in re.finditer(r"\S+", phrase):
        word = match.group(0)
        if not current:
            start = match.start()
        if _POSSESSIVE_RE.search(word):
            current.append(_POSSESSIVE_RE.sub("", word))
            groups.append((start, current))
            current = []
        else:
            current.append(word)
    if current:
        groups.append((start, current))
    return groups


def _trimmed(words: List[str]) -> List[str]:
    while words and words[0] == "of":
        words = words[1:]
    while words and words[-1] == "of":
        words = words[:-1]
    return words


def _is_proper_name(words: Sequence[str], places: Set[str]) -> bool:
    folded = [word.casefold().strip("’'.-") for word in words]
    if any(word in _PROPER_NAME_WORDS or word in places for word in folded):
        return True
    return any(len(word) > 1 and word.isupper() for word in words)


def _operative_text(text: str, title: str) -> str:
    """The paragraph without its inline heading ("Relationship with Accordor. Service Provider's work ...")."""

    stripped = text.strip()
    if title and stripped.startswith(title):
        return stripped[len(title) :].lstrip(" .:")
    return stripped


def find_undefined_terms(paragraphs: Sequence[TextInformation], titles: Dict[str, str], structure: ClauseStructure, places: Sequence[str] = ()) -> Tuple[UndefinedTerm, ...]:
    """Undefined capitalised phrases in operative paragraphs that stand for a term the agreement defines, in document order."""

    defined = _defined_terms(paragraphs)
    parties = extract_defined_parties(paragraphs)
    party_keys = {term_key(name) for legal_name, alias in parties for name in (legal_name, alias)}
    aliases = sorted({alias for _, alias in parties}, key=len, reverse=True)
    place_words = {word.casefold() for place in places for word in place.split()}
    titles_seen = {term_key(title) for title in titles.values()}

    skip = set(defined) | party_keys | titles_seen
    found: List[UndefinedTerm] = []
    for position, para in enumerate(paragraphs):
        if structure.role(position) not in SCANNED_ROLES:
            continue
        title = titles.get(para.paraindetifier, "")
        for words in _capitalised_phrases(_operative_text(para.text, title), set(defined)):
            term = " ".join(words)
            if term_key(term) in skip or _is_proper_name(words, place_words) or any(item.para_identifier == para.paraindetifier and item.term == term for item in found):
                continue
            defined_term = _standing_for(term, aliases, defined, paragraphs)
            if defined_term is not None:
                found.append(UndefinedTerm(para.paraindetifier, title, term, defined_term))
    return tuple(found)


def _capitalised_phrases(text: str, defined: Set[str]) -> List[List[str]]:
    """The capitalised multi-word phrases of a text, split at possessives, without a word that only opens a sentence."""

    phrases: List[List[str]] = []
    for run in _PHRASE_RE.finditer(text):
        for offset, raw_words in _groups(run.group(0)):
            words = _trimmed(raw_words)
            before = text[: run.start() + offset]
            sentence_start = not before.strip() or bool(_SENTENCE_END_RE.search(before))
            if sentence_start and term_key(" ".join(words)) not in defined:
                words = _trimmed(words[1:])  # "Each Statement of Work": the first word only opens the sentence
            if sum(1 for word in words if word != "of") >= 2:
                phrases.append(words)
    return phrases


def _standing_for(term: str, aliases: Sequence[str], defined: Dict[str, Tuple[str, int]], paragraphs: Sequence[TextInformation]) -> Optional[str]:
    """The defined term an undefined "<party> <wording>" phrase stands for, or None when the agreement does not show it.

    The wording must appear in the definition of a defined term ending in the
    same noun ("proprietary information" in the "Confidential Information"
    definition). When several definitions qualify, the earliest wins.
    """

    alias = next((alias for alias in aliases if term.startswith(alias + " ")), None)
    if alias is None:
        return None
    rest = term[len(alias) :].strip()
    head = term_key(rest).split()[-1] if rest else ""
    candidates = [
        (position, phrase)
        for key, (phrase, position) in defined.items()
        if key.split() and key.split()[-1] == head and key != term_key(rest) and rest.casefold() in paragraphs[position].text.casefold()
    ]
    return min(candidates)[1] if candidates else None
