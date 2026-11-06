"""Deterministic party alias and role resolution for the general review.

The reviewer names the party they represent in their own words — often a
shorthand ("ACI") the agreement never uses. Left as-is, the model has to guess
which defined party that is, and it guesses differently from run to run. This
module resolves the shorthand against the agreement's own preamble in plain
code, so the prompt can name the client and the counterparty by the terms the
agreement defines for them, and so the risk computation can tell which party a
clause actually binds.

Everything here is string work on the paragraphs the frontend sends; no model
or network call.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Pattern, Sequence, Tuple

from src.schemas.general_review import TextInformation

ROLE_CLIENT = "Client / Customer"
ROLE_VENDOR = "Vendor / Service Provider"
ROLE_PARTY = "Party"

# How the represented party was matched to a party the preamble defines.
METHOD_ALIAS = "alias"
METHOD_LEGAL_NAME = "legal_name"
METHOD_ROLE = "role"
METHOD_ACRONYM = "acronym"
METHOD_UNRESOLVED = "unresolved"

# Which way a clause runs between the two parties.
FAVORS_REPRESENTED = "favors_represented"
BURDENS_REPRESENTED = "burdens_represented"
MUTUAL = "mutual"
NEUTRAL = "neutral"

# The parties are introduced in the opening paragraphs; nothing further down is scanned.
PREAMBLE_SCAN_PARAGRAPHS = 8

_QUOTES = "\"“”‘’'"

# '(hereinafter “Accordor”)', '("Service Provider")', '(the "Company")'.
_ALIAS_PAREN_RE = re.compile(
    r"\(\s*(?:each,?\s+)?(?:(?:hereinafter|hereafter|herein|collectively|individually|together)\s*,?\s*)*"
    r"(?:(?:referred\s+to|known)\s+as\s+)?(?:the\s+|an?\s+)?"
    r"[" + _QUOTES + r"](?P<alias>[^" + _QUOTES + r"()]{1,40})[" + _QUOTES + r"][^()]*\)"
)

# Defined terms a preamble also introduces that are not parties.
_GENERIC_ALIASES = {"agreement", "effective date", "party", "parties", "services", "deliverables", "term", "msa", "contract", "recitals", "statement of work", "sow"}

_COMPANY_SUFFIXES = ("inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "llp", "lp", "plc", "gmbh", "ag", "bv", "pvt")
_SUFFIX_LOOKAHEAD = r"(?:Inc|Incorporated|LLC|L\.L\.C|Ltd|Limited|Corp|Corporation|Co|Company|LLP|LP|PLC|GmbH|N\.A|S\.A)\b"
_NAME_TOKEN = r"[A-Z][\w&'’\-]*\.?"
_NAME_RUN = rf"{_NAME_TOKEN}(?:(?:\s+(?:(?:of|&|de|du|van|von)\s+)?|,\s+(?={_SUFFIX_LOOKAHEAD})){_NAME_TOKEN})*"
_LEADING_NAME_RE = re.compile(_NAME_RUN)
_TRAILING_NAME_RE = re.compile(rf"({_NAME_RUN})[\s,;]*$")
_BETWEEN_RE = re.compile(r"\b(?:between|among)\s+", re.IGNORECASE)
_AND_NAME_RE = re.compile(r"\band\s+(?=[A-Z])")

_VENDOR_WORDS_RE = re.compile(r"\b(service\s+provider|provider|vendor|supplier|contractor|consultant|licensor|seller|processor|subcontractor)\b", re.IGNORECASE)
_CLIENT_WORDS_RE = re.compile(r"\b(client|customer|buyer|purchaser|licensee|company|controller|subscriber)\b", re.IGNORECASE)

# Words an acronym may pass over ("Bank of America" -> "BA", "Acme Holdings Inc." -> "AH").
_ACRONYM_SKIPPABLE = {"of", "and", "the", "for", "de"} | set(_COMPANY_SUFFIXES)


@dataclass(frozen=True)
class Party:
    """One party to the agreement, as its preamble introduces it."""

    legal_name: str
    alias: str
    role: str

    def describe(self) -> str:
        return f'{self.legal_name} (defined as "{self.alias}", Role: {self.role})'

    @property
    def names(self) -> Tuple[str, ...]:
        """Every name the agreement's text may use for this party, longest first."""

        candidates = {self.alias, self.legal_name, self.legal_name.rstrip("."), _strip_company_suffix(self.legal_name)}
        return tuple(sorted((name for name in candidates if name), key=lambda name: (-len(name), name)))


