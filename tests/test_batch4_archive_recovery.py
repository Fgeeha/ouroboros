"""Verified historical binding recovery through real admission and compaction."""
from __future__ import annotations

import contextlib
import time
from types import SimpleNamespace

import pytest

from ouroboros import _usage_rows_memo as memo
from ouroboros import usage_accounting as ua
from ouroboros import usage_compaction as compact
from ouroboros.usage_admission import ledger_billing_binding, original_group_limit
from tests.test_batch4_compaction_authority import (
    UNCAPPED, _cold, _compact, _legacy_attempt, _rows, _sums, _strip_carriage,
)
from tests.test_billing_group import _spend, data_root as data_root


def _old_block(root, monkeypatch, cap=20.0, rid="P"):
    _legacy_attempt(root, "first", model="z", cap=cap, rid=rid)
    _legacy_attempt(root, "later", model="a", cap=100.0, rid=rid)
    _compact(root, monkeypatch)
    _strip_carriage(root)


@pytest.mark.parametrize("cap", [20.0, None])
def test_verified_recovery_commits_carriage_and_cold_reads(data_root, monkeypatch, cap):
    _old_block(data_root, monkeypatch, cap)
    sums = _sums(data_root, "P")
    assert original_group_limit(data_root, "P") == {"limit_usd": cap, "source": "ledger_first_row"}
    for i in range(8):
        _legacy_attempt(data_root, f"new-{i}", model="m", cap=7.0, rid="N")
    assert _compact(data_root, monkeypatch)
    stored = next(row for row in _rows(data_root) if row.get("root_task_id") == "P"
                  and "original_root_binding" in row)
    assert stored["original_root_binding"]["root_limit_usd"] == cap
    assert stored["original_group_binding"]["root_limit_usd"] == cap
    _cold(data_root)
    monkeypatch.setattr(compact, "_load_segment", lambda *_: pytest.fail("carried authority needs no archive replay"))
    assert ledger_billing_binding(data_root, "P")["billing_group_limit_usd"] == cap
    assert _sums(data_root, "P")[1:] == sums[1:]


def test_faithfully_propagated_old_unknown_recovers_from_ancestry(data_root, monkeypatch):
    _old_block(data_root, monkeypatch)
    for i in range(8):
        _legacy_attempt(data_root, f"new-{i}", model="m", cap=7.0, rid="N")
    # No preparation: the explicit compactor must not read archives under money.
    _cold(data_root)
    _compact(data_root, monkeypatch)
    assert next(row for row in _rows(data_root) if row.get("root_task_id") == "P")["original_root_binding"] == "unknown"
    _cold(data_root)
    assert original_group_limit(data_root, "P")["limit_usd"] == 20.0


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_warm_verified_binding_does_not_outlive_lost_archive(data_root, monkeypatch, damage):
    _old_block(data_root, monkeypatch)
    assert original_group_limit(data_root, "P")["limit_usd"] == 20.0
    segment = data_root / _rows(data_root)[0]["archive_rel"]
    if damage == "missing":
        segment.unlink()
    else:
        segment.write_bytes(segment.read_bytes() + b"corrupt")
    assert original_group_limit(data_root, "P")["source"] == "ledger_binding_unknown"
    with pytest.raises(ValueError, match="original ledger binding unknown"):
        ledger_billing_binding(data_root, "P")
    # Lost historical authority does not freeze independently authorized work.
    _spend(data_root, ua.UsageScope(drive_root=data_root, task_id="fresh", root_task_id="fresh",
                                  root_limit_usd=5.0), .1)
    assert ledger_billing_binding(data_root, "fresh")["billing_group_limit_usd"] == 5.0
    from ouroboros.gateway.extensions import _ApiReviewCtx
    from tests.test_batch4_service_review_scope import _coordinator_reservation
    service = _coordinator_reservation(_ApiReviewCtx(data_root, data_root.parent), monkeypatch)
    assert "attempt" in service and service["scope"].non_task_operation


