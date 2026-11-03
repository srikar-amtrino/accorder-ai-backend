import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, TypeVar, cast

import docx
from docx.document import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml.text.parfmt import CT_PPr
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.shared import Length
from docx.text.paragraph import Paragraph
from docx.text.run import Run

logger = logging.getLogger(__name__)

ALIGNMENT_NAMES = {
    WD_ALIGN_PARAGRAPH.LEFT: "Left",
    WD_ALIGN_PARAGRAPH.CENTER: "Centered",
    WD_ALIGN_PARAGRAPH.RIGHT: "Right",
    WD_ALIGN_PARAGRAPH.JUSTIFY: "Justified",
    WD_ALIGN_PARAGRAPH.DISTRIBUTE: "Distributed",
    WD_ALIGN_PARAGRAPH.JUSTIFY_MED: "Justified",
    WD_ALIGN_PARAGRAPH.JUSTIFY_HI: "Justified",
    WD_ALIGN_PARAGRAPH.JUSTIFY_LOW: "Justified",
    WD_ALIGN_PARAGRAPH.THAI_JUSTIFY: "Justified",
    None: "Left",
}

LINE_SPACING_RULE_NAMES = {
    0: "Single",
    1: "OnePtFive",
    2: "Double",
    3: "AtLeast",
    4: "Exactly",
    5: "Multiple",
    None: None,
}

_NUM_FMT_KIND = {
    "decimal": ("decimal", False),
    "decimalZero": ("decimal", False),
    "lowerLetter": ("alpha", False),
    "upperLetter": ("alpha", True),
    "lowerRoman": ("roman", False),
    "upperRoman": ("roman", True),
    "bullet": ("bullet", False),
    "none": ("none", False),
}

_ROMAN_VALS = [
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
]

T = TypeVar("T")

# Sentinel distinguishing "this paragraph/style explicitly turns numbering
# OFF" (w:numId val="0") from "no numPr element was present at all". Word
# uses numId=0 to let a paragraph opt out of numbering it would otherwise
# inherit from its style — if we treated that the same as "absent" we'd
# fall through to the style chain and wrongly re-apply the very numbering
# the paragraph asked to remove.
_NUMPR_REMOVED = object()


def _to_roman(n: int) -> str:
    out = []
    for val, sym in _ROMAN_VALS:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def _to_alpha(n: int) -> str:
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(97 + rem) + s
    return s


@dataclass
class ParaMeta:
    paragraphIndex: int
    text: str
    wordCount: int
    charCount: int
    charCountNoSpaces: int
    alignment: str
    styleName: str
    styleId: str | None
    leftIndent: float
    rightIndent: float
    firstLineIndent: float
    outlineLevel: int
    spaceBefore: float
    spaceAfter: float
    lineSpacing: float | None
    lineSpacingRule: str | None
    isBold: bool | None
    isItalic: bool | None
    isUnderline: bool | None
    underline: str
    isAllCaps: bool | None
    fontName: str | None
    fontSizePt: float | None
    fontColorHex: str | None
    runCount: int
    hasHyperlink: bool
    isListNumbering: bool
    listType: str
    numId: int | None
    listLevel: int | None
    listNumberValue: str
    pageBreakBefore: bool
    keepWithNext: bool
    keepTogether: bool
    widowControl: bool | None
    tabCount: int