@dataclass(frozen=True)
class PartyContext:
    """The represented party and its counterparty, resolved from the preamble.

    represented and counterparty are None when the reviewer's wording could
    not be tied to exactly one party; the review then falls back to the raw
    wording, as it did before this resolver existed.
    """

    raw: str
    represented: Optional[Party] = None
    counterparty: Optional[Party] = None
    method: str = METHOD_UNRESOLVED

    @property
    def resolved(self) -> bool:
        return self.represented is not None and self.counterparty is not None

    @property
    def cache_token(self) -> str:
        """Stable identity of the resolved parties: "ACI" and "Accordor" give the same token."""

        if self.represented is None or self.counterparty is None:
            return f"unresolved|{_fold(self.raw)}"
        parts = [self.represented.legal_name, self.represented.alias, self.represented.role, self.counterparty.legal_name, self.counterparty.alias, self.counterparty.role]
        return "resolved|" + "|".join(_fold(part) for part in parts)

    def substitute(self, text: str) -> str:
        """Replace the reviewer's shorthand with the agreement's defined term in free text.

        Only a shorthand matched as an acronym is rewritten: a role word
        ("Customer") or a real name can legitimately appear in the reviewer's
        own sentences with another meaning.
        """

        if self.represented is None or self.method != METHOD_ACRONYM or not text:
            return text
        return re.sub(rf"(?<![\w]){re.escape(self.raw)}(?![\w])", self.represented.alias, text)

    def client_line(self) -> str:
        """The reviewer-context line naming the client, with the shorthand replaced when resolved."""

        if self.represented is None:
            return f"Party the client represents: {self.raw}"
        return f'Party the client represents: {self.represented.legal_name} — referred to in this agreement as "{self.represented.alias}"'

    def render_block(self) -> str:
        """The prompt's party block; empty when the parties could not be resolved."""

        if self.represented is None or self.counterparty is None:
            return ""
        client = self.represented
        other = self.counterparty
        shorthand = ""
        if _fold(self.raw) not in {_fold(name) for name in client.names}:
            shorthand = f' The client\'s own shorthand "{self.raw}" means this same party and never the other one.'
        return (
            "Parties to this agreement (resolved from its preamble — authoritative; never swap them):\n\n"
            f'- Represented Party (your client): {client.describe()}. Every reference to "{client.alias}" in the text is your client.{shorthand}\n'
            f'- Counterparty: {other.describe()}. Every reference to "{other.alias}" in the text is the other side, never your client.\n\n'
        )


def _fold(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text or "").casefold().split())


def _strip_company_suffix(name: str) -> str:
    words = name.replace(",", " ").split()
    while len(words) > 1 and words[-1].rstrip(".").casefold() in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def _clean_name(name: str) -> str:
    """Drop sentence punctuation from a captured name, keeping the full stop of a company suffix ("Inc.")."""

    name = " ".join(name.split()).rstrip(",;:")
    if name.endswith(".") and name.rstrip(".").split()[-1].casefold() not in _COMPANY_SUFFIXES:
        name = name.rstrip(".")
    return name


def _party_name(segment: str) -> Optional[str]:
    """The party named in the text that leads up to its '(“Alias”)' parenthetical."""

    between = list(_BETWEEN_RE.finditer(segment))
    if between:
        scoped = segment[between[-1].end() :]
    else:
        joined = _AND_NAME_RE.search(segment)
        scoped = segment[joined.end() :] if joined else segment
    leading = _LEADING_NAME_RE.match(scoped.lstrip(" ,;:"))
    if leading:
        return _clean_name(leading.group(0))
    trailing = _TRAILING_NAME_RE.search(segment)
    return _clean_name(trailing.group(1)) if trailing else None


def extract_defined_parties(paragraphs: Sequence[TextInformation]) -> List[Tuple[str, str]]:
    """Collect (legal name, defined alias) for the parties the preamble introduces, in order."""

    found: List[Tuple[str, str]] = []
    scanned = 0
    for para in paragraphs:
        text = para.text.strip()
        if not text:
            continue
        scanned += 1
        if scanned > PREAMBLE_SCAN_PARAGRAPHS:
            break
        cursor = 0
        for match in _ALIAS_PAREN_RE.finditer(text):
            segment = text[cursor : match.start()]
            cursor = match.end()
            alias = " ".join(match.group("alias").split())
            if _fold(alias) in _GENERIC_ALIASES or "agreement" in _fold(alias):
                continue
            name = _party_name(segment)
            if name and "agreement" not in _fold(name) and all(_fold(alias) != _fold(known) for _, known in found):
                found.append((name, alias))
    return found


def _role_from_words(text: str) -> Optional[str]:
    if _VENDOR_WORDS_RE.search(text):
        return ROLE_VENDOR
    if _CLIENT_WORDS_RE.search(text):
        return ROLE_CLIENT
    return None