def test_no_recorded_original_cap_cannot_be_invented_from_later_rows(data_root, monkeypatch):
    for index in range(4):
        _legacy_attempt(data_root, f"imported-{index}", model="m", cap=UNCAPPED)
    _compact(data_root, monkeypatch)
    _strip_carriage(data_root)
    _legacy_attempt(data_root, "later", model="m", cap=1000.0)
    assert original_group_limit(data_root, "P")["source"] == "ledger_binding_unknown"


def test_recovery_is_off_lock_and_warm_reservations_reuse_it(data_root, monkeypatch):
    _old_block(data_root, monkeypatch)
    locked = False
    calls = []
    original_lock, original_load = ua._locked, compact._load_segment

    @contextlib.contextmanager
    def lock(*args, **kwargs):
        nonlocal locked
        with original_lock(*args, **kwargs) as beat:
            locked = True
            try:
                yield beat
            finally:
                locked = False

    def load(*args):
        assert not locked, "archive parse cannot hold the money lock"
        calls.append(True)
        return original_load(*args)

    monkeypatch.setattr(ua, "_locked", lock)
    monkeypatch.setattr(compact, "_load_segment", load)
    fields = ledger_billing_binding(data_root, "P")
    assert len(calls) == 1
    for i in range(3):
        _spend(data_root, ua.UsageScope(drive_root=data_root, task_id="P-late", root_task_id="P", **fields), .1)
        _legacy_attempt(data_root, f"another-{i}", model="m", cap=7.0, rid="N")
        assert ledger_billing_binding(data_root, "P") == fields
    assert len(calls) == 1


def test_recovery_preparation_revalidates_a_compaction_generation(data_root, monkeypatch):
    _old_block(data_root, monkeypatch)
    for i in range(8):
        _legacy_attempt(data_root, f"new-{i}", model="m", cap=7.0, rid="N")
    original = memo._prepare_writer
    prepared = []

    def prepare(root):
        result = original(root)
        prepared.append(result)
        if len(prepared) == 1:
            _compact(root, monkeypatch)  # atomic replacement after this preparation
        return result

    monkeypatch.setattr(memo, "_prepare_writer", prepare)
    assert ledger_billing_binding(data_root, "P")["billing_group_limit_usd"] == 20.0
    assert len(prepared) == 2
    with memo._writer_locked(data_root) as view:
        assert view is prepared[1]


@pytest.mark.serial
def test_continue_descendant_and_original_late_review_share_recovered_group(data_root, monkeypatch):
    from ouroboros.task_results import load_task_result
    from supervisor.continuation_admission import admit_continuation
    from tests._budget_pause_exact_helpers import _install_queue
    from tests.test_owner_continue import _interrupted, NONCE

    _queue, _state, workers = _install_queue(data_root, monkeypatch)
    _legacy_attempt(data_root, "first", model="z", cap=20.0)
    _interrupted(data_root, "P", reason_code="task_exception")
    _legacy_attempt(data_root, "later", model="a", cap=100.0)
    _compact(data_root, monkeypatch)
    _strip_carriage(data_root)
    accepted = admit_continuation("P", action_nonce=NONCE)
    assert accepted["ok"] and not accepted["held"]
    successor = workers.PENDING[0]
    binding = successor["metadata"]["continuation"]
    assert binding["billing_group_limit_usd"] == 20.0
    fields = {key: value for key, value in binding.items() if key.startswith("billing_group_")}
    _spend(data_root, ua.UsageScope(drive_root=data_root, task_id="helper", root_task_id=successor["id"], parent_task_id=successor["id"], **fields), 18.5)
    from ouroboros.review_substrate import ReviewCoordinator, ReviewRequest, ReviewSlot
    from ouroboros import review_custody
    ctx = SimpleNamespace(task_id="P", task_metadata={"root_task_id": "P"}, drive_root=data_root)
    coordinator = ReviewCoordinator(llm=SimpleNamespace(), drive_root=data_root, usage_ctx=ctx)
    captured = []
    class Captured(Exception):
        pass
    def paid(*_a, **_kw):
        with pytest.raises(ua.BudgetExceeded):
            ua.reserve_attempt(ua.AttemptRequest(model="fixture", provider="fixture", reservation_usd=1.0))
        captured.append(ua.current_usage_scope())
        raise Captured()
    monkeypatch.setattr(coordinator, "_run_slot", paid)
    monkeypatch.setattr(review_custody, "run_custodied_review_slots",
                        lambda **kw: kw["run_slot"](kw["slots"][0], "retained-operation", {}, time.monotonic()+60, None))
    with pytest.raises(Captured):
        coordinator.run(ReviewRequest(surface="task_acceptance", goal="retained answer", task_id="P"),
                        [ReviewSlot(slot_id="slot", model="fixture")])
    assert captured[0].root_task_id == "P" and not captured[0].non_task_operation
    assert load_task_result(data_root, "P")["continued_by"]["successor_task_id"] == successor["id"]


