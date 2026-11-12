"""Keep a redraft's untouched wording character-for-character identical to the source paragraph.

A model asked to change one sentence of a paragraph returns the rest in its
own typing: curly apostrophes come back straight, double spaces single, an en
dash as a hyphen. Applied in Word, each of those is a tracked change the
reviewer never asked for. This module puts the source's own characters back
wherever the redraft's wording is the same as the source's, and types the
new wording the way the source types its own (curly or straight quotes).

It never changes a word: text is compared with quote, dash and spacing
variants folded together, and only stretches that are identical under that
folding are restored.
"""

import re
from difflib import SequenceMatcher
from typing import List

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]")
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
_OPENS_QUOTE_AFTER = " \t\n([{-–—"


def _fold(token: str) -> str:
    return " " if token.isspace() else token.translate(_FOLD)


def uses_curly_apostrophes(text: str) -> bool:
    """True when a text types its apostrophes curly (’) rather than straight (')."""

    return text.count("’") > text.count("'")


def uses_curly_quotes(text: str) -> bool:
    """True when a text types its double quotation marks curly (“ ”) rather than straight (")."""

    return text.count("“") + text.count("”") > text.count('"')


def restore_typography(original: str, revised: str, curly_apostrophes: bool = False, curly_quotes: bool = False) -> str:
    """The redraft with the source's own characters restored in every stretch whose wording did not change.

    curly_apostrophes / curly_quotes say how the agreement types those marks;
    wording the redraft adds is typed the same way.
    """

    source = _TOKEN_RE.findall(original.strip())
    draft = _TOKEN_RE.findall(revised.strip())
    matcher = SequenceMatcher(None, [_fold(token) for token in source], [_fold(token) for token in draft], autojunk=False)
    out: List[str] = []
    for tag, source_start, source_end, draft_start, draft_end in matcher.get_opcodes():
        if tag == "equal":
            out.extend(source[source_start:source_end])
            continue
        for token in draft[draft_start:draft_end]:
            if token == "'" and curly_apostrophes:
                token = "’"
            elif token == '"' and curly_quotes:
                previous = out[-1][-1] if out else " "
                token = "“" if previous in _OPENS_QUOTE_AFTER else "”"
            out.append(token)
    return "".join(out)
