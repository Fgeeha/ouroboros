"""A current author's plan keeps criticism visible without inheriting its approval."""
import copy
import json

import pytest

from ouroboros.owner_hurry import force_plan_decision, plan_review_disclosure
from ouroboros.task_results import closed_plan_review_wave, load_plan_review_state, plan_review_gate_projection
from tests.test_plan_review_engine import DECK_SPEC, _call, _finding, harness as _harness

harness = _harness


@pytest.mark.parametrize("action", ["finish", "stop"])
def test_actual_author_flow_discloses_critic_without_fabricating_closure(harness, monkeypatch, action):
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    ctx = h.make_ctx()
    feedback = json.dumps([_finding("first", "blocking", breaks="claim_1")])
    h.install({s: feedback for s in ("s1", "s2", "s3")})
    _call(ctx)
    old = load_plan_review_state(h.drive, ctx.task_id)
    fingerprint = old["current_attempt"]["fingerprint"]
    result = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["Corrected claim"]},
        review_disposition={"review_fingerprint": fingerprint, "items": [], "author_action": action,
                            "author_disposition": {"disposition": "partial", "rationale": "Addressed feedback."}})
    assert "Current author plan saved" in result
    state = load_plan_review_state(h.drive, ctx.task_id)
    before = copy.deepcopy(state)
    decision = force_plan_decision(ctx, {}, enforcement="advisory")
    assert decision["outcome"] == "REVISE_PLAN"
    assert decision["author_action"] == action and not decision["closed"]
    text = plan_review_disclosure(decision)
    assert "unavailable" not in text and "REVISE_PLAN" in text
    assert "author stopped" in text if action == "stop" else "accepted by its author" in text
    assert closed_plan_review_wave(state) is None
    assert state == before

    blocked = plan_review_gate_projection(state, "blocking")
    assert blocked["allow"] is (action == "stop") and not blocked["closed"]
    assert blocked["outcome"] == "REVISE_PLAN"
    # Even an old GREEN cannot grant critic approval to a revised author subject.
    state["waves"][-1].update(aggregate="GREEN", closed=True)
    blocked = plan_review_gate_projection(state, "blocking")
    assert blocked["allow"] is (action == "stop") and closed_plan_review_wave(state) is None
    state["waves"][-1].update(aggregate="DEGRADED", closed=False, custody_pending=True)
    pending = plan_review_gate_projection(state, "advisory")
    assert pending["custody_pending"]
    assert "running or awaiting collection" in plan_review_disclosure({"required": True, **pending})
    state["waves"] = []
    missing = plan_review_gate_projection(state, "advisory")
    assert not missing["outcome"] and not missing["closed"]
    assert "unavailable" in plan_review_disclosure({"required": True, **missing})


def test_unreadable_plan_source_does_not_invent_a_critic(harness, monkeypatch):
    ctx = harness.make_ctx(force_plan=True)
    monkeypatch.setattr("ouroboros.task_results.load_plan_review_state", lambda *_a: (_ for _ in ()).throw(ValueError("source gap")))
    decision = force_plan_decision(ctx, {}, enforcement="blocking")
    assert not decision["allow"] and not decision["closed"] and not decision["outcome"]


def test_a_historical_critics_aggregate_is_labelled_not_shown_as_this_plans_verdict():
    """The revised plan has no wave of its own, so the disclosure attaches the aggregate of
    the critic the author REFERENCED. Unlabelled, "Plan review is still open (GREEN)" reads
    to the owner as approval of bytes no reviewer ever saw."""
    decision = {"required": True, "status": "open", "allow": True, "enforcement": "advisory",
                "outcome": "GREEN", "historical_critic": True}
    text = plan_review_disclosure(decision)
    assert "referenced critic review of the earlier plan: GREEN" in text
    assert "the current plan has no verdict of its own" in text


def test_the_rail_that_forced_finalization_survives_an_author_decision():
    """Both facts are true and the rail is the one that explains why the task ended.
    Returning the author sentence first dropped "the cap is spent; the task ends blocked"
    whenever an author decision and a rail applied together."""
    decision = {"required": True, "status": "cycles_exhausted", "allow": True,
                "enforcement": "blocking", "outcome": "REVIEW_REQUIRED", "cycles_paid": 2,
                "author_action": "finish"}
    text = plan_review_disclosure(decision)
    assert "cap spent (2 paid cycle(s))" in text and "ends blocked" in text
    assert "Plan author decision: finish" in text
    assert "does not close or replace the critic review" in text
