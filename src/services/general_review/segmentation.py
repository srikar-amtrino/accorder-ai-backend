"""Clause segmentation for the general review: titles, cut points, embedding queries, defined terms.

Everything here is pure string work on the paragraphs the frontend sends; no
model or network call. The title heuristics decide where one clause ends and
the next begins, which drives batch boundaries, clause units for policy
mapping, and the clause names on the suggestions.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from src.schemas.general_review import TextInformation

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

# '"Term" means ...' — the document's own glossary, listed with every batch.
_DEFINED_TERM_RE = re.compile(r"[\"“]([A-Z][A-Za-z0-9\s\-]{1,40})[\"”]\s+(?:means|shall\s+mean)\b")
MAX_DEFINED_TERMS = 40

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
    the most recent titled paragraph above it. Paragraphs with no determinable
    title (e.g. a lone selected body paragraph whose heading was not sent) are
    omitted so the model's own label stays as the fallback.
    """

    titles: Dict[str, str] = {}
    current = ""
    for para in paragraphs:
        if not para.text.strip():
            continue
        own = clause_title(para.text)
        if own:
            current = own
        if current:
            titles[para.paraindetifier] = current
    return titles


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


def extract_defined_terms(paragraphs: Sequence[TextInformation], limit: int = MAX_DEFINED_TERMS) -> List[str]:
    """Collect the document's defined terms ('"Term" means ...') in order of first definition."""

    terms: List[str] = []
    for para in paragraphs:
        for match in _DEFINED_TERM_RE.finditer(para.text):
            term = " ".join(match.group(1).split())
            if term not in terms:
                terms.append(term)
                if len(terms) >= limit:
                    return terms
    return terms