def _role_from_recitals(names: Sequence[str], preamble: str) -> Optional[str]:
    """Client when the recitals say this party is the one retaining or paying for the services."""

    for name in names:
        if re.search(rf"\b{re.escape(name)}\s+(?:desires|wishes|wants|intends|has\s+agreed)\s+to\s+(?:retain|engage|obtain|purchase|procure|receive|license)\b", preamble):
            return ROLE_CLIENT
    return None


def _assign_roles(defined: Sequence[Tuple[str, str]], preamble: str) -> List[Party]:
    """Give each of the two parties a role: from its own defined term, else the recitals, else its counterpart's."""

    roles: List[Optional[str]] = []
    for name, alias in defined:
        role = _role_from_words(alias) or _role_from_recitals((alias, _strip_company_suffix(name)), preamble)
        roles.append(role)
    opposite = {ROLE_CLIENT: ROLE_VENDOR, ROLE_VENDOR: ROLE_CLIENT}
    for index, role in enumerate(roles):
        other = roles[1 - index]
        if role is None and other is not None:
            roles[index] = opposite[other]
    if roles[0] is not None and roles[0] == roles[1]:
        roles = [None, None]
    return [Party(name, alias, role or ROLE_PARTY) for (name, alias), role in zip(defined, roles)]


def is_acronym_of(acronym: str, name: str) -> bool:
    """True when the acronym spells out the name by taking a leading run of letters from its words, in order.

    "ACI" is an acronym of "Accordor Inc." (AC + I) but not of "Amtrino
    Technologies": the first word must contribute, every letter must open a
    word or continue the word's opening letters, and only connectors and
    company suffixes may be passed over.
    """

    letters = re.sub(r"[^a-z0-9]", "", acronym.casefold())
    words = re.findall(r"[a-z0-9]+", name.casefold())
    if len(letters) < 2 or not words:
        return False

    def spell(taken: int, word_index: int) -> bool:
        if taken == len(letters):
            return True
        if word_index == len(words):
            return False
        word = words[word_index]
        length = 0
        while taken + length < len(letters) and length < len(word) and letters[taken + length] == word[length]:
            length += 1
            if spell(taken + length, word_index + 1):
                return True
        return word_index > 0 and word in _ACRONYM_SKIPPABLE and spell(taken, word_index + 1)

    return spell(0, 0)


def _match_party(raw: str, parties: Sequence[Party]) -> Tuple[Optional[int], str]:
    """Index of the one party the reviewer's wording names, and how it matched. Ambiguity resolves to nothing."""

    folded = _fold(raw)
    tests = (
        (METHOD_ALIAS, lambda party: folded == _fold(party.alias)),
        (METHOD_LEGAL_NAME, lambda party: folded in {_fold(party.legal_name), _fold(_strip_company_suffix(party.legal_name))}),
        (METHOD_ROLE, lambda party: party.role != ROLE_PARTY and _role_from_words(raw) == party.role),
        (METHOD_LEGAL_NAME, lambda party: bool(folded) and set(folded.split()) <= set(_fold(party.legal_name).split())),
        (METHOD_ACRONYM, lambda party: " " not in raw.strip() and (is_acronym_of(raw, party.legal_name) or is_acronym_of(raw, party.alias))),
    )
    for method, test in tests:
        hits = [index for index, party in enumerate(parties) if test(party)]
        if len(hits) == 1:
            return hits[0], method
        if len(hits) > 1:
            break
    return None, METHOD_UNRESOLVED


def resolve_party_context(party_represented: Optional[str], paragraphs: Sequence[TextInformation]) -> Optional[PartyContext]:
    """Resolve the reviewer's stated party against the agreement's preamble.

    Returns None when the reviewer named no party. When the wording cannot be
    tied to exactly one of two defined parties the context comes back
    unresolved, carrying only the raw wording.
    """

    if not party_represented:
        return None
    defined = extract_defined_parties(paragraphs)[:2]
    if len(defined) < 2:
        return PartyContext(raw=party_represented)
    preamble = " ".join(para.text for para in paragraphs[: PREAMBLE_SCAN_PARAGRAPHS * 2])
    parties = _assign_roles(defined, preamble)
    index, method = _match_party(party_represented, parties)
    if index is None:
        return PartyContext(raw=party_represented)
    return PartyContext(raw=party_represented, represented=parties[index], counterparty=parties[1 - index], method=method)


# --- directionality ------------------------------------------------------------

