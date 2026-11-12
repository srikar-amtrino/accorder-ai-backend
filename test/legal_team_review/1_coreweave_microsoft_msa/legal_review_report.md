# Legal Review Report: Master Services Agreement (Cloud Infrastructure / GPU Compute)

## Executive Metadata

| Field | Details |
| :--- | :--- |
| **Agreement** | Master Services Agreement (Cloud Infrastructure / GPU Compute) |
| **Parties** | CoreWeave, Inc. (Provider) & Microsoft Corporation (Customer) |
| **Party Represented** | **Customer (Microsoft Corporation)** |
| **Agreement Type** | Master Services Agreement (cloud infrastructure / compute services) |
| **Review Objective** | Review the Master Services Agreement from the Customer's perspective to identify risks, unfavorable terms and gaps in protection, and recommend revisions. |
| **Key Client Concerns** | Service levels, hardware failure and maintenance remedies; limitation of liability and indemnification; fees, Bulk Credits and Reserved Instances commitments; data processing and security incident obligations; termination rights and Acceptable Use Policy restrictions. |
| **Total Identified Issues** | **22** (High: 12, Medium: 8, Low: 2) |

## Risk Level Summary

- 🔴 **High Risk Issues (12)**: Severe commercial, financial, or liability exposures that require mandatory negotiation/redlining.
- 🟡 **Medium Risk Issues (8)**: Substantive imbalances, missing protections, or operational ambiguities to be revised.
- 🟢 **Low Risk Issues (2)**: Minor drafting defects, bracketed placeholders, or clarifications.

## Quick Reference Table of Flagged Clauses

| # | Para ID | Clause Heading | Risk Level | Issue Summary |
| :-: | :---: | :--- | :---: | :--- |
| 1 | `3BE9B181` | Preamble / Front-Matter | 🟢 Low | The field 'Customer Full Legal Name:' is left blank with no value provided, indicating an unfilled placeholder that should contain the customer's full legal name |
| 2 | `1CD839F0` | Preamble / Front-Matter | 🟢 Low | The field 'Customer Address:' contains no value — it is an unfilled blank/placeholder left incomplete in the contract |
| 3 | `13E13284` | Definitions & Interpretation | 🟡 Medium | The definition of "Acceptable Use Policy" or "AUP" references Exhibit B as the governing form, which is appropriate for a static attachment |
| 4 | `3D1C886A` | Definitions & Interpretation | 🟡 Medium | The definition of "Order Form" does more than define a term — it embeds a substantive binding obligation on Customer Affiliates directly within the Definitions section |
| 5 | `4E7D7853` | Definitions & Interpretation | 🟡 Medium | The definition of "Services" is materially overbroad and lacks clear boundaries, which creates meaningful commercial risk for Microsoft as Customer |
| 6 | `17779DC0` | Scope of Services | 🔴 High | This provision grants Customer access to "the Services" without defining or specifically identifying what those Services are, what they include, or — critically — what falls outside their scope |
| 7 | `62EB87B2` | Scope of Services | 🔴 High | This provision gives CoreWeave virtually unchecked unilateral authority to modify or remove features, functionality, or other aspects of the Services at any time, in its sole discretion |
| 8 | `568AB88D` | Customer Obligations | 🔴 High | This provision imposes broad, open-ended liability on Microsoft (as Customer) for any unauthorized access to its account, without capping or defining the consequences of that responsibility |
| 9 | `701478F1` | Customer Obligations | 🔴 High | This provision imposes a notification obligation on Microsoft (as Customer) using the vague standard "promptly," without specifying a defined timeframe |
| 10 | `37B1E9C6` | Customer Obligations | 🔴 High | This provision imposes broad, unilateral obligations on Customer regarding Processed Customer Data but fails to specify any timeframes within which Customer must act — for example, when it must provide required information, approvals, or notifications to CoreWeave |
| 11 | `3F6224A0` | Customer Obligations | 🔴 High | This provision imposes open-ended, unlimited liability on Customer for any loss or damage to Processed Customer Data attributable to Customer, its End Users, or Representatives, without capping or defining the scope of that exposure |
| 12 | `4460C51E` | Representations & Warranties | 🟡 Medium | This clause imposes representations and warranties on the Customer but is entirely silent on the remedy available to either party in the event of a breach |
| 13 | `4F76F50D` | Representations & Warranties | 🟡 Medium | This warranty clause presents two meaningful gaps for Microsoft as Customer |
| 14 | `21A3D5CE` | Representations & Warranties | 🟡 Medium | The legal compliance warranty in clause (v) is currently one-sided: only one party (based on context, the Service Provider) makes the representation that it complies with applicable laws and regulations in connection with the Services |
| 15 | `51E09BD9` | Limitation of Liability | 🟡 Medium | The limitation of liability clause contains two meaningful gaps that leave Microsoft exposed |
| 16 | `6BA5D0DC` | Fees & Payment | 🟡 Medium | This payment clause presents several gaps that expose Microsoft as Customer to meaningful financial and operational uncertainty |
| 17 | `3A0DC61E` | Service Levels | 🔴 High | This provision contains a sweeping liability exclusion that bars Microsoft from recovering any financial or other damages arising from maintenance-related service interruptions, regardless of CoreWeave's fault, the duration of the outage, or the business impact on Microsoft |
| 18 | `3D763308` | Service Levels | 🔴 High | This provision is dangerously thin for a customer of Microsoft's scale and operational dependency |
| 19 | `068E9EBF` | Service Levels | 🔴 High | This Hardware Failure provision is materially deficient from Microsoft's perspective because the only contractual consequence for a prolonged hardware outage is a fee credit — and even that remedy is delayed, triggering only after two full consecutive business days of failure |
| 20 | `286CA90E` | Service Levels | 🔴 High | This provision obligates CoreWeave to maintain hardware spares and replace failed network switches or compute nodes within two consecutive business days, but it stops short of providing any meaningful remedy to the Customer if CoreWeave fails to meet that obligation |
| 21 | `12FA6549` | Miscellaneous / Other Provisions | 🔴 High | This No Publicity clause is entirely one-sided in CoreWeave's favor |
| 22 | `153F1421` | Miscellaneous / Other Provisions | 🔴 High | This Force Majeure clause presents several significant risks for Microsoft as Customer |

---

## Detailed Clause-by-Clause Legal Findings & Suggested Redlines

### 1. Preamble / Front-Matter — [🟢 Low Risk] (`3BE9B181`)

**Legal Analysis & Justification:**
> The field 'Customer Full Legal Name:' is left blank with no value provided, indicating an unfilled placeholder that should contain the customer's full legal name.

**Original Contract Wording:**
```text
Customer Full Legal Name:
```

**Suggested Revision / Redline Fix:**
```text
Customer Full Legal Name: [Insert the customer's full legal name here, e.g., 'Acme Corporation, a Delaware limited liability company']
```

---

### 2. Preamble / Front-Matter — [🟢 Low Risk] (`1CD839F0`)

**Legal Analysis & Justification:**
> The field 'Customer Address:' contains no value — it is an unfilled blank/placeholder left incomplete in the contract.

**Original Contract Wording:**
```text
Customer Address:
```

**Suggested Revision / Redline Fix:**
```text
Customer Address: [Insert full customer address, e.g., 123 Main Street, Suite 100, New York, NY 10001, USA]
```

---

### 3. Definitions & Interpretation — [🟡 Medium Risk] (`13E13284`)

**Legal Analysis & Justification:**
> The definition of "Acceptable Use Policy" or "AUP" references Exhibit B as the governing form, which is appropriate for a static attachment. However, the definition contains no version identifier, effective date, or explicit restriction on unilateral amendment by CoreWeave. Without such anchoring language, CoreWeave could argue that the AUP is subject to periodic updates that automatically bind Microsoft, potentially restricting permitted uses of the services mid-term without meaningful notice or consent. For a customer of Microsoft's scale and operational complexity, an uncontrolled, unilaterally revisable AUP creates real business risk: new restrictions could conflict with existing workloads, compliance obligations, or downstream customer commitments. The definition should be locked to the version attached at signing and should require Microsoft's prior written consent before any amendment to the AUP takes effect against it.

