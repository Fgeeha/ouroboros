"""#1223 — a round that spends is a round that is measured, tools or not.

Only the TOOL tail ever reached `_check_budget_limits`, so a task that kept
re-answering under acceptance feedback (no tool calls at all) could run past its
cost ceiling untouched. These pin the unified tail: the SAME existing
comparison, none of the tool-only bookkeeping beside it, an unchanged disabled
ceiling, and a ready answer that still buys no extra wrap-up.
"""

from __future__ import annotations

import ast
import pathlib
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

from ouroboros import task_pacing
from ouroboros.contracts.task_contract import normalize_budget_profile
from ouroboros.loop import _RoundLimitContext, _finish_no_tool_round_budget


def _ctx(**overrides):
    values = dict(
        messages=[], llm=MagicMock(), active_model="anthropic/claude-test",
        active_effort="high", max_retries=1, drive_logs=None, task_id="task1",
        round_idx=3, event_queue=queue.Queue(),
        accumulated_usage={"cost": 1.0, "_context_prompt_estimate": 0},
        task_type="task", active_use_local=False, max_rounds=100, llm_trace={},
    )
    values.update(overrides)
    return _RoundLimitContext(**values)


def _ceiling(root_cap):
    return task_pacing.resolve_cost_ceiling(
        None, normalize_budget_profile(None), root_cap_usd=root_cap,
    )


def test_an_unfinished_no_tool_round_takes_the_same_over_ceiling_exit(monkeypatch):
    """Parity with the tool tail: same comparison, same typed reason."""
    from ouroboros import loop as loop_mod

    ctx = _ctx()
    monkeypatch.setattr(loop_mod, "_loop_tree_accounting", lambda **_k: {"accounted_usd": 99.0})
    monkeypatch.setattr(
        loop_mod, "_forced_final_answer",
        lambda ctx_, **kwargs: ("wrapped up", ctx_.accumulated_usage, {"kwargs": kwargs}),
    )
    result = _finish_no_tool_round_budget(ctx, None, _ceiling(50.0))
    assert result is not None
    text, _usage, trace = result
    assert text == "wrapped up"
    assert trace is ctx.llm_trace          # the trace the loop keeps returning
    assert ctx.accumulated_usage["cost_stop_spend_basis"]


def test_it_never_reaches_for_the_tool_only_bookkeeping_or_delivery_arming(monkeypatch):
    """No tools ran, so the metered nanny baseline and the post-tool
    delivery-control arming have nothing to say about this round."""
    from ouroboros import loop as loop_mod

    touched = []
    monkeypatch.setattr(loop_mod, "_loop_tree_accounting", lambda **_k: {"accounted_usd": 99.0})
    monkeypatch.setattr(loop_mod, "_prepare_post_tool_budget_context",
                        lambda *a, **k: touched.append("prepare"))
    monkeypatch.setattr(loop_mod, "_note_nanny_delegate_activity",
                        lambda *a, **k: touched.append("nanny"))
    monkeypatch.setattr(loop_mod, "_arm_delivery_control", lambda *a, **k: touched.append("arm"))
    monkeypatch.setattr(
        loop_mod, "_forced_final_answer",
        lambda ctx_, **kwargs: ("wrapped up", ctx_.accumulated_usage, {}),
    )
    _finish_no_tool_round_budget(_ctx(), None, _ceiling(50.0))
    assert touched == []


def test_below_the_ceiling_and_a_disabled_ceiling_both_continue_unchanged(monkeypatch):
    from ouroboros import loop as loop_mod

    monkeypatch.setattr(loop_mod, "_loop_tree_accounting", lambda **_k: {"accounted_usd": 1.0})
    assert _finish_no_tool_round_budget(_ctx(), None, _ceiling(50.0)) is None
    # An explicitly disabled ceiling is untouched by this unification.
    disabled = task_pacing.resolve_cost_ceiling(
        None, normalize_budget_profile({"cost_hard_stop_pct": 0}), root_cap_usd=50.0,
    )
    monkeypatch.setattr(
        task_pacing, "wrapup_reservation_fits",
        lambda **_k: (_ for _ in ()).throw(AssertionError("armed on a disabled ceiling")),
    )
    assert _finish_no_tool_round_budget(_ctx(), None, disabled) is None
    assert _finish_no_tool_round_budget(_ctx(), None, None) is None


def test_the_current_candidate_survives_the_budget_exit(monkeypatch):
    """A budget stop wraps up the answer the round produced; it does not discard it."""
    from ouroboros import loop as loop_mod

    candidate = SimpleNamespace(full_text="the answer so far")
    ctx = _ctx(delivery_candidate=candidate)
    monkeypatch.setattr(loop_mod, "_loop_tree_accounting", lambda **_k: {"accounted_usd": 99.0})
    monkeypatch.setattr(
        loop_mod, "_forced_final_answer",
        lambda ctx_, **kwargs: ("wrapped up", ctx_.accumulated_usage, {}),
    )
    assert _finish_no_tool_round_budget(ctx, None, _ceiling(50.0)) is not None
    assert ctx.delivery_candidate is candidate


def test_only_the_unfinished_no_tool_branch_arms_the_budget_tail():
    """A READY answer returns from the loop before any budget wrap-up: the flag
    is armed only where the round continues (owner: no extra paid wrap-up for a
    perfectly ordinary final answer)."""
    source = (pathlib.Path(__file__).resolve().parents[1] / "ouroboros" / "loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    armings = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pending_no_tool_budget" for t in node.targets)
        and isinstance(node.value, ast.Constant) and node.value.value is True
    ]
    assert len(armings) == 1, "exactly one place may arm the no-tool budget tail"
    # …and it sits inside `if final_result is None:` — the continuation branch.
    guards = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "final_result"
        and any(isinstance(op, ast.Is) for op in node.test.ops)
        and any(arming in ast.walk(node) for arming in armings)
    ]
    assert guards, "the arming must be guarded by an unfinished (final_result is None) round"