_ADVERBS = r"(?:(?:hereby|further|also|expressly|irrevocably|unconditionally|additionally)\s+){0,3}"
# "shall have the right", "will be entitled", "shall not be liable" give a party something, and "shall be set
# forth" is a passive whose subject is not the party; none of them is a duty. "shall be responsible" still is.
_NOT_A_DUTY = r"(?!(?:shall|will)\s+(?:not\s+be\s+(?:liable|required|obligated|responsible)\b|have\b|own\b|retain\b|be\s+(?!responsible\b|liable\b|required\b|obligated\b|bound\b|solely\b)))"
_DUTY = (
    r"(?:shall\s+not|will\s+not|may\s+not|must\s+not|shall|will|must|agrees?|undertakes?|covenants?|represents?|warrants?|acknowledges?|understands?|"
    r"assigns?|designates?|waives?|is\s+(?:required|obligated|responsible|liable))\b"
)
_RIGHT = r"(?:may(?!\s+not)|reserves?\s+the\s+right|is\s+entitled|shall\s+have\s+the\s+right|will\s+have\s+the\s+right|has\s+the\s+right|retains?)\b"
_MUTUAL_DUTY_RE = re.compile(rf"\b(?:(?:either|each|neither|both)\s+part(?:y|ies)|the\s+parties|no\s+party)\s+{_ADVERBS}(?:{_DUTY}|agree\b)", re.IGNORECASE)


# A reason that complains the clause binds, protects, or benefits one party only.
_MUTUALITY_RE = re.compile(
    r"\bone[-\s]sided\b|\bone[-\s]way\b|\bonly\s+one\s+way\b|\bmutual(?:ity|ly)?\b|\bnon-?mutual\b|\breciproc\w+|\bunilateral\w*|\basymmetr\w+|"
    r"\b(?:appl(?:y|ies)|protects?|binds?|runs?)\s+only\b|\bonly\s+(?:to\s+)?(?:binds?|protects?|appl(?:y|ies))\b|\bsolely\s+(?:on|to\s+protect)\b",
    re.IGNORECASE,
)
# Wording that marks a sentence as naming a defect.
_DEFECT_CUE_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|doesn['’]t|no|not|without|missing|omits?|omitted|fails?\s+to|lacks?|lacking|absent|absence|vague|ambiguous|undefined|unclear|unlimited|uncapped|overbroad|silent)\b",
    re.IGNORECASE,
)
_REASON_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def is_sole_mutuality_complaint(reason: str) -> bool:
    """True when the only defect a finding's reason names is that the clause is one-sided or not mutual.

    Every sentence that names a defect must itself be about one-sidedness.
    A reason that goes on to name anything else — a missing exclusion, an
    undefined period — is a finding in its own right and is left alone.
    """

    sentences = [sentence for sentence in _REASON_SENTENCE_RE.split(reason or "") if sentence.strip()]
    if not any(_MUTUALITY_RE.search(sentence) for sentence in sentences):
        return False
    return not any(_DEFECT_CUE_RE.search(sentence) and not _MUTUALITY_RE.search(sentence) for sentence in sentences)


@lru_cache(maxsize=64)
def _subject_patterns(names: Tuple[str, ...]) -> Tuple[Pattern[str], Pattern[str]]:
    """(duty, right) patterns for sentences whose subject is one of the names. Names are matched case-sensitively."""

    subject = "(?:" + "|".join(re.escape(name) for name in names) + ")"
    duty = re.compile(rf"(?<![\w]){subject}\s+(?i:{_ADVERBS}{_NOT_A_DUTY}{_DUTY})")
    right = re.compile(rf"(?<![\w]){subject}\s+(?i:{_ADVERBS}{_RIGHT})")
    return duty, right


def clause_direction(text: str, context: Optional[PartyContext]) -> str:
    """Which way a paragraph runs between the represented party and its counterparty.

    FAVORS_REPRESENTED: only the counterparty takes on duties or restrictions,
    and it gains no right of its own. BURDENS_REPRESENTED: only the client
    takes on duties. MUTUAL: the paragraph binds both parties in so many
    words. NEUTRAL: anything else, including whenever the parties are
    unresolved — the caller then leaves the model's own reading untouched.
    """

    if context is None or context.represented is None or context.counterparty is None:
        return NEUTRAL
    if _MUTUAL_DUTY_RE.search(text):
        return MUTUAL
    client_duty, _ = _subject_patterns(context.represented.names)
    other_duty, other_right = _subject_patterns(context.counterparty.names)
    client_bound = bool(client_duty.search(text))
    other_bound = bool(other_duty.search(text))
    if other_bound and not client_bound and not other_right.search(text):
        return FAVORS_REPRESENTED
    if client_bound and not other_bound:
        return BURDENS_REPRESENTED
    return NEUTRAL