**Original Contract Wording:**
```text
“Acceptable Use Policy” or “AUP” means CoreWeave’s acceptable use policy, in the form attached as Exhibit B.
```

**Suggested Revision / Redline Fix:**
```text
"Acceptable Use Policy" or "AUP" means CoreWeave's acceptable use policy in the form attached as Exhibit B as of the Effective Date of this Agreement, as may be amended only with Customer's prior written consent. Any purported amendment to the AUP that has not been consented to in writing by Customer shall have no force or effect with respect to Customer.
```

---

### 4. Definitions & Interpretation — [🟡 Medium Risk] (`3D1C886A`)

**Legal Analysis & Justification:**
> The definition of "Order Form" does more than define a term — it embeds a substantive binding obligation on Customer Affiliates directly within the Definitions section. The sentence stating that an Affiliate "agrees to be bound by the terms of this Agreement as if it were an original party hereto" upon entering an Order Form or merely using on-demand compute consumption creates a unilateral commitment mechanism that bypasses any formal joinder or consent process. From Microsoft's perspective, this is problematic for several reasons. First, it could inadvertently bind Affiliates to the full Agreement — including its liability caps, indemnification obligations, and payment terms — without those Affiliates having independently reviewed or negotiated the terms. Second, the trigger of "using any on-demand compute consumption" is particularly broad and vague, meaning an Affiliate could become contractually bound simply by accessing services, without executing any written instrument. Third, embedding operative obligations inside a definitions clause obscures their legal effect and makes them easier to overlook during review. Microsoft should ensure that Affiliate participation is governed by a clearly scoped, separately articulated provision that limits Affiliate exposure and preserves Microsoft's ability to control which entities are bound and under what conditions.

**Original Contract Wording:**
```text
“Order Form” means an ordering document specifying the services to be provided hereunder that is entered into between CoreWeave and Customer or any Customer Affiliate, including any addenda and supplements thereto, or any separate on-demand compute consumption by Customer or any Customer Affiliate. By entering into an Order Form (or using any on-demand compute consumption), an Affiliate agrees to be bound by the terms of this Agreement as if it were an original party hereto.
```

**Suggested Revision / Redline Fix:**
```text
"Order Form" means an ordering document specifying the services to be provided hereunder that is entered into between CoreWeave and Customer or any Customer Affiliate, including any addenda and supplements thereto, or any separate on-demand compute consumption by Customer or any Customer Affiliate. For the avoidance of doubt, any Customer Affiliate that enters into an Order Form does so solely in its capacity as a party to that Order Form, and Customer's obligations and liabilities under this Agreement shall not be expanded solely by reason of an Affiliate's entry into an Order Form. The rights and obligations of any such Affiliate under an Order Form shall be limited to the scope of that Order Form, and Customer shall not be jointly and severally liable for the obligations of its Affiliates unless expressly agreed in writing by Customer.
```

---

### 5. Definitions & Interpretation — [🟡 Medium Risk] (`4E7D7853`)

**Legal Analysis & Justification:**
> The definition of "Services" is materially overbroad and lacks clear boundaries, which creates meaningful commercial risk for Microsoft as Customer. By sweeping in "products, and solutions" without further qualification, the definition could be read to encompass offerings, deliverables, or capabilities that are not contemplated by any specific Order Form, leaving the scope of what CoreWeave is actually obligated to provide—and what Microsoft is paying for—ambiguous. Equally, the phrase "as further detailed in an Order Form and Documentation" defers the operative scope to documents that may not yet exist or may be unilaterally updated by CoreWeave, meaning the core contractual commitment is never pinned down at the Master Agreement level. This ambiguity can be exploited to narrow service obligations in a dispute, limit the applicability of SLAs and remedies, or exclude certain infrastructure or platform components from the protections negotiated in this agreement. A well-bounded definition should enumerate the categories of services with reasonable specificity and make clear that the scope is determined by the Order Form in effect at the time of execution, not by Documentation that CoreWeave may revise.

**Original Contract Wording:**
```text
“Services” means the infrastructure-as-a-service, platform-as-a-service, products, and solutions provided by CoreWeave to Customer, as further detailed in an Order Form and Documentation.
```

**Suggested Revision / Redline Fix:**
```text
"Services" means the infrastructure-as-a-service, platform-as-a-service, and any related products and solutions expressly identified and described in a mutually executed Order Form, together with any associated support, maintenance, or professional services ancillary thereto, as further detailed in the applicable Order Form and the Documentation in effect as of the date of such Order Form. For the avoidance of doubt, "Services" does not include any offerings, features, or capabilities not expressly set forth in an executed Order Form, and CoreWeave may not unilaterally expand or modify the scope of Services by amending Documentation after execution of the applicable Order Form without Customer's prior written consent.
```

---

### 6. Scope of Services — [🔴 High Risk] (`17779DC0`)

**Legal Analysis & Justification:**
> This provision grants Customer access to "the Services" without defining or specifically identifying what those Services are, what they include, or — critically — what falls outside their scope. The absence of a clear service description or reference to a schedule, statement of work, or enumerated service catalog creates a significant risk for Microsoft: if a dispute arises over whether CoreWeave is obligated to perform a particular function, CoreWeave can argue that the activity falls outside the undefined scope of "the Services." The phrase "solely for Customer's business operations" further narrows Customer's permitted use without any corresponding obligation on CoreWeave to deliver a defined set of capabilities. Without an explicit out-of-scope exclusion, CoreWeave also retains discretion to unilaterally narrow what it considers in-scope over time. The subcontracting restriction on compute Services is a useful protection, but it is undermined by the vague service definition — if compute Services are not specifically described, the restriction is difficult to enforce. For a customer of Microsoft's scale and technical specificity, this ambiguity poses a high risk of performance disputes, scope creep, and inadequate remedies.

**Original Contract Wording:**
```text
Access to the Services. Subject to and conditioned on Customer’s payment of fees in compliance with all the terms and conditions of this Agreement, Customer has the right to access and use the Services solely for Customer’s business operations (which include provision of services by Customer or its End Users to its customers and engagement of Representatives by Customer to enable such provision of services). Coreweave will not subcontract the performance of any compute Services provided to Customer. CoreWeave may subcontract its non-compute obligations under this Agreement with Customer’s prior written consent in each case.
```

**Suggested Revision / Redline Fix:**
```text
Access to the Services. Subject to and conditioned on Customer's payment of fees in compliance with all the terms and conditions of this Agreement, Customer has the right to access and use the Services solely for Customer's business operations (which include provision of services by Customer or its End Users to its customers and engagement of Representatives by Customer to enable such provision of services). The Services to be provided by CoreWeave are described in detail in the applicable Order Form(s) and Statement(s) of Work attached hereto and incorporated herein by reference, which shall specify the service descriptions, deliverables, service tiers, and any applicable performance standards. For the avoidance of doubt, any services, functions, or responsibilities not expressly set forth in such Order Form(s) or Statement(s) of Work are outside the scope of the Services and are not obligations of CoreWeave under this Agreement. Coreweave will not subcontract the performance of any compute Services provided to Customer. CoreWeave may subcontract its non-compute obligations under this Agreement with Customer's prior written consent in each case, provided that CoreWeave shall remain primarily liable for the performance of any subcontracted obligations and shall ensure that any approved subcontractor is bound by obligations no less protective of Customer than those set forth in this Agreement.
```

---

### 7. Scope of Services — [🔴 High Risk] (`62EB87B2`)

