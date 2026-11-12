import asyncio
import hashlib
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Sequence, Set, Tuple

from src.config.logging import get_logger
from src.core.container import get_bedrock_model, get_clause_search
from src.schemas.clause_mapping import ClauseMatch
from src.schemas.general_review import (
    CriterionAnswerRaw,
    ExplanationResponse,
    ExtractionResponse,
    FragmentDefectResponse,
    FragmentFindingRaw,
    GeneralReviewRequest,
    GeneralReviewResponse,
    Suggestion,
    TextInformation,
)
from src.services.criteria_store import display_to_slug, get_criteria, get_criteria_by_slug, get_all_display_names, resolve_agreement_type_id, slug_to_display
from src.services.clause_mapping.bm25_classifier import BM25TaxonomyClassifier
from src.services.general_criteria import FRAGMENT_DEFECT_CRITERIA, GENERAL_CRITERIA
from src.services.scorer import AnswerRecord, ScoringResult, is_risky, score_group

logger = get_logger(__name__)

# Caps parallel Bedrock calls so large documents don't exhaust the client pool.
# Increased to 16 for high-throughput concurrency without rate limits.
MAX_CONCURRENT_CALLS = 16

# Batch budget for grouping: soft limit, group integrity is hard limit.
BATCH_CHAR_BUDGET = 6000

# Minimum clause words to query vector DB
MIN_CLAUSE_WORDS = 10

# Confidence threshold for 2-tier classification
CONFIDENCE_THRESHOLD = 0.70

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "prompts" / "v2" / "general_review"
_EXTRACTION_SYSTEM = (_PROMPTS_DIR / "extraction_system.mustache").read_text(encoding="utf-8")
_EXTRACTION_USER = (_PROMPTS_DIR / "extraction_user.mustache").read_text(encoding="utf-8")
_EXPLANATION_SYSTEM = (_PROMPTS_DIR / "explanation_system.mustache").read_text(encoding="utf-8")
_EXPLANATION_USER = (_PROMPTS_DIR / "explanation_user.mustache").read_text(encoding="utf-8")
_FRAGMENT_SYSTEM = (_PROMPTS_DIR / "fragment_system.mustache").read_text(encoding="utf-8")
_FRAGMENT_USER = (_PROMPTS_DIR / "fragment_user.mustache").read_text(encoding="utf-8")

# Identical documents always return the identical review (LRU, per process).
_CACHE_MAX_ENTRIES = 32
_response_cache: "OrderedDict[str, GeneralReviewResponse]" = OrderedDict()


@dataclass
class ClauseGroup:
    """A cohesive clause unit consisting of 1 or more consecutive paragraphs."""

    para_indices: List[int]
    canonical_type: Optional[str]      # Display name, e.g. "Limitation of Liability"
    canonical_slug: Optional[str]      # Slug, e.g. "limitation-of-liability"
    confidence: float
    is_classified: bool                # True if confidence >= 0.70 and canonical_type is known
    is_fragment: bool                  # True if unclassified short orphan or front-matter
    paragraphs: Dict[int, str]         # {para_index: text}
    unit_id: int = 0                   # 1-based sequential clause unit identifier


