"""Plan-review facts for the learning surfaces (``ouroboros.plan_review_facts``).

The reflection asks the mind to account for every review finding; these tests pin
that the host now puts the plan review's facts in front of it — bounded, with a
source pointer, never a score — and that a panel settling after the task ended
leaves one bounded row in the reflection log.
"""
from __future__ import annotations

import json

import pytest

from ouroboros import plan_review_facts as facts_mod
from ouroboros.plan_review_facts import (
    LATE_SETTLEMENT_TASK_TYPE,
    PLAN_REVIEW_REFLECTION_CHARS,
    facts_from_state,
    late_settlement_reflection_entry,
    learn_from_late_settlement,
    plan_review_reflection_slice,
    render_plan_review_section,
)

FP = "a" * 64


def _wave(**overrides):
    wave = {
        "request_fingerprint": FP, "cycle_index": 1, "aggregate": "REVIEW_REQUIRED", "closed": False, "paid": True,
        "counts": {"blocking": 1, "need_evidence": 1, "note": 1},
        "spec": {"goal": "Ship the deck",
                 "acceptance_claims": [{"id": "claim_1", "claim": "The deck has ten slides"},
                                       {"id": "claim_2", "claim": "Every slide has speaker notes"}],
                 "decisions": [{"id": "decision_1", "choice": "Use the house template"}], "deferred": []},
        "findings": [
            {"finding_id": "s1:f1", "id": "f1", "slot": "s1", "model": "grok-4.7", "class": "blocking",
             "breaks": "claim_1", "locator": "", "summary": "Ten slides is not enough for the agenda"},
            {"finding_id": "s2:f2", "id": "f2", "slot": "s2", "model": "astra", "class": "need_evidence",
             "breaks": "claim_2", "locator": "", "summary": "Who writes the notes?"},
            {"finding_id": "s2:f3", "id": "f3", "slot": "s2", "model": "astra", "class": "note",
             "breaks": "decision_1", "locator": "", "summary": "The template is dated"},
        ],
        "dispositions": [{"finding_id": "s1:f1", "decision": "accept", "rationale": "Twelve slides then"}],
        "wave_artifact": {"path": "artifacts/plan/wave-1.json", "sha256": "b" * 64},
        "actors": [{"slot_id": "s1", "ok": True, "operation_state": "settled"},
                   {"slot_id": "s2", "ok": False, "operation_state": "in_flight", "late_result_pending": True}],
    }
    wave.update(overrides)
    return wave


def _author_plan():
    return {"kind": "plan_author_subject", "fingerprint": "c" * 64, "review_fingerprint": FP,
            "spec": {"goal": "Ship the deck",
                     "acceptance_claims": [{"id": "claim_1", "claim": "The deck has twelve slides"}],
                     "decisions": [{"id": "decision_1", "choice": "Use the house template"}], "deferred": []},
            "author_disposition": {"action": "finish", "enforcement": "advisory",
                                   "rationale": "Dropped speaker notes; the owner agreed"}}


def test_facts_name_each_element_its_findings_answers_and_fate_in_the_selected_plan():
    from ouroboros.tools.plan_review_runtime import plan_wave_slot_census

    wave = _wave()
    facts = facts_from_state({"waves": [wave]}, critic=wave, author_plan=_author_plan(), claims_source="author_plan",
                             census=plan_wave_slot_census(wave), source_ref={"kind": "task_result", "task_id": "t"})

    by_id = {row["id"]: row for row in facts["elements"]}
    assert set(by_id) == {"claim_1", "claim_2", "decision_1"}
    assert by_id["claim_1"]["changed_in_selected_plan"] == "changed"
    assert by_id["claim_1"]["findings"][0]["disposition"] == "accept: Twelve slides then"
    assert by_id["claim_1"]["findings"][0]["model"] == "grok-4.7"
    assert by_id["claim_2"]["changed_in_selected_plan"] == "removed"
    assert by_id["claim_2"]["findings"][0]["disposition"] == "unanswered"
    assert by_id["decision_1"]["changed_in_selected_plan"] == "same"
    assert facts["questions"] == [{"finding_id": "s2:f2", "breaks": "claim_2",
                                   "question": "Who writes the notes?", "answer": "unanswered"}]
    assert facts["unresolved_reviewers_at_task_end"] == {"count": 1, "slots": ["s2"]}
    assert facts["claims_source"] == "author_plan"
    assert facts["wave_ref"] == wave["wave_artifact"]
    assert facts["selected_plan"]["delta"]["removed"] == ["claim_2"] and facts["selected_plan"]["delta"]["changed"] == ["claim_1"]
    assert facts["reviewed_plan"] == {"aggregate": "REVIEW_REQUIRED", "closed": False, "cycle": 1, "closure_notes": []}
    assert facts["unchanged_elements_without_findings"] == 1  # the goal
    assert "omitted" not in facts
    # Facts, not judgement: nothing in the slice scores a reviewer or ranks a finding.
    assert not {"score", "rank", "weight"} & set(json.dumps(facts))