**Legal Analysis & Justification:**
> This provision gives CoreWeave virtually unchecked unilateral authority to modify or remove features, functionality, or other aspects of the Services at any time, in its sole discretion. For Microsoft as Customer, this is a significant operational risk: CoreWeave could materially degrade or eliminate capabilities that Microsoft's workloads depend on, with no obligation to obtain Microsoft's consent and no minimum advance notice period guaranteed in concrete terms. The phrase "as much advance notice as possible" is entirely subjective and unenforceable as a practical matter — it imposes no floor on the notice period and could be satisfied by notice given the same day as a change. While the provision references a "best efforts" commitment to provide twelve (the sentence appears truncated, suggesting a twelve-month or twelve-week notice period), the absence of a completed, binding minimum notice obligation leaves Microsoft without a reliable planning window. Critically, there is no requirement that CoreWeave obtain Microsoft's written consent before making changes that materially reduce the scope or capability of the Services, no right for Microsoft to terminate for convenience or receive a fee reduction if a material modification is made, and no carve-out protecting features that are the subject of an active Order Form. This asymmetry is commercially unacceptable for an enterprise customer of Microsoft's scale and dependency on contracted service capabilities.

**Original Contract Wording:**
```text
Service Features. Customer may request features or functionality not already offered through the Services. If CoreWeave determines that Customer’s request is feasible, CoreWeave may choose to provide those features or functionalities to Customer for an additional fee, subject to the Parties executing either a separate Order Form or an addendum to an existing Order Form with respect to such customizations. CoreWeave reserves the right, in its sole discretion, to modify or (if deemed necessary in its reasonable opinion) remove the features, functionality, or other aspects of the Services at any time; provided however that Coreweave will notify Customer with as much advance notice as possible prior to any such modification or removal and will use best efforts to provide Customer with twelve
```

**Suggested Revision / Redline Fix:**
```text
Service Features. Customer may request features or functionality not already offered through the Services. If CoreWeave determines that Customer's request is feasible, CoreWeave may choose to provide those features or functionalities to Customer for an additional fee, subject to the Parties executing either a separate Order Form or an addendum to an existing Order Form with respect to such customizations. CoreWeave reserves the right to modify or (if deemed necessary in its reasonable opinion) remove the features, functionality, or other aspects of the Services; provided however that (a) CoreWeave will provide Customer with no less than ninety (90) days' prior written notice of any such modification or removal, (b) CoreWeave will not, without Customer's prior written consent, modify or remove any feature or functionality that is expressly identified in an active Order Form or that materially reduces the performance, capacity, or capabilities of the Services as described in the applicable Documentation, (c) in the event of any modification or removal that materially and adversely affects Customer's use of the Services, Customer shall have the right, exercisable within thirty (30) days of receipt of such notice, to terminate the affected Order Form(s) without penalty and receive a pro-rata refund of any prepaid, unused fees attributable to the terminated Order Form(s), and (d) CoreWeave will use commercially reasonable efforts to provide Customer with twelve (12) months' advance notice prior to any discontinuation of a material Service feature or functionality.
```

---

### 8. Customer Obligations — [🔴 High Risk] (`568AB88D`)

**Legal Analysis & Justification:**
> This provision imposes broad, open-ended liability on Microsoft (as Customer) for any unauthorized access to its account, without capping or defining the consequences of that responsibility. As drafted, "Customer is responsible for any unauthorized access" is an unlimited liability statement — it does not confine the remedy to a specific, defined outcome such as cost relief, indemnification of direct damages only, or a liability cap tied to fees paid. This creates a significant exposure gap: CoreWeave could assert that Microsoft's responsibility for unauthorized access extends to consequential, indirect, or third-party losses arising from such access, with no contractual ceiling. The carve-out for CoreWeave's own Security Incidents is helpful but insufficient on its own, because it does not address the scope or quantum of Microsoft's liability where both parties bear some fault, nor does it limit the remedy to a defined and proportionate consequence. For a customer of Microsoft's scale processing sensitive workloads, leaving this obligation untethered to a defined remedy or liability cap is a material commercial and legal risk.

**Original Contract Wording:**
```text
Customer Security Protocols. Customer shall employ commercially reasonable and appropriate, in accordance with applicable industry standards, physical, administrative, and technical controls, screening, and security procedures and other safeguards designed to securely administer the distribution and use of its account access credentials and protect against any unauthorized access to or use of the Service. Customer must utilize proper security protocols, such as setting strong passwords and access control mechanisms, safeguarding access to all logins and passwords, and verifying the trustworthiness of persons who are entrusted with account access information. Customer is responsible for any unauthorized access to the Customer’s account (except to the extent caused by a breach of CoreWeave’s obligations under the Security Standards or otherwise arising from a Security Incident of CoreWeave). FOR FURTHER CLARITY, CUSTOMER IS RESPONSIBLE FOR APPLYING APPROPRIATE SECURITY MEASURES TO PROCESSED CUSTOMER DATA, INCLUDING ENCRYPTING SENSITIVE DATA AND TAKING APPROPRIATE STEPS TO PREVENT UNAUTHORIZED ACCESS TO ANY PROCESSED CUSTOMER DATA.
```

**Suggested Revision / Redline Fix:**
```text
Customer Security Protocols. Customer shall employ commercially reasonable and appropriate, in accordance with applicable industry standards, physical, administrative, and technical controls, screening, and security procedures and other safeguards designed to securely administer the distribution and use of its account access credentials and protect against any unauthorized access to or use of the Service. Customer must utilize proper security protocols, such as setting strong passwords and access control mechanisms, safeguarding access to all logins and passwords, and verifying the trustworthiness of persons who are entrusted with account access information. Customer is responsible for any unauthorized access to the Customer's account arising solely from Customer's failure to comply with its obligations under this Section (except to the extent caused by a breach of CoreWeave's obligations under the Security Standards or otherwise arising from a Security Incident of CoreWeave), and any such responsibility shall be limited solely to direct damages and shall not exceed the total fees paid or payable by Customer to CoreWeave in the three (3) months immediately preceding the event giving rise to such responsibility; in no event shall Customer be liable for any indirect, incidental, consequential, special, or punitive damages arising from unauthorized access to its account. Where unauthorized access results from the contributory acts or omissions of both parties, each party's responsibility shall be apportioned in proportion to its relative fault. FOR FURTHER CLARITY, CUSTOMER IS RESPONSIBLE FOR APPLYING APPROPRIATE SECURITY MEASURES TO PROCESSED CUSTOMER DATA, INCLUDING ENCRYPTING SENSITIVE DATA AND TAKING APPROPRIATE STEPS TO PREVENT UNAUTHORIZED ACCESS TO ANY PROCESSED CUSTOMER DATA, PROVIDED THAT SUCH RESPONSIBILITY IS SUBJECT TO THE LIABILITY LIMITATIONS SET FORTH IN THIS SECTION AND THE AGREEMENT.
```

---

### 9. Customer Obligations — [🔴 High Risk] (`701478F1`)

**Legal Analysis & Justification:**
> This provision imposes a notification obligation on Microsoft (as Customer) using the vague standard "promptly," without specifying a defined timeframe. In practice, this creates meaningful legal exposure: if CoreWeave later disputes whether Microsoft acted "promptly," Microsoft could be found in breach of its notification obligation even where it acted reasonably and in good faith. The absence of a concrete deadline also creates an asymmetry — CoreWeave's own security incident obligations elsewhere in the agreement are likely held to a defined standard, while Microsoft's reciprocal duty remains open-ended and subject to CoreWeave's interpretation. Additionally, the obligation to "aid in any investigation or legal action" is broadly drafted and could be read to require Microsoft to participate in litigation or regulatory proceedings at CoreWeave's direction, without any qualification limiting that assistance to reasonable cooperation or capping the associated burden on Microsoft. Left unaddressed, this clause could be used to argue that Microsoft failed to timely notify or adequately cooperate, potentially shifting liability for a Security Incident back to Microsoft even where CoreWeave bears primary responsibility for the underlying infrastructure.

**Original Contract Wording:**
```text
Notification by Customer of Security Incidents. Customer will notify CoreWeave promptly if and when Customer becomes aware of any Security Incidents, including unauthorized access to Customer’s account or account credentials, and shall aid in any investigation or legal action that is taken by authorities and/or CoreWeave to investigate and cure such Security Incident to the extent caused by the Customer account or Customer’s or its End Users’ or Representatives’ use of the Services or failure to comply with Section 3(f) above.
```

