from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from src.schemas.clause_mapping import ClauseMatch

# Hits requested per clause. Well above three because the vector DB holds several
# example records per clause type and classify_clause ranks distinct types.
SEARCH_TOP_K = 10


class BaseClauseSearch(ABC):
    """Finds the closest canonical clause types for clause texts in the shared vector DB."""

    @abstractmethod
    async def search(self, texts: Sequence[str], agreement_type: Optional[str], session_id: str, top_k: int = SEARCH_TOP_K) -> List[List[ClauseMatch]]:
        """Return the top_k hits for each text, in input order, filtered by agreement_type when given."""
        pass
