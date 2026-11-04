"""Measure how well the vector DB alone (Layer 2) types clauses it has never seen.

Each held-out clause below is written differently from the seeded exemplars and
is sent WITHOUT a heading, so the heading router cannot help: the result is the
retrieval layer's own top-1 accuracy, the distance-weighted vote's accuracy,
and the confusions, which tell the team where more exemplars are needed.

    poetry run python scripts/eval_clause_retrieval.py
"""

# flake8: noqa: E501
import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.clause_mapping.qdrant_search import QdrantClauseSearch  # noqa: E402
from src.services.clause_mapping.resilient_classifier import VECTOR_SCORE_FLOOR, resolve_qdrant_weighted_knn  # noqa: E402

# (expected canonical_type, held-out clause text). Deliberately phrased unlike the seed exemplars.
HELD_OUT: List[Tuple[str, str]] = [
    (
        "definitions-and-interpretation",
        'For the purposes of this Agreement, "Business Day" means any day other than a Saturday, Sunday, or public holiday in New York, and "Effective Date" means the date first written above.',
    ),
    ("purpose", "The Customer is seeking a partner to operate its payroll platform and the Supplier has the expertise to do so; this Agreement records the basis on which they will work together."),
    (
        "scope-of-services",
        "The Supplier will carry out the activities listed in Annex 1, namely platform configuration, data cleansing, and end-user enablement, and nothing else unless both sides agree in writing.",
    ),
    (
        "statements-of-work-and-work-orders",
        "Each engagement under this framework will be documented in a separately signed project schedule setting out the tasks, the people, the dates, and the price, and each schedule is governed by this framework.",
    ),
    (
        "customer-obligations",
        "The Customer will nominate a decision-maker, answer the Supplier's reasonable questions within three working days, and make the test environment available before the start of phase two.",
    ),
    (
        "service-provider-obligations",
        "The Supplier will carry out the work with the skill and care expected of a competent professional firm, follow the Customer's site rules when on its premises, and tell the Customer at once if it expects any slippage.",
    ),
    ("suspension-and-termination", "Either side may end this Agreement by notice if the other commits a serious breach and has not put it right within thirty days of being told about it in writing."),
    (
        "deliverables-and-acceptance",
        "The Customer has ten working days from delivery to check each item against the agreed criteria; if it reports a defect in that time the Supplier will fix it free of charge, otherwise the item is treated as approved.",
    ),
    (
        "service-levels",
        "The platform will be reachable for at least 99.9 percent of minutes in each month; for every month it falls short the Customer receives a credit of five percent of that month's charges.",
    ),
    (
        "support-and-maintenance",
        "Help desk cover runs from 8am to 6pm on working days, priority-one incidents are answered within one hour around the clock, and patches and minor releases are included in the annual charge.",
    ),
    (
        "change-control",
        "If either side wants to alter the agreed work, it will raise a written request, the Supplier will cost it within a week, and nothing changes until both sides sign the revised schedule.",
    ),
    (
        "fees-and-payment",
        "Invoices are issued monthly in arrears and are payable within thirty days of receipt; the Customer may hold back any amount it disputes in good faith provided it says why before the due date.",
    ),
    (
        "confidentiality",
        "Each side will keep the other's private business information to itself, use it only for this project, and share it only with staff and advisers who need it and are under a matching duty of secrecy.",
    ),
    (
        "data-protection-and-privacy",
        "Where the Supplier handles personal information for the Customer it will act only on the Customer's written instructions, keep it secure, and report any breach of it within forty-eight hours of discovery.",
    ),
    (
        "intellectual-property",
        "Everything the Supplier creates for the Customer under this Agreement belongs to the Customer from the moment it is made, and the Supplier assigns all rights in it to the Customer with full title guarantee.",
    ),
    (
        "representations-and-warranties",
        "The Supplier promises that the work will be carried out to a professional standard, that the software will perform as described for ninety days, and that it will not contain malicious code.",
    ),
    (
        "indemnification",
        "The Supplier will defend the Customer against any claim that the software infringes someone else's patent or copyright and will pay any damages awarded, provided the Customer gives prompt notice and control of the defence.",
    ),
    (
        "limitation-of-liability",
        "Neither side is responsible to the other for loss of profit or any indirect loss, and each side's total responsibility under this Agreement is limited to the charges paid in the previous twelve months.",
    ),
    (
        "insurance",
        "The Supplier will keep in force professional indemnity cover of at least two million dollars per claim and public liability cover of at least one million dollars, and will show the certificates on request.",
    ),
    (
        "compliance",
        "Each side will obey the laws that apply to it, including bribery, sanctions, and export rules, and neither will offer anything of value to a public official to win or keep business connected with this Agreement.",
    ),
    (
        "audit-and-records",
        "The Customer may, once a year and on two weeks' notice, inspect the Supplier's timesheets and invoices for the work; if an overcharge of more than five percent is found the Supplier bears the cost of the inspection.",
    ),
    (
        "subcontracting-and-sub-processors",
        "The Supplier may not pass any significant part of the work to a third party without the Customer's written agreement, and remains answerable for anything such a third party does or fails to do.",
    ),
    (
        "assignment",
        "Neither side may transfer this Agreement to anyone else without the other's consent, except to a group company or to a buyer of its whole business, in which case notice is enough.",
    ),
    (
        "force-majeure",
        "Neither side is in breach if it is stopped from performing by something outside its reasonable control such as flood, war, or government order, provided it tells the other promptly and does what it can to limit the effect.",
    ),
    (
        "governing-law-and-dispute-resolution",
        "English law applies to this Agreement, and any disagreement the senior managers cannot settle within a month will be decided by the courts of England and Wales.",
    ),
    (
        "notices",
        "Formal communications under this Agreement must be in writing and sent by courier or recorded post to the addresses on the front page, with a copy by email, and count as received on delivery.",
    ),
    (
        "independent-contractor",
        "The Supplier acts on its own account and not as the Customer's employee, partner, or agent; it pays its own staff and taxes and cannot make promises on the Customer's behalf.",
    ),
    (
        "general-provisions",
        "This document is the whole bargain between the sides and replaces earlier discussions; it can be changed only by a signed written amendment, and if any part is unenforceable the rest still stands.",
    ),
    (
        "information-security-and-data-management",
        "The Supplier will protect the Customer's systems and files with controls consistent with ISO 27001, encrypt data in transit and at rest, and fix critical vulnerabilities within thirty days of learning of them.",
    ),
    (
        "personnel",
        "The Supplier will staff the engagement with suitably experienced people, will not swap out the named lead without the Customer's agreement, and will replace anyone the Customer reasonably objects to.",
    ),
    (
        "exit-and-transition-assistance",
        "When this Agreement ends for any reason the Supplier will hand back all of the Customer's files in a usable format, help move the service to a replacement for up to ninety days, and then securely erase what it holds.",
    ),
    (
        "business-continuity-and-disaster-recovery",
        "The Supplier will keep and test every year a plan for carrying on the service after a major incident, restoring production within four hours with no more than one hour of lost data.",
    ),
    (
        "order-of-precedence",
        "If these terms and a project schedule say different things, these terms win unless the schedule names the clause it is meant to replace, and the small print on any purchase order has no effect.",
    ),
    (
        "governance-and-project-management",
        "The two project leads will meet every week, a joint steering group of senior managers will meet every quarter, and unresolved problems move up from one level to the next within ten working days.",
    ),
    ("publicity-and-use-of-name", "Neither side may mention the other in a press release, on its website, or in sales material without first getting written permission for the specific wording."),
    ("non-compete", "For the length of this Agreement and one year afterwards the Supplier will not provide similar services to the three named competitors of the Customer in the European market."),
    ("non-solicitation", "For twelve months after the project ends neither side will try to hire the other's people who worked on it, although replying to a public job advert does not count."),
]


