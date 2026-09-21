"""Per-chapter byte budgets for the two reference books.

Official-CI ``size_ratchet`` lane only (local runs exclude the marker, like the repository size
gates): a chapter may grow past its budget only in a diff that raises the number here and says
why. The budget is the chapter's size after the compression pass plus roughly ten percent, so
ordinary maintenance fits and accretion does not. The same maintenance margin applies to unchanged registry chapters.
"""
from __future__ import annotations

import pathlib

import pytest

from ouroboros.reference_books import BOOK_ENTRYPOINTS, load_reference_book

REPO = pathlib.Path(__file__).resolve().parents[1]

# UTF-8 bytes of each chapter source. Raise a value in the same diff that needs it, with a reason.
CHAPTER_BYTE_BUDGETS: dict[str, int] = {
    "docs/architecture/01-high-level-architecture.md": 161453,
    "docs/architecture/02-startup-onboarding-flow.md": 15517,
    # 97435 -> 99500: the notification owner is a new subsystem of this chapter
    # (its module, its client-level subscription, its room gate and its disclosed
    # limits), so the description is added rather than replacing another node's.
    # Raised for the chat-authorship paragraph in "Main rows and host-stamped
    # card rows": the previous raise consumed its own headroom, and the new
    # description replaces nothing (System voice is a fact the chapter lacked).
    # 100300 -> 100900: the Project completion mirror adds a typed key and a
    # second rendering to "Main rows"; the stale sentence is replaced, and the
    # new mechanism (gate, ordinary-message path, decorator) has no older text to displace.
    "docs/architecture/03-web-ui-pages-and-buttons.md": 100900,
    "docs/architecture/04-server-api-endpoints.md": 26833,
    "docs/architecture/05-supervisor-loop.md": 27137,
    # 286850 -> 287600: "an answer that has not arrived is a gap" is a new invariant of
    # plan review and task acceptance (the slot census vocabulary, the `awaiting`
    # projection, the only-awaited task outcome); the in-flight sentence it grew from is
    # replaced, the rest has no older text to displace.
    # +1000 (2026-09-22): the custody row memo and the per-task recent-activity
    # windows are two new mechanisms described in the paragraphs they changed;
    # the base sat 28 bytes under the previous budget.
    "docs/architecture/06-agent-core.md": 288600,
    "docs/architecture/07-configuration.md": 36991,
    # 18947 -> 19287: CI failure collection now documents diagnostic desktop builds while release remains gated.
    "docs/architecture/08-git-branching-ci-and-build.md": 19287,
    "docs/architecture/09-shutdown-and-process-cleanup.md": 12405,
    # 17655 -> 20400: the supervisor-reliability sprint adds eight invariants the chapter lacked
    # (typed permanent engine refusal, interrupted parent, stalled-loop facts, source-ack
    # pre-check, host-owed round, reviewer tool bound, off-thread custody, fence transport) —
    # new rules, one or two sentences each, so nothing is replaced; ~4 % maintenance margin.
    # 20400 -> 20650: the contracts PR adds two more rules the chapter lacked (a cross-process
    # guard derives from the durable artifact it guards; the predecessor list is a hint and an
    # emitted promote is a pending fact). Six neighbouring invariants were compressed first
    # (-153 bytes, no fact removed); the remainder is the cost of the two new rules.
    # 20650 -> 21100: one more rule the chapter lacked, the usage ledger's reader contract
    # ("money never reads a snapshot; a display never waits on money"). It REPLACES the
    # residual sentence of the off-thread invariant; the rule itself has no older text.
    # +200 (2026-09-22): invariant 10 names the process-local fingerprint memos
    # and their fallback rule; the base sat 23 bytes under the previous budget.
    "docs/architecture/10-key-invariants.md": 21300,
    "docs/architecture/11-frozen-contracts-v1.md": 24194,
    "docs/architecture/12-host-service-companions-and-chat-ids.md": 11007,
    "docs/architecture/13-external-skills-layer.md": 7764,
    "docs/development/01-role-and-authority.md": 2437,
    "docs/development/02-naming-and-boundaries.md": 36372,
    # 22873 -> 23100: one new invariant (notifications ring for live events
    # only). Its text was compressed to the load-bearing facts first; the
    # remainder is the cost of stating a rule that did not exist before.
    # +300 (2026-09-22): two new house precedents (custody row memo, bounded
    # filtered tail reader) join the projection-over-replay list; the base sat
    # 15 bytes under the previous budget.
    "docs/development/03-module-size-and-complexity.md": 23400,
    "docs/development/04-core-governance-artifacts.md": 16431,
    "docs/development/05-review-and-commit-protocol.md": 12956,
    # 94197 -> 94520: the usage-ledger lock rule gains its reader contract (a display read
    # on the supervisor loop or a gateway thread rides the last validated snapshot; money
    # never does; a pre-check's refusal takes the exact read). The one sentence it touches
    # (the lock's caller wait) is replaced; the rest is a rule the chapter lacked, and the
    # chapter had 5 bytes left. Sized to the text: 5 bytes of margin.
    "docs/development/06-rules-by-change-class.md": 94520,
    "docs/development/07-managed-update-rule.md": 4166,
    "docs/development/08-mutation-attribution-rule.md": 2899,
    "docs/development/09-process-custody-rule.md": 10028,
    "docs/development/10-platform-abstraction-rule.md": 3316,
    # 27103 -> 27600: one bullet for the Project completion mirror (the engineering
    # twin of the DESIGN paragraph); it describes a new seam, so it replaces nothing.
    # 27600 -> 28300: the "one owner intent has one control" rule. It REPLACES the
    # system-message-actions bullet and adds what no older text held: the door, the
    # regenerate-and-read-the-neighbours duty and what enforces each half.
    "docs/development/11-design-system.md": 28300,
    "docs/development/12-mcp-client-integration.md": 3313,
    "docs/development/13-gateway-boundary-pattern.md": 2228,
    # 14958 -> 16100: release proof now records diagnostic signing/attestation side effects, authority asymmetry, and fail-closed prerequisites.
    "docs/development/14-build-and-ci.md": 16100,
}


@pytest.mark.size_ratchet
def test_every_chapter_has_a_budget_and_stays_inside_it():
    seen = set()
    for book_id in BOOK_ENTRYPOINTS:
        for chapter in load_reference_book(REPO, book_id).chapters:
            seen.add(chapter.source_path)
            budget = CHAPTER_BYTE_BUDGETS.get(chapter.source_path)
            assert budget is not None, f"{chapter.source_path}: add a byte budget for the new chapter"
            size = len(chapter.raw)
            assert size <= budget, (
                f"{chapter.source_path}: {size} bytes exceeds its budget {budget}; replace the description "
                "you touched instead of appending, or raise the budget in this diff with a reason"
            )
    stale = set(CHAPTER_BYTE_BUDGETS) - seen
    assert not stale, f"budgets for chapters that no longer exist: {sorted(stale)}"
