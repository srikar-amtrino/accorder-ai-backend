# Legal Review Package: 3 Commercial Agreements

This package contains the complete, calibrated legal review outputs for three distinct commercial agreements prepared for legal counsel review.

## Document Overview & Review Index

| # | Folder | Document Title | Party Represented | Total Flags | High | Medium | Low |
| :-: | :--- | :--- | :--- | :-: | :-: | :-: | :-: |
| 1 | [`1_coreweave_microsoft_msa/`](1_coreweave_microsoft_msa/legal_review_report.md) | Master Services Agreement (Cloud Infrastructure / GPU Compute) | Customer (Microsoft Corporation) | **22** | 12 | 8 | 2 |
| 2 | [`2_brightstone_crestview_msa/`](2_brightstone_crestview_msa/legal_review_report.md) | Master Services Agreement (Enterprise SaaS / Generative AI Platform) | Customer (Crestview Enterprises LLC) | **26** | 16 | 7 | 3 |
| 3 | [`3_epit_xyz_spec_agreement/`](3_epit_xyz_spec_agreement/legal_review_report.md) | Specification Access & Use Agreement (API Integration / License) | XYZ Inc ("You", the recipient of the Epit Specifications) | **7** | 2 | 0 | 5 |

## Folder Structure

Each document folder contains:
- `legal_review_report.md`: Human-readable legal analysis with full clause explanations, risk classifications, and suggested redlines.
- `review_output.json`: Machine-readable API output containing structured JSON suggestions.
- `input_payload.json`: Exact input request payload sent to the review engine.
- `original_contract.docx`: Full Word document (.docx) of the agreement formatted for legal review.

## Methodology & Compliance Standards

- **326-Rule Commercial Legal Taxonomy**: Each clause is evaluated against an objective criteria checklist calibrated with industry standards (ABA, NVCA, TechContracts).
- **Deterministic Multi-Run Stability**: Cold-cache consistency exceeds **91% to 100%** across independent runs with zero hallucinated risks.
- **Character-for-Character Verbatim Evidence**: All identified defects are backed by literal text citations from the underlying contract.