def test_no_recorded_wave_means_no_slice():
    assert facts_from_state({"waves": []}, critic=None) is None


def test_the_slice_is_bounded_with_every_cut_named():
    claims = [{"id": f"claim_{i}", "claim": f"Claim number {i} " + "x" * 120} for i in range(1, 41)]
    findings = [{"finding_id": f"s{j}:f{i}", "id": f"f{i}", "slot": f"s{j}", "model": "m", "class": "note",
                 "breaks": f"claim_{i}", "locator": "", "summary": "note text " * 10}
                for i in range(1, 41) for j in range(1, 33)]
    wave = _wave(spec={"goal": "g", "acceptance_claims": claims, "decisions": [], "deferred": []},
                 findings=findings, dispositions=[])

    facts = facts_from_state({"waves": [wave]}, critic=wave)

    assert len(render_plan_review_section(facts)) <= PLAN_REVIEW_REFLECTION_CHARS + 100
    assert facts["omitted"]["note_findings_summaries"] > 0 or facts["omitted"]["elements"] > 0
    assert facts["omitted"]["note"].startswith("whole rows omitted")
    assert facts["source_ref"] == {}


def _patch_sources(monkeypatch, *, state, author, authority=None):
    from ouroboros.tools import plan_review_artifacts as artifacts

    monkeypatch.setattr("ouroboros.task_results.load_plan_review_state", lambda root, tid: state)
    monkeypatch.setattr(artifacts, "current_author_plan", lambda root, tid, st: author)
    monkeypatch.setattr(artifacts, "authority_wave", authority or (lambda root, tid, hot: hot))
    monkeypatch.setattr("ouroboros.review_evidence_sections._accept_effective_claims",
                        lambda ctx, contract, root, tid: ([], "author_plan", {}))


def test_the_loader_reads_the_reviewed_wave_the_author_answered(tmp_path, monkeypatch):
    wave = _wave()
    _patch_sources(monkeypatch, state={"waves": [wave], "current_attempt": {"fingerprint": FP}}, author=_author_plan())

    facts = plan_review_reflection_slice(tmp_path, "task-1", task={"task_contract": {}})

    assert facts["source_ref"] == {"kind": "task_result", "reader": "get_task_result", "task_id": "task-1",
                                   "field": "plan_review_state"}
    assert {row["id"] for row in facts["elements"]} == {"claim_1", "claim_2", "decision_1"}
    assert facts["claims_source"] == "author_plan"


def test_an_unreadable_recorded_source_is_disclosed_never_silent(tmp_path, monkeypatch):
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable

    def gone(root, tid, hot):
        raise PlanReviewSourceUnavailable("PLAN_REVIEW_SOURCE_UNAVAILABLE: artifact gone")

    _patch_sources(monkeypatch, state={"waves": [_wave()]}, author=None, authority=gone)

    facts = plan_review_reflection_slice(tmp_path, "task-2")

    assert facts["unavailable"].endswith("artifact gone") and facts["source_ref"]["task_id"] == "task-2"


def test_a_task_without_waves_or_without_an_id_has_no_slice(tmp_path, monkeypatch):
    _patch_sources(monkeypatch, state={"waves": []}, author=None)
    assert plan_review_reflection_slice(tmp_path, "task-3") is None
    assert plan_review_reflection_slice(tmp_path, "") is None


