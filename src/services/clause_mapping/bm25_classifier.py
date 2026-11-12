"""
BM25 Classifier for Legal Clause Taxonomy.

Provides in-memory Okapi BM25 scoring over the 52 taxonomy types.
Each document in the BM25 index represents one canonical clause type,
built from:
  - Canonical type name
  - Taxonomy aliases / keywords
  - All criterion names, descriptions, and questions for that type

Runs in ~0.5ms in RAM with zero network round trips.
"""

import math
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

_WORD_RE = re.compile(r"[a-z0-9]+")

_STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "aren't", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both",
    "but", "by", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't",
    "doing", "don't", "down", "during", "each", "few", "for", "from", "further", "had", "hadn't",
    "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here",
    "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm",
    "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself", "let's", "me", "more",
    "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once", "only", "or",
    "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she",
    "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", "that", "that's",
    "the", "their", "theirs", "them", "themselves", "then", "there", "there's", "these", "they",
    "they'd", "they'll", "they're", "they've", "this", "those", "through", "to", "too", "under",
    "until", "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't",
    "what", "what's", "when", "when's", "where", "where's", "which", "while", "who", "who's", "whom",
    "why", "why's", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've",
    "your", "yours", "yourself", "yourselves", "clause", "section", "article", "agreement", "party", "parties"
}


def _tokenize(text: str) -> List[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP_WORDS and len(w) > 2]


class BM25TaxonomyClassifier:
    """Okapi BM25 Index over the canonical criteria taxonomy."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_types: List[str] = []
        self.doc_lens: List[int] = []
        self.avg_dl: float = 0.0
        self.doc_freqs: Dict[str, int] = Counter()
        self.term_freqs: List[Dict[str, int]] = []
        self.num_docs: int = 0
        self._initialized = False

    def build_from_taxonomy(self, taxonomy_data: dict, aliases: Optional[Dict[str, str]] = None) -> None:
        """Build the index from taxonomy criteria and optional alias dictionary."""
        types_dict = taxonomy_data.get("types", {})
        docs: List[Tuple[str, List[str]]] = []

        alias_reverse: Dict[str, List[str]] = {}
        if aliases:
            for alias_phrase, canonical in aliases.items():
                alias_reverse.setdefault(canonical, []).append(alias_phrase)

        for type_name, type_info in types_dict.items():
            text_parts = [type_name, type_name]  # boost type name
            # add aliases for this type
            for a in alias_reverse.get(type_name, []):
                text_parts.append(a)
                text_parts.append(a)

            # add criteria text
            for crit in type_info.get("criteria", {}).values():
                text_parts.append(crit.get("criterion", ""))
                text_parts.append(crit.get("question_for_llm", ""))
                text_parts.append(crit.get("issue_category", ""))

            full_text = " ".join(text_parts)
            tokens = _tokenize(full_text)
            if tokens:
                docs.append((type_name, tokens))

        self.num_docs = len(docs)
        self.doc_types = [t for t, _ in docs]
        self.doc_lens = [len(toks) for _, toks in docs]
        self.avg_dl = sum(self.doc_lens) / max(1, self.num_docs)

        for _, toks in docs:
            tf = Counter(toks)
            self.term_freqs.append(tf)
            for word in tf.keys():
                self.doc_freqs[word] += 1

        self._initialized = True

    def score(self, query: str) -> List[Tuple[str, float]]:
        """Score query against all taxonomy types. Returns sorted (type_name, score)."""
        if not self._initialized or not self.num_docs:
            return []

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        scores: List[float] = [0.0] * self.num_docs

        for token in q_tokens:
            df = self.doc_freqs.get(token, 0)
            if df == 0:
                continue

            # Standard Robertson-Spärck Jones IDF
            idf = math.log((self.num_docs - df + 0.5) / (df + 0.5) + 1.0)

            for i in range(self.num_docs):
                tf = self.term_freqs[i].get(token, 0)
                if tf > 0:
                    doc_len = self.doc_lens[i]
                    num = tf * (self.k1 + 1)
                    denom = tf + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_dl))
                    scores[i] += idf * (num / denom)

        ranked = sorted(
            zip(self.doc_types, scores),
            key=lambda x: x[1],
            reverse=True
        )
        return ranked

    def get_top_match(self, query: str, threshold: float = 3.0) -> Optional[Tuple[str, float]]:
        """Return the best taxonomy match if score >= threshold and has clear margin."""
        ranked = self.score(query)
        if not ranked or ranked[0][1] < threshold:
            return None
        top_name, top_score = ranked[0]
        # Check margin over runner up if available
        if len(ranked) > 1 and ranked[1][1] > 0:
            ratio = top_score / ranked[1][1]
            if ratio < 1.15:  # ambiguous
                return None
        return (top_name, top_score)

