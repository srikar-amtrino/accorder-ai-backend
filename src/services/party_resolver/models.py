"""Data models for the Party Persona Resolver."""

from typing import List, Optional
from pydantic import BaseModel, Field


class EntityPersona(BaseModel):
    """Represents a party as identified in the agreement preamble."""

    name: str = Field(description="Full legal name of the party as stated in the agreement.")
    defined_term: str = Field(description="Exact capitalized defined term used to refer to this party (e.g. 'Consultant', 'Licensee', 'Borrower').")
    transactional_position: str = Field(description="One-sentence description of this party's commercial/legal role in the transaction.")


class ClientInterests(BaseModel):
    """Key legal interests of the represented party — extracted from the agreement type and party roles."""

    assets_to_protect: List[str] = Field(
        default_factory=list,
        description="Rights, benefits, and protections the client currently holds or should hold."
    )
    burdens_to_limit: List[str] = Field(
        default_factory=list,
        description="Liabilities, obligations, and duties imposed on or against the client."
    )


class PartyContext(BaseModel):
    """Complete resolved party context for injection into LLM prompts."""

    is_active: bool = Field(
        default=False,
        description="True when the resolver successfully identified the client's defined term."
    )
    client: Optional[EntityPersona] = Field(
        default=None,
        description="The represented party (the client we are advising)."
    )
    counterparty: Optional[EntityPersona] = Field(
        default=None,
        description="The opposing party in the agreement."
    )
    client_interests: Optional[ClientInterests] = Field(
        default=None,
        description="Extracted legal interests of the client."
    )
    agreement_nature: str = Field(
        default="",
        description="Short description of the agreement type and transaction (e.g. 'Professional services agreement for financial advisory work')."
    )
    hired_counsel_directive: str = Field(
        default="",
        description="Pre-compiled prompt injection string encoding hired-counsel identity rules."
    )

    @property
    def client_defined_term(self) -> str:
        """Convenience accessor — returns the client's defined term or empty string."""
        return self.client.defined_term if self.client else ""

    @property
    def counterparty_defined_term(self) -> str:
        """Convenience accessor — returns the counterparty's defined term or empty string."""
        return self.counterparty.defined_term if self.counterparty else ""
