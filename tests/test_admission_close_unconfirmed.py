"""Owner decision 2A: a reviewer-approved answer whose admission close the supervisor
never confirmed is ACCEPTED, and the card says so.

The vocabulary half: one reason token, one owner sentence in BOTH twins
(``project_dialogue.TASK_CAUSE_PHRASES`` / ``web/modules/log_events.js``), pinned by
``web/tests/fixtures/outcome_phase_parity.json`` on both sides of the boundary, and a
reducer that keeps an accepted decision out of BLOCKED. The behaviour half (the paid
fence wait is gone; the final seal reads the queue's typed answer) lives below it.
"""

from __future__ import annotations

import pathlib

from ouroboros.loop_acceptance import ACCEPTANCE_DECISION_REASONS
from ouroboros.outcomes import (
    ACCEPTANCE_ACCEPTED, ACCEPTANCE_FINALIZED_UNACCEPTED, OBJECTIVE_PASS, OUTCOME_TIER_BLOCKED,
    OUTCOME_TIER_SOLVED, _objective_axis,
)
from ouroboros.project_dialogue import TASK_CAUSE_PHRASES, _completion_verdict, outcome_phase

NOTE = "admission_close_unconfirmed"
SENTENCE = "Reviewers approved this answer; the supervisor did not confirm that task admission was closed."


def _record(status: str, reason: str, *, enforcement: str = "blocking") -> dict:
    review = {"status": "pass", "outcome_tier": OUTCOME_TIER_SOLVED,
              "acceptance_decision": {"status": status, "reason": reason, "enforcement": enforcement}}
    return {"status": "completed", "reason_code": "final_message",
            "outcome_axes": {"execution": {"status": "ok"}, "review": review, "objective": _objective_axis(review)}}


def test_the_note_is_a_typed_accepted_reason_with_one_sentence_in_both_twins():
    assert NOTE in ACCEPTANCE_DECISION_REASONS
    assert TASK_CAUSE_PHRASES[NOTE] == SENTENCE
    twin = (pathlib.Path(__file__).resolve().parents[1] / "web" / "modules" / "log_events.js").read_text(encoding="utf-8")
    assert f'{NOTE}: "{SENTENCE}",' in twin, "the browser twin carries the byte-identical sentence"


def test_an_accepted_answer_with_the_note_is_done_and_never_blocked():
    """Both directions of the reducer: the note rides an ACCEPTED decision and keeps the
    objective a PASS at the reviewer's tier, while the honest unaccepted cells that a
    transport gap must NOT be laundered into still terminalize BLOCKED under blocking."""
    record = _record(ACCEPTANCE_ACCEPTED, NOTE)
    objective = record["outcome_axes"]["objective"]
    assert objective["status"] == OBJECTIVE_PASS and objective["outcome_tier"] == OUTCOME_TIER_SOLVED
    assert outcome_phase(record, {}) == "done" and outcome_phase({}, record) == "done"
    assert _completion_verdict(record, {}) == _completion_verdict({}, record) == SENTENCE
    for refusal in ("infra_failure", "review_degraded"):
        blocked = _record(ACCEPTANCE_FINALIZED_UNACCEPTED, refusal)["outcome_axes"]["objective"]
        assert blocked["outcome_tier"] == OUTCOME_TIER_BLOCKED and blocked["reason"] == refusal
    # A clean accepted decision keeps rendering nothing: the note is the only accepted cell that speaks here.
    assert _completion_verdict(_record(ACCEPTANCE_ACCEPTED, "clean_pass"), {}) == ""
