"""Validation script for Party Persona Resolver.

Tests:
1. Preamble extraction from paragraphs list and dicts.
2. Inactive handling for missing / neutral party.
3. Directive formatting check (presence of 4 directional rules, identity rule).
4. Live Bedrock resolution on Sample 3 (Columbia Capital Management -> Consultant).
5. Live Bedrock resolution on Sample 5 (Sunesis Pharmaceuticals -> Sponsor).
"""

import asyncio
import json
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath("."))

from src.services.party_resolver.extractor import PartyPersonaResolver
from src.services.party_resolver.directive import build_hired_counsel_directive
from src.services.party_resolver.models import EntityPersona, ClientInterests, PartyContext


async def test_offline_directive_builder():
    """Verify that build_hired_counsel_directive compiles properly with the 4 rules."""
    print("\n--- Test 1: Directive Builder Offline ---")
    client = EntityPersona(
        name="Columbia Capital Management, LLC",
        defined_term="Consultant",
        transactional_position="Financial advisory firm providing consulting services",
    )
    counterparty = EntityPersona(
        name="City of Westwood, Kansas",
        defined_term="City",
        transactional_position="Municipal government procuring financial advisory services",
    )
    interests = ClientInterests(
        assets_to_protect=["Fee payment", "Liability limitations", "IP rights"],
        burdens_to_limit=["Indemnities", "Unqualified warranties", "Unlimited re-performance"],
    )
    directive = build_hired_counsel_directive(
        client=client,
        counterparty=counterparty,
        client_interests=interests,
        agreement_nature="Professional services agreement for financial advisory services",
    )

    assert "Columbia Capital Management, LLC" in directive
    assert 'CONTRACT TERM: "Consultant"' in directive
    assert 'IDENTITY RULE (ABSOLUTE — NO EXCEPTIONS)' in directive
    assert 'RULE 1 — DUTY ON CLIENT = BURDEN' in directive
    assert 'RULE 2 — RIGHT FOR CLIENT = ASSET (DO NOT FLAG)' in directive
    assert 'RULE 3 — DUTY ON COUNTERPARTY = CLIENT PROTECTION (DO NOT FLAG)' in directive
    assert 'RULE 4 — FAVOURABLE ASYMMETRY = CLIENT WIN (DO NOT FLAG)' in directive
    assert 'Consultant' in directive
    print("✅ Directive contains all 4 directional rules and identity constraints.")


async def test_inactive_on_empty_party():
    """Verify that resolver returns is_active=False when party is None or empty."""
    print("\n--- Test 2: Inactive on Empty Party ---")
    resolver = PartyPersonaResolver()
    ctx = await resolver.resolve(None, [{"text": "Preamble..."}])
    assert not ctx.is_active, "Expected is_active=False for None party"

    ctx2 = await resolver.resolve("", [{"text": "Preamble..."}])
    assert not ctx2.is_active, "Expected is_active=False for empty party"
    print("✅ Inactive fallback works correctly.")


async def test_sample3_live_resolution():
    """Verify live resolution on Sample 3 preamble."""
    print("\n--- Test 3: Sample 3 Live Resolution (Columbia Capital Management) ---")
    sample3_path = "test/documents/Sample 3 Clause Simulation/Sample_3_clause_payload.json"
    with open(sample3_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    # Flatten clause paragraphs to simulate what general_review receives
    paragraphs = []
    for c in payload.get("clauses", []):
        for p in c.get("paragraphs", []):
            paragraphs.append(p)

    party = payload.get("party_represented")  # "Columbia Capital Management"
    print(f"Resolving for party: '{party}' with {len(paragraphs)} paragraphs...")

    resolver = PartyPersonaResolver()
    ctx = await resolver.resolve(party, paragraphs)

    print(f"Resolved is_active: {ctx.is_active}")
    print(f"Client: {ctx.client}")
    print(f"Counterparty: {ctx.counterparty}")
    print(f"Nature: {ctx.agreement_nature}")
    print(f"Client Assets: {ctx.client_interests.assets_to_protect if ctx.client_interests else None}")
    print(f"Client Burdens: {ctx.client_interests.burdens_to_limit if ctx.client_interests else None}")

    assert ctx.is_active, "Expected resolver to succeed for Sample 3"
    assert "Consultant" in ctx.client.defined_term, f"Expected defined_term 'Consultant', got: {ctx.client.defined_term}"
    assert "City" in ctx.counterparty.defined_term, f"Expected counterparty 'City', got: {ctx.counterparty.defined_term}"
    assert "Consultant" in ctx.hired_counsel_directive
    print("✅ Sample 3 successfully resolved Columbia Capital Management -> Consultant!")


async def test_sample5_live_resolution():
    """Verify live resolution on Sample 5 preamble."""
    print("\n--- Test 4: Sample 5 Live Resolution (Sunesis Pharmaceuticals) ---")
    sample5_path = "test/documents/Sample 5/Sample_5_payload.json"
    with open(sample5_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    paragraphs = payload.get("textinformation", [])
    party = payload.get("party_represented")  # "Sunesis Pharmaceuticals"
    print(f"Resolving for party: '{party}' with {len(paragraphs)} paragraphs...")

    resolver = PartyPersonaResolver()
    ctx = await resolver.resolve(party, paragraphs)

    print(f"Resolved is_active: {ctx.is_active}")
    print(f"Client: {ctx.client}")
    print(f"Counterparty: {ctx.counterparty}")
    print(f"Nature: {ctx.agreement_nature}")

    assert ctx.is_active, "Expected resolver to succeed for Sample 5"
    assert "Sponsor" in ctx.client.defined_term, f"Expected defined_term 'Sponsor', got: {ctx.client.defined_term}"
    assert "Quintiles" in ctx.counterparty.defined_term, f"Expected counterparty 'Quintiles', got: {ctx.counterparty.defined_term}"
    print("✅ Sample 5 successfully resolved Sunesis Pharmaceuticals -> Sponsor!")


async def main():
    await test_offline_directive_builder()
    await test_inactive_on_empty_party()
    await test_sample3_live_resolution()
    await test_sample5_live_resolution()
    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY! 🎉\n")


if __name__ == "__main__":
    asyncio.run(main())