def test_the_evidence_formatter_leads_with_the_slice_and_is_byte_identical_without_it():
    from ouroboros.review_evidence import format_review_evidence_for_prompt

    evidence = {"task_id": "t-1", "has_evidence": True, "lens_marker": "lens-survives"}
    plain = format_review_evidence_for_prompt(evidence, max_chars=8000, acceptance_panels=None)
    assert format_review_evidence_for_prompt(evidence, max_chars=8000, acceptance_panels=None, plan_review=None) == plain

    with_facts = format_review_evidence_for_prompt(evidence, max_chars=8000, acceptance_panels=None,
                                                   plan_review={"claims_source": "author_plan", "elements": []})
    assert with_facts.startswith("TASK PLAN REVIEW (host-recorded facts; which advice mattered is yours to judge):")
    assert with_facts.endswith(plain) and "author_plan" in with_facts


@pytest.mark.parametrize("recorded", [True, False])
def test_the_reflection_prompt_carries_the_slice_exactly_when_the_task_recorded_waves(monkeypatch, recorded):
    from ouroboros.reflection import generate_reflection

    captured = {}

    class _Llm:
        def chat(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            return {"content": "Reflection completed."}, {}

    slice_ = {"claims_source": "author_plan", "elements": [{"id": "claim_7", "text": "MARKER_CLAIM_SEVEN"}]}
    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice",
                        lambda root, tid, task=None: slice_ if recorded else None)

    generate_reflection(task={"id": "reflection-task", "text": "reflect"},
                        llm_trace={"tool_calls": [], "review_runs": []}, trace_summary="completed",
                        llm_client=_Llm(), usage_dict={"rounds": 2, "cost": 0.1},
                        review_evidence={"has_evidence": True, "lens_marker": "lens-survives"})

    assert ("TASK PLAN REVIEW" in captured["prompt"]) is recorded
    assert ("MARKER_CLAIM_SEVEN" in captured["prompt"]) is recorded
    assert "lens-survives" in captured["prompt"]


def _late_result(tmp_path, task_id="late-1", retry_key="rk-1"):
    from ouroboros.task_results import write_task_result

    write_task_result(tmp_path, task_id, "completed", text="Ship the deck", result="done", review_projection={
        "panels": [{"panel_id": "panel_1", "late_settlement": {
            "note": "Reviewers later passed it.\nSecond line of the host's sentence.",
            "reviewed_subject": {"retry_key": retry_key, "panel_id": "panel_1"},
            "reviewer_outputs": [{"slot_id": "a", "verdict": "PASS", "model": "model/a"}]}}]})
    return task_id


def test_the_late_settlement_row_is_bounded_sourced_and_closed_to_the_pattern_register(tmp_path, monkeypatch):
    from ouroboros.reflection import _admits_pattern_register

    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice", lambda root, tid, task=None: None)
    task_id = _late_result(tmp_path)

    entry = late_settlement_reflection_entry(tmp_path, task_id, "rk-1")

    assert entry["type"] == entry["task_type"] == LATE_SETTLEMENT_TASK_TYPE
    assert entry["supplement_id"] == "acceptance-late:rk-1" and entry["task_id"] == task_id
    assert entry["reflection"].startswith("Reviewers later passed it.")
    assert "- a (model/a): PASS" in entry["reflection"]
    assert entry["reflection"].rstrip().endswith("review_projection panel panel_1.")
    assert entry["source_ref"]["panel_id"] == "panel_1" and entry["goal"] == "Ship the deck"
    assert not _admits_pattern_register(entry)
    assert late_settlement_reflection_entry(tmp_path, task_id, "unknown-key") is None


def test_learning_from_a_late_settlement_appends_one_routed_row_and_never_raises(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result

    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice", lambda root, tid, task=None: None)
    task_id = _late_result(tmp_path)
    result = load_task_result(tmp_path, task_id)

    assert learn_from_late_settlement(tmp_path, result, "rk-1") is True
    assert learn_from_late_settlement(tmp_path, result, "unknown-key") is False
    rows = [json.loads(line) for line in (tmp_path / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["supplement_id"] for row in rows] == ["acceptance-late:rk-1"]

    monkeypatch.setattr("ouroboros.reflection.append_reflection_routed",
                        lambda env, task, entry: (_ for _ in ()).throw(OSError("disk gone")))
    assert learn_from_late_settlement(tmp_path, result, "rk-1") is False
