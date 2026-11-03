import hashlib
import json
import os
import re
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.core.container import get_bedrock_model

# Each pattern's FIRST capture group is the marker exactly as it appears in
# the source text (including its own punctuation/brackets — "1.", "(a)",
# "☐" — not just the bare token). We rebuild the clause name from this
# captured text verbatim so a checkbox item doesn't get relabeled as if it
# were numbered, and "(a)" doesn't get flattened into "a.".
DEFAULT_MARKER_PATTERNS = [
    re.compile(r"^(\d+\.)\s+(.*)$"),
    re.compile(r"^([A-Za-z]\.)\s+(.*)$"),
    re.compile(r"^([IVXLCivxlc]+\.)\s+(.*)$"),
    re.compile(r"^(\([A-Za-z0-9]+\))\s*[-\u2013]?\s*(.*)$"),
    re.compile(r"^(\u2610|\u2611|\u25a1)\s*[-\u2013]?\s*(.*)$"),
]

# Lazily constructed so importing this module (e.g. for tests/tooling) never
# requires AWS/Bedrock credentials just to exist. Call _get_model() instead
# of touching a module-level global directly.
_model = None


def _get_model():
    global _model
    if _model is None:
        _model = get_bedrock_model()
    return _model


def _promote_unmarked_title_children(clauses: list) -> list:
    """Collapse a single bare, unmarked, textless opening title node so its
    children become top-level siblings instead of nesting under it.

    This applies ONLY to clauses[0] at the root of the document — the "very
    first structural paragraph" the system prompt describes — and is
    deliberately NOT recursive and NOT applied to every unmarked/empty node
    anywhere in the tree. That distinction matters: an Article/Section
    heading such as "ARTICLE 1 — DEFINITIONS" is also bold, unmarked, and
    textless, with 1.1/1.2 nested under it — and it MUST stay a real parent,
    not get flattened away. Only the document's opening title (position 0,
    nothing else precedes it) is eligible for this collapse.
    """
    if not clauses:
        return clauses
    first = clauses[0]
    if not first.get("_marked") and not first["text"] and first["child_clauses"]:
        children = first["child_clauses"]
        return children + clauses[1:]
    return clauses


def _expose_marker_field(clauses: list) -> list:
    """Rename the internal bookkeeping flag to a public field, recursively,
    so downstream consumers can see which nodes opened via an actual
    enumerated marker vs. a bare heading with no marker at all — without
    this pipeline asserting what an unmarked node IS (title, recital,
    signature block, exhibit, schedule, etc.). That varies by document and
    is left to the consumer to interpret however fits their use case."""
    for node in clauses:
        node["has_marker"] = node.pop("_marked", False)
        _expose_marker_field(node["child_clauses"])
    return clauses


def split_name_and_body(text: str, max_name_words: int = 8) -> tuple[str, str]:
    """Split 'Label. Rest of the text' into (label, rest)."""

    stripped = text.strip()
    if not stripped:
        return "", ""
    best = None
    for sep in (". ", ": "):
        idx = stripped.find(sep)
        if idx == -1:
            continue
        if best is None or idx < best[0]:
            best = (idx, sep)
    if best is not None:
        idx, sep = best
        candidate_name = stripped[: idx + 1].strip()
        rest = stripped[idx + 1 :].strip()
        if len(candidate_name.rstrip(".:").split()) <= max_name_words and rest:
            return candidate_name, rest
    if stripped.endswith((".", ":")) and len(stripped.rstrip(".:").split()) <= max_name_words:
        return stripped, ""
    return "", stripped


def match_marker(text: str, marker_patterns: list = DEFAULT_MARKER_PATTERNS) -> tuple[str, str] | None:
    """Returns (marker_as_written, rest_of_text) or None. The marker is the
    literal matched text (e.g. "1.", "(a)", "☐"), not a normalized form —
    nesting depth is decided by the LLM's plan, not by which pattern matched,
    so there is nothing else useful to return here."""
    stripped = text.strip()
    for pattern in marker_patterns:
        m = pattern.match(stripped)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None


def dedupe_name(name: str, siblings: list) -> str:
    count = sum(1 for s in siblings if s["clause_name"] == name or s["clause_name"].startswith(name + "-"))
    return name if count == 0 else f"{name}-{count + 1}"


