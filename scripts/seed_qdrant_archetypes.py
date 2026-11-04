"""Seed the shared clause vector DB with gold-standard archetype clauses for every policy slug.

Layer 2 of the general review's clause classifier (see
src/services/clause_mapping/resilient_classifier.py) looks a clause up in the
Qdrant collection by similarity. That only works for clause types the
collection contains, so this script writes four to six exemplar clauses per
canonical_type in clause_policies.json — drawn from MSA, NDA, SaaS, and SOW
drafting — embedded with the same bge-m3 service the classifier queries with.

Point ids are UUID5 digests of (slug, text), so re-running the script replaces
the same points instead of duplicating them. Payloads carry exactly what
QdrantClauseSearch filters on and reads back: label="Clause", is_exhibit=False,
canonical_type, sub_type.

Run from the repository root, with QDRANT_URL / QDRANT_COLLECTION /
QDRANT_API_KEY / EMBEDDING_SERVICE_URL set as for the API:

    poetry run python scripts/seed_qdrant_archetypes.py            # embed + upsert
    poetry run python scripts/seed_qdrant_archetypes.py --dry-run  # validate the exemplar table only
"""

# flake8: noqa: E501  (the exemplar table holds full legal sentences; wrapping them would hurt readability)
import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.clause_mapping.policy_store import POLICIES_PATH  # noqa: E402
from src.services.vector_store.embeddings.embedding_client import EmbeddingClient  # noqa: E402
from src.services.vector_store.qdrant_db import QDRANT_COLLECTION, QDRANT_URL, _get_headers, ensure_collection, upsert_vectors  # noqa: E402

# Fixed namespace so the same (slug, text) always yields the same point id.
SEED_NAMESPACE = uuid.UUID("4f2c7c2e-7b4d-4a7f-9c9e-ac0de5000001")
SEED_SOURCE = "archetype_seed_v1"

MIN_PER_SLUG = 4
MAX_PER_SLUG = 6

# Texts per embedding request; the service is single-threaded so this only saves round trips.
EMBED_BATCH_SIZE = 16
EMBED_TIMEOUT_SECONDS = 180.0

MSA, NDA, SAAS, SOW = "MSA", "NDA", "SaaS", "SOW"