**Suggested Revision / Redline Fix:**
```text
Notification by Customer of Security Incidents. Customer will notify CoreWeave within forty-eight (48) hours after Customer becomes aware of any Security Incidents, including unauthorized access to Customer's account or account credentials, and shall provide reasonable cooperation and assistance in any investigation or legal action that is taken by authorities and/or CoreWeave to investigate and cure such Security Incident to the extent caused by the Customer account or Customer's or its End Users' or Representatives' use of the Services or failure to comply with Section 3(f) above. For the avoidance of doubt, Customer's obligation to aid in any investigation or legal action shall be limited to reasonable cooperation and shall not require Customer to incur material out-of-pocket costs or assume any liability beyond that which is otherwise allocated to Customer under this Agreement.
```

---

### 10. Customer Obligations — [🔴 High Risk] (`37B1E9C6`)

**Legal Analysis & Justification:**
> This provision imposes broad, unilateral obligations on Customer regarding Processed Customer Data but fails to specify any timeframes within which Customer must act — for example, when it must provide required information, approvals, or notifications to CoreWeave. The absence of defined response or action deadlines is a significant gap that exposes Microsoft to risk: without agreed timelines, CoreWeave could argue that any delay by Customer — however brief or reasonable — constitutes a breach of Customer's responsibilities, potentially relieving CoreWeave of its own service obligations or triggering liability for Customer. For a large-scale enterprise customer like Microsoft, clear and reasonable timeframes for any required Customer actions are essential to managing operational workflows and avoiding manufactured defaults. The clause should be amended to make clear that Customer's obligations are subject to reasonable timeframes, and that any specific deadlines must be mutually agreed in writing.

**Original Contract Wording:**
```text
Customer Responsibilities. Customer is solely responsible for its Processed Customer Data and agrees that CoreWeave is not and will not in any way be liable for the content of the Processed Customer Data. By providing Processed Customer Data, Customer affirms, represents and warrants that: (i) to Customer’s knowledge its Processed Customer Data and use thereof will not violate this Agreement (including the AUP) or any applicable law or regulation; (ii) Customer is solely responsible for Customer’s development, moderation, operation, maintenance, support and use of Processed Customer Data, including when Processed Customer Data is provided by Customer’s End Users or Representatives; (iii) to its knowledge, Customer’s Processed Customer Data and its use thereof does not and will not:
```

**Suggested Revision / Redline Fix:**
```text
Customer Responsibilities. Customer is solely responsible for its Processed Customer Data and agrees that CoreWeave is not and will not in any way be liable for the content of the Processed Customer Data. Customer shall fulfill any obligations requiring the provision of information, approvals, or other actions within a reasonable time, or within such specific timeframes as the parties may mutually agree in writing; CoreWeave shall provide Customer with reasonable advance notice of any such required action and a description of the information or approval needed. By providing Processed Customer Data, Customer affirms, represents and warrants that: (i) to Customer's knowledge its Processed Customer Data and use thereof will not violate this Agreement (including the AUP) or any applicable law or regulation; (ii) Customer is solely responsible for Customer's development, moderation, operation, maintenance, support and use of Processed Customer Data, including when Processed Customer Data is provided by Customer's End Users or Representatives; (iii) to its knowledge, Customer's Processed Customer Data and its use thereof does not and will not:
```

---

### 11. Customer Obligations — [🔴 High Risk] (`3F6224A0`)

**Legal Analysis & Justification:**
> This provision imposes open-ended, unlimited liability on Customer for any loss or damage to Processed Customer Data attributable to Customer, its End Users, or Representatives, without capping or defining the scope of that exposure. As drafted, the consequence of a Customer-side failure is entirely unbounded — CoreWeave faces "no liability," but there is no reciprocal ceiling or defined remedy framework limiting what CoreWeave could claim or withhold against Customer in the same scenario. This asymmetry is particularly dangerous for Microsoft because the clause sweeps in the acts of any individual using the Service on Customer's behalf, a broad class that could include contractors, agents, or downstream users, and it does not distinguish between minor operational errors and material breaches. The carve-out preserving CoreWeave's liability where it is itself in breach is helpful but insufficient: it does not limit the consequence to Customer to a defined remedy such as cost relief, service credits, or a liability cap tied to fees paid. Left unaddressed, this exposes Microsoft to disproportionate and unpredictable financial risk arising from the conduct of a wide range of third parties acting on its behalf, with no contractual ceiling on the resulting exposure.

**Original Contract Wording:**
```text
(A) infringe, violate, or misappropriate any third party right, including any copyright, trademark, patent, trade secret, moral right, privacy right, right of publicity, or any other intellectual property or proprietary right; (B) slander, defame, libel, or invade a right of privacy, publicity or other property rights of any other person; or (C) cause Coreweave to violate any law, regulation or rule, or the rights of any third party; and (iv) except for the specific Services provided under this Agreement, Customer is solely responsible for the technical operation of Processed Customer Data, including on behalf of Customer’s End Users and Representatives. COREWEAVE SHALL BEAR NO LIABILITY WITH RESPECT TO PROCESSED CUSTOMER DATA THAT IS LOST OR DAMAGED AS A RESULT OF THE ACTIONS OF CUSTOMER, ITS END USERS, REPRESENTATIVES OR THE ACTIONS OF ANY INDIVIDUAL WHO USES THE SERVICE ON CUSTOMER’S OR ITS END USERS’ OR REPRESENTATIVES’ BEHALF EXCEPT WHERE COREWEAVE IS IN
```

**Suggested Revision / Redline Fix:**
```text
(A) infringe, violate, or misappropriate any third party right, including any copyright, trademark, patent, trade secret, moral right, privacy right, right of publicity, or any other intellectual property or proprietary right; (B) slander, defame, libel, or invade a right of privacy, publicity or other property rights of any other person; or (C) cause Coreweave to violate any law, regulation or rule, or the rights of any third party; and (iv) except for the specific Services provided under this Agreement, Customer is solely responsible for the technical operation of Processed Customer Data, including on behalf of Customer's End Users and Representatives. COREWEAVE SHALL BEAR NO LIABILITY WITH RESPECT TO PROCESSED CUSTOMER DATA THAT IS LOST OR DAMAGED AS A DIRECT RESULT OF THE INTENTIONAL OR GROSSLY NEGLIGENT ACTIONS OF CUSTOMER, ITS END USERS, OR REPRESENTATIVES, OR THE INTENTIONAL OR GROSSLY NEGLIGENT ACTIONS OF ANY INDIVIDUAL WHO USES THE SERVICE ON CUSTOMER'S OR ITS END USERS' OR REPRESENTATIVES' BEHALF, PROVIDED THAT (A) COREWEAVE PROMPTLY NOTIFIES CUSTOMER IN WRITING OF ANY SUCH LOSS OR DAMAGE AND TAKES COMMERCIALLY REASONABLE STEPS TO MITIGATE THE IMPACT THEREOF; (B) CUSTOMER'S AGGREGATE LIABILITY ARISING FROM OR RELATED TO ANY SUCH LOSS OR DAMAGE SHALL NOT EXCEED THE TOTAL FEES PAID OR PAYABLE BY CUSTOMER TO COREWEAVE IN THE TWELVE (12) MONTHS IMMEDIATELY PRECEDING THE EVENT GIVING RISE TO SUCH LOSS OR DAMAGE; AND (C) THIS LIMITATION SHALL NOT APPLY WHERE COREWEAVE IS IN BREACH OF ITS OBLIGATIONS UNDER THIS AGREEMENT OR WHERE SUCH LOSS OR DAMAGE RESULTS FROM COREWEAVE'S FAILURE TO MAINTAIN ADEQUATE BACKUP, SECURITY, OR DATA PROTECTION MEASURES AS REQUIRED UNDER THIS AGREEMENT.
```

---

### 12. Representations & Warranties — [🟡 Medium Risk] (`4460C51E`)