async def main() -> None:
    search = QdrantClauseSearch()
    texts = [text for _, text in HELD_OUT]
    hits_per_text = await search.search(texts, None, "eval")

    top1_correct = 0
    vote_correct = 0
    below_floor = 0
    confusions: Counter = Counter()
    print(f"{'expected':42} {'top-1 hit':42} {'score':>6}  {'vote winner':42} {'ok'}")
    print("-" * 140)
    for (expected, _), hits in zip(HELD_OUT, hits_per_text):
        top = hits[0] if hits else None
        voted, vote_score = resolve_qdrant_weighted_knn(hits)
        top_ok = top is not None and top.canonical_type == expected
        vote_ok = voted == expected
        top1_correct += top_ok
        vote_correct += vote_ok
        if top is None or top.score < VECTOR_SCORE_FLOOR:
            below_floor += 1
        if not vote_ok:
            confusions[(expected, voted)] += 1
        print(f"{expected:42} {(top.canonical_type if top else '-'):42} {(top.score if top else 0):6.3f}  {str(voted):42} {'yes' if vote_ok else 'NO'}")

    n = len(HELD_OUT)
    print("-" * 140)
    print(
        f"top-1 accuracy: {top1_correct}/{n} ({100 * top1_correct / n:.0f}%)   weighted-vote accuracy: {vote_correct}/{n} ({100 * vote_correct / n:.0f}%)   top score below {VECTOR_SCORE_FLOOR} floor: {below_floor}/{n}"
    )
    if confusions:
        print("confusions (expected -> got):")
        for (expected, got), count in confusions.most_common():
            print(f"  {expected} -> {got}  x{count}")


if __name__ == "__main__":
    asyncio.run(main())
