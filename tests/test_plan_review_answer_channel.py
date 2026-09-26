"""The plan-review ANSWER CHANNEL: answers merge by ``finding_id`` across calls (a later
answer supersedes only its own id; a same-call duplicate stays contradictory), an author
finish carries its answers, and an envelope sent beside answers is validated BEFORE anything
is recorded — the answers land first, then the envelope is reviewed with them in view.

Driven through the real engine, the real ``task_results``/artifact store and the fake
review substrate of ``tests.test_plan_review_engine``.
"""
from __future__ import annotations

import json

from ouroboros.task_results import (STATUS_RUNNING, load_plan_review_state, plan_review_wave,
    record_plan_review_dispositions, record_plan_review_wave, write_task_result)
from ouroboros.tools import plan_review as pr
from ouroboros.tools.plan_review_artifacts import authority_wave
from tests.test_plan_review_engine import (  # noqa: F401
    CLEAN, DECK_SPEC, _call, _control, _finding, _state, _user_text, harness,
)


def _question(fid: str, breaks: str = "claim_1") -> dict:
    return _finding(fid, "need_evidence", breaks=breaks, summary="which one?", rec="")


def _item(fid: str, decision: str = "accept", rationale: str = "the author's word") -> dict:
    return {"finding_id": fid, "decision": decision, "rationale": rationale}


def _answer(ctx, fp: str, *items: dict) -> str:
    return pr._handle_plan_task(ctx, review_disposition={"review_fingerprint": fp, "items": list(items)})


def _two_questions(h):
    """A REVIEW_REQUIRED wave with two questions to the author (s1:q1, s2:q2); s3 is clean."""
    sub = h.install({"s1": json.dumps([_question("q1")]), "s2": json.dumps([_question("q2", "goal")]), "s3": CLEAN})
    ctx = h.make_ctx()
    out = _call(ctx)
    assert _control(out) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    return ctx, _state(h)["waves"][-1]["request_fingerprint"], sub


def _ids(wave: dict) -> list:
    return [(d["finding_id"], d["decision"]) for d in wave.get("dispositions") or []]


# ------------------------------------------------------------------ item 1: merge by id

def test_answers_across_calls_merge_and_close_the_wave(harness):  # noqa: F811
    """Call A answers one question, call B the other: B's closure sees A's answer, and the
    hot record and the exact artifact both hold BOTH answers (before: B erased A)."""
    ctx, fp, _sub = _two_questions(harness)
    first = _answer(ctx, fp, _item("s1:q1"))
    assert _control(first) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    second = _answer(ctx, fp, _item("s2:q2"))
    assert _control(second) == {"outcome": "GREEN", "closed": True}
    hot = _state(harness)["waves"][-1]
    assert _ids(hot) == [("s1:q1", "accept"), ("s2:q2", "accept")] and hot["closed"]
    exact = authority_wave(harness.drive, ctx.task_id, hot)
    assert _ids(exact) == [("s1:q1", "accept"), ("s2:q2", "accept")] and exact["closed"]


def test_a_later_answer_supersedes_only_its_own_id(harness):  # noqa: F811
    ctx, fp, _sub = _two_questions(harness)
    _answer(ctx, fp, _item("s1:q1"))
    _answer(ctx, fp, _item("s1:q1", "defer", "deferred to the owner"))
    assert _ids(_state(harness)["waves"][-1]) == [("s1:q1", "defer")]
    closed = _answer(ctx, fp, _item("s2:q2"))
    assert _control(closed) == {"outcome": "GREEN", "closed": True}
    assert _ids(_state(harness)["waves"][-1]) == [("s1:q1", "defer"), ("s2:q2", "accept")]


def test_same_call_duplicate_stays_contradictory(harness):  # noqa: F811
    """The guard stays active: accept AND reject for one id in ONE call is refused as a
    contradiction and keeps the finding open; a later single answer then closes it."""
    ctx, fp, _sub = _two_questions(harness)
    twice = _answer(ctx, fp, _item("s1:q1", "accept"), _item("s1:q1", "reject", "no"), _item("s2:q2"))
    assert "duplicate_disposition:s1:q1" in twice
    assert _control(twice) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    later = _answer(ctx, fp, _item("s1:q1"))
    assert _control(later) == {"outcome": "GREEN", "closed": True}
    assert _ids(_state(harness)["waves"][-1]) == [("s2:q2", "accept"), ("s1:q1", "accept")]