**Legal Analysis & Justification:**
> This clause imposes representations and warranties on the Customer but is entirely silent on the remedy available to either party in the event of a breach. Without a defined remedy — such as a right to cure, re-performance, or a cap on resulting liability — CoreWeave retains maximum flexibility to pursue any available legal remedy, including termination or uncapped damages, if it alleges a Customer warranty breach. From Microsoft's perspective as Customer, this asymmetry is commercially risky: the Agreement elsewhere likely contains CoreWeave's own warranty disclaimers and liability limitations that protect the vendor, yet the Customer's warranty obligations carry no corresponding remedy ceiling or cure period. Specifying a remedy framework — at minimum a notice-and-cure right before any claim may be asserted — limits Microsoft's exposure and brings this provision into balance with standard enterprise contracting practice.

**Original Contract Wording:**
```text
Customer Representations and Warranties. Customer represents and warrants to CoreWeave that: (i) it has the full right and authority to enter into, execute, and perform its obligations under this Agreement; (ii) it has accurately identified itself and to its knowledge, it has not provided any inaccurate information about itself or its End Users to or through the Service; and (iii) the information Customer provides in registering for the Service is accurate and the Customer has the right to use and disclose such information to CoreWeave.
```

**Suggested Revision / Redline Fix:**
```text
Customer Representations and Warranties. Customer represents and warrants to CoreWeave that: (i) it has the full right and authority to enter into, execute, and perform its obligations under this Agreement; (ii) it has accurately identified itself and to its knowledge, it has not provided any inaccurate information about itself or its End Users to or through the Service; and (iii) the information Customer provides in registering for the Service is accurate and the Customer has the right to use and disclose such information to CoreWeave. In the event of a breach or alleged breach of any of the foregoing representations and warranties, CoreWeave's sole and exclusive remedy shall be to provide Customer with written notice specifying the nature of the breach in reasonable detail, whereupon Customer shall have thirty (30) days from receipt of such notice to cure the breach to CoreWeave's reasonable satisfaction. If the breach is not cured within such period, CoreWeave may pursue the remedies available to it under this Agreement, subject in all cases to the limitation of liability provisions set forth herein. Nothing in this Section shall be construed to expand Customer's liability beyond the limitations expressly set forth in this Agreement.
```

---

### 13. Representations & Warranties — [🟡 Medium Risk] (`4F76F50D`)

**Legal Analysis & Justification:**
> This warranty clause presents two meaningful gaps for Microsoft as Customer. First, the performance warranty is limited to material conformance with Documentation and Order Forms, but contains no express commitment to a professional standard of care — meaning CoreWeave could deliver services that technically meet documentation specs yet are executed negligently or without the skill expected of a sophisticated cloud infrastructure provider. For a customer of Microsoft's scale and operational dependency, the absence of a "reasonable skill and care" standard leaves a practical enforcement gap when service quality degrades in ways not captured by written specifications. Second, the non-infringement warranty is narrowly scoped to use "in accordance with the applicable Order," which means that if Microsoft's use deviates even slightly from the Order's terms — including through any ambiguity in scope — CoreWeave could disclaim the warranty entirely. This creates an asymmetric risk where Microsoft bears infringement exposure for conduct that CoreWeave's own platform enables. Both limitations should be tightened: the performance warranty should incorporate an explicit professional standard, and the non-infringement warranty should cover Microsoft's reasonably anticipated use of the Services rather than being strictly tethered to Order compliance.

**Original Contract Wording:**
```text
CoreWeave Representations and Warranties. CoreWeave represents and warrants that (i) it has the full right and authority to enter into, execute, and perform its obligations under this Agreement; (ii) during the Term (a) CoreWeave will not materially decrease the overall security of the Service, and (b) the Services will perform materially in accordance with the applicable Documentation and any Order Form (the Order Form controlling in the event of any conflict), (iii) neither the Services or Documentation, nor their provision, receipt or use, in accordance with the applicable Order, infringe or misappropriate any intellectual property or other proprietary right of any third party;
```

**Suggested Revision / Redline Fix:**
```text
CoreWeave Representations and Warranties. CoreWeave represents and warrants that (i) it has the full right and authority to enter into, execute, and perform its obligations under this Agreement; (ii) during the Term (a) CoreWeave will not materially decrease the overall security of the Service, (b) the Services will perform materially in accordance with the applicable Documentation and any Order Form (the Order Form controlling in the event of any conflict), and (c) CoreWeave will perform the Services with reasonable skill, care, and diligence consistent with industry standards applicable to a professional provider of cloud infrastructure services; (iii) neither the Services or Documentation, nor their provision, receipt or use by Customer in connection with this Agreement or any Order, infringe or misappropriate any intellectual property or other proprietary right of any third party;
```

---

### 14. Representations & Warranties — [🟡 Medium Risk] (`21A3D5CE`)

**Legal Analysis & Justification:**
> The legal compliance warranty in clause (v) is currently one-sided: only one party (based on context, the Service Provider) makes the representation that it complies with applicable laws and regulations in connection with the Services. From Microsoft's perspective as Customer, this creates an asymmetry — Microsoft should also receive the benefit of a mutual compliance warranty, and critically, the Provider's warranty should be broadened to cover not just its own compliance but also that of its subcontractors, given that subcontractors may be performing material portions of the Services. Without an explicit subcontractor compliance obligation, Microsoft bears residual risk if a subcontractor's non-compliance exposes Microsoft to regulatory liability or service disruption. Additionally, the phrase "in connection with the Services" may be read narrowly to exclude compliance obligations that arise from the Provider's general business operations but that nonetheless affect the Services (e.g., data protection laws, export controls, labor laws). Leaving this gap unaddressed creates a medium-level risk that Microsoft could face regulatory exposure or be unable to enforce compliance obligations against the Provider or its subcontractors.

**Original Contract Wording:**
```text
it has accurately identified itself and to its knowledge, it has not provided any inaccurate information about itself or its subcontractors, and; (v) it does and will comply with all applicable laws and regulations in connection with the Services.
```

**Suggested Revision / Redline Fix:**
```text
it has accurately identified itself and to its knowledge, it has not provided any inaccurate information about itself or its subcontractors, and; (v) it does and will comply, and shall ensure that its subcontractors do and will comply, with all applicable laws and regulations in connection with the Services, including without limitation applicable data protection, privacy, export control, and anti-corruption laws and regulations.
```

---

### 15. Limitation of Liability — [🟡 Medium Risk] (`51E09BD9`)

**Legal Analysis & Justification:**
> The limitation of liability clause contains two meaningful gaps that leave Microsoft exposed. First, the carve-outs from the liability cap do not expressly preserve unlimited liability for fraud or death and personal injury caused by negligence. While willful misconduct and gross negligence are carved out, those standards are narrower than fraud and personal injury by negligence, which are categories that courts in many jurisdictions will not permit to be capped by contract. Failing to name them explicitly creates ambiguity that a counterparty could exploit to argue the cap applies. Second, and more practically significant for Microsoft as a large-spend customer, the general liability cap is set at only six months of fees paid or payable. Given the scale of Microsoft's anticipated spend under this agreement, a six-month lookback is a materially low ceiling for claims arising from service failures, security incidents, or other breaches not covered by the elevated twelve-month cap. The twelve-month cap already applies to confidentiality breaches and data processing agreement violations, so there is no commercial justification for the general cap to be set at half that period. Aligning the general cap to twelve months is a standard market position and directly protects Microsoft's ability to recover meaningful compensation for significant service disruptions or other material breaches.

