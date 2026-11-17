"""
Cross-Clause Reference Graph (CRG) System
-----------------------------------------
Resolves explicit and implicit cross-clause references across contract paragraphs,
assigns hierarchical addresses (S7, S7.a, S7.a.i), builds reference links,
detects cyclic dependencies, and produces bounded context bundles for review evaluation.
"""

from src.services.reference_graph.structural_tree import ContractStructuralTree, ParaNode
from src.services.reference_graph.reference_extractor import ReferenceExtractor, ExtractedReference
from src.services.reference_graph.cycle_detector import ReferenceGraph, CycleDetectionResult
from src.services.reference_graph.context_bundler import ContextBundler

__all__ = [
    "ContractStructuralTree",
    "ParaNode",
    "ReferenceExtractor",
    "ExtractedReference",
    "ReferenceGraph",
    "CycleDetectionResult",
    "ContextBundler",
]