def test_the_durable_writer_merges_by_finding_id(tmp_path):
    """``record_plan_review_dispositions`` applied to prior ``[a]`` and new ``[b]`` stores
    ``[a, b]``; with new ``[a']`` it stores ``[a']`` — never replace-on-write."""
    write_task_result(tmp_path, "t1", STATUS_RUNNING, result="running")
    fp = "c" * 64
    record_plan_review_wave(tmp_path, "t1", {
        "schema_version": 2, "cycle_index": 1, "request_fingerprint": fp,
        "spec": {"goal": "g", "acceptance_claims": []}, "spec_hash": "b" * 64,
        "findings": [{"finding_id": "s1:q1", "class": "need_evidence", "breaks": "goal"},
                     {"finding_id": "s2:q2", "class": "need_evidence", "breaks": "goal"}],
        "aggregate": "REVIEW_REQUIRED", "closed": False, "dispositions": [], "paid": True,
    })
    a = _item("s1:q1")
    b = _item("s2:q2", "defer", "later")
    record_plan_review_dispositions(tmp_path, "t1", fingerprint=fp, dispositions=[a], closed=False)
    record_plan_review_dispositions(tmp_path, "t1", fingerprint=fp, dispositions=[b], closed=False)
    assert plan_review_wave(load_plan_review_state(tmp_path, "t1"), fp)["dispositions"] == [a, b]
    a2 = _item("s1:q1", "reject", "no")
    record_plan_review_dispositions(tmp_path, "t1", fingerprint=fp, dispositions=[a2], closed=False)
    assert plan_review_wave(load_plan_review_state(tmp_path, "t1"), fp)["dispositions"] == [b, a2]


# ------------------------------------------------- item 2: answers travel with an envelope

def test_author_finish_with_items_records_answers_then_selects_the_plan(harness, monkeypatch):  # noqa: F811
    """Advisory: one call answers the critic's findings AND selects the corrected plan; the
    answers land on the critic wave first, merged by id, and no new panel runs."""
    harness.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    transport = harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]),
                                 "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    first = _call(ctx)
    assert _control(first) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    critic_fp = _state(harness)["waves"][-1]["request_fingerprint"]
    spec = {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]}
    result = _call(ctx, spec, plan="Corrected complete plan.", review_disposition={
        "review_fingerprint": critic_fp, "author_action": "finish",
        "items": [_item("s1:f1", "reject", "the budget line is already approved")],
        "author_disposition": {"disposition": "partial", "rationale": "Corrected the budget."}})
    assert "Current author plan saved" in result
    assert "1 answer(s) recorded on the critic wave, merged by finding_id." in result
    after = load_plan_review_state(harness.drive, ctx.task_id)
    assert len(transport.calls) == 1 and after["cycles_paid"] == 1
    critic = next(w for w in after["waves"] if w["request_fingerprint"] == critic_fp)
    assert _ids(critic) == [("s1:f1", "reject")]
    assert critic["closed"] and critic["aggregate"] == "GREEN"  # advisory: a reasoned reject closes it
    assert after["current_attempt"]["author_subject"]["review_fingerprint"] == critic_fp


def test_finish_while_reviewers_run_is_refused_and_records_no_answers(harness, monkeypatch):  # noqa: F811
    """The kept guard fires first: a finish while reviewers are still running is refused, and
    the items it carried are NOT written (nothing lands when the call refuses)."""
    from ouroboros import review_records

    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    monkeypatch.setattr(review_records, "review_outcome_received", lambda *_a, **_kw: False)
    refused = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": fp, "author_action": "finish",
        "items": [_item("s1:f1", "reject", "already approved")],
        "author_disposition": {"disposition": "accepted", "rationale": "Proceed."}})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in refused and "reviewers are still running" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_author_finish_items_are_validated_against_the_critic_wave(harness):  # noqa: F811
    """An unknown finding id beside a finish is refused as a whole, before any write."""
    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    refused = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": fp, "author_action": "stop", "items": [_item("s9:zz", "reject", "phantom")],
        "author_disposition": {"disposition": "partial", "rationale": "Stopping."}})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in refused and "unknown finding ids s9:zz" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_invalid_envelope_beside_answers_records_nothing(harness):  # noqa: F811
    """Validate-first: a malformed envelope beside answers is refused by its form, and neither
    the answers nor a superseding raw attempt are written (the answered wave stays current)."""
    harness.install({"s1": json.dumps([_question("q1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    refused = pr._handle_plan_task(ctx, goal="Ship the deck", plan="Changed prose.",
        spec={"in_scope": ["one table"]},  # the legacy form: no affected_paths list
        review_disposition={"review_fingerprint": fp, "items": [_item("s1:q1")]})
    assert "PLAN_RESOURCE_FORM_REQUIRED" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_changed_envelope_with_items_records_the_answers_then_reviews_every_slot(harness, monkeypatch):  # noqa: F811
    """A CHANGED envelope with items is an ordinary full wave: the answers are stored on the
    answered wave first, every slot reviews the new envelope, and the packet's PRIOR CYCLES
    section carries the rationale."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    old_fp = _state(harness)["waves"][-1]["request_fingerprint"]
    sub2 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, plan="A revised outline with the budget line fixed.", review_disposition={
        "review_fingerprint": old_fp, "items": [_item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1", "s2", "s3"]
    packet = _user_text(sub2.calls[0]["request"].messages[1]["content"])
    assert "PRIOR CYCLES" in packet and "the budget line is already approved" in packet
    state = _state(harness)
    old = next(w for w in state["waves"] if w["request_fingerprint"] == old_fp)
    assert _ids(old) == [("s1:f1", "reject")]
    assert state["cycles_paid"] == 2 and state["waves"][-1]["request_fingerprint"] != old_fp