**Original Contract Wording:**
```text
LIMITATION OF LIABILITY. EXCEPT FOR EITHER PARTY’S WILLFUL MISCONDUCT, GROSS NEGLIGENCE OR OBLIGATIONS UNDER SECTION 9(A)Y OR 9(B)(i) (INFRINGEMENT INDEMNIFICATION), OR EITHER PARTY’S BREACH OF SECTIONS 6 (CONFIDENTIALITY), IN NO EVENT SHALL THE AGGREGATE LIABILITY OF EACH PARTY TOGETHER WITH ALL OF ITS AFFILIATES ARISING OUT OF OR RELATED TO THIS AGREEMENT EXCEED THE FEES PAID OR PAYABLE BY CUSTOMER IN THE [SIX MONTHS] PRECEDING THE EVENT GIVING RISE TO THE CLAIM; PROVIDED THAT, NOTWITHSTANDING THE FOREGOING, EACH PARTY’S AGGREGATE LIABILITY WITH RESPECT TO ANY OTHER BREACH OF CONFIDENTIALITY, BREACH OF SECTIONS 4(E) (WITH RESPECT TO COREWEAVE) or 3(F) (WITH RESPECT TO CUSTOMER), OR THE DATA PROCESSING AGREEMENT, SHALL NOT EXCEED THE FEES PAID OR PAYABLE BY CUSTOMER IN THE [TWELVE MONTHS] PRECEDING THE EVENT GIVING RISE TO THE CLAIM. THE FOREGOING LIMITATION WILL APPLY WHETHER AN ACTION IS IN CONTRACT OR TORT AND REGARDLESS OF THE THEORY OF LIABILITY, BUT WILL NOT LIMIT CUSTOMER’S AND ITS AFFILIATES’ PAYMENT OBLIGATIONS UNDER THE “FEES AND PAYMENT” SECTION ABOVE.
```

**Suggested Revision / Redline Fix:**
```text
LIMITATION OF LIABILITY. EXCEPT FOR EITHER PARTY'S WILLFUL MISCONDUCT, GROSS NEGLIGENCE, FRAUD, DEATH OR PERSONAL INJURY CAUSED BY NEGLIGENCE, OR OBLIGATIONS UNDER SECTION 9(A) OR 9(B)(i) (INFRINGEMENT INDEMNIFICATION), OR EITHER PARTY'S BREACH OF SECTIONS 6 (CONFIDENTIALITY), IN NO EVENT SHALL THE AGGREGATE LIABILITY OF EACH PARTY TOGETHER WITH ALL OF ITS AFFILIATES ARISING OUT OF OR RELATED TO THIS AGREEMENT EXCEED THE FEES PAID OR PAYABLE BY CUSTOMER IN THE [TWELVE MONTHS] PRECEDING THE EVENT GIVING RISE TO THE CLAIM; PROVIDED THAT, NOTWITHSTANDING THE FOREGOING, EACH PARTY'S AGGREGATE LIABILITY WITH RESPECT TO ANY OTHER BREACH OF CONFIDENTIALITY, BREACH OF SECTIONS 4(E) (WITH RESPECT TO COREWEAVE) or 3(F) (WITH RESPECT TO CUSTOMER), OR THE DATA PROCESSING AGREEMENT, SHALL NOT EXCEED THE FEES PAID OR PAYABLE BY CUSTOMER IN THE [TWELVE MONTHS] PRECEDING THE EVENT GIVING RISE TO THE CLAIM. THE FOREGOING LIMITATION WILL APPLY WHETHER AN ACTION IS IN CONTRACT OR TORT AND REGARDLESS OF THE THEORY OF LIABILITY, BUT WILL NOT LIMIT CUSTOMER'S AND ITS AFFILIATES' PAYMENT OBLIGATIONS UNDER THE "FEES AND PAYMENT" SECTION ABOVE.
```

---

### 16. Fees & Payment — [🟡 Medium Risk] (`6BA5D0DC`)

**Legal Analysis & Justification:**
> This payment clause presents several gaps that expose Microsoft as Customer to meaningful financial and operational uncertainty. First, while the clause references fees "stated in each Order Form," it provides no fallback mechanism or rate schedule for situations where an Order Form is silent or disputed, leaving fee determinability dependent entirely on the completeness of individual Order Forms. Second, and more significantly, the clause contains no statement on whether fees are fixed for any period or subject to unilateral adjustment by CoreWeave — meaning CoreWeave could theoretically revise rates on short notice without contractual constraint. Third, the clause is silent on the consequences of late payment: there is no grace period, no specified interest rate, and no cure period before any suspension right could be exercised, which creates ambiguity that could be used against Customer. Finally, the sentence governing payment timing after invoice acceptance is incomplete — it trails off without specifying a payment deadline (e.g., net 30 days), which is a basic commercial term that must be present to make the obligation enforceable and predictable. Taken together, these omissions create a clause that is structurally incomplete and disproportionately favorable to CoreWeave.

**Original Contract Wording:**
```text
Payment of Fees. Fees for either Reserved Compute Instances or Bulk Credits are due and payable in accordance with the applicable Order Forms and this Agreement. Any on-demand use by Customer will be billed monthly in arrears based on the Customer’s prior month’s usage. Customer will pay CoreWeave fees stated in each Order Form. CoreWeave is responsible for expenses it incurs unless agreed otherwise in an Order Form. CoreWeave will not markup expenses Customer agrees to pay. CoreWeave will not offset against amounts Customer owes unless agreed otherwise expressly set forth in this Agreement or in an Order Form. After Customer accepts Services and receives a proper and undisputed invoice, it will pay Fees and approved expenses
```

**Suggested Revision / Redline Fix:**
```text
Payment of Fees. Fees for either Reserved Compute Instances or Bulk Credits are due and payable in accordance with the applicable Order Forms and this Agreement. Any on-demand use by Customer will be billed monthly in arrears based on the Customer's prior month's usage. Customer will pay CoreWeave fees stated in each Order Form, and CoreWeave may not increase any fees applicable to a committed Order Form term without Customer's prior written consent; any fee changes applicable to on-demand usage shall require no less than sixty (60) days' prior written notice to Customer before taking effect. CoreWeave is responsible for expenses it incurs unless agreed otherwise in an Order Form. CoreWeave will not markup expenses Customer agrees to pay. CoreWeave will not offset against amounts Customer owes unless agreed otherwise expressly set forth in this Agreement or in an Order Form. After Customer accepts Services and receives a proper and undisputed invoice, it will pay Fees and approved expenses within thirty (30) days of receipt of such invoice. Any amounts not disputed in good faith that remain unpaid after such thirty (30) day period shall accrue interest at the lower of one and one-half percent (1.5%) per month or the maximum rate permitted by applicable law, and CoreWeave shall provide Customer with written notice and a fifteen (15) day cure period before exercising any right to suspend Services on account of non-payment.
```

---

### 17. Service Levels — [🔴 High Risk] (`3A0DC61E`)

**Legal Analysis & Justification:**
> This provision contains a sweeping liability exclusion that bars Microsoft from recovering any financial or other damages arising from maintenance-related service interruptions, regardless of CoreWeave's fault, the duration of the outage, or the business impact on Microsoft. The phrase "under no circumstances will CoreWeave be held liable for any financial or other damages due to such interruptions" effectively makes the Service Level Objective the sole and exclusive remedy for downtime — yet the agreement does not expressly guarantee that SLO credits will be adequate or even available for all interruption scenarios. This is a high-risk provision for Microsoft because it eliminates all common-law and contractual remedies (including damages for breach of contract, negligence, or gross negligence) that would otherwise be available when CoreWeave's maintenance activities cause material harm. In practice, CoreWeave could conduct poorly planned, excessively frequent, or negligently executed maintenance that causes significant operational disruption to Microsoft's workloads, and Microsoft would have no recourse beyond whatever SLO credit mechanism applies. The fix preserves CoreWeave's reasonable protection from liability for properly conducted, duly noticed maintenance while ensuring Microsoft retains its rights where CoreWeave fails to meet its own maintenance obligations or acts negligently.

**Original Contract Wording:**
```text
Maintenance. Interruptions of data processing and access may occur due to planned or emergency maintenance and repair by CoreWeave, or due to a Force Majeure Event (as defined in Section 13.c.). Subject to the Service Level Objective, under no circumstances will CoreWeave be held liable for any financial or other damages due to such interruptions. For the purposes of this Section 12, maintenance shall include, planned maintenance windows (scheduled and communicated to Customer using best commercial efforts to notify in as much advance as possible, as needed, but in no event less than five (5) business days in advance), planned critical maintenance (communicated to Customer using best commercial efforts to notify in as much advance time, as needed, but in no event less than 24 hours’ notice in advance) and emergency unplanned maintenance windows (critical, unforeseen maintenance needed for the security or performance of the Services). CoreWeave will use best efforts to conduct maintenance at non-peak hours (as may be communicated by Customer from time to time) and inform Customer in advance if CoreWeave plans to exercise a quarterly planned maintenance window.
```