class NumberingResolver:
    """Resolves numId/ilvl pairs to rendered list values like 2.1."""

    def __init__(self, doc: DocxDocument):
        self._levels: dict[tuple[int, int], dict] = {}
        self._counters: dict[tuple[int, int], int] = {}
        self._loaded = False
        try:
            numbering_part = doc.part.numbering_part
        except Exception:  # noqa: BLE001
            numbering_part = None
        if numbering_part is None:
            return
        root = numbering_part.element

        abstract_defs: dict[str, dict[int, dict]] = {}
        for abstract_num in root.findall(qn("w:abstractNum")):
            abstract_id = abstract_num.get(qn("w:abstractNumId"))
            levels = {}
            for lvl in abstract_num.findall(qn("w:lvl")):
                ilvl = int(lvl.get(qn("w:ilvl")))
                num_fmt_el = lvl.find(qn("w:numFmt"))
                lvl_text_el = lvl.find(qn("w:lvlText"))
                start_el = lvl.find(qn("w:start"))
                levels[ilvl] = {
                    "numFmt": num_fmt_el.get(qn("w:val")) if num_fmt_el is not None else "decimal",
                    "lvlText": lvl_text_el.get(qn("w:val")) if lvl_text_el is not None else "%1.",
                    "start": int(start_el.get(qn("w:val"))) if start_el is not None else 1,
                }
            abstract_defs[abstract_id] = levels

        num_to_abstract: dict[int, str] = {}
        for num in root.findall(qn("w:num")):
            num_id = int(num.get(qn("w:numId")))
            abstract_id_el = num.find(qn("w:abstractNumId"))
            if abstract_id_el is not None:
                num_to_abstract[num_id] = abstract_id_el.get(qn("w:val"))

        for num_id, abstract_id in num_to_abstract.items():
            for ilvl, level_def in abstract_defs.get(abstract_id, {}).items():
                self._levels[(num_id, ilvl)] = level_def

        self._loaded = True

    @staticmethod
    def _find_num_pr(pPr: BaseOxmlElement) -> tuple[int, int] | object | None:
        """Returns (numId, ilvl), None (no numPr present), or the
        _NUMPR_REMOVED sentinel (numPr present with numId=0, i.e. numbering
        explicitly turned off for this paragraph/style)."""

        if pPr is None:
            return None
        num_pr = pPr.find(qn("w:numPr"))
        if num_pr is None:
            return None
        num_id_el = num_pr.find(qn("w:numId"))
        ilvl_el = num_pr.find(qn("w:ilvl"))
        if num_id_el is None:
            return None
        num_id = int(num_id_el.get(qn("w:val")))
        if num_id == 0:
            return _NUMPR_REMOVED
        ilvl = int(ilvl_el.get(qn("w:val"))) if ilvl_el is not None else 0
        return num_id, ilvl

    def get_num_pr(self, paragraph: Paragraph) -> tuple[int, int] | None:
        """Check the paragraph, then walk its style chain looking for numPr.

        An explicit numId=0 (numbering removed) at any point in the chain
        stops the search immediately and returns None — it must NOT fall
        through to a numPr inherited from a base style, since that would
        silently reinstate the numbering the paragraph opted out of.
        """

        direct = self._find_num_pr(paragraph._p.pPr)  # type: ignore
        if direct is _NUMPR_REMOVED:
            return None
        if direct is not None:
            return direct

        style = paragraph.style
        seen_ids: set[str] = set()

        while style is not None and style.style_id not in seen_ids:
            seen_ids.add(style.style_id)

            style_el = getattr(style, "element", None)
            style_pPr = cast(
                CT_PPr | None,
                getattr(style_el, "pPr", None),
            )

            from_style = self._find_num_pr(style_pPr)  # type: ignore
            if from_style is _NUMPR_REMOVED:
                return None
            if from_style is not None:
                return from_style

            style = cast(Any, getattr(style, "base_style", None))

        return None

    def render(self, num_id: int, ilvl: int) -> tuple[str, str]:
        level_def = self._levels.get((num_id, ilvl))
        if level_def is None:
            return "Numbering", ""

        num_fmt = level_def["numFmt"]
        kind, _ = _NUM_FMT_KIND.get(num_fmt, ("decimal", False))

        if kind == "bullet":
            lvl_text = level_def["lvlText"] or "\u2022"
            return "Bullet", lvl_text

        key = (num_id, ilvl)
        self._counters[key] = self._counters.get(key, level_def["start"] - 1) + 1
        for nid, lvl2 in list(self._counters.keys()):
            if nid == num_id and lvl2 > ilvl:
                del self._counters[(nid, lvl2)]

        lvl_text = level_def["lvlText"] or "%1."
        rendered = lvl_text
        for place in range(1, 10):
            token = f"%{place}"
            if token not in rendered:
                continue
            target_key = (num_id, place - 1)
            count = self._counters.get(target_key, self._levels.get(target_key, {}).get("start", 1))
            target_def = self._levels.get(target_key, level_def)
            t_kind, t_upper = _NUM_FMT_KIND.get(target_def["numFmt"], ("decimal", False))
            if t_kind == "roman":
                val = _to_roman(count)
                val = val.upper() if t_upper else val
            elif t_kind == "alpha":
                val = _to_alpha(count)
                val = val.upper() if t_upper else val
            else:
                val = str(count)
            rendered = rendered.replace(token, val)

        return "Numbering", rendered


def _get_outline_level(paragraph: Paragraph) -> int:
    """Reads w:outlineLvl directly; body text (val=9) maps to 10."""

    pPr = paragraph._p.pPr
    if pPr is not None:
        outline_el = pPr.find(qn("w:outlineLvl"))
        if outline_el is not None:
            val = outline_el.get(qn("w:val"))
            if val is not None:
                return int(val) + 1
    return 10


def _len_to_pt(length: Length | None) -> float:
    return round(length.pt, 2) if length is not None else 0.0


def _aggregate_run_bool(runs: list[Run], getter: Callable[[Run], bool | None]) -> bool | None:
    values = {getter(r) for r in runs if getter(r) is not None}
    if not values:
        return None
    if len(values) > 1:
        return None
    return values.pop()


