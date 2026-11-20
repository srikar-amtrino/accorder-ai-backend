"""Party Persona Extractor — identifies client and counterparty from the agreement preamble.

Uses the main Bedrock model (Sonnet during dev; swap to Haiku at deployment via PARTY_RESOLVER_MODEL_ID).
Single fast call with max_tokens=600. Falls back gracefully to inactive PartyContext on any error.
"""

import asyncio
import json
import logging
import re
import time
from typing import List, Optional, Sequence

import boto3
from botocore.config import Config

from src.config.settings import get_settings
from src.services.party_resolver.directive import build_hired_counsel_directive
from src.services.party_resolver.models import (
    ClientInterests,
    EntityPersona,
    PartyContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM constants for the resolver — cheap, fast, structured output
# ---------------------------------------------------------------------------
_RESOLVER_MAX_TOKENS = 700
_RESOLVER_TEMPERATURE = 0.0
_PREAMBLE_CHAR_LIMIT = 1200   # First ~600-800 words of agreement text

_RESOLVER_SYSTEM_PROMPT = """\
You are a contract analysis specialist. Your ONLY task is to identify the two main parties to an agreement from its opening text and determine which party is the represented client.

Extract the information requested in strict JSON only. Do not explain or add commentary.
"""

_RESOLVER_USER_TEMPLATE = """\
AGREEMENT OPENING TEXT:
{preamble}

REPRESENTED PARTY (the client we are advising):
{party_represented}

TASK:
1. Find the exact legal name and capitalized defined term of EACH party.
2. Determine which party is the client (matches or is associated with: "{party_represented}").
3. Describe each party's commercial/legal role in one short sentence.
4. List the client's top 2-3 legal assets (rights/benefits) and top 2-3 legal burdens (obligations/liabilities) based on the agreement type and party roles.
5. Write a one-line description of the agreement type and transaction.

IMPORTANT RULES:
- The "defined_term" must be the EXACT capitalized label used in the agreement (e.g. "Consultant", "Licensee", "Borrower", "Seller", "Tenant").
- If you cannot find a defined term, use the party's short name.
- Do NOT invent parties or defined terms that are not in the text.
- Match the client by name similarity — partial matches are acceptable (e.g. "Columbia Capital" matches "Columbia Capital Management, LLC").

Respond ONLY with this JSON (no markdown fences, no extra keys):
{{
  "client": {{
    "name": "<full legal name>",
    "defined_term": "<Exact defined term>",
    "transactional_position": "<one sentence>"
  }},
  "counterparty": {{
    "name": "<full legal name>",
    "defined_term": "<Exact defined term>",
    "transactional_position": "<one sentence>"
  }},
  "client_interests": {{
    "assets_to_protect": ["<asset 1>", "<asset 2>"],
    "burdens_to_limit": ["<burden 1>", "<burden 2>"]
  }},
  "agreement_nature": "<One line: agreement type and transaction purpose>"
}}
"""


class PartyPersonaResolver:
    """Resolves the represented party's contract persona from the agreement preamble."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = self._build_bedrock_client()

    def _build_bedrock_client(self):
        """Build a Bedrock runtime client using settings credentials."""
        kwargs = {
            "region_name": self._settings.aws_region,
            "config": Config(
                read_timeout=30,
                connect_timeout=10,
                retries={"max_attempts": 1},
            ),
        }
        if self._settings.env == "development":
            if self._settings.aws_access_key_id:
                kwargs["aws_access_key_id"] = self._settings.aws_access_key_id
            if self._settings.aws_secret_access_key:
                kwargs["aws_secret_access_key"] = self._settings.aws_secret_access_key
        return boto3.client("bedrock-runtime", **kwargs)

    @staticmethod
    def _extract_preamble(paragraphs) -> str:
        """Extract the first ~1200 chars from the agreement for party identification."""
        chunks: List[str] = []
        total = 0
        for p in paragraphs:
            text = getattr(p, "text", None) or (p.get("text", "") if isinstance(p, dict) else "")
            text = text.strip()
            if not text:
                continue
            chunks.append(text)
            total += len(text)
            if total >= _PREAMBLE_CHAR_LIMIT:
                break
        return "\n\n".join(chunks)[:_PREAMBLE_CHAR_LIMIT]

    def _call_bedrock_raw(self, user_message: str) -> str:
        """Synchronous raw Bedrock text call — returns the text content block."""
        native_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": _RESOLVER_MAX_TOKENS,
            "temperature": _RESOLVER_TEMPERATURE,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_message}],
                }
            ],
            "system": _RESOLVER_SYSTEM_PROMPT,
        }
        start = time.time()
        response = self._client.invoke_model(
            modelId=self._settings.bedrock_model_id,
            body=json.dumps(native_request),
        )
        body = json.loads(response["body"].read())
        elapsed = time.time() - start
        logger.info(
            "PartyPersonaResolver: bedrock call complete in %.2fs tokens_in=%s tokens_out=%s",
            elapsed,
            body.get("usage", {}).get("input_tokens"),
            body.get("usage", {}).get("output_tokens"),
        )
        content = body.get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block.get("text", "")
        return ""

    def _parse_response(self, raw: str) -> Optional[dict]:
        """Strip markdown fences and parse JSON from the model response."""
        raw = raw.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```\s*$", "", raw, flags=re.IGNORECASE)
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try to find JSON object within text
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        logger.warning("PartyPersonaResolver: failed to parse JSON response: %s", raw[:300])
        return None

    def _build_party_context(self, data: dict, party_represented: str) -> PartyContext:
        """Build a PartyContext from parsed LLM data."""
        client_data = data.get("client", {})
        cp_data = data.get("counterparty", {})
        ci_data = data.get("client_interests", {})

        client = EntityPersona(
            name=client_data.get("name", party_represented),
            defined_term=client_data.get("defined_term", party_represented),
            transactional_position=client_data.get("transactional_position", ""),
        )
        counterparty = EntityPersona(
            name=cp_data.get("name", "Counterparty"),
            defined_term=cp_data.get("defined_term", "Counterparty"),
            transactional_position=cp_data.get("transactional_position", ""),
        )
        client_interests = ClientInterests(
            assets_to_protect=ci_data.get("assets_to_protect", []),
            burdens_to_limit=ci_data.get("burdens_to_limit", []),
        )
        agreement_nature = data.get("agreement_nature", "")

        # Build the directive string
        directive = build_hired_counsel_directive(
            client=client,
            counterparty=counterparty,
            client_interests=client_interests,
            agreement_nature=agreement_nature,
        )

        return PartyContext(
            is_active=True,
            client=client,
            counterparty=counterparty,
            client_interests=client_interests,
            agreement_nature=agreement_nature,
            hired_counsel_directive=directive,
        )

    async def resolve(
        self,
        party_represented: Optional[str],
        paragraphs,
    ) -> PartyContext:
        """
        Resolve party personas from the agreement preamble.

        Args:
            party_represented: The client's name as supplied in the review request.
            paragraphs: The agreement paragraph objects (TextInformation list or dicts).

        Returns:
            PartyContext — always returns a valid object; is_active=False on failure.
        """
        # Short-circuit: if no party_represented, skip resolution
        if not party_represented or not party_represented.strip():
            logger.info("PartyPersonaResolver: no party_represented supplied — resolver inactive.")
            return PartyContext(is_active=False)

        preamble = self._extract_preamble(paragraphs)
        if not preamble.strip():
            logger.warning("PartyPersonaResolver: empty preamble — resolver inactive.")
            return PartyContext(is_active=False)

        user_message = _RESOLVER_USER_TEMPLATE.format(
            preamble=preamble,
            party_represented=party_represented.strip(),
        )

        try:
            raw = await asyncio.to_thread(self._call_bedrock_raw, user_message)
        except Exception as exc:
            logger.warning("PartyPersonaResolver: bedrock call failed: %s", exc)
            return PartyContext(is_active=False)

        parsed = self._parse_response(raw)
        if not parsed:
            logger.warning("PartyPersonaResolver: could not parse response — resolver inactive.")
            return PartyContext(is_active=False)

        try:
            ctx = self._build_party_context(parsed, party_represented)
            logger.info(
                "PartyPersonaResolver: resolved — client=%s defined_term=%s counterparty=%s defined_term=%s",
                ctx.client.name if ctx.client else "?",
                ctx.client.defined_term if ctx.client else "?",
                ctx.counterparty.name if ctx.counterparty else "?",
                ctx.counterparty.defined_term if ctx.counterparty else "?",
            )
            return ctx
        except Exception as exc:
            logger.warning("PartyPersonaResolver: context build failed: %s", exc)
            return PartyContext(is_active=False)