**Suggested Revision / Redline Fix:**
```text
Maintenance. Interruptions of data processing and access may occur due to planned or emergency maintenance and repair by CoreWeave, or due to a Force Majeure Event (as defined in Section 13.c.). Subject to the Service Level Objective, CoreWeave will not be liable for financial or other damages arising solely from service interruptions caused by maintenance conducted in full compliance with the notice requirements and procedures set forth in this Section 12; provided, however, that nothing in this Section 12 shall limit or exclude CoreWeave's liability for damages arising from (i) CoreWeave's failure to provide the required advance notice, (ii) CoreWeave's negligence or willful misconduct in connection with any maintenance activity, or (iii) any breach of CoreWeave's obligations under this Agreement. For the purposes of this Section 12, maintenance shall include, planned maintenance windows (scheduled and communicated to Customer using best commercial efforts to notify in as much advance as possible, as needed, but in no event less than five (5) business days in advance), planned critical maintenance (communicated to Customer using best commercial efforts to notify in as much advance time, as needed, but in no event less than 24 hours' notice in advance) and emergency unplanned maintenance windows (critical, unforeseen maintenance needed for the security or performance of the Services). CoreWeave will use best efforts to conduct maintenance at non-peak hours (as may be communicated by Customer from time to time) and inform Customer in advance if CoreWeave plans to exercise a quarterly planned maintenance window.
```

---

### 18. Service Levels — [🔴 High Risk] (`3D763308`)

**Legal Analysis & Justification:**
> This provision is dangerously thin for a customer of Microsoft's scale and operational dependency. The clause does no more than point to Exhibit A for service level objectives without stating any measurable targets, defining how or over what period performance will be calculated, or requiring CoreWeave to report performance results to the Customer. In practice, this means the Customer has no contractually enforceable benchmark against which to hold CoreWeave accountable within the body of the agreement itself, no visibility into whether SLOs are being met unless the Customer independently monitors performance, and no obligation on CoreWeave to proactively surface failures. If Exhibit A is incomplete, amended unilaterally, or simply silent on measurement methodology and reporting cadence, the Customer is left without a meaningful remedy trigger. For a mission-critical infrastructure engagement, this gap creates serious operational and legal exposure: the Customer cannot demonstrate a breach, cannot calculate credits, and cannot build escalation or termination rights on a solid contractual foundation.

**Original Contract Wording:**
```text
Service Level Objective. CoreWeave provides the Services to Customer at the service levels objectives (“Service Level Objective” or “SLO”) detailed in Exhibit A.
```

**Suggested Revision / Redline Fix:**
```text
Service Level Objective. CoreWeave provides the Services to Customer at the service levels objectives ("Service Level Objective" or "SLO") detailed in Exhibit A. Each SLO set forth in Exhibit A shall include: (a) a named, measurable performance standard (including, at minimum, availability and, where applicable, response time targets expressed as specific numerical thresholds); (b) the methodology by which performance against each SLO is calculated; and (c) the measurement period over which each SLO is assessed, which shall be no less frequent than a calendar month. CoreWeave shall provide Customer with a written performance report no later than ten (10) business days following the end of each measurement period, detailing actual performance against each applicable SLO for that period. Failure to deliver such reports shall not relieve CoreWeave of any obligation or liability arising from an SLO failure during the relevant period.
```

---

### 19. Service Levels — [🔴 High Risk] (`068E9EBF`)

**Legal Analysis & Justification:**
> This Hardware Failure provision is materially deficient from Microsoft's perspective because the only contractual consequence for a prolonged hardware outage is a fee credit — and even that remedy is delayed, triggering only after two full consecutive business days of failure. There is no remedial action plan obligation, no escalation mechanism, no service restoration timeline, and critically, no right for Microsoft to terminate the affected Services (or the Agreement) if a Hardware Failure persists beyond a reasonable period. In a high-volume, mission-critical compute environment, a two-day threshold before any remedy attaches — with no further consequence thereafter — leaves Microsoft exposed to indefinite outages with no meaningful recourse. The absence of a termination right for persistent failures is a significant gap: Microsoft could be locked into a non-performing contract with no exit. The provision should be strengthened to require a remediation plan within a defined period, impose escalating credits for extended failures, and grant Microsoft a termination right if the failure is not resolved within an agreed outside date.

**Original Contract Wording:**
```text
Hardware Failures. CoreWeave shall promptly notify Customer in the event a Hardware Failure occurs and, for so long as such Hardware Failure continues, CoreWeave shall use best efforts to repair the hardware without undue delay. CoreWeave shall promptly notify Customer when the Hardware Failure has been resolved. To the extent the Hardware Failure continues for a period of two (2) consecutive business days, then Customer’s payment obligations pursuant to Section 11(a) for fees payable for Services not performed due to the Hardware Failure shall be canceled with respect to such Services. Payment obligations for such hardware will not recommence until such time that the Hardware Failure is resolved and Services of such hardware are restored.
```

**Suggested Revision / Redline Fix:**
```text
Hardware Failures. CoreWeave shall promptly notify Customer in the event a Hardware Failure occurs and, for so long as such Hardware Failure continues, CoreWeave shall use best efforts to repair the hardware without undue delay. Within twenty-four (24) hours of the onset of a Hardware Failure, CoreWeave shall provide Customer with a written remediation plan, including a targeted resolution timeline and escalation contacts. CoreWeave shall promptly notify Customer when the Hardware Failure has been resolved. To the extent the Hardware Failure continues for a period of two (2) consecutive business days, then Customer's payment obligations pursuant to Section 11(a) for fees payable for Services not performed due to the Hardware Failure shall be canceled with respect to such Services. Payment obligations for such hardware will not recommence until such time that the Hardware Failure is resolved and Services of such hardware are restored. In addition, if a Hardware Failure continues for a period of five (5) or more consecutive business days, CoreWeave shall issue Customer a service credit equal to ten percent (10%) of the monthly fees attributable to the affected Services for each additional business day the Hardware Failure remains unresolved. If a Hardware Failure continues for a period of fifteen (15) or more consecutive business days, Customer may, upon written notice to CoreWeave, terminate the affected Services (or, at Customer's election, the Agreement in its entirety) without penalty, and CoreWeave shall promptly refund any prepaid fees attributable to the terminated Services for the unused portion of the applicable term.
```

---

### 20. Service Levels — [🔴 High Risk] (`286CA90E`)

**Legal Analysis & Justification:**
> This provision obligates CoreWeave to maintain hardware spares and replace failed network switches or compute nodes within two consecutive business days, but it stops short of providing any meaningful remedy to the Customer if CoreWeave fails to meet that obligation. There is no consequence attached to a breach — no service credits, no right to terminate, and no escalation path if hardware failures persist or recur. From Microsoft's perspective, this is a significant gap: a prolonged or repeated hardware failure could materially disrupt workloads, yet the Customer would have no contractual lever beyond a general breach claim. The absence of a termination right for chronic or serious failures is particularly concerning given the scale and criticality of Microsoft's anticipated usage. Without an express remedy tied to this commitment, the hardware spares obligation is effectively unenforceable in any practical sense.

**Original Contract Wording:**
```text
Hardware Spares: Coreweave shall maintain the right amount of spares in all locations to replace the network switches or compute nodes that cannot be remediated from a hardware failure beyond the 2 consecutive business days.
```

