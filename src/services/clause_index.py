"""Builds an ordered, numbered list of a contract's clause headings.

Pure string rules (no LLM). The list is injected into the prompt so the model
evaluates the same clauses in the same order and copies titles verbatim instead
of inventing them. Conservative by design: a missed heading just falls back to
the clause's own words; a spurious one is filtered out by the severity rubric.
"""

import re
from typing import List

# Function/joiner words that may appear lowercase inside a legitimate heading
# ("Return of Advances", "Governing Law and Choice of Venue"). Every OTHER word
# in a heading candidate must start uppercase for it to count as a title.
_JOINER_WORDS = {
    "of", "and", "the", "for", "with", "to", "or", "a", "an",
    "in", "on", "at", "by", "from", "&", "/",
}

# Section/article prefixes: "Section 5", "ARTICLE III", "1.2 ...".
_NUMBERED_RE = re.compile(r"^((?:Section|Article|ARTICLE)\s+[\w.\-]+|\d+\.[\d.]*)(?:\s+(.*))?$")

# Inline heading: "Indemnity.  Service Provider agrees ..." -> "Indemnity".
# Capture a short Title-ish phrase (no interior period) that is immediately
# followed by a period and then real body text. 80 chars covers long headings
# ("Maintenance and Support of Your Development/the Vendor Software"); the
# word-count and Title-Case checks in _looks_like_title screen out sentences.
_INLINE_RE = re.compile(r"^([A-Z][^.]{1,80}?)\.\s+\S")

# All-caps section banner: "LIMITATION OF LIABILITY", "RECITALS".
_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z0-9 &/\-]{3,}$")


def _looks_like_title(phrase: str) -> bool:
    """True when ``phrase`` reads like a clause heading, not a sentence."""
    phrase = phrase.strip().strip("\"'()[]")
    words = phrase.split()
    if not (1 <= len(words) <= 8):
        return False
    significant = [w for w in words if w.lower().strip(".,;:") not in _JOINER_WORDS]
    if not significant:
        return False
    for w in significant:
        head = w.lstrip("(\"'“‘")  # ignore leading quotes/brackets
        if not head[:1].isupper():
            return False
    return True


def _title_from_line(line: str) -> str:
    """Return the clause title a line introduces, or "" if it isn't a heading."""
    line = line.strip()
    if not line:
        return ""

    # Section / article / numbered heading.
    m = _NUMBERED_RE.match(line)
    if m:
        label, rest = m.group(1), (m.group(2) or "")
        rest_title = rest.split(".")[0].strip() if rest else ""
        if rest_title and _looks_like_title(rest_title):
            return f"{label} {rest_title}".strip()
        return label.strip()

    # Standalone heading line: whole line is a short title ending with a period.
    if line.endswith(".") and _looks_like_title(line[:-1]):
        return line[:-1].strip()

    # Inline heading: "Title.  body ...".
    m = _INLINE_RE.match(line)
    if m and _looks_like_title(m.group(1)):
        return m.group(1).strip()

    # All-caps banner.
    if _ALLCAPS_RE.match(line):
        return line.strip()

    return ""


def extract_clause_titles(content: str, max_titles: int = 80) -> List[str]:
    """Ordered, de-duplicated list of clause titles found in ``content``."""
    titles: List[str] = []
    seen = set()
    for raw in content.splitlines():
        title = _title_from_line(raw)
        if not title:
            continue
        key = re.sub(r"\s+", " ", title).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(re.sub(r"\s+", " ", title).strip())
        if len(titles) >= max_titles:
            break
    return titles


def build_clause_index(content: str) -> str:
    """Numbered clause-index block for prompt injection (empty string if none)."""
    titles = extract_clause_titles(content)
    if not titles:
        return ""
    return "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
