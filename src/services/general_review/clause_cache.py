"""In-memory, zero-data-retention cache of per-paragraph review outcomes.

Keys are SHA-256 digests of the normalised paragraph text, its clause title,
the resolved parties, the reviewer context, the clause and document facts the
paragraph was judged against, and the policy/prompt version; values are the
outcome (the note and the redrafted paragraph, or an explicit "nothing to
flag"). An unchanged clause reviewed again — in the same document or a
revised one — returns exactly the outcome it got the first time, without
another model call.

Nothing is written to disk. The original paragraph is never stored, but a
redrafted paragraph is contract text, so every entry is discarded once it is
older than the configured time to live (GENERAL_REVIEW_CACHE_TTL_MINUTES), and
the whole cache goes when the process stops.
"""

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

from src.config.settings import get_settings
from src.services.clause_mapping.policy_store import POLICIES_PATH
from src.services.general_review.party_resolver import PartyContext
from src.services.general_review.taxonomy_gates import OVERRIDES_PATH, TAXONOMY_PATH

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "v3" / "general_review"
_PROMPT_FILES = ("assess_system.mustache", "assess_user.mustache", "draft_system.mustache", "draft_user.mustache")

# Paragraph outcomes kept per process; oldest evicted first.
CACHE_MAX_ENTRIES = 5000


@dataclass(frozen=True)
class ClauseAssessment:
    """Outcome of reviewing one paragraph. flagged=False means the paragraph needed no change."""

    flagged: bool
    clause: str = ""
    risk_level: str = ""
    reason: str = ""
    suggested_fix: str = ""


NO_FINDING = ClauseAssessment(flagged=False)


@lru_cache
def policy_version_hash() -> str:
    """Digest of the policy file, the review prompts, and the criteria taxonomy: editing any of them invalidates every cached outcome."""

    digest = hashlib.sha256()
    for path in (POLICIES_PATH, *(_PROMPTS_DIR / name for name in _PROMPT_FILES), TAXONOMY_PATH, OVERRIDES_PATH):
        # The taxonomy is optional; its absence is part of the version too.
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
        digest.update(b"|")
    return digest.hexdigest()


def cache_ttl_seconds() -> float:
    """How long a general review outcome may stay in memory, from settings; 0 keeps nothing."""

    return max(0.0, float(get_settings().general_review_cache_ttl_minutes) * 60.0)


class ZDRClauseHashCache:
    """Bounded in-memory map from a paragraph's content hash to its review outcome; entries expire after ttl_seconds."""

    def __init__(self, version_hash: Optional[str] = None, max_entries: int = CACHE_MAX_ENTRIES, ttl_seconds: Optional[float] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._version_hash = version_hash
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._store: "OrderedDict[str, Tuple[float, ClauseAssessment]]" = OrderedDict()

    @property
    def ttl_seconds(self) -> float:
        if self._ttl_seconds is None:
            self._ttl_seconds = cache_ttl_seconds()
        return self._ttl_seconds

    @property
    def version_hash(self) -> str:
        if self._version_hash is None:
            self._version_hash = policy_version_hash()
        return self._version_hash

    @staticmethod
    def normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip()).casefold()

    def compute_key(
        self,
        paragraph_text: str,
        clause_title: str,
        party_context: Union[PartyContext, str, None],
        agreement_type: Optional[str],
        reviewer_context: str = "",
        clause_context: str = "",
        document_facts: str = "",
    ) -> str:
        """Key one paragraph's outcome by everything that decides it.

        party_context is the resolved PartyContext (or the reviewer's raw
        wording when there is none): "ACI" and "Accordor" resolve to the same
        parties and so share a key. clause_context is a digest of the whole
        clause the paragraph sits in, because a clause-wide finding raised on
        one paragraph is anchored on another; document_facts is the digest of
        the cross-document facts every batch is given.
        """

        party = party_context.cache_token if isinstance(party_context, PartyContext) else (party_context or "")
        payload = "|".join(
            [
                self.normalize_text(paragraph_text),
                self.normalize_text(clause_title),
                self.normalize_text(party),
                self.normalize_text(agreement_type or ""),
                self.normalize_text(reviewer_context),
                clause_context,
                document_facts,
                self.version_hash,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[ClauseAssessment]:
        cached = self._store.get(key)
        if cached is None:
            return None
        stored_at, value = cached
        if self._clock() - stored_at >= self.ttl_seconds:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: str, value: ClauseAssessment) -> None:
        if self.ttl_seconds <= 0:
            return
        self._drop_expired()
        self._store[key] = (self._clock(), value)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def _drop_expired(self) -> None:
        """Discard every entry past its time to live, so expired contract text does not sit in memory until it is next asked for."""

        now = self._clock()
        for key in [key for key, (stored_at, _) in self._store.items() if now - stored_at >= self.ttl_seconds]:
            del self._store[key]

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# Process-wide cache shared by every review.
clause_cache = ZDRClauseHashCache()