**Suggested Revision / Redline Fix:**
```text
Hardware Spares: Coreweave shall maintain the right amount of spares in all locations to replace the network switches or compute nodes that cannot be remediated from a hardware failure beyond the 2 consecutive business days. In the event Coreweave fails to replace or remediate a failed network switch or compute node within 2 consecutive business days, Customer shall be entitled to a Service Credit equal to the pro-rated fees for the affected resources for each additional day the failure remains unremediated. If any single hardware failure remains unremediated for more than 10 consecutive business days, or if Customer experiences 3 or more separate hardware failures affecting the same location within any rolling 90-day period that each exceed the 2 consecutive business day remediation period, Customer shall have the right, upon written notice to Coreweave, to terminate the affected Service Order or, at Customer's election, the Agreement in its entirety without penalty, and Coreweave shall refund any prepaid, unused fees attributable to the terminated services.
```

---

### 21. Miscellaneous / Other Provisions — [🔴 High Risk] (`12FA6549`)

**Legal Analysis & Justification:**
> This No Publicity clause is entirely one-sided in CoreWeave's favor. It imposes a strict confidentiality obligation on CoreWeave regarding Customer's identity and branding, which is appropriate, but it contains no reciprocal restriction preventing Customer from being compelled to disclose CoreWeave's identity or the relationship in Customer's own marketing, press releases, or public filings — nor does it restrict CoreWeave from referencing the relationship in investor materials or other non-advertising contexts not explicitly covered. More critically, while a breach by CoreWeave is elevated to a material breach (triggering termination rights), there is no corresponding remedy mechanism for Customer beyond that label — no injunctive relief acknowledgment, no liquidated damages, and no explicit right to seek equitable relief, which is the most practically useful remedy for an unauthorized publicity disclosure that cannot be undone with money damages. From Microsoft's perspective, an unauthorized public disclosure of its identity as a CoreWeave customer could have significant competitive, reputational, and market-sensitivity consequences. The clause should be made mutual, and Customer's right to seek injunctive or other equitable relief without bond or proof of damages should be expressly preserved, given the irreparable harm nature of such disclosures.

**Original Contract Wording:**
```text
No Publicity. Coreweave shall not disclose to any third party for use in advertising, publicity or similar initiatives: (i) the identity of Customer or any of its Affiliates or Representatives or End Users as a customer or the existence or nature of the relationship of the parties under this Agreement; or (ii) any name or logo of Customer, any of its Affiliates or Representatives or End Users. Any breach of this Section 13(a) shall be deemed a material breach of this Agreement.
```

**Suggested Revision / Redline Fix:**
```text
No Publicity. Neither party shall disclose to any third party for use in advertising, publicity, press releases, marketing materials, investor communications, or similar initiatives: (i) the identity of the other party or any of its Affiliates or Representatives or End Users as a customer or vendor, or the existence or nature of the relationship of the parties under this Agreement; or (ii) any name, logo, or trademark of the other party or any of its Affiliates or Representatives or End Users, in each case without the prior written consent of the other party. Any breach of this Section 13(a) shall be deemed a material breach of this Agreement. The parties acknowledge that any breach or threatened breach of this Section 13(a) may cause irreparable harm for which monetary damages would be an inadequate remedy, and accordingly each party shall be entitled to seek injunctive or other equitable relief to prevent or remedy such breach, without the requirement to post bond or other security or to prove actual damages, in addition to all other remedies available at law or in equity.
```

---

### 22. Miscellaneous / Other Provisions — [🔴 High Risk] (`153F1421`)

**Legal Analysis & Justification:**
> This Force Majeure clause presents several significant risks for Microsoft as Customer. First, the notification obligations lack defined timeframes — "promptly" is undefined, leaving CoreWeave with unconstrained discretion on when to alert Microsoft to a service disruption, which is commercially unacceptable for a customer of Microsoft's scale that depends on predictable infrastructure. Second, the clause improperly houses liability, payment, and termination-adjacent provisions that belong in their dedicated sections, creating interpretive conflicts with those sections and making the agreement harder to administer. Third, the two-consecutive-business-day threshold before payment obligations are canceled is too long for a cloud infrastructure customer — Microsoft should not be obligated to pay for services it is not receiving from the moment of non-performance, not after 48 hours. Fourth, the clause provides no Customer termination right tied to a prolonged Force Majeure Event, leaving Microsoft locked into the agreement indefinitely if CoreWeave cannot restore services. Fifth, "best efforts" to recommence performance is a high standard that may be appropriate here, but it is paired with "without undue delay," which is again undefined and unenforceable in practice. Sixth, the clause does not address Microsoft's right to seek alternative services or providers during a Force Majeure Event, nor does it require CoreWeave to provide status updates at defined intervals. Taken together, these gaps expose Microsoft to extended service outages with no payment relief, no termination exit, and no enforceable timeline for restoration.

**Original Contract Wording:**
```text
Force Majeure. CoreWeave will not be liable for any delay or failure to perform the Services under this Agreement due to circumstances beyond CoreWeave’s reasonable control, including acts of God, acts of government, flood, fire, earthquakes, pandemics, civil unrest, acts of terror, strikes or other labor problems, regional shortage of adequate power or telecommunications or transportation, internet or other service disruptions involving hardware, software or power systems not within CoreWeave’s possession or reasonable control, and denial of service attacks (“Force Majeure Event”). CoreWeave shall promptly notify Customer in the event a Force Majeure Event occurs and, for so long as such Force Majeure Event continues, CoreWeave shall use best efforts to recommence its performance hereunder without undue delay. CoreWeave shall promptly notify Customer when the Force Majeure Event has abated or can be circumvented. To the extent the Force Majeure Event continues for a period of two (2) consecutive business days, then Customer’s payment obligations pursuant to Section 11(a) for fees payable for Services not performed due to the Force Majeure Event shall be canceled with respect to such services. Payment obligations will not recommence until such time that the Force Majeure Event is cured and Services are restored. For clarity, CoreWeave shall only be excused from performance under this Section 13(c) to the extent CoreWeave has implemented its disaster recovery and business continuity plan but was unable to resume performance despite enacting such plans.
```

**Suggested Revision / Redline Fix:**
```text
Force Majeure. CoreWeave will not be liable for any delay or failure to perform the Services under this Agreement due to circumstances beyond CoreWeave's reasonable control, including acts of God, acts of government, flood, fire, earthquakes, pandemics, civil unrest, acts of terror, strikes or other labor problems (excluding strikes or labor problems involving CoreWeave's own workforce), regional shortage of adequate power or telecommunications or transportation, internet or other service disruptions involving hardware, software or power systems not within CoreWeave's possession or reasonable control, and denial of service attacks ("Force Majeure Event"); provided, however, that CoreWeave's financial inability to perform, changes in market conditions, or events that could have been prevented by CoreWeave's reasonable disaster recovery or business continuity measures shall not constitute a Force Majeure Event. CoreWeave shall notify Customer within two (2) hours of the occurrence of a Force Majeure Event, which notice shall describe the nature of the event, the Services affected, the anticipated duration, and the steps CoreWeave is taking to mitigate and restore performance. CoreWeave shall provide Customer with written status updates no less frequently than every twenty-four (24) hours for so long as the Force Majeure Event continues, and shall use best efforts to recommence its performance hereunder as soon as reasonably practicable. CoreWeave shall notify Customer within two (2) hours when the Force Majeure Event has abated or has been circumvented and Services have been restored. To the extent the Force Majeure Event results in CoreWeave's failure to perform any Services, Customer's payment obligations pursuant to Section 11(a) for fees payable for Services not performed due to the Force Majeure Event shall be canceled from the time of such non-performance, without any minimum threshold period. Payment obligations will not recommence until such time that the Force Majeure Event is cured and the applicable Services are fully restored. During any Force Majeure Event, Customer shall have the right, without penalty or liability, to procure equivalent services from alternative providers for the duration of the event. In the event a Force Majeure Event continues for a period of five (5) or more consecutive calendar days, Customer shall have the right to terminate this Agreement or any affected Order Form, in whole or in part, upon written notice to CoreWeave, without liability to either Party, and CoreWeave shall promptly refund any prepaid fees attributable to the terminated Services for the period following the effective date of termination. For clarity, CoreWeave shall only be excused from performance under this Section 13(c) to the extent CoreWeave has implemented its disaster recovery and business continuity plan but was unable to resume performance despite enacting such plans.
```

---
