"""A filled optional argument that asks for nothing is not a refusal.

Models fill every key of a tool schema. These tests pin, through the REAL registry,
that such a value takes the omitted path with one disclosure line, and that a
genuine argument mistake is refused once, typed, naming the value it received.
"""
import queue

import pytest

from ouroboros.owner_wait import direct_owner_wait
from ouroboros.task_results import load_task_result
from ouroboros.tools.arg_feedback import argument_refusal, ignored_argument_note
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _registry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path, task_id="root-task",
                      is_direct_chat=True, current_chat_id=1, event_queue=queue.Queue())
    ctx.owner_wait_callback = direct_owner_wait
    registry = ToolRegistry(repo_dir=repo, drive_root=tmp_path)
    registry.set_context(ctx)
    return registry, ctx


def test_helpers_name_the_field_the_value_and_the_repair(tmp_path):
    assert ignored_argument_note("timezone", "UTC", "run_at carries its own offset") == (
        "timezone='UTC' ignored: run_at carries its own offset")
    _registry_unused, ctx = _registry(tmp_path)
    text = argument_refusal(ctx, "DEMO_INVALID", ["a=1 must be 2.", "b is missing"],
                            example="demo(a=2, b='x')", effect="Nothing was recorded.")
    assert text == ("⚠️ DEMO_INVALID: a=1 must be 2; b is missing. "
                    "Correct example: demo(a=2, b='x'). Nothing was recorded.")


@pytest.mark.parametrize("reflex", [0, 1])
def test_escalate_ignores_a_wait_bound_on_a_quiz_that_does_not_wait(tmp_path, reflex):
    """The live loop: `max_wait_minutes` 0 or 1 beside `wait_for_answer=false` and a real
    assumption was refused eight times in one day. The quiz is sent; the receipt says
    the bound was ignored; the ignored bound is never persisted with the quiz."""
    registry, ctx = _registry(tmp_path)
    result = registry.execute_result("escalate", {
        "question": "Which theme?", "options": ["Light", "Dark"], "stake": "",
        "assumption": "Light meanwhile", "wait_for_answer": False, "max_wait_minutes": reflex,
    })
    assert result.status == "ok" and result.text.startswith("OK: quiz ")
    assert f"max_wait_minutes={reflex!r} ignored: it bounds a required wait only" in result.text
    quiz_id = ctx.event_queue.get_nowait()["quiz_id"]
    block = load_task_result(tmp_path, "root-task")["owner_quiz"][quiz_id]
    assert "max_wait_minutes" not in block or block["max_wait_minutes"] in (None, 0)


def test_escalate_zero_bound_waits_unbounded_and_a_huge_bound_is_lowered(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_TASK_ABS_CEILING_SEC", "21600")  # 360 minutes
    registry, ctx = _registry(tmp_path)
    unbounded = registry.execute_result("escalate", {
        "question": "Continue?", "options": ["Yes", "No"], "wait_for_answer": True, "max_wait_minutes": 0})
    assert unbounded.status == "ok" and "the task waits after this tool batch" in unbounded.text
    assert "max_wait_minutes=0 ignored: 0 means no bound" in unbounded.text
    assert not getattr(ctx, "_owner_wait_max_minutes", None)

    lowered = registry.execute_result("escalate", {
        "question": "Continue?", "options": ["Yes", "No"], "wait_for_answer": True, "max_wait_minutes": 100000})
    assert lowered.status == "ok" and "waits up to 360 minutes" in lowered.text
    assert "max_wait_minutes=100000 lowered to 360" in lowered.text
    assert ctx._owner_wait_max_minutes == 360


def test_escalate_genuine_argument_mistake_is_one_typed_refusal(tmp_path):
    registry, ctx = _registry(tmp_path)
    result = registry.execute_result("escalate", {
        "question": "Continue?", "options": ["Yes", "No"], "wait_for_answer": True, "max_wait_minutes": -3})
    assert result.status == "error" and result.code == "TOOL_ARG_ERROR"
    assert result.text.startswith("⚠️ QUIZ_WAIT_BOUND_INVALID: max_wait_minutes=-3 ")
    assert result.text.endswith("The quiz was not sent.") and ctx.event_queue.empty()