# canonical_type -> [(agreement_type, exemplar clause text)]
ARCHETYPES: Dict[str, List[Tuple[str, str]]] = {
    "definitions-and-interpretation": [
        (
            MSA,
            'Definitions. "Affiliate" means any entity that directly or indirectly controls, is controlled by, or is under common control with a Party, where control means ownership of more than fifty percent of the voting interests.',
        ),
        (
            MSA,
            'Definitions and Interpretation. "Deliverables" means all work product, documentation, software, and materials developed by Service Provider for Client under a Statement of Work. Headings are for convenience only and do not affect interpretation.',
        ),
        (
            NDA,
            'Definitions. "Confidential Information" means all non-public information disclosed by either Party, whether orally, in writing, or electronically, that is designated as confidential or that a reasonable person would understand to be confidential given the nature of the information.',
        ),
        (
            SAAS,
            'Definitions. "Subscription Term" means the period during which Customer is authorized to access the Service as specified in the applicable Order Form. "Users" means Customer\'s employees and contractors authorized to use the Service.',
        ),
        (
            MSA,
            "Interpretation. In this Agreement, unless the context otherwise requires, words in the singular include the plural, the word including means including without limitation, and references to Sections and Exhibits are to sections of and exhibits to this Agreement.",
        ),
    ],
    "purpose": [
        (
            MSA,
            "Purpose. The purpose of this Agreement is to set out the terms and conditions under which Service Provider will provide professional services to Client as described in one or more Statements of Work executed from time to time.",
        ),
        (
            NDA,
            "Purpose. The Parties wish to explore a potential business relationship and, in connection with that evaluation, may disclose Confidential Information to each other solely for that purpose.",
        ),
        (
            MSA,
            "Background. Client wishes to engage Service Provider to perform certain consulting and implementation services, and Service Provider wishes to perform those services, on the terms set out in this Agreement. The recitals are for context only and do not create binding obligations.",
        ),
        (
            SAAS,
            "Overview. Provider operates a cloud-based platform for contract analysis and wishes to make the platform available to Customer, and Customer wishes to subscribe to the platform, in accordance with this Agreement.",
        ),
        (
            SOW,
            "Objective. This Statement of Work describes the migration of Client's on-premises document repository to the Provider-hosted environment, the project phases, and the responsibilities of each Party.",
        ),
    ],
    "scope-of-services": [
        (
            MSA,
            "Scope of Services. Service Provider shall perform the services described in each mutually executed Statement of Work in accordance with the specifications, milestones, and timelines set out therein. Any change to the scope of the Services requires a written Change Order signed by both Parties.",
        ),
        (
            SOW,
            "Services. Provider will deliver the following services: requirements workshops, solution design, configuration of the platform, data migration, user acceptance testing support, and go-live assistance, as further detailed in the project plan attached as Schedule A.",
        ),
        (
            SAAS,
            "Scope of the Service. Provider shall make the Service available to Customer in accordance with the Documentation and the applicable Order Form. Provider shall not materially decrease the functionality of the Service during the Subscription Term.",
        ),
        (
            MSA,
            "Description of Services. The Services consist of the consulting, development, and support activities set out in Exhibit A. Work not expressly described in an executed Statement of Work is out of scope and will not be invoiced unless agreed in writing.",
        ),
        (
            SOW,
            "Out of Scope. The following are expressly excluded from this Statement of Work: custom integrations not listed in Section 3, training beyond two administrator sessions, and support for third-party systems not identified in the project plan.",
        ),
    ],
    "statements-of-work-and-work-orders": [
        (
            MSA,
            "Statements of Work. The Parties may from time to time execute Statements of Work describing the Services, Deliverables, fees, schedule, and acceptance criteria. Each Statement of Work is incorporated into and governed by this Agreement, and in the event of conflict the terms of this Agreement prevail unless the Statement of Work expressly identifies the Section it overrides.",
        ),
        (
            MSA,
            "Work Orders. Client may request Services by issuing a Work Order. No Work Order is binding until signed by authorized representatives of both Parties. Each Work Order shall specify the scope, deliverables, fees, and estimated timeline for the Services.",
        ),
        (
            SOW,
            "Statement of Work Number 3. This Statement of Work is entered into under and subject to the Master Services Agreement dated January 15, 2024 between the Parties. Capitalized terms not defined here have the meanings given in the Master Services Agreement.",
        ),
        (
            SAAS,
            "Order Forms. Customer may purchase subscriptions and professional services by executing Order Forms referencing this Agreement. Each Order Form identifies the services purchased, the Subscription Term, the number of Users, and the fees payable.",
        ),
        (
            MSA,
            "Precedence of Work Orders. Each Work Order constitutes a separate agreement between the Parties incorporating the terms of this Master Agreement. Terms in a Work Order that conflict with this Agreement apply only to that Work Order and only if the Work Order expressly states the intent to override.",
        ),
    ],
    "customer-obligations": [
        (
            MSA,
            "Client Responsibilities. Client shall provide Service Provider with timely access to Client personnel, systems, information, and facilities reasonably required to perform the Services, and shall designate a project manager with authority to make decisions on Client's behalf.",
        ),
        (
            SAAS,
            "Customer Obligations. Customer shall use the Service only in accordance with the Documentation and applicable law, maintain the confidentiality of User credentials, and promptly notify Provider of any unauthorized access to or use of the Service.",
        ),
        (
            SOW,
            "Client Dependencies. Client will provide the sample data set, environment credentials, and sign-off on the solution design document within five business days of request. Delays in Client dependencies entitle Provider to an equitable day-for-day extension of affected milestones upon prompt written notice.",
        ),
        (
            MSA,
            "Cooperation. Client acknowledges that Service Provider's performance depends on Client's timely cooperation and shall perform the Client responsibilities set out in each Statement of Work. Service Provider is excused from delays to the extent caused by Client's failure to perform those responsibilities.",
        ),
        (
            SAAS,
            "Acceptable Use. Customer shall not, and shall not permit Users to, reverse engineer the Service, use the Service to store unlawful material, interfere with the integrity or performance of the Service, or access the Service to build a competing product.",
        ),
    ],
    "service-provider-obligations": [
        (
            MSA,
            "Service Provider Obligations. Service Provider shall perform the Services in a professional and workmanlike manner, using qualified personnel, in accordance with industry standards and all applicable laws, and in compliance with Client's reasonable security, safety, and conduct policies while on Client premises or accessing Client systems.",
        ),
        (
            SAAS,
            "Provider Responsibilities. Provider shall make the Service available in accordance with the Service Level Agreement, provide standard support, maintain administrative, physical, and technical safeguards for Customer Data, and use Customer Data only to provide the Service.",
        ),
        (
            MSA,
            "Standard of Performance. Service Provider shall exercise due care, skill, and diligence in performing the Services and shall promptly notify Client in writing of any circumstance likely to delay the delivery of any Deliverable or the achievement of any milestone.",
        ),
        (
            SOW,
            "Provider Responsibilities. Provider will assign a dedicated project manager, deliver weekly status reports, maintain the project plan and risk register, and complete each phase in accordance with the acceptance criteria set out in this Statement of Work.",
        ),
        (
            MSA,
            "Compliance with Client Policies. While performing Services at Client locations or on Client systems, Service Provider personnel shall comply with Client's information security, health and safety, and site access policies as provided to Service Provider in writing.",
        ),
    ],
    "suspension-and-termination": [
        (
            MSA,
            "Termination for Cause. Either Party may terminate this Agreement or any Statement of Work upon written notice if the other Party materially breaches this Agreement and fails to cure the breach within thirty days after receiving written notice describing the breach.",
        ),
        (
            MSA,
            "Termination for Convenience. Client may terminate this Agreement or any Statement of Work for convenience upon thirty days' prior written notice to Service Provider, in which case Client shall pay for Services performed and accepted through the effective date of termination.",
        ),
        (
            SAAS,
            "Suspension. Provider may suspend Customer's access to the Service upon written notice if Customer's use poses a security risk to the Service or any third party, or if undisputed fees remain unpaid thirty days after written notice of non-payment, and shall restore access promptly once the cause is resolved.",
        ),
        (
            MSA,
            "Termination for Insolvency. Either Party may terminate this Agreement immediately upon written notice if the other Party becomes insolvent, makes an assignment for the benefit of creditors, or becomes subject to bankruptcy, receivership, or similar proceedings that are not dismissed within sixty days.",
        ),
        (
            NDA,
            "Term and Termination. This Agreement remains in effect for two years from the Effective Date unless terminated earlier by either Party upon thirty days' written notice. The obligations of confidentiality survive termination for the period set out in Section 5.",
        ),
        (
            SAAS,
            "Effect of Termination. Upon expiration or termination, Customer's right to access the Service ceases, Provider shall make Customer Data available for export for thirty days, and Provider shall refund any prepaid fees covering the remainder of the Subscription Term if Customer terminated for Provider's uncured breach.",
        ),
    ],
    "deliverables-and-acceptance": [
        (
            MSA,
            "Acceptance. Client shall have fifteen business days after delivery of each Deliverable to test it against the acceptance criteria in the applicable Statement of Work. If Client notifies Service Provider in writing of any non-conformity within that period, Service Provider shall correct it at no additional charge within ten business days.",
        ),
        (
            SOW,
            "Acceptance Criteria. A Deliverable is accepted when it performs in all material respects in accordance with the specifications in Appendix B, as confirmed by Client's written acceptance or by Client's failure to deliver a written rejection within ten business days after delivery.",
        ),
        (
            MSA,
            "Rejection and Re-performance. If a Deliverable fails acceptance testing a second time, Client may, at its option, terminate the applicable Statement of Work and receive a refund of all fees paid for the non-conforming Deliverable, or require Service Provider to continue correcting the Deliverable.",
        ),
        (
            SOW,
            "Deliverables. Provider shall deliver the following: a solution design document, configured production and test environments, migrated data validated against the reconciliation report, administrator training materials, and a go-live readiness report.",
        ),
        (
            MSA,
            "Deliverables. Service Provider shall deliver the Deliverables described in each Statement of Work on or before the delivery dates specified. Deliverables shall conform to the specifications and documentation set out in the Statement of Work.",
        ),
    ],
    "service-levels": [
        (
            SAAS,
            "Service Levels. Provider shall make the Service available ninety-nine point nine percent of the time in each calendar month, excluding scheduled maintenance notified at least seventy-two hours in advance and performed outside Customer's core business hours.",
        ),
        (
            SAAS,
            "Service Credits. If Provider fails to meet the Availability Commitment in any month, Customer is entitled to a service credit equal to ten percent of the monthly fees for that month, rising to twenty-five percent if availability falls below ninety-nine percent, applied against the next invoice.",
        ),
        (
            SAAS,
            "Chronic Failure. If Provider fails to meet the Availability Commitment in any three consecutive months or in any four months within a twelve-month period, Customer may terminate the affected subscription for cause and receive a pro-rata refund of prepaid fees.",
        ),
        (
            MSA,
            "Response and Resolution Times. Service Provider shall respond to Severity 1 incidents within one hour and resolve or provide a workaround within four hours, respond to Severity 2 incidents within four hours, and respond to Severity 3 incidents within one business day.",
        ),
        (
            SAAS,
            "Uptime Measurement. Availability is calculated monthly as total minutes in the month less minutes of unavailability, divided by total minutes in the month, as measured by Provider's monitoring systems and reported to Customer on request.",
        ),
    ],
    "support-and-maintenance": [
        (
            SAAS,
            "Support. Provider shall provide technical support to Customer's designated administrators by email and telephone during business hours, and twenty-four hours a day, seven days a week for Severity 1 issues, at no additional charge during the Subscription Term.",
        ),
        (
            SAAS,
            "Maintenance and Updates. Provider shall provide all bug fixes, security patches, updates, and upgrades to the Service that it makes generally available to its customers, at no additional charge, as part of the subscription fees.",
        ),
        (
            MSA,
            "Software Maintenance. During the Maintenance Term, Licensor shall provide error corrections, updates, and new releases of the Software, and shall maintain support for each major release for at least twenty-four months after the release of its successor.",
        ),
        (
            SAAS,
            "End of Life. Provider shall not discontinue the Service or any material feature during a paid Subscription Term without providing at least twelve months' prior written notice and reasonable migration assistance, and shall refund prepaid fees for any discontinued functionality.",
        ),
        (
            SAAS,
            "Severity Levels. Severity 1 means the Service is unavailable or a critical function is unusable in production with no workaround. Provider shall acknowledge Severity 1 tickets within thirty minutes and provide status updates at least hourly until resolution.",
        ),
    ],
    "change-control": [
        (
            MSA,
            "Change Control. Either Party may request a change to the scope, schedule, or fees of a Statement of Work by submitting a written change request. Service Provider shall provide a written impact assessment within five business days, and no change takes effect until a Change Order is signed by both Parties.",
        ),
        (
            SOW,
            "Change Orders. Any work outside the scope defined in this Statement of Work requires a Change Order describing the additional work, the effect on the schedule, and the associated fees, executed by authorized representatives of both Parties before the work begins.",
        ),
        (
            MSA,
            "Change Procedure. Service Provider shall continue to perform the Services in accordance with the existing Statement of Work while a change request is being evaluated. Impact assessments are provided at no charge to Client.",
        ),
        (
            SAAS,
            "Changes to the Service. Provider may modify the Service from time to time provided that no modification materially reduces the functionality, security, or performance of the Service during the Subscription Term. Provider shall give Customer at least sixty days' notice of any material change.",
        ),
        (
            MSA,
            "Scope Changes. No oral agreement, course of dealing, or email exchange modifies the scope or fees of any Statement of Work. Service Provider shall not be entitled to payment for any work performed outside the agreed scope without a signed Change Order.",
        ),
    ],
    "fees-and-payment": [
        (
            MSA,
            "Fees and Payment. Client shall pay the fees set out in each Statement of Work within thirty days after receipt of a valid and undisputed invoice. Client may withhold payment of any amount disputed in good faith, provided it notifies Service Provider of the dispute in writing before the due date.",
        ),
        (
            MSA,
            "Expenses. Client shall reimburse Service Provider for reasonable, pre-approved travel and out-of-pocket expenses incurred in performing the Services, billed at actual cost without markup and supported by receipts, in accordance with Client's travel policy.",
        ),
        (
            SAAS,
            "Subscription Fees. Customer shall pay the subscription fees specified in the Order Form annually in advance. Fees for renewal terms shall not increase by more than five percent over the fees for the preceding term unless Provider gives at least ninety days' notice before renewal.",
        ),
        (
            MSA,
            "Late Payment. Undisputed amounts not paid when due accrue interest at the lesser of one percent per month or the maximum rate permitted by law, calculated from the due date until paid. Service Provider shall not suspend Services for non-payment without thirty days' prior written notice.",
        ),
        (
            MSA,
            "Taxes. Fees are exclusive of sales, use, value added, and similar taxes, which Client shall pay where applicable, excluding taxes based on Service Provider's income, employment, or property, for which Service Provider is solely responsible.",
        ),
        (
            SOW,
            "Pricing. The Services under this Statement of Work are provided on a time and materials basis at the rates in Appendix C, not to exceed a total of two hundred fifty thousand dollars without Client's prior written approval. Invoices are issued monthly in arrears with supporting timesheets.",
        ),
    ],
    "confidentiality": [
        (
            NDA,
            "Confidentiality. Each Party shall hold the other Party's Confidential Information in strict confidence, use it solely for the Purpose, and not disclose it to any third party except to its employees, advisors, and contractors who need to know it and are bound by written obligations at least as protective as these.",
        ),
        (
            NDA,
            "Exclusions. Confidential Information does not include information that is or becomes publicly available through no fault of the receiving Party, was known to the receiving Party before disclosure, is independently developed without use of the Confidential Information, or is rightfully received from a third party without restriction.",
        ),
        (
            NDA,
            "Compelled Disclosure. If the receiving Party is required by law, regulation, or court order to disclose Confidential Information, it shall, where legally permitted, give the disclosing Party prompt written notice and reasonable cooperation so that the disclosing Party may seek a protective order.",
        ),
        (
            MSA,
            "Confidential Information. The obligations in this Section continue for three years after termination of this Agreement, and indefinitely with respect to trade secrets and Personal Data. Upon request, the receiving Party shall return or destroy all Confidential Information and certify destruction in writing.",
        ),
        (
            SAAS,
            "Non-Disclosure. Provider shall treat Customer Data and Customer's business information as Confidential Information and shall protect it using at least the same degree of care it uses for its own confidential information, and no less than reasonable care.",
        ),
        (
            NDA,
            "Standard of Care. The receiving Party shall protect the Confidential Information using the same degree of care it uses to protect its own confidential information of a similar nature, but in no event less than a reasonable degree of care.",
        ),
    ],
    "data-protection-and-privacy": [
        (
            SAAS,
            "Data Protection. Provider shall process Personal Data only on Customer's documented instructions, implement appropriate technical and organizational measures to protect it, and assist Customer in responding to data subject requests and regulatory inquiries.",
        ),
        (
            MSA,
            "Personal Data. To the extent Service Provider processes Personal Data on behalf of Client, the Parties shall comply with the Data Processing Addendum attached as Exhibit D, which is incorporated into this Agreement and prevails over conflicting terms with respect to the processing of Personal Data.",
        ),
        (
            SAAS,
            "Data Breach Notification. Provider shall notify Customer without undue delay, and in any event within forty-eight hours, after becoming aware of a Personal Data breach affecting Customer Data, and shall provide the information reasonably required for Customer to meet its notification obligations.",
        ),
        (
            SAAS,
            "Sub-processors. Provider shall not engage a sub-processor to process Customer Personal Data without prior written notice to Customer, shall give Customer the opportunity to object, shall impose data protection obligations on the sub-processor no less protective than these, and remains fully liable for the sub-processor's performance.",
        ),
        (
            MSA,
            "Cross-Border Transfers. Service Provider shall not transfer Personal Data outside the jurisdiction in which it was collected without Client's prior written consent and without appropriate safeguards, such as the Standard Contractual Clauses, being in place.",
        ),
        (
            SAAS,
            "Use of Customer Data. Provider shall not sell Customer Data, use it for advertising, or use it to train machine learning models made available to other customers, and shall use Customer Data solely to provide and improve the Service for Customer.",
        ),
    ],
    "intellectual-property": [
        (
            MSA,
            "Intellectual Property Rights. All Deliverables are works made for hire for Client. To the extent any Deliverable does not qualify as a work made for hire, Service Provider hereby irrevocably assigns to Client all right, title, and interest in and to the Deliverables, including all intellectual property rights.",
        ),
        (
            MSA,
            "Pre-Existing IP. Each Party retains all rights in its pre-existing intellectual property. Service Provider grants Client a perpetual, irrevocable, worldwide, royalty-free, non-exclusive license to use any Service Provider background technology incorporated into the Deliverables as part of the Deliverables.",
        ),
        (
            SAAS,
            "Ownership. Provider and its licensors own all rights in the Service, the Documentation, and all improvements and derivative works. Customer owns all rights in Customer Data and grants Provider a limited license to use Customer Data solely to provide the Service.",
        ),
        (
            NDA,
            "No License. Nothing in this Agreement grants either Party any right, title, or license in the other Party's intellectual property, and all Confidential Information remains the property of the disclosing Party.",
        ),
        (
            MSA,
            "Further Assurances. Service Provider shall execute, and shall cause its personnel to execute, all documents reasonably requested by Client to perfect, register, or enforce Client's ownership of the Deliverables, and waives any moral rights in the Deliverables to the fullest extent permitted by law.",
        ),
        (
            SOW,
            "Work Product. All work product created under this Statement of Work, including source code, configurations, documentation, and reports, is owned exclusively by Client upon creation and payment, and Provider retains no right to reuse Client-specific work product.",
        ),
    ],
    "representations-and-warranties": [
        (
            MSA,
            "Representations and Warranties. Each Party represents and warrants that it has full power and authority to enter into this Agreement, that its performance will not violate any agreement to which it is a party, and that it will comply with all applicable laws in performing this Agreement.",
        ),
        (
            MSA,
            "Service Warranty. Service Provider warrants that the Services will be performed in a professional and workmanlike manner by qualified personnel in accordance with industry standards, and that the Deliverables will conform to their specifications for ninety days after acceptance.",
        ),
        (
            SAAS,
            "Warranties. Provider warrants that the Service will perform materially in accordance with the Documentation, that it will not materially decrease the functionality of the Service during the Subscription Term, and that the Service will not contain viruses or malicious code.",
        ),
        (
            MSA,
            "Warranty Remedy. If Service Provider breaches the service warranty, Client shall notify Service Provider in writing and Service Provider shall re-perform the non-conforming Services at no charge. If Service Provider fails to cure within thirty days, Client may terminate the affected Statement of Work and receive a refund of fees paid for the non-conforming Services.",
        ),
        (
            SAAS,
            "Disclaimer. Except as expressly set out in this Section, the Service is provided as is and Provider disclaims all other warranties, express or implied, including implied warranties of merchantability, fitness for a particular purpose, and non-infringement.",
        ),
        (
            MSA,
            "Non-Infringement Warranty. Service Provider warrants that the Deliverables and Client's use of them in accordance with this Agreement will not infringe or misappropriate any patent, copyright, trademark, trade secret, or other intellectual property right of any third party.",
        ),
    ],
    "indemnification": [
        (
            MSA,
            "Indemnification by Service Provider. Service Provider shall defend, indemnify, and hold harmless Client and its Affiliates, officers, directors, and employees from and against all third-party claims, damages, liabilities, costs, and expenses, including reasonable attorneys' fees, arising out of Service Provider's gross negligence, willful misconduct, breach of applicable law, or bodily injury or property damage caused by Service Provider personnel.",
        ),
        (
            SAAS,
            "IP Infringement Indemnity. Provider shall defend Customer against any claim that the Service infringes a third party's intellectual property rights and shall pay any damages finally awarded or agreed in settlement. If the Service is enjoined, Provider shall procure the right to continue use, modify the Service to be non-infringing, or terminate the subscription and refund prepaid fees.",
        ),
        (
            MSA,
            "Indemnification Procedure. The indemnified Party shall give the indemnifying Party prompt written notice of the claim, sole control of the defense and settlement, and reasonable cooperation at the indemnifying Party's expense. The indemnifying Party shall not settle any claim in a manner that admits fault or imposes obligations on the indemnified Party without its prior written consent.",
        ),
        (
            MSA,
            "Mutual Indemnity. Each Party shall indemnify the other against third-party claims arising from the indemnifying Party's breach of its confidentiality or data protection obligations, its violation of applicable law, or the gross negligence or willful misconduct of its personnel.",
        ),
        (
            SAAS,
            "Customer Indemnity. Customer shall defend Provider against any third-party claim alleging that Customer Data, or Customer's use of the Service in breach of this Agreement, infringes or misappropriates a third party's rights or violates applicable law, and shall pay damages finally awarded against Provider.",
        ),
    ],
    "limitation-of-liability": [
        (
            MSA,
            "Limitation of Liability. Except for the Excluded Claims, neither Party shall be liable to the other for any indirect, incidental, special, consequential, or punitive damages, or for lost profits, revenue, or data, arising out of or relating to this Agreement, even if advised of the possibility of such damages.",
        ),
        (
            MSA,
            "Liability Cap. Except for the Excluded Claims, each Party's total aggregate liability arising out of or relating to this Agreement shall not exceed the total fees paid or payable by Client under the applicable Statement of Work in the twelve months preceding the event giving rise to the claim.",
        ),
        (
            MSA,
            "Exclusions from Limitation. The limitations and exclusions in this Section do not apply to a Party's indemnification obligations, breach of confidentiality or data protection obligations, infringement or misappropriation of the other Party's intellectual property, fraud, gross negligence, or willful misconduct.",
        ),
        (
            SAAS,
            "Damages. In no event will either Party be liable, whether in contract, tort, warranty, or otherwise, for any special, indirect, incidental, exemplary, punitive, or consequential damages, including loss of anticipated profits, except with respect to breaches of confidentiality or a Party's indemnification obligations.",
        ),
        (
            NDA,
            "Limitation of Remedies. Neither Party shall be liable for indirect or consequential losses arising from a breach of this Agreement; however, this limitation does not apply to unauthorized disclosure or use of Confidential Information, for which the disclosing Party may seek all available remedies.",
        ),
        (
            SAAS,
            "Super Cap. For claims arising from a breach of the Data Protection section, Provider's aggregate liability shall not exceed three times the fees paid by Customer in the twelve months preceding the claim, notwithstanding the general limitation of liability.",
        ),
    ],
    "insurance": [
        (
            MSA,
            "Insurance. Service Provider shall maintain, at its own expense, commercial general liability insurance of at least one million dollars per occurrence and two million dollars in the aggregate, professional liability insurance of at least two million dollars per claim, cyber liability insurance of at least two million dollars, and workers' compensation insurance as required by law.",
        ),
        (
            MSA,
            "Certificates of Insurance. Upon request, Service Provider shall provide certificates of insurance evidencing the required coverage, name Client as an additional insured on the commercial general liability policy, and give Client at least thirty days' prior written notice of cancellation or material reduction in coverage.",
        ),
        (
            SOW,
            "Insurance Requirements. Provider shall maintain throughout the term of this Statement of Work the insurance coverage specified in the Master Services Agreement and, for on-site work at Client facilities, automobile liability coverage of at least one million dollars combined single limit.",
        ),
        (
            SAAS,
            "Insurance. Provider shall maintain technology errors and omissions and cyber liability insurance with limits of not less than five million dollars per claim with insurers rated A- VII or better by A.M. Best throughout the Subscription Term and for two years thereafter.",
        ),
        (
            MSA,
            "Insurance Carriers. All insurance required under this Section shall be placed with insurers licensed to do business in the jurisdiction where the Services are performed and rated at least A- by A.M. Best, and shall be primary and non-contributory with respect to any insurance maintained by Client.",
        ),
    ],
    "compliance": [
        (
            MSA,
            "Compliance with Laws. Each Party shall comply with all laws and regulations applicable to its performance under this Agreement, including anti-bribery and anti-corruption laws such as the U.S. Foreign Corrupt Practices Act and the UK Bribery Act, export control and economic sanctions laws, and applicable employment laws.",
        ),
        (
            MSA,
            "Anti-Corruption. Service Provider represents that neither it nor any of its personnel has offered, paid, or promised anything of value to any government official or other person to obtain or retain business in connection with this Agreement, and shall maintain accurate books and records.",
        ),
        (
            SAAS,
            "Export Control. Customer shall not access or use the Service in violation of any export control or economic sanctions law, and represents that it is not located in, or a national of, any embargoed country and is not on any government denied-party list.",
        ),
        (
            MSA,
            "Regulatory Compliance. Service Provider shall perform the Services in compliance with all industry regulations applicable to Client that Client identifies in writing, shall cooperate with Client's regulatory audits, and shall promptly notify Client of any regulatory inquiry relating to the Services.",
        ),
        (
            MSA,
            "Modern Slavery and Ethics. Service Provider shall comply with applicable modern slavery, human trafficking, and labor laws, shall adhere to Client's Supplier Code of Conduct, and shall notify Client promptly of any actual or suspected breach of those obligations in its supply chain.",
        ),
    ],
    "audit-and-records": [
        (
            MSA,
            "Audit Rights. Client or its independent auditor may, upon fifteen business days' prior written notice and no more than once per year, audit Service Provider's books, records, and facilities relating to the Services to verify compliance with this Agreement and the accuracy of invoices.",
        ),
        (
            MSA,
            "Records. Service Provider shall maintain complete and accurate records of the Services performed, time spent, and expenses incurred for at least three years after the end of the Statement of Work to which they relate, and shall make them available to Client on request.",
        ),
        (
            MSA,
            "Audit Findings. If an audit reveals that Service Provider overcharged Client by more than five percent for any period, Service Provider shall promptly refund the overcharge and reimburse Client for the reasonable cost of the audit.",
        ),
        (
            SAAS,
            "Security Audits. Provider shall provide Customer, upon request and not more than once annually, with its most recent SOC 2 Type II report and penetration test summary, and shall permit a Customer audit of its security controls following any security incident affecting Customer Data.",
        ),
        (
            MSA,
            "Inspection. Client may inspect the work in progress at Service Provider's facilities during normal business hours upon reasonable notice. Inspections do not relieve Service Provider of its obligation to deliver conforming Deliverables.",
        ),
    ],
    "subcontracting-and-sub-processors": [
        (
            MSA,
            "Subcontracting. Service Provider shall not subcontract any material portion of the Services without Client's prior written consent. Service Provider remains fully responsible for the acts and omissions of its subcontractors as if they were its own and shall bind each subcontractor to written terms no less protective of Client than this Agreement.",
        ),
        (
            SAAS,
            "Sub-processors. Provider maintains a list of sub-processors authorized to process Customer Data at the address specified in the Documentation. Provider shall notify Customer at least thirty days before adding a sub-processor and Customer may object on reasonable grounds relating to data protection.",
        ),
        (
            MSA,
            "Approved Subcontractors. The subcontractors listed in Exhibit E are approved as of the Effective Date. Any replacement or additional subcontractor requires Client's prior written approval, which shall not be unreasonably withheld.",
        ),
        (
            MSA,
            "Flow-Down. Service Provider shall ensure that each subcontractor is bound by confidentiality, intellectual property assignment, data protection, and security obligations substantially equivalent to those in this Agreement, and shall provide copies of those terms to Client on request.",
        ),
        (
            SOW,
            "Use of Third Parties. Provider may use the offshore delivery center identified in Section 7 for development work only, provided that no Client Data is transferred outside the approved jurisdictions without Client's prior written consent.",
        ),
    ],
    "assignment": [
        (
            MSA,
            "Assignment. Neither Party may assign this Agreement without the prior written consent of the other Party, except that either Party may assign this Agreement without consent to an Affiliate or to a successor in connection with a merger, acquisition, or sale of all or substantially all of its assets, upon written notice to the other Party.",
        ),
        (
            SAAS,
            "Assignment and Change of Control. Customer may assign this Agreement to a successor in a merger or acquisition upon notice to Provider. Provider shall not assign this Agreement to a competitor of Customer without Customer's prior written consent. Any attempted assignment in violation of this Section is void.",
        ),
        (
            NDA,
            "Successors and Assigns. This Agreement binds and benefits the Parties and their respective successors and permitted assigns. Neither Party may assign its rights or delegate its obligations under this Agreement without the other Party's prior written consent, not to be unreasonably withheld.",
        ),
        (
            MSA,
            "Delegation. Service Provider shall not delegate performance of the Services, in whole or in part, to any third party without Client's prior written consent, and any permitted delegation does not relieve Service Provider of its obligations under this Agreement.",
        ),
        (
            MSA,
            "Permitted Assignment. Client may assign this Agreement, in whole or in part, to any Affiliate without Service Provider's consent, provided the assignee assumes Client's obligations in writing and Client gives Service Provider notice of the assignment.",
        ),
    ],
    "force-majeure": [
        (
            MSA,
            "Force Majeure. Neither Party shall be liable for any failure or delay in performing its obligations, other than payment obligations, to the extent caused by events beyond its reasonable control, including natural disasters, war, terrorism, civil unrest, epidemics, or governmental action, provided it gives prompt written notice and uses reasonable efforts to mitigate the effect.",
        ),
        (
            SAAS,
            "Excused Performance. A Force Majeure Event does not include labor disputes limited to a Party's own workforce, failures of a Party's subcontractors or suppliers that could have been avoided through a business continuity plan, or changes in market conditions.",
        ),
        (
            MSA,
            "Termination for Extended Force Majeure. If a Force Majeure Event prevents a Party from performing its material obligations for more than thirty consecutive days, the other Party may terminate the affected Statement of Work or this Agreement upon written notice and receive a pro-rata refund of any prepaid fees for Services not performed.",
        ),
        (
            SOW,
            "Delays Beyond Control. If performance of a milestone is delayed by an event beyond the reasonable control of the affected Party, the milestone date is extended by the duration of the delay, provided the affected Party notifies the other Party within three business days of the event.",
        ),
        (
            NDA,
            "Force Majeure. Neither Party is in breach of this Agreement to the extent its performance is prevented by circumstances beyond its reasonable control, except that no such circumstance excuses a Party's obligation to protect Confidential Information.",
        ),
    ],
    "governing-law-and-dispute-resolution": [
        (
            MSA,
            "Governing Law. This Agreement is governed by and construed in accordance with the laws of the State of Delaware, without regard to its conflict of laws principles. The United Nations Convention on Contracts for the International Sale of Goods does not apply.",
        ),
        (
            MSA,
            "Dispute Resolution. Before commencing litigation, the Parties shall attempt in good faith to resolve any dispute through escalation to senior executives, who shall meet within fifteen business days of a written request. If the dispute is not resolved within thirty days, either Party may pursue its remedies.",
        ),
        (
            MSA,
            "Jurisdiction and Venue. Each Party irrevocably submits to the exclusive jurisdiction of the state and federal courts located in New York County, New York for any action arising out of or relating to this Agreement and waives any objection to venue in those courts.",
        ),
        (
            SAAS,
            "Arbitration. Any dispute not resolved through executive escalation shall be finally settled by binding arbitration administered by JAMS under its Comprehensive Arbitration Rules before a single arbitrator seated in San Francisco, California, provided that either Party may seek injunctive relief in any court of competent jurisdiction.",
        ),
        (
            NDA,
            "Equitable Relief. Each Party acknowledges that unauthorized disclosure of Confidential Information may cause irreparable harm for which damages would be an inadequate remedy, and agrees that the disclosing Party is entitled to seek injunctive relief in any court of competent jurisdiction without posting a bond.",
        ),
        (
            MSA,
            "Governing Law and Choice of Venue. This Agreement will be governed by and construed in accordance with the laws of the State of California, and the Parties consent to the exclusive jurisdiction of the courts located in Santa Clara County, California.",
        ),
    ],
    "notices": [
        (
            MSA,
            "Notices. All notices under this Agreement shall be in writing and delivered by hand, by nationally recognized overnight courier, by certified mail with return receipt requested, or by email with confirmation of receipt, to the addresses set out below or as updated by notice.",
        ),
        (
            MSA,
            "Effectiveness of Notices. Notices are deemed received when delivered by hand, one business day after deposit with an overnight courier, three business days after mailing by certified mail, or when the recipient confirms receipt of an email notice.",
        ),
        (
            SAAS,
            "Notice Addresses. Legal notices to Provider shall be sent to the attention of the Legal Department at the address on the Order Form, with a copy by email to legal@provider.example. Notices to Customer shall be sent to the billing contact identified in the Order Form.",
        ),
        (
            NDA,
            "Notices. Any notice of breach, termination, or compelled disclosure shall be given in writing to the other Party's designated representative at the address specified on the signature page, with a copy to its general counsel.",
        ),
        (
            MSA,
            "Operational Communications. Routine project communications may be given by email to the Parties' project managers, but notices of breach, termination, or indemnification claims must be given in accordance with this Section.",
        ),
    ],
    "independent-contractor": [
        (
            MSA,
            "Independent Contractor. Service Provider is an independent contractor and nothing in this Agreement creates a partnership, joint venture, agency, franchise, or employment relationship between the Parties. Neither Party has authority to bind the other or to incur any obligation on its behalf.",
        ),
        (
            MSA,
            "Relationship of the Parties. Service Provider is solely responsible for the payment of all wages, benefits, payroll taxes, withholdings, and workers' compensation for its personnel, and shall indemnify Client against any claim that Service Provider personnel are employees of Client.",
        ),
        (
            SOW,
            "Status of Personnel. Provider personnel assigned to this engagement remain employees or contractors of Provider at all times, are not entitled to participate in Client's benefit plans, and shall not represent themselves as employees or agents of Client.",
        ),
        (
            MSA,
            "No Agency. Service Provider shall not hold itself out as an agent of Client, shall not enter into any contract on Client's behalf, and shall not make any representation or warranty to any third party on Client's behalf.",
        ),
        (
            MSA,
            "Relationship with Client. Service Provider's work under this Agreement is performed as an independent contractor and not as an employee or agent of Client, and Service Provider retains control over the manner and means by which the Services are performed, subject to the requirements of this Agreement.",
        ),
    ],
    "general-provisions": [
        (
            MSA,
            "Entire Agreement. This Agreement, together with its Exhibits and any executed Statements of Work, constitutes the entire agreement between the Parties regarding its subject matter and supersedes all prior or contemporaneous agreements, proposals, and representations, whether written or oral.",
        ),
        (
            MSA,
            "Amendment and Waiver. No amendment to this Agreement is effective unless in writing and signed by authorized representatives of both Parties. No waiver of any provision is effective unless in writing, and no failure or delay in exercising any right operates as a waiver of that right.",
        ),
        (
            NDA,
            "Severability. If any provision of this Agreement is held invalid or unenforceable, that provision shall be enforced to the maximum extent permissible and the remaining provisions shall remain in full force and effect.",
        ),
        (
            MSA,
            "Counterparts and Electronic Signatures. This Agreement may be executed in counterparts, each of which is an original and all of which together constitute one instrument. Signatures delivered by electronic means are deemed original signatures.",
        ),
        (
            SAAS,
            "Survival. Sections concerning confidentiality, intellectual property, indemnification, limitation of liability, payment of accrued fees, and governing law survive any expiration or termination of this Agreement.",
        ),
        (
            MSA,
            "No Third-Party Beneficiaries. This Agreement is for the sole benefit of the Parties and their permitted successors and assigns, and nothing in this Agreement confers any right, benefit, or remedy on any other person.",
        ),
    ],
    "information-security-and-data-management": [
        (
            SAAS,
            "Information Security. Provider shall maintain a comprehensive written information security program aligned with ISO 27001 or SOC 2 Type II that includes administrative, physical, and technical safeguards appropriate to the sensitivity of Customer Data, and shall not materially degrade those safeguards during the Subscription Term.",
        ),
        (
            SAAS,
            "Encryption. Provider shall encrypt Customer Data in transit using TLS 1.2 or higher and at rest using AES-256 or an equivalent industry-standard algorithm, and shall manage encryption keys in accordance with industry best practices.",
        ),
        (
            MSA,
            "Security Measures. Service Provider shall implement and maintain access controls, multi-factor authentication for privileged access, logging and monitoring, vulnerability management, and secure development practices for all systems used to perform the Services or store Client Data.",
        ),
        (
            MSA,
            "Security Incidents. Service Provider shall notify Client within twenty-four hours of discovering any actual or reasonably suspected unauthorized access to Client Data or systems, shall investigate and remediate the incident, and shall provide Client with a written root cause analysis.",
        ),
        (
            SAAS,
            "Penetration Testing. Provider shall conduct annual third-party penetration tests of the Service, remediate critical and high-severity vulnerabilities within thirty days of discovery, and provide Customer with an executive summary of the results on request.",
        ),
        (
            MSA,
            "Data Management. Service Provider shall store Client Data only in the locations approved in writing by Client, shall maintain backups sufficient to restore Client Data in accordance with the agreed recovery objectives, and shall return or securely destroy Client Data in accordance with Client's instructions.",
        ),
    ],
    "personnel": [
        (
            MSA,
            "Personnel. Service Provider shall assign qualified, experienced personnel to perform the Services and shall conduct background checks on all personnel with access to Client facilities, systems, or data, to the extent permitted by law.",
        ),
        (
            MSA,
            "Key Personnel. Service Provider shall not remove or reassign the Key Personnel identified in a Statement of Work without Client's prior written consent, except for resignation, termination, illness, or leave, and shall promptly replace any Key Personnel with an individual of equivalent qualifications acceptable to Client.",
        ),
        (
            MSA,
            "Replacement of Personnel. Client may request the removal of any Service Provider personnel for reasonable cause, including performance or conduct concerns, and Service Provider shall promptly provide a qualified replacement at no additional charge for ramp-up or knowledge transfer.",
        ),
        (
            SOW,
            "Project Staffing. Provider will staff the project with a project manager, a solution architect, two senior consultants, and a data migration specialist, each of whom has at least three years of experience with the platform being implemented.",
        ),
        (
            MSA,
            "Conduct of Personnel. Service Provider personnel shall comply with Client's workplace policies while on Client premises, including policies on harassment, drug and alcohol use, and information security, and Service Provider shall be responsible for their compliance.",
        ),
    ],
    "exit-and-transition-assistance": [
        (
            MSA,
            "Transition Assistance. Upon expiration or termination of this Agreement for any reason, Service Provider shall provide up to ninety days of transition assistance at the rates then in effect, or at no charge if termination results from Service Provider's breach, to facilitate the orderly transfer of the Services to Client or a successor provider.",
        ),
        (
            SAAS,
            "Return of Data. For thirty days after termination, Provider shall make Customer Data available for export in a standard machine-readable format at no additional charge, and thereafter shall securely delete Customer Data in accordance with NIST 800-88 and certify deletion in writing on request.",
        ),
        (
            MSA,
            "Return of Materials. Upon termination, Service Provider shall promptly deliver to Client all Deliverables and work in progress, whether complete or incomplete, all Client materials and data, and all documentation necessary for Client to use and maintain the Deliverables.",
        ),
        (
            MSA,
            "No Withholding. Service Provider shall not withhold any Client Data, Deliverables, or materials, or interrupt transition assistance, pending resolution of any dispute, including a dispute over fees, and shall not assert any lien over Client property.",
        ),
        (
            SOW,
            "Knowledge Transfer. During the final phase, Provider will deliver operational runbooks, conduct knowledge transfer sessions with Client's support team, and provide thirty days of hypercare support following go-live.",
        ),
    ],
    "business-continuity-and-disaster-recovery": [
        (
            SAAS,
            "Business Continuity. Provider shall maintain and test at least annually a business continuity and disaster recovery plan designed to ensure the continued availability of the Service, with a recovery time objective of four hours and a recovery point objective of one hour for production systems.",
        ),
        (
            MSA,
            "Disaster Recovery. Service Provider shall maintain geographically separate backup facilities, back up Client Data at least daily, and be able to restore the Services and Client Data within the recovery objectives set out in the Statement of Work following a disaster.",
        ),
        (
            SAAS,
            "Plan Testing. Provider shall provide Customer, upon request, with a summary of the results of its most recent business continuity and disaster recovery test and a description of any material remediation actions taken.",
        ),
        (
            MSA,
            "Continuity of Services. In the event of a disruption, Service Provider shall implement its business continuity plan, notify Client without undue delay, prioritize restoration of the Services, and provide regular status updates until normal operations resume.",
        ),
        (
            SAAS,
            "Backups. Provider shall retain backups of Customer Data for at least thirty days, store backups in an encrypted form in a separate availability zone, and verify the integrity of backups through regular restoration testing.",
        ),
    ],
    "order-of-precedence": [
        (
            MSA,
            "Order of Precedence. In the event of any conflict between the documents forming this Agreement, the following order of precedence applies: first, the Data Processing Addendum with respect to Personal Data; second, the body of this Agreement; third, the applicable Statement of Work; and fourth, any Exhibits or Schedules.",
        ),
        (
            SAAS,
            "Conflicting Terms. If there is a conflict between this Agreement and an Order Form, this Agreement prevails unless the Order Form expressly states that a specific provision of this Agreement is modified for that Order Form only. Any terms on a purchase order or vendor portal are void.",
        ),
        (
            SOW,
            "Precedence. This Statement of Work is subject to the Master Services Agreement. If any term of this Statement of Work conflicts with the Master Services Agreement, the Master Services Agreement controls, except where this Statement of Work expressly references the Section being modified.",
        ),
        (
            MSA,
            "Pre-Printed Terms. Any pre-printed, click-through, or shrink-wrap terms on invoices, acknowledgments, online portals, or software installations are of no force or effect and do not modify this Agreement, even if accepted by Client personnel.",
        ),
        (
            MSA,
            "Hierarchy of Documents. The Parties intend the documents comprising this Agreement to be read together. Where a conflict cannot be reconciled, the more specific provision governs within the document hierarchy set out in this Section.",
        ),
    ],
    "governance-and-project-management": [
        (
            MSA,
            "Governance. Each Party shall designate a relationship manager responsible for overseeing performance under this Agreement. The relationship managers shall meet at least monthly to review performance, service levels, open issues, and planned changes.",
        ),
        (
            SOW,
            "Project Management. Provider's project manager shall maintain the project plan, issue log, and risk register, chair weekly status meetings with Client's project manager, and deliver a written status report each week summarizing progress, risks, and upcoming decisions.",
        ),
        (
            MSA,
            "Steering Committee. The Parties shall establish a steering committee composed of senior representatives of each Party that meets quarterly to review the strategic direction of the relationship, resolve escalated issues, and approve material changes to the Services.",
        ),
        (
            MSA,
            "Escalation. Issues not resolved by the project managers within five business days shall be escalated to the relationship managers, and if not resolved within a further ten business days, to the steering committee, before either Party invokes the dispute resolution procedure.",
        ),
        (
            SOW,
            "Reporting. Provider shall provide Client with a monthly report covering hours consumed against budget, milestone status, change requests, and quality metrics, in the format agreed by the Parties at project kick-off.",
        ),
    ],
    "publicity-and-use-of-name": [
        (
            MSA,
            "Publicity. Neither Party shall issue any press release or public announcement regarding this Agreement, or use the other Party's name, logo, or trademarks in any marketing material, customer list, or case study, without the other Party's prior written consent.",
        ),
        (
            SAAS,
            "Use of Name. Provider may identify Customer as a customer in Provider's customer lists and on its website using Customer's name and logo, subject to Customer's trademark usage guidelines, and shall cease such use within ten days of Customer's written request.",
        ),
        (
            NDA,
            "No Publicity. Neither Party shall disclose the existence or terms of this Agreement or the fact that the Parties are in discussions, except as required by law, without the prior written consent of the other Party.",
        ),
        (
            MSA,
            "Marketing Materials. Any case study, testimonial, or reference call involving Client requires Client's prior written approval of the specific content, and Client may revoke its approval at any time upon written notice.",
        ),
        (
            SOW,
            "Trademarks. Provider shall not use Client's trademarks, trade names, or logos in any manner, including in proposals to other customers, without Client's express prior written consent in each instance.",
        ),
    ],
    "non-compete": [
        (
            MSA,
            "Non-Compete. Nothing in this Agreement restricts Client from developing, acquiring, or marketing products or services that compete with those of Service Provider, or from engaging third parties to provide services similar to the Services.",
        ),
        (
            MSA,
            "Exclusivity. During the Term and for twelve months thereafter, Service Provider shall not provide services substantially similar to the Services to the competitors of Client identified in Exhibit F within North America, in exchange for the exclusivity fee set out in the Statement of Work.",
        ),
        (
            SOW,
            "Competing Business. During the term of this Statement of Work and for six months thereafter, Provider shall not assign personnel who have worked on this engagement to perform services for a direct competitor of Client in the same product area without Client's prior written consent.",
        ),
        (
            MSA,
            "Non-Competition. During the term of this Agreement and for two years thereafter, Service Provider shall not, directly or indirectly, engage in any business that competes with Client's business anywhere in the world.",
        ),
        (
            SAAS,
            "No Restriction on Customer. Customer remains free to use competing products and services and to build or buy internal solutions with similar functionality, and nothing in this Agreement obligates Customer to purchase any minimum volume from Provider.",
        ),
    ],
    "non-solicitation": [
        (
            MSA,
            "Non-Solicitation. During the Term and for twelve months thereafter, neither Party shall directly solicit for employment any employee of the other Party who was directly involved in the performance of the Services, provided that general solicitations not targeted at such employees, and hiring in response to them, are not prohibited.",
        ),
        (
            MSA,
            "No-Hire. During the term of this Agreement and for one year after its termination, Client shall not hire or engage, directly or indirectly, any employee or contractor of Service Provider who performed Services for Client without Service Provider's prior written consent.",
        ),
        (
            SOW,
            "Personnel Solicitation. If either Party hires an employee of the other Party who worked on this engagement during the engagement or within six months afterward, the hiring Party shall pay the other Party a placement fee equal to twenty percent of the employee's first-year base salary.",
        ),
        (
            NDA,
            "Non-Solicitation of Employees. For twelve months after the date of this Agreement, neither Party shall, directly or indirectly, solicit for employment any employee of the other Party with whom it had contact in connection with the Purpose, excluding responses to general public advertisements.",
        ),
        (
            MSA,
            "Mutual Non-Solicit. Each Party agrees not to actively recruit the other Party's personnel who were materially involved in the Services during the term and for twelve months following termination, and this restriction does not apply to individuals whose employment was terminated by the other Party.",
        ),
    ],
}


