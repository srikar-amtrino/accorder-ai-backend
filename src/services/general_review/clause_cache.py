"""In-memory, zero-data-retention cache of per-paragraph review outcomes.

Keys are SHA-256 digests of the normalised paragraph text, its clause title,
the resolved parties, the reviewer context, the clause and document facts the
paragraph was judged against, and the policy/prompt version; values are the computed
assessment (or an explicit "nothing to flag"). Nothing is written to disk and
no raw contract text is stored, so an unchanged clause reviewed again — in
the same document or a revised one — returns exactly the outcome it got the
first time, without another model call.
"""

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

from src.services.clause_mapping.policy_store import POLICIES_PATH
from src.services.general_review.party_resolver import PartyContext
from src.services.general_review.taxonomy_gates import OVERRIDES_PATH, TAXONOMY_PATH

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "v2" / "general_review"

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
    for path in (POLICIES_PATH, _PROMPTS_DIR / "system.mustache", _PROMPTS_DIR / "user.mustache", TAXONOMY_PATH, OVERRIDES_PATH):
        # The taxonomy is optional; its absence is part of the version too.
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
        digest.update(b"|")
    return digest.hexdigest()


class ZDRClauseHashCache:
    """Bounded in-memory map from a paragraph's content hash to its review outcome."""

    def __init__(self, version_hash: Optional[str] = None, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._version_hash = version_hash
        self._max_entries = max_entries
        self._store: "OrderedDict[str, ClauseAssessment]" = OrderedDict()

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
        if cached is not None:
            self._store.move_to_end(key)
        return cached

    def set(self, key: str, value: ClauseAssessment) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# Process-wide cache shared by every review.
clause_cache = ZDRClauseHashCache()
