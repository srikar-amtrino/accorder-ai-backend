"""
test/test_cross_clause_reference.py
-----------------------------------
Comprehensive test suite for the Cross-Clause Reference Graph (CRG) system.
Tests:
  1. ContractStructuralTree (addressing, nesting, alias generation, range expansion)
  2. ReferenceExtractor (explicit sections, ranges, exhibits, statutes, implicit refs)
  3. ReferenceGraph (dependency tracking, cycle detection, DAG validation)
  4. ContextBundler (token/word budgeting, Markdown formatting, party framing)
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path("c:/accorder ai/Applications/accorder-ai-backend")
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from src.schemas.general_review import TextInformation
from src.services.reference_graph.structural_tree import ContractStructuralTree, ParaNode
from src.services.reference_graph.reference_extractor import ReferenceExtractor, ExtractedReference
from src.services.reference_graph.cycle_detector import ReferenceGraph, CycleDetectionResult
from src.services.reference_graph.context_bundler import ContextBundler


def test_structural_tree_on_sample_3():
    payload_path = PROJECT_ROOT / "test" / "documents" / "Sample 3" / "Sample_3_payload.json"
    with open(payload_path, encoding="utf-8") as f:
        payload = json.load(f)

    paras = [TextInformation(**p) for p in payload["textinformation"]]
    tree = ContractStructuralTree(paras)

    assert len(tree.nodes) == len(paras), f"Expected {len(paras)} nodes, got {len(tree.nodes)}"

    # Check Section 7 structure
    sec7_node = tree.nodes[18]
    assert "S7" in sec7_node.canonical_address, f"Expected S7, got {sec7_node.canonical_address}"

    # Check Section 7(a) Discrimination Prohibited
    sec7a_node = tree.nodes[19]
    assert sec7a_node.canonical_address == "S7.a", f"Expected S7.a, got {sec7a_node.canonical_address}"
    assert sec7a_node.heading_text == "Discrimination Prohibited"

    # Check Section 7(b) Solicitations
    sec7b_node = tree.nodes[20]
    assert sec7b_node.canonical_address == "S7.b", f"Expected S7.b, got {sec7b_node.canonical_address}"

    # Check Section 7(c) Non-Compliance
    sec7c_node = tree.nodes[21]
    assert sec7c_node.canonical_address == "S7.c", f"Expected S7.c, got {sec7c_node.canonical_address}"

    # Check Section 7(c) items
    assert tree.nodes[22].canonical_address == "S7.c.item1"
    assert tree.nodes[23].canonical_address == "S7.c.item2"

    # Check Section 7(d) Flow Through Requirements
    sec7d_node = tree.nodes[24]
    assert sec7d_node.canonical_address == "S7.d", f"Expected S7.d, got {sec7d_node.canonical_address}"

    # Test alias resolution
    res_7a = tree.resolve_reference("Section 7(a)")
    assert any(n.canonical_address == "S7.a" for n in res_7a)

    res_sec7 = tree.resolve_reference("Section 7")
    assert len(res_sec7) > 0

    # Test range resolution: Section 7(a)-(c)
    range_res = tree.resolve_reference("Section 7(a)-(c)")
    range_addrs = {n.canonical_address for n in range_res}
    assert "S7.a" in range_addrs
    assert "S7.b" in range_addrs
    assert "S7.c" in range_addrs

    # Test Exhibit A
    res_exha = tree.resolve_reference("Exhibit A")
    assert any(n.canonical_address == "ExhA" for n in res_exha)

    print("PASS: test_structural_tree_on_sample_3")


def test_reference_extractor():
    payload_path = PROJECT_ROOT / "test" / "documents" / "Sample 3" / "Sample_3_payload.json"
    with open(payload_path, encoding="utf-8") as f:
        payload = json.load(f)

    paras = [TextInformation(**p) for p in payload["textinformation"]]
    tree = ContractStructuralTree(paras)
    extractor = ReferenceExtractor(tree)

    # Check Flow Through paragraph (idx 24)
    refs_24 = extractor.extract_from_paragraph(24)
    range_refs = [r for r in refs_24 if r.ref_type == "section_range"]
    assert len(range_refs) > 0, "Expected Section 7(a)-(c) range reference"
    assert any("S7.a" in r.target_addresses for r in range_refs)

    # Check Compensation paragraph (idx 15) referencing Exhibit A and Section 2
    refs_15 = extractor.extract_from_paragraph(15)
    exh_refs = [r for r in refs_15 if r.ref_type == "exhibit"]
    sec_refs = [r for r in refs_15 if r.ref_type == "section"]
    assert len(exh_refs) > 0, "Expected Exhibit A reference in Section 3"
    assert len(sec_refs) > 0, "Expected Section 2 reference in Section 3"

    # Check statute extraction
    all_refs = extractor.extract_all()
    statute_refs = [r for r in all_refs if r.ref_type == "statute"]
    assert len(statute_refs) >= 5, f"Expected statute references, found {len(statute_refs)}"

    print("PASS: test_reference_extractor")


def test_cycle_detection():
    # Test 1: Real contract without cycles is a DAG
    payload_path = PROJECT_ROOT / "test" / "documents" / "Sample 3" / "Sample_3_payload.json"
    with open(payload_path, encoding="utf-8") as f:
        payload = json.load(f)

    paras = [TextInformation(**p) for p in payload["textinformation"]]
    tree = ContractStructuralTree(paras)
    extractor = ReferenceExtractor(tree)
    all_refs = extractor.extract_all()

    graph = ReferenceGraph(tree, all_refs)
    res = graph.detect_cycles()
    assert res.has_cycles is False
    assert res.is_dag is True

    # Test 2: Injected circular reference loop (Clause A -> Clause B -> Clause A)
    circular_refs = list(all_refs)
    circular_refs.append(
        ExtractedReference(
            source_para_index=15,
            source_address="ExhA",
            raw_reference="Section 3",
            ref_type="section",
            target_addresses=["S3"],
            target_nodes=[],
        )
    )
    cyclic_graph = ReferenceGraph(tree, circular_refs)
    cyclic_res = cyclic_graph.detect_cycles()
    assert cyclic_res.has_cycles is True, "Cycle should be detected"
    assert len(cyclic_res.cycles) > 0
    print(f"PASS: test_cycle_detection (detected expected simulated cycle: {cyclic_res.cycles})")


def test_context_bundler():
    payload_path = PROJECT_ROOT / "test" / "documents" / "Sample 3" / "Sample_3_payload.json"
    with open(payload_path, encoding="utf-8") as f:
        payload = json.load(f)

    paras = [TextInformation(**p) for p in payload["textinformation"]]
    tree = ContractStructuralTree(paras)
    extractor = ReferenceExtractor(tree)
    all_refs = extractor.extract_all()

    bundler = ContextBundler(tree, all_refs)

    # Test bundle for paragraph 24 (Section 7(a)-(c) range)
    bundle_24 = bundler.build_bundle_for_indices([24], party_represented="City")
    assert bundle_24 is not None
    assert "CROSS-CLAUSE REFERENCE CONTEXT" in bundle_24
    assert "S7.a" in bundle_24
    assert "Discrimination Prohibited" in bundle_24
    assert "Review Perspective: Representing City." in bundle_24

    # Test bundle for paragraph without references -> returns None
    bundle_empty = bundler.build_bundle_for_indices([0])
    assert bundle_empty is None

    print("PASS: test_context_bundler")


if __name__ == "__main__":
    test_structural_tree_on_sample_3()
    test_reference_extractor()
    test_cycle_detection()
    test_context_bundler()
    print("\nALL CROSS-CLAUSE REFERENCE TESTS PASSED SUCCESSFULLY!")