def _request_fingerprint(request: GeneralReviewRequest) -> str:
    payload = json.dumps(
        {
            "paragraphs": [[para.paraindetifier, para.text] for para in request.textinformation],
            "context": [request.party_represented, request.review_objective, request.specific_concerns, request.agreement_type],
        },
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[GeneralReviewResponse]:
    cached = _response_cache.get(key)
    if cached is not None:
        _response_cache.move_to_end(key)
    return cached


def _cache_put(key: str, response: GeneralReviewResponse) -> None:
    _response_cache[key] = response
    _response_cache.move_to_end(key)
    while len(_response_cache) > _CACHE_MAX_ENTRIES:
        _response_cache.popitem(last=False)


def _is_general_review_party(party: Optional[str]) -> bool:
    if not party:
        return True
    cleaned = party.strip().casefold()
    return cleaned in {
        "general review",
        "general",
        "none",
        "n/a",
        "na",
        "both",
        "both parties",
        "neutral",
        "general side",
        "neither",
        "unspecified",
        "general review or empty",
    }


def _build_reviewer_context(request: GeneralReviewRequest) -> str:
    """Render the questionnaire answers as the prompt's reviewer-context block.

    Explicitly separates:
    1. Client Advocacy Mode (when a specific party is represented) - carries commanding weight.
    2. Neutral General Review Mode (when party is empty or 'General Review') - objective bilateral review.
    Also ensures review_objective and specific_concerns carry commanding weight.
    """

    party = request.party_represented
    is_general = _is_general_review_party(party)

    sections: List[str] = []

    if is_general:
        sections.append(
            "=== REVIEW PERSPECTIVE: NEUTRAL GENERAL REVIEW ===\n"
            "Mode: NEUTRAL / BILATERAL GENERAL REVIEW (No single party is represented; this is an objective review for general commercial fairness).\n"
            "MANDATORY INSTRUCTIONS FOR NEUTRAL GENERAL REVIEW:\n"
            "- You are NOT representing or favoring either party. Do not adopt a one-sided client bias.\n"
            "- Review the agreement from an objective, neutral commercial perspective to ensure bilateral fairness, mutual balance, and operational clarity.\n"
            "- Flag provisions that are one-sided in favor of either party, unreciprocated, ambiguous, or lacking market-standard protections.\n"
            "- In 'suggested_fix', balance the clause mutually (e.g., make obligations bilateral or insert reciprocal protections) rather than shifting advantage to one side."
        )
    else:
        document_text = " ".join(para.text for para in request.textinformation).casefold()
        if party and party.casefold() not in document_text:
            logger.warning(
                "Party represented %r does not appear in the document; the model will infer which party is the client.",
                party,
            )
        sections.append(
            "=== REVIEW PERSPECTIVE: CLIENT ADVOCACY REVIEW (COMMANDING WEIGHT) ===\n"
            f"Party Represented: {party}\n"
            "MANDATORY INSTRUCTIONS FOR CLIENT ADVOCACY REVIEW:\n"
            f"- You are acting as dedicated legal counsel representing {party}.\n"
            f"- This client perspective carries COMMANDING WEIGHT in your analysis, risk assessment, explanation, and redlining.\n"
            f"- Vigorously protect {party}'s legal, operational, and financial interests against one-sided, overreaching, or ambiguous terms.\n"
            f"- In the 'reason', explicitly articulate the practical operational, financial, and legal risk TO {party} by name.\n"
            f"- In the 'suggested_fix', surgically amend the text to shield {party} from liability, eliminate unilateral counterparty discretion, and establish necessary rights and remedies for {party}."
        )

    if request.agreement_type:
        sections.append(f"Agreement Type: {request.agreement_type}")

    if request.review_objective and request.review_objective.strip():
        sections.append(
            "=== PRIMARY REVIEW OBJECTIVE (COMMANDING WEIGHT) ===\n"
            f"Primary Objective: {request.review_objective.strip()}\n"
            "MANDATORY INSTRUCTION: This objective carries commanding weight. Scrutinize every clause through this lens. If a clause affects or frustrates this objective, highlight it in the 'reason' and tailor the 'suggested_fix' to fulfill this objective."
        )

    if request.specific_concerns and request.specific_concerns.strip():
        sections.append(
            "=== SPECIFIC CLIENT CONCERNS (COMMANDING WEIGHT) ===\n"
            f"Specific Concerns: {request.specific_concerns.strip()}\n"
            "MANDATORY INSTRUCTION: These specific concerns carry commanding weight. Give elevated scrutiny to any clause touching upon these topics. The 'reason' must explicitly explain how the clause impacts these concerns, and the 'suggested_fix' must directly resolve and mitigate them."
        )

    return "\n\n".join(sections) + "\n\n"


# Character families the model tends to normalize while copying; each matches all variants.
_APOSTROPHES = "'’‘"
_QUOTES = '"“”'
_DASHES = "-–—"


def _tolerant_pattern(needle: str) -> str:
    """Regex matching needle while tolerating whitespace runs and quote/dash variants."""

    tokens = []
    for token in needle.split():
        chars = []
        for char in token:
            if char in _APOSTROPHES:
                chars.append(f"[{_APOSTROPHES}]")
            elif char in _QUOTES:
                chars.append(f"[{_QUOTES}]")
            elif char in _DASHES:
                chars.append(f"[{re.escape(_DASHES)}]")
            else:
                chars.append(re.escape(char))
        tokens.append("".join(chars))
    return r"\s+".join(tokens)


def _ground(original_text: str, candidates: Sequence[Tuple[int, TextInformation]]) -> Optional[Tuple[int, TextInformation, str]]:
    """Find original_text inside candidate paragraphs and return (position, paragraph, exact source text)."""

    needle = original_text.strip()
    if not needle:
        return None

    for idx, para in candidates:
        if needle in para.text:
            return idx, para, needle

    pattern = _tolerant_pattern(needle)
    for idx, para in candidates:
        match = re.search(pattern, para.text)
        if match:
            return idx, para, match.group(0)
    return None


def _expand_to_paragraph(suggestion_fix: str, para: TextInformation, exact_text: str) -> Tuple[str, str]:
    """Return (original, fix) covering the complete paragraph, splicing a partial fix if needed."""

    if exact_text.strip() == para.text.strip():
        return para.text, suggestion_fix

    span = para.text.find(exact_text)
    if span == -1:
        return para.text, suggestion_fix

    prefix = para.text[:span]
    suffix = para.text[span + len(exact_text):]

    already_full = (len(prefix.strip()) < 20 or prefix.strip()[:30] in suggestion_fix) and (len(suffix.strip()) < 20 or suffix.strip()[-30:] in suggestion_fix)
    full_fix = suggestion_fix if already_full else prefix + suggestion_fix + suffix
    return para.text, full_fix


_CONNECTORS = {
    "the", "such", "this", "these", "any", "all", "each", "in", "for", "if",
    "provided", "subject", "pursuant", "notwithstanding"
}
_OBLIGATION_VERBS = {
    "shall", "will", "must", "agrees", "agree", "warrants", "warrant",
    "represents", "represent", "indemnifies", "indemnify", "covenants",
    "covenant", "undertakes", "undertake"
}


def _is_fragment(text: str) -> bool:
    """Heuristic to detect orphaned text fragments or continuation paragraphs."""

    words = text.split()
    if len(words) < 25:
        return True
    lower_words = {w.lower().strip(".,;:()\"'“”‘’[]") for w in words}
    if not any(v in lower_words for v in _OBLIGATION_VERBS):
        return True
    return False


# ---------------------------------------------------------------------------
# Universal Structural Boundary Patterns (Supporting Any Numbering Scheme)
# ---------------------------------------------------------------------------
_ROMAN_NUMERALS = r'(?:X[CL]|L?X{0,3})(?:I[XV]|V?I{0,3})'

_EXECUTION_BLOCK_START = re.compile(
    r'^\s*(?:IN\s+WITNESS\s+WHEREOF|(?:EXECUTION|SIGNATURE)\s+(?:PAGE|BLOCK|SECTION)|The\s+Parties\s+have\s+signed)',
    re.IGNORECASE
)

_BACK_MATTER_START = re.compile(
    r'^\s*(?:ANNEX|SCHEDULE|EXHIBIT|APPENDIX|ATTACHMENT|LIST\s+OF\s+SCHEDULES)\b',
    re.IGNORECASE
)

_SECTION_BOUNDARY_PATTERNS = [
    # Explicit prefix: "Section 1", "Article IV", "Clause 9", "Part 2", "Schedule A", "Annex A"
    re.compile(r'^\s*(?:Section|Article|Clause|Part|Schedule|Annex|Exhibit)\s+(?:\d+|[A-Z]+|\b' + _ROMAN_NUMERALS + r'\b)[.:\s]', re.IGNORECASE),
    # Numbered top-level: "1. DEFINITIONS", "13. LIMITATION OF LIABILITY"
    re.compile(r'^\s*\d{1,3}[.)]\s+[A-Z]'),
    # Roman top-level: "I. DEFINITIONS", "XIV. TERMINATION"
    re.compile(r'^\s*\b' + _ROMAN_NUMERALS + r'\b[.)]\s+[A-Z]'),
    # Alpha top-level: "A. DEFINITIONS", "B. SERVICES"
    re.compile(r'^\s*[A-Z][.)]\s+[A-Z]'),
    # Background / Recitals marker
    re.compile(r'^\s*(?:BACKGROUND|RECITALS|WITNESSETH)[.:\s]*$', re.IGNORECASE),
    # Signature block start: "IN WITNESS WHEREOF"
    _EXECUTION_BLOCK_START,
    # Annex / Schedule start
    _BACK_MATTER_START,
]

_WORDS_OF_AGREEMENT = re.compile(
    r'(?:NOW,?\s+THEREFORE|IT\s+IS\s+(?:HEREBY\s+)?AGREED|WHEREBY\s+IT\s+IS\s+AGREED|THE\s+PARTIES\s+(?:HEREBY\s+)?AGREE|IN\s+CONSIDERATION\s+OF.+?AGREE)',
    re.IGNORECASE
)

# Sub-clause boundary pattern: "8.1", "31.2.1", "15.3:" etc.
# Used to split oversized article groups into deterministic evaluation units.
_SUB_CLAUSE_PATTERN = re.compile(r'^\s*\d{1,3}\.\d{1,3}(?:\.\d{1,3})?[.:\s\t]')

# ---------------------------------------------------------------------------
# Fix 2: Data artefact pre-filter
# Matches paragraphs that are purely numeric/unit table data, e.g. "34.2 Mb"
# or "4.18 Mb". These should never reach the LLM.
# ---------------------------------------------------------------------------
_DATA_ARTEFACT_PATTERN = re.compile(
    r"""^\s*                      # optional leading whitespace
        (?:                        # one or more of:
            (?:\d{1,6}(?:[.,]\d+)?)  # integer or decimal number
            |(?:[A-Z]{1,3}\d{1,2})   # codes like "A5", "B2" (table row labels)
        )
        (?:\s+(?:\d{1,6}(?:[.,]\d+)?|[A-Z]{1,3}\d{1,2}))*  # more numbers/codes
        (?:\s+(?:Mb|MB|KB|GB|TB|PB|ms|Mbps|Gbps|%|bps|hrs?|days?|months?))?  # optional unit
        \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_data_artefact(text: str) -> bool:
    """Return True for pure numeric/unit table data that should never be evaluated."""
    stripped = text.strip()
    if not stripped:
        return False
    # Must be very short (table cell length) and match the pattern
    if len(stripped) > 20:
        return False
    return bool(_DATA_ARTEFACT_PATTERN.match(stripped))

# Maximum paragraphs in one ClauseGroup before we split at sub-clause boundaries.
_MAX_GROUP_PARAGRAPHS = 8


def _is_major_section_boundary(text: str, prev_text: str = "") -> bool:
    """Detect whether a paragraph begins a new major top-level section across any numbering scheme."""

    stripped = text.strip()
    if not stripped:
        return False
    first_line = stripped.split("\n")[0].strip()
    words = first_line.split()
    if not words:
        return False

    # Guard 1: If preceding paragraph ended with a colon ':', this is an enumerated list item, NOT a section
    if prev_text and prev_text.strip().endswith(":"):
        return False

    # Tier 1: Explicit pattern matches (numbered 1., Section 1, Article IV, etc.)
    for pat in _SECTION_BOUNDARY_PATTERNS:
        if pat.match(first_line):
            return True

    # Standalone short all-caps heading paragraph: e.g. "CONFIDENTIALITY", "LIMITATION OF LIABILITY"
    if first_line.isupper() and 1 <= len(words) <= 8 and 3 <= len(first_line) <= 80:
        if any(c.isalpha() for c in first_line):
            return True

    # Guard 2: If preceding paragraph ended mid-sentence without terminal punctuation, unnumbered lines cannot break
    if prev_text:
        prev_stripped = prev_text.strip()
        if prev_stripped and prev_stripped[-1] not in ".!?;\"'”’":
            return False

    clean = first_line.rstrip(".:;").strip()
    clean_words = clean.split()
    if not clean_words or not clean_words[0][0].isupper():
        return False

    # Tier 2: Taxonomy match (e.g. "Fees and Billing", "Term and Termination", "Limitations of Liability")
    if 1 <= len(clean_words) <= 7 and len(clean) <= 60:
        if _looks_like_title(clean) and _match_heading_to_taxonomy(clean) is not None:
            return True

    # Tier 3: Standalone title-like paragraph
    if len(stripped.split("\n")) <= 2 and 1 <= len(clean_words) <= 7 and len(first_line) <= 60:
        if not any(w.lower().strip(".:;,'\"“”‘’") in _TITLE_VERBS for w in clean_words):
            if _looks_like_title(clean):
                return True

    return False


_HEADING_MAX_CHARS = 80
_HEADING_MAX_WORDS = 10
_TITLE_NUMBER_PREFIX_RE = re.compile(
    r"^(?:(?:section|article|clause|part)\s+)?(?:(?:\d+|[A-Z])(?:\.\d+)*[.)]?\s+|(?:\([0-9a-zA-Z]+\)\s*)+)",
    re.IGNORECASE
)
_INLINE_TITLE_RE = re.compile(r"^([^.:]{1,70})[.:](?:\s+|$)")
_DEFINITION_TITLE_RE = re.compile(
    r'^[“"\'‘]([^”"\'’]{1,60})[”"\'’](?:\s+or\s+[“"\'‘][^”"\'’]{1,60}[”"\'’])?\s+(?:means|shall mean|has the meaning)',
    re.IGNORECASE
)
_TITLE_STOP_WORDS = {"a", "an", "and", "by", "for", "in", "of", "on", "or", "the", "to", "with", "&"}
_TITLE_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "co"}
_ADDRESS_WORDS = {"street", "avenue", "way", "suite", "road", "boulevard", "drive", "lane", "box", "floor"}
_TITLE_VERBS = {
    "shall", "will", "may", "must", "should", "can", "cannot", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "does", "do", "did", "not", "hereby", "means", "agrees", "agree", "includes",
    "include", "bears", "pays", "provides", "grants", "retains", "acknowledges", "represents", "warrants"
}


def _is_heading_text(text: str) -> bool:
    stripped = text.strip()
    words = stripped.split()
    return 0 < len(stripped) <= _HEADING_MAX_CHARS and 0 < len(words) <= _HEADING_MAX_WORDS


def _looks_like_title(segment: str) -> bool:
    if "." in segment or ":" in segment or "\t" in segment:
        return False
    if re.search(r",\s*[A-Z]{2}$", segment):
        return False
    words = [word.strip(",;()'\"‘’“”[]") for word in segment.split()]
    words = [word for word in words if word]
    if not words or len(words) > _HEADING_MAX_WORDS:
        return False
    # Allow party-specific lead-ins e.g. "By Company", "By Customer", "By Provider"
    if words[0].lower() == "by" and len(words) <= 3 and len(words) >= 2 and words[1][0].isupper():
        pass
    elif words[0].lower() in _TITLE_STOP_WORDS:
        return False
    if any(w.lower() in _TITLE_COMPANY_SUFFIXES for w in words):
        return False
    if any(w.lower() in _ADDRESS_WORDS for w in words):
        return False
    if any(any(char.isdigit() for char in word) for word in words):
        return False
    if any(w.lower() in _TITLE_VERBS for w in words):
        return False
    clean_seg = "".join(c for c in segment if c.isalpha())
    if clean_seg and clean_seg.isupper():
        return True
    return any(word[0].isupper() for word in words)


def _extract_clause_title(text: str) -> str:
    first_line = text.strip().split("\n")[0].strip()
    stripped = _TITLE_NUMBER_PREFIX_RE.sub("", first_line).strip()
    def_match = _DEFINITION_TITLE_RE.match(stripped) or _DEFINITION_TITLE_RE.match(first_line)
    if def_match:
        return f'“{def_match.group(1).strip()}”'
    if _is_heading_text(stripped):
        candidate = stripped.rstrip(".:;").strip()
        if _looks_like_title(candidate):
            return candidate
    match = _INLINE_TITLE_RE.match(stripped)
    if match:
        cand = match.group(1).strip()
        if _looks_like_title(cand):
            return cand
    match_raw = _INLINE_TITLE_RE.match(first_line)
    if match_raw:
        cand = match_raw.group(1).strip()
        cand_clean = _TITLE_NUMBER_PREFIX_RE.sub("", cand).strip()
        if _looks_like_title(cand_clean):
            return cand_clean
    return ""


def _build_clause_titles(paragraphs: Sequence[TextInformation]) -> Dict[str, str]:
    """Map each paragraph id to its actual heading from the contract.

    Resolves to:
    1. The paragraph's own inline title / sub-heading (e.g. 'Suspension', 'Fees and Expenses', 'Ownership').
    2. Or the enclosing main section heading if the paragraph has no sub-heading of its own (e.g. 'LIMITATION OF LIABILITY').
    3. Or 'Signature Block' once the execution block boundary is encountered.
    """
    titles: Dict[str, str] = {}
    current_section = ""
    in_execution_block = False
    for p in paragraphs:
        text = p.text.strip()
        pid = p.paraindetifier
        if not text:
            continue
        if _is_data_artefact(text):
            continue
        first_line = text.split("\n")[0].strip()

        # Check execution block boundary (IN WITNESS WHEREOF / SIGNATURE PAGE)
        if _EXECUTION_BLOCK_START.search(first_line):
            in_execution_block = True
            current_section = "Signature Block"
            titles[pid] = "Signature Block"
            continue

        if in_execution_block:
            titles[pid] = "Signature Block"
            continue

        own = _extract_clause_title(text)
        if _is_heading_text(first_line) and own and not _DEFINITION_TITLE_RE.match(first_line):
            current_section = own
            titles[pid] = own
        elif own:
            titles[pid] = own
        elif current_section:
            titles[pid] = current_section
    return titles


_HEADING_TAXONOMY_ALIASES: Dict[str, str] = {
    "general provisions": "Miscellaneous / Other Provisions",
    "general": "Miscellaneous / Other Provisions",
    "miscellaneous": "Miscellaneous / Other Provisions",
    "miscellaneous provisions": "Miscellaneous / Other Provisions",
    "boilerplate": "Miscellaneous / Other Provisions",
    "other provisions": "Miscellaneous / Other Provisions",
    "other business activities": "Non-Exclusivity",
    "non exclusivity": "Non-Exclusivity",
    "dispute resolution and governing law": "Governing Law & Dispute Resolution",
    "governing law and dispute resolution": "Governing Law & Dispute Resolution",
    "governing law and jurisdiction": "Governing Law & Dispute Resolution",
    "governing law": "Governing Law & Dispute Resolution",
    "jurisdiction and governing law": "Governing Law & Dispute Resolution",
    "resolving disagreements": "Governing Law & Dispute Resolution",
    "dispute resolution": "Governing Law & Dispute Resolution",
    "security measures and data protection": "Data Protection & Privacy",
    "delivery locations and security": "Data Protection & Privacy",
    "data protection and privacy": "Data Protection & Privacy",
    "data protection": "Data Protection & Privacy",
    "data privacy": "Data Protection & Privacy",
    "client data": "Data Protection & Privacy",
    "subscriptions and service capacity": "Service Levels",
    "service levels": "Service Levels",
    "performance standards": "Service Levels",
    "client feedback surveys": "Service Levels",
    "free services and free pilots": "Scope of Services",
    "the services": "Scope of Services",
    "center transfer right": "Scope of Services",
    "services and provider obligations": "Service Provider Obligations",
    "services and providers obligations": "Service Provider Obligations",
    "provider personnel": "Service Provider Obligations",
    "customer use of the services": "Customer Obligations",
    "customers use of the services": "Customer Obligations",
    "client obligations": "Customer Obligations",
    "third party consents": "Customer Obligations",
    "fees payment and taxes": "Fees & Payment",
    "fees and payment": "Fees & Payment",
    "fees and payment terms": "Fees & Payment",
    "fees": "Fees & Payment",
    "payment": "Fees & Payment",
    "charges and payment": "Fees & Payment",
    "price benchmarking": "Fees & Payment",
    "taxes": "Taxes",
    "duration": "Term & Termination",
    "term and termination": "Term & Termination",
    "suspension and termination": "Term & Termination",
    "duration and termination": "Term & Termination",
    "ending the agreement": "Term & Termination",
    "early exit charges": "Term & Termination",
    "exit support and transition out": "Term & Termination",
    "termination": "Term & Termination",
    "term": "Term & Termination",
    "limitation of liability": "Limitation of Liability",
    "limitations of liability": "Limitation of Liability",
    "limits on liability": "Limitation of Liability",
    "liability": "Limitation of Liability",
    "mutual indemnification": "Indemnification",
    "indemnification": "Indemnification",
    "indemnity": "Indemnification",
    "confidentiality": "Confidentiality",
    "intellectual property": "Intellectual Property",
    "intellectual property rights": "Intellectual Property",
    "proprietary rights and licenses": "Intellectual Property",
    "proprietary rights and licensing": "Intellectual Property",
    "proprietary rights": "Intellectual Property",
    "intellectual property and proprietary rights": "Intellectual Property",
    "intellectual property rights and licenses": "Intellectual Property",
    "intellectual property and licensing": "Intellectual Property",
    "ip ownership and licenses": "Intellectual Property",
    "ip and proprietary rights": "Intellectual Property",
    "representations warranties and disclaimers": "Representations & Warranties",
    "representations and warranties": "Representations & Warranties",
    "ongoing undertakings": "Representations & Warranties",
    "warranties": "Representations & Warranties",
    "force majeure": "Force Majeure",
    "business continuity and unforeseen events": "Force Majeure",
    "definitions": "Definitions & Interpretation",
    "definitions and interpretation": "Definitions & Interpretation",
    "interpretation and defined terms": "Definitions & Interpretation",
    "onboarding acquisitions and divestitures collaboration": "Statements of Work & Work Orders",
    "additional service streams": "Statements of Work & Work Orders",
    "order of precedence": "Order of Precedence",
    "entire agreement": "Entire Agreement",
    "severability": "Severability",
    "assignment": "Assignment",
    "notices": "Notices",
    "insurance": "Insurance",
    "compliance": "Compliance",
    "audit and records": "Audit & Records",
    "records and audits": "Audit & Records",
    "subcontractors": "Subcontractors",
    "governance and change management": "Change Control",
}


def _normalize_heading(text: str) -> str:
    cleaned = re.sub(r'^(?:(?:Section|Article|Clause|Part)\s+)?\d+(?:\.\d+)*[.)]?\s+', '', text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'^[A-Z][.)]\s+', '', cleaned).strip()
    cleaned = re.sub(r'^(?:X[CL]|L?X{0,3})(?:I[XV]|V?I{0,3})[.)]\s+', '', cleaned).strip()
    cleaned = cleaned.replace('&', 'and').replace('’', "'").replace('–', '-').replace('—', '-')
    cleaned = re.sub(r"['’]s\b|['’]", '', cleaned)
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip().lower()


def _match_heading_to_taxonomy(heading_text: str) -> Optional[str]:
    """Match section heading text directly against taxonomy canonical types."""

    all_types = get_all_display_names()

    norm = _normalize_heading(heading_text)

    # 1. Alias dictionary lookup
    if norm in _HEADING_TAXONOMY_ALIASES:
        return _HEADING_TAXONOMY_ALIASES[norm]

    # 2. Exact match against taxonomy types
    for t in all_types:
        if norm == _normalize_heading(t):
            return t

    # 3. BM25 sparse index match
    bm25 = _get_bm25_classifier()
    bm25_match = bm25.get_top_match(norm, threshold=3.5)
    if bm25_match:
        top_name, _ = bm25_match
        if top_name in all_types:
            return top_name

    # 4. Stemmed word overlap match
    def stem(w: str) -> str:
        for suffix in ("ions", "tion", "ing", "ies", "es", "s"):
            if w.endswith(suffix) and len(w) > len(suffix) + 3:
                return w[:-len(suffix)]
        return w

    stop = {"and", "of", "the", "or", "in", "to", "for", "with", "a", "an"}
    h_stems = {stem(w) for w in norm.split() if w not in stop}

    scores = []
    for t in all_types:
        t_norm = _normalize_heading(t)
        t_stems = {stem(w) for w in t_norm.split() if w not in stop}
        if not t_stems:
            continue
        overlap = len(h_stems & t_stems)
        if overlap > 0:
            scores.append((overlap / len(t_stems), overlap, t))

    if scores:
        scores.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if scores[0][0] >= 0.5:
            return scores[0][2]

    return None


_BM25_CLASSIFIER: Optional[BM25TaxonomyClassifier] = None


def _get_bm25_classifier() -> BM25TaxonomyClassifier:
    """Lazy singleton for Okapi BM25 sparse index over criteria taxonomy."""
    global _BM25_CLASSIFIER
    if _BM25_CLASSIFIER is None:
        clf = BM25TaxonomyClassifier()
        try:
            tax_path = Path(__file__).resolve().parent.parent / "services" / "clause_mapping" / "MSA_Criteria_Final_Taxonomy_Calibrated.json"
            if tax_path.exists():
                raw = json.loads(tax_path.read_text(encoding="utf-8"))
                clf.build_from_taxonomy(raw, _HEADING_TAXONOMY_ALIASES)
        except Exception as e:
            logger.warning("Could not build BM25 classifier: %s", e)
        _BM25_CLASSIFIER = clf
    return _BM25_CLASSIFIER


async def _classify_all_paragraphs(
    request: GeneralReviewRequest,
    session_id: str
) -> Dict[int, ClauseMatch]:
    """Query vector DB for operative paragraphs, chunked to avoid Qdrant timeouts.

    Optimized:
    - Pre-filters non-operative zones (front matter, execution block, back matter)
      which are routed to PATH C defect checks and never use vector classification.
      This saves ~50+ expensive remote embedding calls per document.
    - Large documents are chunked into VECTOR_BATCH_SIZE (100) per Qdrant call.
    """
    VECTOR_BATCH_SIZE = 100   # paragraphs per Qdrant batch search call

    search = get_clause_search()
    if search is None:
        logger.warning("No clause search configured; reviewing without vector classifications.")
        return {}

    paragraphs = request.textinformation

    # Pre-filter non-operative zones to save embedding round trips
    front_matter_end = -1
    for i, item in enumerate(paragraphs):
        if _WORDS_OF_AGREEMENT.search(item.text):
            front_matter_end = i
            break

    execution_start = len(paragraphs)
    for i, item in enumerate(paragraphs):
        if _EXECUTION_BLOCK_START.search(item.text):
            execution_start = i
            break

    back_matter_start = len(paragraphs)
    for i, item in enumerate(paragraphs):
        if _BACK_MATTER_START.search(item.text):
            back_matter_start = i
            break

    cutoff_end = min(execution_start, back_matter_start)

    eligible: List[Tuple[int, str]] = [
        (idx, para.text.strip())
        for idx, para in enumerate(paragraphs)
        if len(para.text.split()) >= MIN_CLAUSE_WORDS
        and (front_matter_end == -1 or idx > front_matter_end)
        and idx < cutoff_end
    ]
    if not eligible:
        return {}

    matches: Dict[int, ClauseMatch] = {}

    # Process in chunks so no single Qdrant batch search is too large
    for chunk_start in range(0, len(eligible), VECTOR_BATCH_SIZE):
        chunk = eligible[chunk_start : chunk_start + VECTOR_BATCH_SIZE]
        indices = [idx for idx, _ in chunk]
        texts = [text for _, text in chunk]

        try:
            results = await search.search(texts, request.agreement_type, session_id)
            for idx, hit_list in zip(indices, results):
                if hit_list:
                    best_hit = max(hit_list, key=lambda h: h.score)
                    matches[idx] = best_hit
        except Exception as exc:
            logger.exception(
                "Clause vector search failed for batch starting at eligible[%d]: %s; "
                "affected paragraphs will be reviewed unclassified.",
                chunk_start, exc,
            )
            # Continue with remaining batches — partial classification is better than none

    return matches


def _smooth_classifications(
    raw: Dict[int, ClauseMatch],
    paragraphs: Sequence[TextInformation]
) -> Dict[int, Optional[ClauseMatch]]:
    """Apply the 3 smoothing rules across document paragraphs."""

    n = len(paragraphs)
    smoothed: Dict[int, Optional[ClauseMatch]] = {i: raw.get(i) for i in range(n)}

    # Rule 1: Interior gap fill (X Y X -> X X X if Y.score < 0.70 or Y is None)
    for i in range(1, n - 1):
        prev_m = smoothed[i - 1]
        next_m = smoothed[i + 1]
        curr_m = smoothed[i]
        if prev_m and next_m and prev_m.canonical_type == next_m.canonical_type:
            if curr_m is None or curr_m.score < CONFIDENCE_THRESHOLD or curr_m.canonical_type != prev_m.canonical_type:
                smoothed[i] = prev_m

    # Rule 2: Boundary solo absorption (short/connector with score < 0.70 absorbed into adjacent group)
    for i in range(n):
        curr_m = smoothed[i]
        if curr_m is None or curr_m.score < CONFIDENCE_THRESHOLD:
            text = paragraphs[i].text.strip()
            # A major section boundary starts a new section and must never be absorbed backwards
            if _is_major_section_boundary(text):
                continue
            words = text.split()
            first_word = words[0].lower().strip(".,;:()\"'“”‘’[]") if words else ""
            is_continuation = len(words) < 25 or first_word in _CONNECTORS or _is_fragment(text)
            if is_continuation:
                # Guard: never absorb across a sub-clause number boundary
                curr_text_sc = paragraphs[i].text.strip()
                if _SUB_CLAUSE_PATTERN.match(curr_text_sc):
                    continue
                if i > 0 and smoothed[i - 1] is not None:
                    prev_type = smoothed[i - 1].canonical_type
                    if i > 1 and smoothed[i - 2] and smoothed[i - 2].canonical_type == prev_type:
                        smoothed[i] = smoothed[i - 1]
                        continue
                if i < n - 1 and smoothed[i + 1] is not None:
                    next_type = smoothed[i + 1].canonical_type
                    if i < n - 2 and smoothed[i + 2] and smoothed[i + 2].canonical_type == next_type:
                        smoothed[i] = smoothed[i + 1]
                        continue

    # Rule 3: Minimum run-length (run of 1 with score < 0.70 adjacent to different type -> None)
    for i in range(n):
        curr_m = smoothed[i]
        if curr_m is not None and curr_m.score < CONFIDENCE_THRESHOLD:
            prev_type = smoothed[i - 1].canonical_type if i > 0 and smoothed[i - 1] else None
            next_type = smoothed[i + 1].canonical_type if i < n - 1 and smoothed[i + 1] else None
            if curr_m.canonical_type != prev_type and curr_m.canonical_type != next_type:
                smoothed[i] = None

    return smoothed


def _build_clause_groups(
    smoothed: Dict[int, Optional[ClauseMatch]],
    paragraphs: Sequence[TextInformation],
    agreement_type_id: int = 84,
) -> List[ClauseGroup]:
    """Group paragraphs into unified section containers based on 4-zone contract architecture."""

    if not paragraphs:
        return []

    # 1. Detect Front-Matter Boundary (Title, Parties, Background, Words of Agreement in opening paragraphs)
    front_matter_end = -1
    for i, item in enumerate(paragraphs[:10]):
        if _WORDS_OF_AGREEMENT.search(item.text):
            front_matter_end = i
            break

    # 2. Partition into 4-Zone Section Containers
    containers: List[Dict[str, Any]] = []
    current_paras: List[int] = []
    current_title = "FRONT MATTER"
    in_execution_block = False
    prev_text = ""

    for idx, item in enumerate(paragraphs):
        if not item.text.strip():
            continue
        text = item.text.strip()
        # Skip pure data artefacts (table numbers/units like "34.2 Mb", "96", "92")
        if _is_data_artefact(text):
            continue
        first_line = text.split("\n")[0].strip()

        # If front-matter marker was not found via regex, but we hit the first major section boundary
        if front_matter_end == -1 and idx > 0 and _is_major_section_boundary(text, prev_text=prev_text) and not containers:
            containers.append({
                "zone": "FRONT_MATTER",
                "title": "Front Matter (Title, Parties, Recitals)",
                "para_indices": current_paras,
            })
            current_paras = [idx]
            current_title = first_line
            prev_text = text
            continue

        # Front-matter zone
        if front_matter_end != -1 and idx <= front_matter_end:
            current_paras.append(idx)
            if idx == front_matter_end:
                containers.append({
                    "zone": "FRONT_MATTER",
                    "title": "Front Matter (Title, Parties, Recitals)",
                    "para_indices": current_paras,
                })
                current_paras = []
            prev_text = text
            continue

        # Execution block start (IN WITNESS WHEREOF / SIGNATURE PAGE)
        if _EXECUTION_BLOCK_START.search(first_line):
            if current_paras:
                containers.append({
                    "zone": "OPERATIVE",
                    "title": current_title,
                    "para_indices": current_paras,
                })
                current_paras = []
            in_execution_block = True
            current_paras.append(idx)
            current_title = "Signature / Execution Block"
            prev_text = text
            continue

        # Annex / Schedule / Exhibit — treat as a new OPERATIVE section boundary.
        # These contain active contract clauses (SOWs, SLAs, Schedules) and must
        # be evaluated normally, NOT routed to PATH C as back-matter.
        if _BACK_MATTER_START.search(first_line):
            if current_paras:
                containers.append({
                    "zone": "EXECUTION" if in_execution_block else "OPERATIVE",
                    "title": current_title,
                    "para_indices": current_paras,
                })
                current_paras = []
            in_execution_block = False
            current_paras.append(idx)
            current_title = first_line
            prev_text = text
            continue

        if in_execution_block:
            # Continue accumulating signature blocks until an Annex or end of document
            current_paras.append(idx)
            prev_text = text
            continue

        # Operative section boundary
        if _is_major_section_boundary(text, prev_text=prev_text) and current_paras:
            containers.append({
                "zone": "OPERATIVE",
                "title": current_title,
                "para_indices": current_paras,
            })
            current_paras = [idx]
            current_title = first_line
        else:
            current_paras.append(idx)
            if len(current_paras) == 1:
                current_title = first_line
        prev_text = text

    if current_paras:
        containers.append({
            "zone": "EXECUTION" if in_execution_block else "OPERATIVE",
            "title": current_title,
            "para_indices": current_paras,
        })

    # 2b. Split oversized OPERATIVE containers at sub-clause boundaries
    split_containers: List[Dict[str, Any]] = []
    for c in containers:
        if c.get("zone") != "OPERATIVE" or len(c["para_indices"]) <= _MAX_GROUP_PARAGRAPHS:
            split_containers.append(c)
            continue
        # Split at sub-clause boundaries within this article
        sub_groups: List[List[int]] = []
        current_sub: List[int] = []
        for pi in c["para_indices"]:
            first_line = paragraphs[pi].text.strip().split("\n")[0].strip()
            # Start a new sub-group only when current sub-group has accumulated at least 4 paragraphs
            # and a new numbered sub-clause boundary is encountered.
            if len(current_sub) >= 4 and _SUB_CLAUSE_PATTERN.match(first_line):
                sub_groups.append(current_sub)
                current_sub = [pi]
            else:
                current_sub.append(pi)
        if current_sub:
            sub_groups.append(current_sub)
        if len(sub_groups) <= 1:
            split_containers.append(c)
        else:
            for sg in sub_groups:
                split_containers.append({"zone": "OPERATIVE", "title": c["title"], "para_indices": sg})
    containers = split_containers

    # 3. Assemble ClauseGroup objects per container
    groups: List[ClauseGroup] = []

    for c in containers:
        indices = c["para_indices"]
        paras_dict = {i: paragraphs[i].text for i in indices}
        title = c["title"]
        zone = c.get("zone", "OPERATIVE")

        # Case A: Front-Matter Container -> Routed to PATH C (Defect check only)
        if zone == "FRONT_MATTER":
            groups.append(
                ClauseGroup(
                    para_indices=indices,
                    canonical_type="Front Matter",
                    canonical_slug=None,
                    confidence=1.0,
                    is_classified=False,
                    is_fragment=True,
                    paragraphs=paras_dict,
                )
            )
            continue

        # Case B: Execution / Signature Block Container -> Routed to PATH C (Missing signature / date defect check)
        if zone == "EXECUTION":
            groups.append(
                ClauseGroup(
                    para_indices=indices,
                    canonical_type="Signature Block",
                    canonical_slug=None,
                    confidence=1.0,
                    is_classified=False,
                    is_fragment=True,
                    paragraphs=paras_dict,
                )
            )
            continue

        # Case C: Back-Matter is now treated as OPERATIVE (Annexes/SOWs contain active clauses).
        # This path is no longer used — all non-execution containers fall through to Case D.

        # Case D: Operative Section Container -> Determine Dominant Type
        # Check 1: Match heading directly to taxonomy
        heading_match = _match_heading_to_taxonomy(title)

        # Check 2: Tally body paragraph vector votes
        body_slugs: List[str] = []
        body_scores: Dict[str, float] = {}
        for i in indices:
            m = smoothed.get(i)
            if m and m.canonical_type and m.score >= 0.65:
                body_slugs.append(m.canonical_type)
                body_scores[m.canonical_type] = max(body_scores.get(m.canonical_type, 0.0), m.score)

        dominant_slug: Optional[str] = None
        dominant_conf = 0.0

        if body_slugs:
            counts = Counter(body_slugs)
            dominant_slug, _ = counts.most_common(1)[0]
            dominant_conf = body_scores[dominant_slug]

        # Heading match has priority if valid taxonomy type; else use dominant body vector
        canonical_display = heading_match or (slug_to_display(dominant_slug, agreement_type_id) if dominant_slug else None)
        canonical_slug = (display_to_slug(canonical_display, agreement_type_id) if canonical_display else None) or dominant_slug
        is_classified = bool(canonical_display and (heading_match or dominant_conf >= CONFIDENCE_THRESHOLD))

        is_frag = False
        if not is_classified:
            assembled_text = "\n\n".join(paras_dict[i].strip() for i in indices)
            is_frag = _is_fragment(assembled_text)

        groups.append(
            ClauseGroup(
                para_indices=indices,
                canonical_type=canonical_display,
                canonical_slug=canonical_slug,
                confidence=dominant_conf if dominant_conf > 0 else 1.0,
                is_classified=is_classified,
                is_fragment=is_frag,
                paragraphs=paras_dict,
            )
        )

    # Assign 1-based sequential clause unit ID across document
    for g_idx, g in enumerate(groups, 1):
        g.unit_id = g_idx

    return groups


def _build_batches_v2(
    groups: List[ClauseGroup],
    paragraphs: Sequence[TextInformation]
) -> List[List[ClauseGroup]]:
    """Pack ClauseGroup objects into batches respecting BATCH_CHAR_BUDGET.

    Hard rule: A ClauseGroup is NEVER split across batches.
    """

    batches: List[List[ClauseGroup]] = []
    current_batch: List[ClauseGroup] = []
    current_chars = 0

    for group in groups:
        group_chars = sum(len(paragraphs[i].text) for i in group.para_indices)
        if current_batch and (current_chars + group_chars > BATCH_CHAR_BUDGET):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(group)
        current_chars += group_chars

    if current_batch:
        batches.append(current_batch)

    return batches


async def _extraction_call(
    group: ClauseGroup,
    criteria: List[dict],
    paragraphs: Sequence[TextInformation],
    request: GeneralReviewRequest,
    semaphore: asyncio.Semaphore,
    session_id: str
) -> ExtractionResponse:
    """Run criteria extraction against the clause text with structured output."""

    llm_model = get_bedrock_model()
    reviewer_context = _build_reviewer_context(request)

    lines = []
    for idx in group.para_indices:
        para = paragraphs[idx]
        lines.append(f"[{para.paraindetifier}]: {para.text.strip()}")
    clause_text_with_ids = "\n\n".join(lines)

    criteria_summary = [
        {
            "criterion_id": c["criterion_id"],
            "question": c.get("question") or c.get("question_for_llm") or c.get("criterion", ""),
            "met_when": c.get("met_when", ""),
            "partly_met_when": c.get("partly_met_when", ""),
            "not_met_when": c.get("not_met_when", ""),
            "applies_when": c.get("applies_when", ""),
            "evidence_required": c.get("evidence_required", ""),
        }
        for c in criteria
    ]
    criteria_json = json.dumps(criteria_summary, indent=2, ensure_ascii=False)

    async with semaphore:
        for attempt, temperature in ((1, 0.0), (2, 0.3)):
            try:
                return await llm_model.generate(
                    prompt=_EXTRACTION_USER,
                    context={
                        "canonical_type": group.canonical_type or "General Clause",
                        "reviewer_context": reviewer_context,
                        "criteria_json": criteria_json,
                        "clause_text_with_ids": clause_text_with_ids,
                    },
                    response_model=ExtractionResponse,
                    system_message=_EXTRACTION_SYSTEM,
                    session_id=session_id,
                    temperature=temperature,
                )
            except Exception:
                if attempt == 2:
                    raise
                logger.warning("Extraction call failed on attempt %d; retrying once.", attempt)
                await asyncio.sleep(1)

    raise RuntimeError("unreachable")


def _resolve_and_score(
    extraction: ExtractionResponse,
    group: ClauseGroup,
    criteria: List[dict],
    paragraphs: Sequence[TextInformation]
) -> ScoringResult:
    """Resolve evidence paragraphs for each answer and compute the deterministic score."""

    pid_to_index = {paragraphs[i].paraindetifier: i for i in group.para_indices}
    group_candidates = [(i, paragraphs[i]) for i in group.para_indices]

    answers: List[AnswerRecord] = []
    # Primary substantive body paragraph (skip standalone section title if multi-para)
    default_substantive_idx = group.para_indices[1] if len(group.para_indices) > 1 else group.para_indices[0]

    for raw_ans in extraction.answers:
        resolved_idx: Optional[int] = None

        # 1. Verbatim quote grounding takes precedence (100% exact, deterministic text match)
        if raw_ans.quote and raw_ans.quote.strip() != "Not stated in text":
            grounded = _ground(raw_ans.quote, group_candidates)
            if grounded:
                resolved_idx = grounded[0]

        # 2. Match source_para_id if returned and valid for this group
        if resolved_idx is None and raw_ans.source_para_id and raw_ans.source_para_id in pid_to_index:
            cand_idx = pid_to_index[raw_ans.source_para_id]
            # Avoid anchoring on a bare title paragraph when substantive body paragraphs exist
            if len(group.para_indices) > 1 and cand_idx == group.para_indices[0]:
                resolved_idx = default_substantive_idx
            else:
                resolved_idx = cand_idx

        # 3. Default to the primary substantive body paragraph for omissions / unassigned
        if resolved_idx is None and group.para_indices:
            resolved_idx = default_substantive_idx

        answers.append(
            AnswerRecord(
                criterion_id=raw_ans.criterion_id,
                answer=raw_ans.answer,
                quote=raw_ans.quote,
                source_para_id=raw_ans.source_para_id,
                source_para_index=resolved_idx,
            )
        )

    return score_group(answers, criteria)


async def _explanation_call(
    para_index: int,
    group: ClauseGroup,
    scoring_result: ScoringResult,
    criteria: List[dict],
    request: GeneralReviewRequest,
    semaphore: asyncio.Semaphore,
    session_id: str,
    clause_name: Optional[str] = None,
) -> ExplanationResponse:
    """Draft professional review finding with pre-computed, locked risk level."""

    llm_model = get_bedrock_model()
    para = request.textinformation[para_index]
    criteria_map = {c["criterion_id"]: c for c in criteria}

    para_failed = [a for a in scoring_result.failed_answers if a.source_para_index == para_index]
    if not para_failed:
        para_failed = scoring_result.failed_answers

    issue_lines = []
    for a in para_failed:
        crit = criteria_map.get(a.criterion_id, {})
        q_text = crit.get("question") or crit.get("question_for_llm") or crit.get("criterion", a.criterion_id)
        evidence_str = f'Evidence quote: "{a.quote}"' if a.quote and a.quote.strip() != "Not stated in text" else "Evidence: Required element not stated in text."
        issue_lines.append(f"- Issue: {q_text}\n  Outcome: {a.answer}\n  {evidence_str}")

    failed_criteria_block = "\n".join(issue_lines)
    reviewer_context = _build_reviewer_context(request)
    resolved_clause_name = clause_name or group.canonical_type or "General Clause"

    async with semaphore:
        for attempt, temperature in ((1, 0.0), (2, 0.3)):
            try:
                return await llm_model.generate(
                    prompt=_EXPLANATION_USER,
                    context={
                        "reviewer_context": reviewer_context,
                        "clause_name": resolved_clause_name,
                        "risk_level": scoring_result.final_level,
                        "failed_criteria_block": failed_criteria_block,
                        "original_text": para.text,
                    },
                    response_model=ExplanationResponse,
                    system_message=_EXPLANATION_SYSTEM,
                    session_id=session_id,
                    temperature=temperature,
                )
            except Exception:
                if attempt == 2:
                    raise
                logger.warning("Explanation call failed on attempt %d; retrying once.", attempt)
                await asyncio.sleep(1)

    raise RuntimeError("unreachable")


async def _fragment_defect_call(
    group: ClauseGroup,
    paragraphs: Sequence[TextInformation],
    semaphore: asyncio.Semaphore,
    session_id: str,
    titles: Optional[Dict[str, str]] = None,
) -> List[Tuple[int, Suggestion]]:
    """Check orphaned fragments and front-matter for blanks, placeholders, and direct contradictions."""

    llm_model = get_bedrock_model()
    suggestions: List[Tuple[int, Suggestion]] = []

    _PLACEHOLDER_MARKERS = ("[", "]", "___", "TBD", "INSERT", "TODO", "[●]", "[*]")

    for idx in group.para_indices:
        para = paragraphs[idx]
        t = para.text.strip()
        if not t:
            continue

        # Skip pure data artefacts (e.g. "34.2 Mb", "4.18 Mb")
        if _is_data_artefact(t):
            continue

        # Skip standalone titles/headings (e.g. "Network Latency Guarantee")
        if _looks_like_title(t) or _is_heading_text(t):
            continue

        # Fragment defect check ONLY inspects real drafting placeholders
        has_placeholder = any(marker in t for marker in _PLACEHOLDER_MARKERS)
        if not has_placeholder:
            continue

        res = FragmentDefectResponse(findings=[])
        async with semaphore:
            for attempt, temperature in ((1, 0.0), (2, 0.3)):
                try:
                    res = await llm_model.generate(
                        prompt=_FRAGMENT_USER,
                        context={"text": t},
                        response_model=FragmentDefectResponse,
                        system_message=_FRAGMENT_SYSTEM,
                        session_id=session_id,
                        temperature=temperature,
                    )
                    break
                except Exception:
                    if attempt == 2:
                        res = FragmentDefectResponse(findings=[])
                        break
                    await asyncio.sleep(1)

        for finding in res.findings:
            _, full_fix = _expand_to_paragraph(finding.suggested_fix, para, para.text.strip())
            fallback_label = (
                "Preamble / Front-Matter"
                if group.canonical_type == "Front Matter"
                else (
                    "Signature Block"
                    if group.canonical_type == "Signature Block"
                    else (
                        "Schedule / Annex"
                        if group.canonical_type == "Schedule / Annex"
                        else "General Clause"
                    )
                )
            )
            actual_label = (titles.get(para.paraindetifier) if titles else None) or fallback_label
            sugg = Suggestion(
                clause=actual_label,
                reason=finding.reason,
                risk_level="Low",
                original_text=para.text,
                suggested_fix=full_fix,
                para_identifier=para.paraindetifier,
            )
            suggestions.append((idx, sugg))

    return suggestions


async def _evaluate_group(
    group: ClauseGroup,
    request: GeneralReviewRequest,
    semaphore: asyncio.Semaphore,
    session_id: str,
    titles: Optional[Dict[str, str]] = None,
    audit_trace: Optional[List[Dict[str, Any]]] = None,
) -> List[Tuple[int, Suggestion]]:
    """Route group through PATH A, B, or C and return resulting suggestions."""

    paragraphs = request.textinformation

    # Fix 2 (applied here): Skip pure data artefact paragraphs entirely.
    # E.g. "34.2 Mb", "4.18 Mb" — table cell values from billing tables.
    if group.is_fragment and not group.is_classified:
        assembled_check = " ".join(group.paragraphs[i].strip() for i in group.para_indices)
        if _is_data_artefact(assembled_check):
            logger.debug("Skipping data artefact paragraph: %r", assembled_check[:40])
            if audit_trace is not None:
                audit_trace.append({
                    "clause_unit_id": group.unit_id,
                    "clause_title": "Data Artefact / Table Cell",
                    "canonical_type": None,
                    "canonical_slug": None,
                    "pipeline_path": "SKIPPED_PRE_FILTER",
                    "paragraph_identifiers": [paragraphs[i].paraindetifier for i in group.para_indices],
                    "clause_unit_entire_text": assembled_check,
                    "attached_output": {
                        "status": "SKIPPED_DATA_ARTEFACT",
                        "summary": "Pure numeric/unit table cell skipped before evaluation.",
                        "suggestions": [],
                    },
                })
            return []

    # Fix 3: Skip standalone section headings — they have no evaluable body.
    # The LLM only sees the heading text with no surrounding context, which
    # causes hallucinated "placeholder" findings. The body paragraphs under
    # the heading are evaluated separately in their own group.
    if group.is_fragment and not group.is_classified:
        assembled = " ".join(group.paragraphs[i].strip() for i in group.para_indices)
        words = assembled.split()
        # Standalone heading: short + no obligation verbs → skip entirely
        _obligation_verbs = {
            "shall", "will", "must", "agrees", "agree", "warrants", "represents",
            "indemnifies", "covenants", "undertakes", "provide", "ensure", "guarantee"
        }
        if len(words) <= 7 and not any(w.lower() in _obligation_verbs for w in words):
            logger.debug(
                "Skipping standalone heading group (no body): %r",
                assembled[:60],
            )
            if audit_trace is not None:
                audit_trace.append({
                    "clause_unit_id": group.unit_id,
                    "clause_title": assembled,
                    "canonical_type": None,
                    "canonical_slug": None,
                    "pipeline_path": "SKIPPED_PRE_FILTER",
                    "paragraph_identifiers": [paragraphs[i].paraindetifier for i in group.para_indices],
                    "clause_unit_entire_text": assembled,
                    "attached_output": {
                        "status": "SKIPPED_STANDALONE_HEADING",
                        "summary": "Standalone section heading lacking body text skipped; body evaluated in subsequent unit.",
                        "suggestions": [],
                    },
                })
            return []

    # PATH C: Fragment / Front-Matter Defect
    if group.is_fragment:
        frag_res = await _fragment_defect_call(group, paragraphs, semaphore, session_id, titles=titles)
        if audit_trace is not None:
            audit_trace.append({
                "clause_unit_id": group.unit_id,
                "clause_title": group.canonical_type or "Fragment / Front-Matter",
                "canonical_type": group.canonical_type,
                "canonical_slug": group.canonical_slug,
                "pipeline_path": "PATH C (Fragment / Front-Matter Defect)",
                "paragraph_identifiers": [paragraphs[i].paraindetifier for i in group.para_indices],
                "clause_unit_entire_text": "\n\n".join(paragraphs[i].text.strip() for i in group.para_indices),
                "prompt_and_criteria_context": {
                    "system_prompt_used": "fragment_system.mustache",
                    "reviewer_perspective_mode": (
                        "NEUTRAL GENERAL REVIEW"
                        if _is_general_review_party(request.party_represented)
                        else f"CLIENT ADVOCACY REVIEW ({request.party_represented})"
                    ),
                    "criteria_evaluated": [
                        {"criterion_id": "FRG-01", "question": "Unfilled drafting placeholder"},
                        {"criterion_id": "FRG-02", "question": "Direct contractual contradiction"}
                    ],
                },
                "llm_reasoning_and_evaluation": {
                    "extraction_call": {"findings_count": len(frag_res)},
                    "scorer_decision": {"conclusive_risk_level": "Low" if frag_res else "Clean"},
                },
                "attached_output": {
                    "status": "FLAGGED_FOR_REDLINE" if frag_res else "COMPLIANT / PASSED",
                    "suggestions": [sugg.model_dump() for _, sugg in frag_res],
                },
            })
        return frag_res

    # Determine criteria: PATH A (type-specific) or PATH B (general criteria)
    criteria: List[dict] = []
    _atid = resolve_agreement_type_id(request.agreement_type or "")
    if group.is_classified:
        if group.canonical_type:
            criteria = get_criteria(group.canonical_type, _atid)
        if not criteria and group.canonical_slug:
            criteria = get_criteria_by_slug(group.canonical_slug, _atid)

    if not criteria:
        criteria = list(GENERAL_CRITERIA)

    # Call A: Criteria extraction
    extraction = await _extraction_call(group, criteria, paragraphs, request, semaphore, session_id)

    # Safety Net: LLM flagged type mismatch -> fall back to general criteria (PATH B)
    if not extraction.is_type_match:
        logger.info(
            "Type mismatch flagged by LLM for group '%s' - falling back to general criteria (PATH B)",
            group.canonical_type,
        )
        criteria = list(GENERAL_CRITERIA)
        misclassified_group = ClauseGroup(
            para_indices=group.para_indices,
            canonical_type=group.canonical_type,
            canonical_slug=None,
            confidence=0.0,
            is_classified=False,
            is_fragment=group.is_fragment,
            paragraphs=group.paragraphs,
            unit_id=group.unit_id,
        )
        extraction = await _extraction_call(misclassified_group, criteria, paragraphs, request, semaphore, session_id)
        group = misclassified_group

    # Deterministic scoring in Python
    scoring_result = _resolve_and_score(extraction, group, criteria, paragraphs)

    # Clean / non-risky clauses produce zero suggestions
    if not is_risky(scoring_result.final_level, has_failed_answers=bool(scoring_result.failed_answers)):
        if audit_trace is not None:
            first_p = paragraphs[group.para_indices[0]]
            heading_title = (titles.get(first_p.paraindetifier) if titles else None) or group.canonical_type or "General Clause"
            audit_trace.append({
                "clause_unit_id": group.unit_id,
                "clause_title": heading_title,
                "canonical_type": group.canonical_type,
                "canonical_slug": group.canonical_slug,
                "pipeline_path": "PATH A (Type-Specific Rubric)" if group.is_classified else "PATH B (General Criteria)",
                "paragraph_identifiers": [paragraphs[i].paraindetifier for i in group.para_indices],
                "clause_unit_entire_text": "\n\n".join(paragraphs[i].text.strip() for i in group.para_indices),
                "prompt_and_criteria_context": {
                    "system_prompt_used": "extraction_system.mustache (v2 calibrated - 7 interpretation rules)",
                    "reviewer_perspective_mode": (
                        "NEUTRAL GENERAL REVIEW"
                        if _is_general_review_party(request.party_represented)
                        else f"CLIENT ADVOCACY REVIEW ({request.party_represented})"
                    ),
                    "criteria_evaluated": [
                        {
                            "criterion_id": c.get("id") or c.get("criterion_id"),
                            "question": c.get("question") or c.get("question_for_llm") or c.get("criterion", ""),
                            "weight": c.get("weight"),
                            "gate_if_not_met": c.get("gate_if_not_met"),
                        }
                        for c in criteria
                    ],
                },
                "llm_reasoning_and_evaluation": {
                    "extraction_call": {
                        "is_type_match": extraction.is_type_match,
                        "criterion_answers": [
                            {
                                "criterion_id": a.criterion_id,
                                "answer": a.answer,
                                "quote": a.quote,
                                "source_para_id": a.source_para_id,
                            }
                            for a in extraction.answers
                        ],
                    },
                    "scorer_decision": {
                        "gap_score": scoring_result.gap_score,
                        "conclusive_risk_level": scoring_result.final_level,
                        "gate_triggered": getattr(scoring_result, "gate_name", None),
                        "failed_criteria_count": len(scoring_result.failed_answers),
                        "is_risky": False,
                    },
                },
                "attached_output": {
                    "status": "COMPLIANT / PASSED",
                    "summary": f"All {len(criteria)} criteria met or within acceptable threshold. No adverse risk identified.",
                    "suggestions": [],
                },
            })
        return []

    # Identify risky paragraphs that need an explanation call
    risky_para_indices = scoring_result.risky_para_indices
    # Never attach suggestions to a standalone section heading when substantive body paragraphs exist
    if len(group.para_indices) > 1 and _is_major_section_boundary(paragraphs[group.para_indices[0]].text):
        risky_para_indices = [pid for pid in risky_para_indices if pid != group.para_indices[0]]

    if not risky_para_indices:
        # Default to first substantive body paragraph (avoid section header)
        risky_para_indices = [group.para_indices[1]] if len(group.para_indices) > 1 else [group.para_indices[0]]

    canonical_name = group.canonical_type or "General Clause"

    async def _explain_para(p_idx: int) -> Tuple[int, Suggestion]:
        p = paragraphs[p_idx]
        actual_heading = (titles.get(p.paraindetifier) if titles else None) or canonical_name
        explanation = await _explanation_call(
            p_idx, group, scoring_result, criteria, request, semaphore, session_id,
            clause_name=actual_heading,
        )
        _, full_fix = _expand_to_paragraph(explanation.suggested_fix, p, p.text.strip())
        sugg = Suggestion(
            clause=actual_heading,
            reason=explanation.reason,
            risk_level=scoring_result.final_level,
            original_text=p.text,
            suggested_fix=full_fix,
            para_identifier=p.paraindetifier,
        )
        return (p_idx, sugg)

    # Concurrently generate explanations for all risky paragraphs in this group
    explained_results = await asyncio.gather(*[_explain_para(pid) for pid in risky_para_indices])

    if audit_trace is not None:
        first_p = paragraphs[group.para_indices[0]]
        heading_title = (titles.get(first_p.paraindetifier) if titles else None) or group.canonical_type or "General Clause"
        audit_trace.append({
            "clause_unit_id": group.unit_id,
            "clause_title": heading_title,
            "canonical_type": group.canonical_type,
            "canonical_slug": group.canonical_slug,
            "pipeline_path": "PATH A (Type-Specific Rubric)" if group.is_classified else "PATH B (General Criteria)",
            "paragraph_identifiers": [paragraphs[i].paraindetifier for i in group.para_indices],
            "clause_unit_entire_text": "\n\n".join(paragraphs[i].text.strip() for i in group.para_indices),
            "prompt_and_criteria_context": {
                "system_prompt_used": "extraction_system.mustache (v2 calibrated - 7 interpretation rules)",
                "explanation_system_prompt": "explanation_system.mustache (Parts A-I surgical brevity rules)",
                "reviewer_perspective_mode": (
                    "NEUTRAL GENERAL REVIEW"
                    if _is_general_review_party(request.party_represented)
                    else f"CLIENT ADVOCACY REVIEW ({request.party_represented})"
                ),
                "criteria_evaluated": [
                    {
                        "criterion_id": c.get("id") or c.get("criterion_id"),
                        "question": c.get("question") or c.get("question_for_llm") or c.get("criterion", ""),
                        "weight": c.get("weight"),
                        "gate_if_not_met": c.get("gate_if_not_met"),
                        "met_when": c.get("met_when", ""),
                        "not_met_when": c.get("not_met_when", ""),
                    }
                    for c in criteria
                ],
            },
            "llm_reasoning_and_evaluation": {
                "extraction_call": {
                    "is_type_match": extraction.is_type_match,
                    "criterion_answers": [
                        {
                            "criterion_id": a.criterion_id,
                            "answer": a.answer,
                            "quote": a.quote,
                            "source_para_id": a.source_para_id,
                        }
                        for a in extraction.answers
                    ],
                },
                "scorer_decision": {
                    "gap_score": scoring_result.gap_score,
                    "conclusive_risk_level": scoring_result.final_level,
                    "gate_triggered": getattr(scoring_result, "gate_name", None),
                    "failed_criteria_count": len(scoring_result.failed_answers),
                    "is_risky": True,
                },
                "explanation_prompt_input": {
                    "failed_criteria_fed_to_llm": [
                        f"{a.criterion_id}: {a.quote or 'Not stated in text'}"
                        for a in scoring_result.failed_answers
                    ],
                },
            },
            "attached_output": {
                "status": "FLAGGED_FOR_REDLINE",
                "suggestions": [sugg.model_dump() for _, sugg in explained_results],
            },
        })

    return list(explained_results)


async def _evaluate_batch(
    batch: List[ClauseGroup],
    request: GeneralReviewRequest,
    semaphore: asyncio.Semaphore,
    session_id: str,
    titles: Optional[Dict[str, str]] = None,
    audit_trace: Optional[List[Dict[str, Any]]] = None,
) -> List[Tuple[int, Suggestion]]:
    """Evaluate all groups in a batch concurrently."""

    if titles is None:
        titles = _build_clause_titles(request.textinformation)

    group_results = await asyncio.gather(
        *[_evaluate_group(group, request, semaphore, session_id, titles=titles, audit_trace=audit_trace) for group in batch],
        return_exceptions=True
    )
    batch_suggestions: List[Tuple[int, Suggestion]] = []
    for res in group_results:
        if isinstance(res, Exception):
            logger.exception("Error evaluating clause group: %s", res)
            continue
        batch_suggestions.extend(res)
    batch_suggestions.sort(key=lambda pair: pair[0])
    return batch_suggestions


async def general_review_service(
    request: GeneralReviewRequest,
    session_id: str,
    audit_trace_collector: Optional[List[Dict[str, Any]]] = None,
) -> GeneralReviewResponse:
    """Deterministic, rubric-driven general review pipeline."""

    fingerprint = _request_fingerprint(request)
    if audit_trace_collector is None:
        cached = _cache_get(fingerprint)
        if cached is not None:
            logger.info("General review: cache hit — returning the stored review for this document.")
            return cached

    if not request.textinformation:
        return GeneralReviewResponse(suggestions=[])

    paragraphs = request.textinformation

    # Step 1: Classify all paragraphs with vector DB
    raw_matches = await _classify_all_paragraphs(request, session_id)

    # Step 2: Smooth classifications to remove classification noise
    smoothed = _smooth_classifications(raw_matches, paragraphs)

    # Step 3: Build cohesive section containers
    _atid_main = resolve_agreement_type_id(request.agreement_type or "")
    groups = _build_clause_groups(smoothed, paragraphs, _atid_main)

    # Step 4: Build batches respecting clause group integrity
    batches = _build_batches_v2(groups, paragraphs)
    logger.info("General review: %d paragraphs in %d clause groups split into %d batches.", len(paragraphs), len(groups), len(batches))

    titles = _build_clause_titles(paragraphs)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    batch_results = await asyncio.gather(
        *[_evaluate_batch(batch, request, semaphore, session_id, titles=titles, audit_trace=audit_trace_collector) for batch in batches]
    )

    if audit_trace_collector is not None:
        audit_trace_collector.sort(key=lambda item: item.get("clause_unit_id", 0))

    seen_positions: Set[int] = set()
    positioned: List[Tuple[int, Suggestion]] = []
    for b_res in batch_results:
        for pos, sugg in b_res:
            if pos in seen_positions:
                continue
            seen_positions.add(pos)
            positioned.append((pos, sugg))

    positioned.sort(key=lambda pair: pair[0])

    if audit_trace_collector is not None:
        audit_trace_collector.sort(key=lambda item: item.get("clause_unit_id", 0))

    # Fix 4: Exclude Signature Block / execution-zone Low-risk fragment noise.
    # Keep High/Medium risk findings from execution-zone (e.g. unsigned agreement).
    final_suggestions = [
        sugg for _, sugg in positioned
        if not (sugg.clause == "Signature Block" and sugg.risk_level == "Low")
    ]
    response = GeneralReviewResponse(suggestions=final_suggestions)
    _cache_put(fingerprint, response)
    return response


async def general_review_streaming_service(request: GeneralReviewRequest, session_id: str) -> AsyncIterator[str]:
    """Stream the review as SSE text fragments that concatenate into one valid JSON response."""

    def frame(fragment: str) -> str:
        return f"data: {json.dumps(fragment)}\n\n"

    try:
        fingerprint = _request_fingerprint(request)
        cached = _cache_get(fingerprint)
        if cached is not None:
            logger.info("General review stream: cache hit — streaming the stored review for this document.")
            yield frame('{"suggestions": [')
            for index, suggestion in enumerate(cached.suggestions):
                separator = "" if index == 0 else ","
                yield frame(separator + suggestion.model_dump_json())
            yield frame("]}")
            yield "data: [DONE]\n\n"
            return

        if not request.textinformation:
            yield frame('{"suggestions": []}')
            yield "data: [DONE]\n\n"
            return

        paragraphs = request.textinformation
        raw_matches = await _classify_all_paragraphs(request, session_id)
        smoothed = _smooth_classifications(raw_matches, paragraphs)
        _atid_stream = resolve_agreement_type_id(request.agreement_type or "")
        groups = _build_clause_groups(smoothed, paragraphs, _atid_stream)
        batches = _build_batches_v2(groups, paragraphs)

        logger.info("General review stream: %d paragraphs in %d clause groups split into %d batches.", len(paragraphs), len(groups), len(batches))

        titles = _build_clause_titles(paragraphs)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        tasks = [
            asyncio.create_task(_evaluate_batch(batch, request, semaphore, session_id, titles=titles))
            for batch in batches
        ]

        yield frame('{"suggestions": [')

        emitted: List[Suggestion] = []
        seen_positions: Set[int] = set()
        for task in tasks:
            batch_result = await task
            for pos, suggestion in batch_result:
                if pos in seen_positions:
                    continue
                seen_positions.add(pos)
                if suggestion.clause == "Signature Block" and suggestion.risk_level == "Low":
                    continue
                separator = "" if not emitted else ","
                yield frame(separator + suggestion.model_dump_json())
                emitted.append(suggestion)

        yield frame("]}")
        # Fix 4: Exclude Signature Block Low-risk noise from cache too
        clean_emitted = [s for s in emitted if not (s.clause == "Signature Block" and s.risk_level == "Low")]
        _cache_put(fingerprint, GeneralReviewResponse(suggestions=clean_emitted))

    except Exception as exc:
        logger.exception("General review stream failed: %s", exc)
        yield f"data: {json.dumps({'error': 'General review failed. Please try again.'})}\n\n"

    yield "data: [DONE]\n\n"
