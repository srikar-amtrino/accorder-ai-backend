"""Party Persona Resolver — universal hired-counsel identity layer.

Resolves the represented party's contract defined term and commercial role
from the agreement preamble, then compiles a Hired Counsel Directive that
is injected into extraction and explanation prompts so the LLM never
confuses the client's name with its defined term.
"""

from .models import PartyContext, EntityPersona, ClientInterests
from .extractor import PartyPersonaResolver

__all__ = [
    "PartyPersonaResolver",
    "PartyContext",
    "EntityPersona",
    "ClientInterests",
]