def point_id(slug: str, text: str) -> str:
    """Deterministic point id so re-running the seed replaces, not duplicates."""

    return str(uuid.uuid5(SEED_NAMESPACE, f"{slug}|{text}"))


def load_policy_slugs(path: Path = POLICIES_PATH) -> List[str]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [row["canonical_type"] for row in rows]


def validate_archetypes(slugs: List[str]) -> List[Tuple[str, str, str]]:
    """Check the exemplar table covers every policy slug with 4–6 texts and return (slug, agreement_type, text) rows."""

    missing = sorted(set(slugs) - set(ARCHETYPES))
    extra = sorted(set(ARCHETYPES) - set(slugs))
    if missing or extra:
        raise SystemExit(f"Archetype table out of sync with clause_policies.json: missing={missing} extra={extra}")
    rows: List[Tuple[str, str, str]] = []
    seen_text: set = set()
    for slug in slugs:
        exemplars = ARCHETYPES[slug]
        if not MIN_PER_SLUG <= len(exemplars) <= MAX_PER_SLUG:
            raise SystemExit(f"{slug}: {len(exemplars)} exemplars; expected {MIN_PER_SLUG}–{MAX_PER_SLUG}")
        for agreement_type, text in exemplars:
            if text in seen_text:
                raise SystemExit(f"Duplicate exemplar text under {slug}: {text[:60]}...")
            seen_text.add(text)
            rows.append((slug, agreement_type, text))
    return rows