def test_cold_display_never_reads_binding_archives(data_root, monkeypatch):
    _old_block(data_root, monkeypatch)
    original = compact._load_segment
    monkeypatch.setattr(compact, "_load_segment", lambda *_: pytest.fail("display archive replay"))
    assert ua.usage_projection(data_root, root_task_id="P")["accounted_usd"] == 1.0
    monkeypatch.setattr(compact, "_load_segment", original)
    assert ledger_billing_binding(data_root, "P")["billing_group_limit_usd"] == 20.0


def test_torn_tail_quarantine_does_not_skip_off_lock_recovery(data_root, monkeypatch):
    _old_block(data_root, monkeypatch)
    with (data_root / ua.LEDGER_REL).open("ab") as handle:
        handle.write(b"{torn")
    assert ledger_billing_binding(data_root, "P")["billing_group_limit_usd"] == 20.0


@pytest.mark.serial
def test_registered_historical_acceptance_uses_recovered_group_once(late, tmp_path, monkeypatch):
    from tests.test_acceptance_late_consumers import delivered
    from tests.test_acceptance_history import _caller, _request, _source
    from tests.test_review_operation_lifetime import until
    from ouroboros import review_operation

    f = delivered(tmp_path, monkeypatch)
    _old_block(f.root, monkeypatch, rid=f.accounting)
    ctx = _caller(f)
    outcome = _request(f, ctx, _source(ctx, text="Review this delivered answer."))
    assert outcome["status"] in {"pending", "announced", "published", "settled"}, outcome
    until(lambda: not review_operation._LIVE)
    assert len(late.calls) == 3
    assert all(scope.root_task_id == f.accounting and scope.billing_group_id == f.accounting
               and scope.billing_group_limit_usd == 20.0 and scope.root_limit_usd == 4.0
               for scope, _ in late.calls)
    assert ua.usage_projection(f.root, billing_group_id=f.accounting)["accounted_usd"] == pytest.approx(1.03)
    repeated = _request(f, ctx, _source(ctx, text="Review this delivered answer."))
    assert repeated["reason"] == "existing_paid_operation" and len(late.calls) == 3


from tests.test_acceptance_late_consumers import late as late  # noqa: E402
from tests.test_review_operation_collection import fresh_sends as fresh_sends  # noqa: E402


def test_recovery_retains_original_source_and_revision_fields(data_root, monkeypatch):
    original = {"billing_group_id": "P", "billing_group_limit_usd": 20.0,
                "billing_group_limit_source": "initial_task_admission", "billing_group_limit_revision": "original-r1"}
    for index in range(4):
        _legacy_attempt(data_root, f"seed-{index}", model="m", cap=20.0, **original)
    _compact(data_root, monkeypatch)
    _strip_carriage(data_root)
    assert ledger_billing_binding(data_root, "P") == original
    for index in range(8):
        _legacy_attempt(data_root, f"tail-{index}", model="m", cap=100.0)
    _compact(data_root, monkeypatch)
    _cold(data_root)
    assert ledger_billing_binding(data_root, "P") == original