def node_name_and_body(row: dict) -> tuple[str, str, bool]:
    """Given a paragraph the LLM flagged as starting a new node, split off its
    marker/label deterministically. Returns (name, body, has_marker).

    has_marker is True only when this node genuinely opened because of an
    enumerated marker — either literal text ("1.", "(a)", ...) OR Word's own
    auto-numbering (isListNumbering + listNumberValue, when the number itself
    isn't part of paragraph.text). False means it's a bare heading/title with
    no marker at all, which is what makes it eligible for title-flattening.

    The marker text is preserved exactly as written (see match_marker) so a
    "(a)" clause is never relabeled as "a." and a checkbox item keeps its
    glyph rather than being renumbered into a decimal-style name.
    """

    text = row["text"]

    matched = match_marker(text)
    if matched:
        marker, rest = matched
        label, body = split_name_and_body(rest)
        name = f"{marker} {label}" if label else marker
        return name, (body if label else rest), True

    # No inline textual marker — but Word's native numbered-list formatting
    # can still make this an enumerated clause; the "2." / "(a)" you'd see
    # on screen is rendered from metadata, not present in the paragraph text.
    list_marker = (row.get("listNumberValue") or "").strip() if row.get("isListNumbering") else ""
    if list_marker:
        label, body = split_name_and_body(text)
        name = f"{list_marker} {label}".strip() if label else list_marker
        return name, (body if label else text), True

    label, body = split_name_and_body(text)
    if not label and len(text.split()) <= 12:
        return text.strip(), "", False
    return label, body, False


class NewClauseParagraph(BaseModel):
    idx: int = Field(description="The idx value (0-based, from the idx column of the paragraph table) of the paragraph where a new clause or heading starts.")
    depth: int = Field(
        description="Nesting depth this new node opens at: 0 = top-level, 1 = nested one level under whichever node is currently open at depth 0, 2 = nested under whichever is open at depth 1, etc."
    )


class DocumentPlan(BaseModel):
    new_clause_paragraphs: list[NewClauseParagraph] = Field(
        description="One entry per paragraph (in idx order) that STARTS a new clause/heading node. "
        "Do NOT include paragraphs that merely continue the text of whatever clause is already open — "
        "those are implied by omission and their text is appended automatically."
    )


SYSTEM_MESSAGE = """You classify paragraphs of a legal/business document by structural role — you \
do NOT retype any paragraph's text.

You will see a table of paragraphs (idx, text, and formatting metadata — some columns may be \
omitted entirely if that signal never appears anywhere in this document). For each paragraph, \
decide: does it START a new clause or heading, and if so, at what nesting depth?

Rules:
- A paragraph is a HEADING if it is bold and its font size is larger than the surrounding body \
text, OR if it is bold, centered, short (<=12 words), and has no leading marker. Headings whose \
bold font size is larger open a shallower (more outer) depth than headings with a smaller bold \
font size — rank distinct bold sizes to get the heading hierarchy, largest = depth 0.
- A paragraph STARTS A NEW CLAUSE if it begins with a marker: "1.", "A.", "I."/roman numerals, \
"(a)"/"(1)"/similar parenthetical, a checkbox glyph, or Word list-numbering fields \
(isListNumbering + listNumberValue, nested by listLevel).
- Nested numeric markers like "1.1", "1.2" appearing INSIDE a paragraph whose marker is "1." are \
still part of that same paragraph's text (don't split paragraphs) — only classify at the \
paragraph level. If "1.1" and "1.2" are their OWN separate paragraphs in the table, they nest one \
depth deeper than "1.".
- A marked clause nests one depth deeper than whatever heading/marker is currently open above it. \
If nothing is open yet, it's depth 0.
- A plain paragraph with no heading and no marker is a CONTINUATION — do not list it. Its text \
will be appended to whichever clause is currently open at the deepest depth.
- Depth is about NESTING, not document position: two headings at the same depth (e.g. two \
top-level sections) both get depth 0, even though the second one closes out everything the first \
one had open.
- If the very first structural paragraph is a TITLE that simply names the overall
  document/agreement (appears once, at the top, before any numbered/lettered clauses,
  and nothing else in the document nests under a similar bare heading), do NOT treat
  it as something clauses nest under. Classify the numbered/lettered clauses that
  follow it at the depth they'd have if the title weren't there — normally depth 0 —
  not one level deeper.
- Note for context only (this does not change how you classify): a document's
  opening title/preamble/recitals and any closing signature/execution block are
  structurally different from the numbered or lettered operative clauses in
  between — but their exact form varies a lot by document (recitals, definitions,
  exhibits, schedules, notary blocks, etc.), so there's no fixed category list.
  You should still classify every paragraph normally per the rules above — heading
  vs. marked clause vs. continuation, at whatever depth applies. The pipeline
  exposes, per node, whether it carries an enumerated marker or not; it is the
  consumer's job to interpret what an unmarked node represents, not yours.

List every new-clause-paragraph idx exactly once, in ascending idx order. Return only the \
structured DocumentPlan — no commentary, no paragraph text."""


