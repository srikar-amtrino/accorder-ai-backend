"""Three-layer clause-type classification that always resolves one type when it can.

Layer 1 — heading router: the clause's own heading (or, weaker, its opening
words) matched against the legal team's clause types in code. Instant, offline,
and immune to the vector path's failure modes.

Layer 2 — vector DB: a distance-weighted vote over the Qdrant hits for the
clause's heading and operative words, picking a single winning type.

Layer 3 — offline fallback: when the vector path is down, timed out, or found
nothing above the floor, the Layer 1 result (including its weaker opening-text
match) is used, so the review still carries the clause's policy.

The slugs here are the vector DB's / clause_policies.json's canonical_type
labels; tests check every slug has a policy.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Pattern, Sequence, Tuple

from src.schemas.clause_mapping import ClauseMatch

# The catch-all type: governance, third-party beneficiaries, headings, and anything general.
MISCELLANEOUS_TYPE = "miscellaneous-other-provisions"

# Heading patterns, first match wins, so the more specific patterns come first
# ("Limitation of Liability" before "Representations and Warranties"; "Export
# Controls" before "Compliance"; "Amendments" before "Waiver", so "Modification,
# Waiver and Authorized Representative" is an amendments clause). The slugs are
# the taxonomy's clause types (clause_policies.json); tests check each has a policy.
HEADING_TO_CANONICAL_TYPE: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"\b(limitation\s+of\s+liability|limitations?\s+on\s+liability|liability\s+cap|exclusion\s+of\s+damages|damages)\b", re.I), "limitation-of-liability"),
    (re.compile(r"\b(indemnif(y|ication)|indemnit(y|ies)|hold\s+harmless|third[-\s]party\s+claims)\b", re.I), "indemnification"),
    (re.compile(r"\b(intellectual\s+property|ip\s+rights|ownership\s+of\s+(deliverables|work\s+product)|work\s+made\s+for\s+hire|proprietary\s+rights)\b", re.I), "intellectual-property"),
    (
        re.compile(
            r"\b(data\s+protection|data\s+privacy|personal\s+data|personal\s+information|gdpr|ccpa|data\s+processing|information\s+security|cybersecurity|cyber\s+security"
            r"|security\s+(safeguards|measures|requirements)|data\s+security|data\s+management)\b",
            re.I,
        ),
        "data-protection-and-privacy",
    ),
    (re.compile(r"\b(confidential(ity)?|non[-\s]disclosure|proprietary\s+information)\b", re.I), "confidentiality"),
    (re.compile(r"\b(equitable\s+relief|injunctive\s+relief|injunctions?|specific\s+performance)\b", re.I), "equitable-relief"),
    (re.compile(r"\b(representations?\s+and\s+warrant(y|ies)|warrant(y|ies)|disclaimer\s+of\s+warranties|disclaimers?)\b", re.I), "representations-and-warranties"),
    (re.compile(r"\b(export\s+controls?|export\s+compliance|sanctions|ofac|embargo(es|ed)?)\b", re.I), "export-controls-and-sanctions"),
    (
        re.compile(
            r"\b(term\s+and\s+termination|term\s*/\s*termination|termination|return\s+of\s+(materials|data|advances?|deposits?|property|documents|unused\s+\w+)"
            r"|transition\s+(assistance|services)|exit\s+(plan|assistance)|wind[-\s]down|effect\s+of\s+(termination|expiration))\b",
            re.I,
        ),
        "term-and-termination",
    ),
    (re.compile(r"\b(fees?(\s+and\s+(expenses|payment))?|compensation|invoic(e|es|ing)|payments?(\s+terms)?|pricing|expenses)\b", re.I), "fees-and-payment"),
    (re.compile(r"\b(taxes|taxation|withholding\s+tax|tax\s+gross[-\s]?up)\b", re.I), "taxes"),
    (re.compile(r"\b(non[-\s]?compet(e|ition)|restrictive\s+covenants?|competing\s+business)\b", re.I), "non-compete"),
    (re.compile(r"\b((non[-\s]?)?exclusivity|non[-\s]?exclusive)\b", re.I), "non-exclusivity"),
    (re.compile(r"\b(non[-\s]?solicit(ation)?|no[-\s]hire|(personnel|employee)\s+solicitation)\b", re.I), "non-solicitation"),
    (
        re.compile(
            r"\b(key\s+personnel|personnel|staffing|project\s+staff|background\s+checks?|(service\s+provider|provider|vendor|supplier|contractor)('s)?\s+(obligations|responsibilities|duties))\b",
            re.I,
        ),
        "service-provider-obligations",
    ),
    (re.compile(r"\b(governing\s+law|choice\s+of\s+(law|venue)|jurisdiction|venue|dispute\s+resolution|disputes|mediation|arbitration)\b", re.I), "governing-law-and-dispute-resolution"),
    (re.compile(r"\b(force\s+majeure|excused\s+performance|business\s+continuity|disaster\s+recovery|bc\s*/?\s*dr)\b", re.I), "force-majeure"),
    (re.compile(r"\b(audit(s|ing)?(\s+rights)?|books\s+and\s+records|record[-\s]keeping|inspection\s+rights)\b", re.I), "audit-and-records"),
    (re.compile(r"\b(insurance|certificates?\s+of\s+insurance)\b", re.I), "insurance"),
    (re.compile(r"\b(statements?\s+of\s+work|work\s+orders?|order\s+forms?|consulting\s+services)\b", re.I), "statements-of-work-and-work-orders"),
    (re.compile(r"\b(acceptance(\s+testing)?|deliverables)\b", re.I), "deliverables-and-acceptance"),
    (re.compile(r"\b(scope\s+of\s+(services|work)|services\s+to\s+be\s+(performed|provided)|the\s+services|description\s+of\s+services)\b", re.I), "scope-of-services"),
    (re.compile(r"\b(conflicts?\s+of\s+interest)\b", re.I), "conflict-of-interest"),
    (re.compile(r"\b(no\s+agency|no\s+partnership|no\s+joint\s+venture|authority\s+to\s+bind|no\s+authority)\b", re.I), "no-agency-or-authority"),
    (
        re.compile(r"\b(independent\s+contractor|relationship\s+(of\s+the\s+parties|between\s+the\s+parties|with\s+\w+)|status\s+of\s+(the\s+)?(contractor|provider))\b", re.I),
        "independent-contractor",
    ),
    (re.compile(r"\b(subcontract(ing|ors?)|sub[-\s]?processors?)\b", re.I), "subcontractors"),
    (re.compile(r"\b(service\s+levels?|sla|slas|uptime|availability|service\s+credits)\b", re.I), "service-levels"),
    (re.compile(r"\b(support(\s+and\s+maintenance)?|maintenance|help\s*desk)\b", re.I), "support-and-maintenance"),
    (re.compile(r"\b(customer|client)\s+(obligations|responsibilities)|cooperation\b", re.I), "customer-obligations"),
    (re.compile(r"\b(definitions?|defined\s+terms|interpretation)\b", re.I), "definitions-and-interpretation"),
    (re.compile(r"\b(notices?|notification\s+address)\b", re.I), "notices"),
    (re.compile(r"\b(assignment|assign(s|ability)?|successors\s+and\s+assigns|change\s+of\s+control)\b", re.I), "assignment"),
    (re.compile(r"\b(order\s+of\s+precedence|precedence|conflicting\s+terms|conflicts?\s+between)\b", re.I), "order-of-precedence"),
    (re.compile(r"\b(publicity|press\s+releases?|use\s+of\s+names?|marketing\s+use|trademarks?\s+and\s+logos?)\b", re.I), "publicity-and-use-of-names"),
    (re.compile(r"\b(change\s+(control|orders?|requests?|management))\b", re.I), "change-control"),
    (re.compile(r"\b(anti[-\s]?corruption|anti[-\s]?bribery|fcpa|code\s+of\s+conduct|modern\s+slavery|compliance(\s+with\s+laws)?)\b", re.I), "compliance"),
    (re.compile(r"\b(time\s+(is\s+)?of\s+the\s+essence)\b", re.I), "time-of-the-essence"),
    (re.compile(r"\b(purpose|background|recitals?|whereas)\b", re.I), "purpose"),
    (re.compile(r"\b(entire\s+agreement|complete\s+agreement|integration\s+clause|merger\s+clause|non[-\s]reliance)\b", re.I), "entire-agreement"),
    (re.compile(r"\b(amendments?|modifications?|variations?\s+(of|to)\s+(this\s+)?agreement)\b", re.I), "amendments-and-modifications"),
    (re.compile(r"\b(waivers?)\b", re.I), "waiver"),
    (re.compile(r"\b(severability|severance|partial\s+invalidity)\b", re.I), "severability"),
    (re.compile(r"\b(survival|accrued\s+rights)\b", re.I), "survival"),
    (re.compile(r"\b(counterparts|electronic\s+signatures?)\b", re.I), "counterparts"),
    (
        re.compile(
            r"\b(miscellaneous|general(\s+(provisions|terms))?|headings|captions|(no\s+)?third[-\s]party\s+beneficiar(y|ies)|further\s+assurances|cumulative\s+remedies"
            r"|governance|project\s+management|steering\s+committee|project\s+managers?|relationship\s+managers?|status\s+(meetings|reports))\b",
            re.I,
        ),
        MISCELLANEOUS_TYPE,
    ),
]

# Clause types the vector DB may still carry from the policy set before the
# taxonomy replaced it, and the taxonomy type each became. "general-provisions"
# was split into seven types, so a hit with that label says nothing and is
# dropped. Remove once the collection is reseeded with the taxonomy's types.
LEGACY_CANONICAL_TYPES: Dict[str, Optional[str]] = {
    "suspension-and-termination": "term-and-termination",
    "exit-and-transition-assistance": "term-and-termination",
    "information-security-and-data-management": "data-protection-and-privacy",
    "business-continuity-and-disaster-recovery": "force-majeure",
    "personnel": "service-provider-obligations",
    "subcontracting-and-sub-processors": "subcontractors",
    "publicity-and-use-of-name": "publicity-and-use-of-names",
    "governance-and-project-management": MISCELLANEOUS_TYPE,
    "general-provisions": None,
}


def current_canonical_type(slug: str) -> Optional[str]:
    """The taxonomy type a vector DB label stands for today; None for a label that no longer names one type."""

    return LEGACY_CANONICAL_TYPES.get(slug, slug)


# Catch-all headings ("Miscellaneous", "Additional Terms", "General") name no
# topic, so they never lock a type: the vector vote may override them, and only
# when it has nothing does the clause fall back to miscellaneous-other-provisions.
GENERIC_HEADING_RE = re.compile(r"^\s*(miscellaneous|general(\s+(provisions|terms))?|additional\s+(terms|provisions)|other(\s+(terms|provisions|matters))?|further\s+provisions|boilerplate)\s*$", re.I)

# Confidence attached to each layer's verdicts.
HEADING_MATCH_CONFIDENCE = 0.99
OPENING_MATCH_CONFIDENCE = 0.85

# Layer 2 floor: a Qdrant hit below this says nothing about the clause's type.
# Matches the candidate floor of the simple classifier.
VECTOR_SCORE_FLOOR = 0.70

# A hit agreeing with the heading router gets this much added before the vote.
HEADING_AGREEMENT_BOOST = 0.15

# Opening text consulted when the heading alone does not match.
OPENING_TEXT_CHARS = 200

# A clause unit shorter than this is a bare heading, a list item, or a
# fragment: it never classifies, so it is skipped without a network call.
MIN_CLAUSE_WORDS = 10

LAYER1_HEADING = "layer1_heading"
LAYER2_QDRANT = "layer2_qdrant"
LAYER3_FALLBACK = "layer3_fallback"
SKIPPED_SHORT = "skipped_short"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ResolvedClause:
    """The single clause type a clause resolved to, how confidently, and which layer decided."""

    canonical_type: Optional[str]
    confidence: float
    source: str


def classify_layer1_heading(heading: str, opening_text: str = "") -> Optional[Tuple[str, float]]:
    """Match a clause heading (or, weaker, its opening words) to a clause type in code."""

    # Word types apostrophes curly ("PROVIDER’S OBLIGATIONS"); the patterns are written with straight ones.
    heading = (heading or "").strip().replace("’", "'").replace("‘", "'")
    if heading:
        for pattern, slug in HEADING_TO_CANONICAL_TYPE:
            if pattern.search(heading):
                confidence = OPENING_MATCH_CONFIDENCE if GENERIC_HEADING_RE.match(heading) else HEADING_MATCH_CONFIDENCE
                return slug, confidence
    opening = (opening_text or "")[:OPENING_TEXT_CHARS]
    if opening:
        for pattern, slug in HEADING_TO_CANONICAL_TYPE:
            if pattern.search(opening):
                return slug, OPENING_MATCH_CONFIDENCE
    return None


def resolve_qdrant_weighted_knn(hits: Sequence[ClauseMatch], heading_boost_slug: Optional[str] = None, min_floor: float = VECTOR_SCORE_FLOOR) -> Tuple[Optional[str], float]:
    """Pick one clause type from ranked vector hits by a distance-weighted vote.

    Each type's best score counts for three quarters and its rank-weighted
    support across the hits for one quarter, so a type with several close hits
    beats a lone marginally-higher one. A type the heading router also suggests
    is boosted. Returns (None, 0.0) when no hit clears the floor.
    """

    best_per_type: Dict[str, float] = {}
    support_per_type: Dict[str, float] = {}
    for rank, hit in enumerate(hits):
        slug = hit.canonical_type
        score = float(hit.score)
        if not slug or score < min_floor:
            continue
        if heading_boost_slug and slug == heading_boost_slug:
            score = min(1.0, score + HEADING_AGREEMENT_BOOST)
        best_per_type[slug] = max(best_per_type.get(slug, 0.0), score)
        support_per_type[slug] = support_per_type.get(slug, 0.0) + score / (1.0 + 0.15 * rank)

    if not best_per_type:
        return None, 0.0

    # Deterministic tie-break on the slug so equal votes never flip between runs.
    winner = max(sorted(best_per_type), key=lambda slug: best_per_type[slug] * 0.75 + min(1.0, support_per_type[slug] / 2.5) * 0.25)
    return winner, round(best_per_type[winner], 3)


def classify_clause_resilient(heading: str, operative_query: str, word_count: int, hits: Optional[Sequence[ClauseMatch]] = None) -> ResolvedClause:
    """Resolve a clause to one clause type through the three layers.

    hits is None when the vector path was unavailable (down, timed out, over
    budget, or disabled); an empty list means it ran and found nothing.
    """

    if word_count < MIN_CLAUSE_WORDS:
        return ResolvedClause(None, 0.0, SKIPPED_SHORT)

    layer1 = classify_layer1_heading(heading, operative_query)
    if layer1 and layer1[1] >= HEADING_MATCH_CONFIDENCE:
        return ResolvedClause(layer1[0], layer1[1], LAYER1_HEADING)

    if hits:
        slug, score = resolve_qdrant_weighted_knn(hits, heading_boost_slug=layer1[0] if layer1 else None)
        if slug and score >= VECTOR_SCORE_FLOOR:
            return ResolvedClause(slug, score, LAYER2_QDRANT)

    if layer1:
        return ResolvedClause(layer1[0], layer1[1], LAYER3_FALLBACK)

    return ResolvedClause(None, 0.0, UNCLASSIFIED)
