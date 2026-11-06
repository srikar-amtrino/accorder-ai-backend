"""Paragraph text extraction that keeps Word content controls.

python-docx's ``Paragraph.text`` reads only the runs and hyperlinks that are
direct children of the paragraph. Text held in an inline content control
(``<w:sdt>`` / ``<w:sdtContent>``) — a dropdown or a fill-in field such as the
governing-law state — is silently dropped, so "the laws of the State of
California," comes out as "the laws of the State of ,". Every .docx paragraph
extractor reads its text through ``paragraph_text`` instead.
"""

from typing import Any, List

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

_RUN = qn("w:r")
_HYPERLINK = qn("w:hyperlink")
_SDT = qn("w:sdt")
_SDT_CONTENT = qn("w:sdtContent")
_TEXT = qn("w:t")
_TAB = qn("w:tab")
_BREAK = qn("w:br")
_CARRIAGE_RETURN = qn("w:cr")
_NO_BREAK_HYPHEN = qn("w:noBreakHyphen")
_BREAK_TYPE = qn("w:type")


def _run_text(run: Any) -> str:
    """Text of one run, with the same tab / line-break / hyphen handling as python-docx."""

    parts: List[str] = []
    for child in run:
        if child.tag == _TEXT:
            parts.append(child.text or "")
        elif child.tag == _TAB:
            parts.append("\t")
        elif child.tag == _CARRIAGE_RETURN:
            parts.append("\n")
        elif child.tag == _BREAK:
            # Page and column breaks carry no text; a plain (text-wrapping) break is a newline.
            if child.get(_BREAK_TYPE) in (None, "textWrapping"):
                parts.append("\n")
        elif child.tag == _NO_BREAK_HYPHEN:
            parts.append("-")
    return "".join(parts)


def _inline_text(element: Any) -> str:
    """Text of a paragraph-level container, descending into hyperlinks and content controls in document order."""

    parts: List[str] = []
    for child in element:
        if child.tag == _RUN:
            parts.append(_run_text(child))
        elif child.tag in (_HYPERLINK, _SDT_CONTENT):
            parts.append(_inline_text(child))
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                parts.append(_inline_text(content))
    return "".join(parts)


def paragraph_text(paragraph: Paragraph) -> str:
    """The paragraph's text exactly as python-docx gives it, plus the text of its inline content controls."""

    return _inline_text(paragraph._p)