def build_points(rows: List[Tuple[str, str, str]], vectors: List[List[float]]) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    for (slug, agreement_type, text), vector in zip(rows, vectors):
        points.append(
            {
                "id": point_id(slug, text),
                "vector": {"dense": vector},
                "payload": {
                    "canonical_type": slug,
                    "sub_type": None,
                    "label": "Clause",
                    "is_exhibit": False,
                    "agreement_type": agreement_type,
                    "clause_text": text,
                    "source": SEED_SOURCE,
                },
            }
        )
    return points


def collection_point_count() -> int:
    import requests

    response = requests.get(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}", headers=_get_headers(), timeout=30)
    response.raise_for_status()
    return int(response.json()["result"].get("points_count") or 0)


async def seed(dry_run: bool) -> int:
    slugs = load_policy_slugs()
    rows = validate_archetypes(slugs)
    print(f"{len(rows)} exemplars across {len(slugs)} clause types ({len(rows) / len(slugs):.1f} per type).")
    if dry_run:
        return 0

    started = time.perf_counter()
    client = EmbeddingClient(timeout=EMBED_TIMEOUT_SECONDS, batch_size=EMBED_BATCH_SIZE)
    vectors = await client.embed([text for _, _, text in rows])
    print(f"Embedded {len(vectors)} texts (dimension {len(vectors[0])}) in {time.perf_counter() - started:.1f}s.")

    ensure_collection(len(vectors[0]))
    before = collection_point_count()
    upserted = upsert_vectors(build_points(rows, vectors))
    after = collection_point_count()
    print(f"Upserted {upserted} points into '{QDRANT_COLLECTION}' at {QDRANT_URL}: {before} -> {after} points.")
    return upserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Validate the exemplar table against clause_policies.json without embedding or writing to Qdrant.")
    args = parser.parse_args()
    asyncio.run(seed(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
