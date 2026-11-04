import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config.logging import get_logger
from src.schemas.clause_mapping import ClausePolicy

logger = get_logger(__name__)

# The legal team's clause taxonomy and inherent risk tiers; standards, triggers,
# and fallbacks fill in as they supply them. Labels must match the vector DB
# records' canonical_type / sub_type exactly.
POLICIES_PATH = Path(__file__).resolve().parent / "clause_policies.json"

# (canonical_type, sub_type, agreement_type), each folded by _label.
_Key = Tuple[Optional[str], Optional[str], Optional[str]]


def _label(value: Optional[str]) -> Optional[str]:
    """Fold case and whitespace so 'Staffing  Levels' matches 'staffing levels'; blank means unset."""

    if value is None:
        return None
    return " ".join(value.split()).casefold() or None


class ClausePolicyStore:
    """Looks up the legal team's policy for a clause type mapped from the vector DB."""

    def __init__(self, policies: List[ClausePolicy]) -> None:
        self._policies: Dict[_Key, ClausePolicy] = {}
        for policy in policies:
            key = (_label(policy.canonical_type), _label(policy.sub_type), _label(policy.agreement_type))
            if key in self._policies:
                raise ValueError(f"Duplicate clause policy for canonical_type={policy.canonical_type!r}, sub_type={policy.sub_type!r}, agreement_type={policy.agreement_type!r}.")
            self._policies[key] = policy

    @classmethod
    def from_file(cls, path: Path = POLICIES_PATH) -> "ClausePolicyStore":
        """Load and validate a policy file; a malformed row fails loudly instead of silently dropping a rule."""

        rows = json.loads(path.read_text(encoding="utf-8"))
        store = cls([ClausePolicy.model_validate(row) for row in rows])
        logger.info("Loaded clause policies.", count=len(store), path=str(path))
        return store

    def __len__(self) -> int:
        return len(self._policies)

    def get(self, canonical_type: str, sub_type: Optional[str] = None, agreement_type: Optional[str] = None) -> Optional[ClausePolicy]:
        """Return the most specific policy for a clause type, or None when the legal team has none.

        Tries, in order: this sub-type in this agreement type; this sub-type in
        any agreement; the whole clause type in this agreement; the whole clause
        type in any agreement. A general policy therefore covers every sub-type
        and agreement type until a more specific one is added.
        """

        canonical = _label(canonical_type)
        sub = _label(sub_type)
        agreement = _label(agreement_type)
        for key in ((canonical, sub, agreement), (canonical, sub, None), (canonical, None, agreement), (canonical, None, None)):
            policy = self._policies.get(key)
            if policy is not None:
                return policy
        return None


@lru_cache
def get_clause_policy_store() -> ClausePolicyStore:
    """Get the process-wide policy store, loaded from disk on first use."""

    return ClausePolicyStore.from_file()