TOON_COLUMNS = (
    "idx",
    "text",
    "isBold",
    "fontSizePt",
    "alignment",
    "isListNumbering",
    "listNumberValue",
    "listLevel",
)

ALWAYS_KEEP_COLUMNS = ("idx", "text")


def compact_paragraphs(paras: list[dict]) -> list[dict]:
    """Strip to the fields that actually drive nesting, to cut tokens/latency."""

    rows = []
    for p in paras:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        row = {k: p.get(k) for k in TOON_COLUMNS}
        row["text"] = text
        rows.append(row)
    for i, row in enumerate(rows):
        row["idx"] = i
        for k in TOON_COLUMNS:
            if row.get(k) is None:
                row[k] = ""
    return rows


_MAX_PROMPT_WORDS = 40  # tune based on your longest real headings/markers


def _truncate_for_prompt(text: str, max_words: int = _MAX_PROMPT_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " …"


def rows_for_prompt(rows: list[dict]) -> list[dict]:
    """Same rows as compact_paragraphs(), but with long paragraph text
    truncated. The classifier only needs enough of each paragraph to see
    a marker/heading signal — it doesn't need the full boilerplate body,
    and long continuation paragraphs are the biggest token-count offenders.
    NOTE: only use this for building the prompt. _assemble_tree() must
    keep operating on the untruncated `rows`, since that's where the real
    clause text comes from."""
    out = []
    for row in rows:
        r = dict(row)
        r["text"] = _truncate_for_prompt(row["text"])
        out.append(r)
    return out


def _is_blank(value: Any) -> bool:
    return value in ("", None, False)


def prune_uniform_columns(rows: list[dict], columns: tuple = TOON_COLUMNS, always_keep: tuple = ALWAYS_KEEP_COLUMNS) -> tuple:
    """Drop columns that are blank for EVERY row in this document."""

    kept = []
    for col in columns:
        if col in always_keep or any(not _is_blank(row.get(col)) for row in rows):
            kept.append(col)
    return tuple(kept)


def _toon_scalar(value: Any) -> str:
    if isinstance(value, bool):
        s = "true" if value else "false"
    elif value is None:
        s = ""
    else:
        s = str(value)
    needs_quote = s == "" or any(c in s for c in (",", ":", "\n", '"')) or s != s.strip()
    if needs_quote:
        return '"' + s.replace('"', '""') + '"'
    return s


def to_toon(rows: list[dict], columns: tuple, array_name: str = "paragraphs") -> str:
    """Encode a list of uniform dicts as TOON: one header row declaring the array length and column names, then one comma-delimited row per record."""

    header = f"{array_name}[{len(rows)}]{{{','.join(columns)}}}:"
    lines = [header]
    for row in rows:
        lines.append("  " + ",".join(_toon_scalar(row.get(c, "")) for c in columns))
    return "\n".join(lines)


TOON_FORMAT_NOTE = """The paragraphs below are encoded in TOON (Token-Oriented Object Notation), \
not JSON: the first line "name[N]{col1,col2,...}:" declares the array length and column names, \
and each following indented line is one row of comma-separated values in that same column order. \
A value is double-quoted only if it contains a comma, colon, quote, or a leading/trailing space; \
unquote it to read the real value. An empty value means that field was absent/False for that \
paragraph. Some formatting columns may be omitted from the header entirely if that signal never \
occurs anywhere in this document — treat an omitted column the same as an empty value everywhere."""


def _assemble_tree(rows: list[dict], plan: DocumentPlan) -> dict:
    """Rebuild the nested clause tree from the LLM's flat structural plan, slicing real paragraph text locally."""

    new_node_at = {e.idx: max(e.depth, 0) for e in plan.new_clause_paragraphs}

    clauses: list = []
    stack: list = []

    for row in rows:
        idx = row["idx"]
        text = row["text"]

        if idx not in new_node_at:
            target = stack[-1] if stack else (clauses[-1] if clauses else None)
            if target is None:
                # Nothing has opened yet — this paragraph is front-matter that
                # precedes the document's first real heading/clause (letterhead,
                # address/phone/email blocks, logos-as-text, etc.). It isn't
                # clause content, so drop it rather than emitting an anonymous
                # clause with an empty name to hold it.
                continue
            target["text"] = (target["text"] + " " + text).strip()
            continue

        depth = new_node_at[idx]
        name, body, has_marker = node_name_and_body(row)
        node = {"clause_name": name, "text": body, "child_clauses": [], "_marked": has_marker}

        stack[:] = stack[:depth]
        parent = stack[-1] if stack else None
        if parent is None:
            node["clause_name"] = dedupe_name(node["clause_name"], clauses)
            clauses.append(node)
        else:
            node["clause_name"] = dedupe_name(node["clause_name"], parent["child_clauses"])
            parent["child_clauses"].append(node)
        stack.append(node)

    promoted = _promote_unmarked_title_children(clauses)
    return {"clauses": _expose_marker_field(promoted)}


async def build_clause_tree_llm(paras: list[dict]) -> dict:

    model = _get_model()

    rows = compact_paragraphs(paras)  # full text — kept for _assemble_tree below
    prompt_rows = rows_for_prompt(rows)  # truncated — only for the LLM prompt
    columns = prune_uniform_columns(prompt_rows, TOON_COLUMNS)

    prompt = TOON_FORMAT_NOTE + "\n\nParagraphs (in document order):\n" + to_toon(prompt_rows, columns) + "\n\nClassify which paragraphs start a new clause/heading, and at what depth."

    plan: DocumentPlan = await model.generate(
        prompt=prompt,
        context={},
        response_model=DocumentPlan,  # type: ignore
        session_id=str(uuid.uuid4()),
        system_message=SYSTEM_MESSAGE,
        temperature=0.0,
    )
    return _assemble_tree(rows, plan)


_MEMORY_CACHE_MAX_SIZE = 256
_CACHE: "OrderedDict[str, dict]" = OrderedDict()

# Bump this whenever SYSTEM_MESSAGE, node_name_and_body, or _assemble_tree
# change in a way that would change the output for the same input paragraphs.
# Without this, a prompt/logic fix silently keeps serving stale cached trees
# from before the fix, both in-memory and on disk, with no error to signal it.
_CACHE_VERSION = "v2"


def _memory_cache_get(key: str) -> dict | None:

    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return None


def _memory_cache_set(key: str, value: dict) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    if len(_CACHE) > _MEMORY_CACHE_MAX_SIZE:
        _CACHE.popitem(last=False)


def _hash_paras(paras: list[dict]) -> str:

    blob = _CACHE_VERSION + "\n" + to_toon(compact_paragraphs(paras), TOON_COLUMNS)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _disk_cache_path(cache_dir: str, key: str) -> Path:
    return Path(cache_dir) / f"{key}.json"


async def cached_build_clause_tree(paras: list[dict], cache_dir: str | None = None) -> Any:
    cache_dir = cache_dir or os.environ.get("CLAUSE_TREE_CACHE_DIR")
    key = _hash_paras(paras)

    hit = _memory_cache_get(key)
    if hit is not None:
        return hit

    if cache_dir:
        path = _disk_cache_path(cache_dir, key)
        if path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
            _memory_cache_set(key, result)
            return result

    result = await build_clause_tree_llm(paras)
    _memory_cache_set(key, result)

    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        _disk_cache_path(cache_dir, key).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    return result