def _aggregate_run_value(runs: list[Run], getter: Callable[[Run], T | None]) -> T | None:
    values = {getter(r) for r in runs}
    values.discard(None)
    if len(values) == 1:
        return values.pop()
    return None


def _has_hyperlink(paragraph: Paragraph) -> bool:

    return paragraph._p.find(qn("w:hyperlink")) is not None


def _font_color_hex(run: Run) -> str | None:

    try:
        color = run.font.color
        if color is not None and color.type is not None and color.rgb is not None:
            return str(color.rgb)
    except Exception:  # noqa: BLE001
        # python-docx raises for a handful of legitimate cases (e.g. a color
        # defined by theme reference rather than explicit RGB) — that's not
        # actionable, but swallowing it with no trace at all makes real bugs
        # here invisible. Log at debug level and treat as "no explicit color".
        logger.debug("Could not read font color for run %r", run.text, exc_info=True)
    return None


def extract_paragraph(paragraph: Paragraph, idx: int, resolver: NumberingResolver) -> ParaMeta:

    text = paragraph.text
    stripped = text.strip()
    words = stripped.split()

    pf = paragraph.paragraph_format
    style = paragraph.style
    runs = paragraph.runs

    alignment_val = pf.alignment
    alignment_name = ALIGNMENT_NAMES.get(alignment_val, "Left")

    is_bold = _aggregate_run_bool(runs, lambda r: r.bold)
    is_italic = _aggregate_run_bool(runs, lambda r: r.italic)
    is_underline = _aggregate_run_bool(runs, lambda r: bool(r.underline) if r.underline is not None else None)
    underline_str = "Single" if is_underline is True else ("None" if is_underline is False else "Mixed")
    is_all_caps = _aggregate_run_bool(runs, lambda r: r.font.all_caps)

    font_name = _aggregate_run_value(runs, lambda r: r.font.name)
    font_size = _aggregate_run_value(runs, lambda r: r.font.size.pt if r.font.size else None)
    font_color = _aggregate_run_value(runs, _font_color_hex)

    num_pr = resolver.get_num_pr(paragraph)
    if num_pr is not None:
        num_id, ilvl = num_pr
        list_type, list_value = resolver.render(num_id, ilvl)
        is_list = True
    else:
        num_id, ilvl, list_type, list_value = None, None, "None", ""
        is_list = False

    line_spacing_rule = pf.line_spacing_rule
    line_spacing_rule_name = LINE_SPACING_RULE_NAMES.get(int(line_spacing_rule) if line_spacing_rule is not None else None, None)

    return ParaMeta(
        paragraphIndex=idx,
        text=text,
        wordCount=len(words),
        charCount=len(text),
        charCountNoSpaces=len(text.replace(" ", "").replace("\t", "")),
        alignment=alignment_name,
        styleName=style.name if style else "",
        styleId=style.style_id if style else None,
        leftIndent=_len_to_pt(pf.left_indent),
        rightIndent=_len_to_pt(pf.right_indent),
        firstLineIndent=_len_to_pt(pf.first_line_indent),
        outlineLevel=_get_outline_level(paragraph),
        spaceBefore=_len_to_pt(pf.space_before),
        spaceAfter=_len_to_pt(pf.space_after),
        lineSpacing=round(pf.line_spacing, 2) if isinstance(pf.line_spacing, float) else None,
        lineSpacingRule=line_spacing_rule_name,
        isBold=is_bold,
        isItalic=is_italic,
        isUnderline=is_underline,
        underline=underline_str,
        isAllCaps=is_all_caps,
        fontName=font_name,
        fontSizePt=round(font_size, 1) if font_size else None,
        fontColorHex=font_color,
        runCount=len(runs),
        hasHyperlink=_has_hyperlink(paragraph),
        isListNumbering=is_list,
        listType=list_type,
        numId=num_id,
        listLevel=ilvl,
        listNumberValue=list_value,
        pageBreakBefore=bool(pf.page_break_before),
        keepWithNext=bool(pf.keep_with_next),
        keepTogether=bool(pf.keep_together),
        widowControl=pf.widow_control,
        tabCount=text.count("\t"),
    )


def parse_docx(path: str) -> list[ParaMeta]:
    """Extracts metadata for every non-empty paragraph in the file."""

    d = docx.Document(path)
    resolver = NumberingResolver(d)
    result = []
    for i, p in enumerate(d.paragraphs):
        if not p.text.strip():
            continue
        result.append(extract_paragraph(p, i, resolver))
    return result


def to_json(paras: list[ParaMeta]) -> str:
    return json.dumps([asdict(p) for p in paras], indent=2, ensure_ascii=False)
