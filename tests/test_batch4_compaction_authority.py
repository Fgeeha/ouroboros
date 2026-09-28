"""Batch4: a root's ORIGINAL cap authority survives real usage-ledger compaction.

Compaction folds closed attempts into aggregates sorted by attribution, with a
minimum ``root_limit_usd``. Those aggregates carry money exactly but not which
cap came first. Every scenario here compacts through the production pass,
reloads the writer view warm and cold, and reads the binding through the real
consumers: ``original_group_limit``, ``ledger_billing_binding``, the durable
admission resolver and the Continue binding.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from types import SimpleNamespace

import pytest

from ouroboros import _usage_rows_memo as memo
from ouroboros import usage_accounting as ua
from ouroboros import usage_compaction as compact
from ouroboros.usage_admission import (
    effective_billing_fields,
    ledger_billing_binding,
    original_group_limit,
    task_billing_fields,
    task_money_snapshot,
)
from supervisor.continuation_admission import _billing_group
from tests.test_billing_group import _scope, _spend
from tests.test_billing_group import data_root as data_root

OLD_TS = "2026-01-01T00:00:00+00:00"


def _legacy_attempt(root, attempt_id, *, model, cap, rid="P", **extra):
    """A pre-group attempt chain: no ``billing_group_*`` fields, only its root cap."""
    base = {"kind": "attempt", "attempt_id": attempt_id, "model": model, "provider": "openai", "task_id": rid,
            "root_task_id": rid, "parent_task_id": "", "category": "task", "source": "old", "ts": OLD_TS,
            "reservation_upper_bound_usd": 1.0, "pricing_known": True, "root_limit_usd": cap, **extra}
    with ua._locked(root):
        records = ua._read_records_locked(root)
        ua._append_rows_locked(root, records, [{**base, "state": "reserved"}, {**base, "state": "dispatched"},
                                               {**base, "state": "settled", "cost_usd": .5, "cost_final": True}])


def _compact(root, monkeypatch):
    monkeypatch.setattr(compact, "_fold_clock", lambda: time.time() + 1_000_000)
    with ua._locked(root) as lock:
        receipt = compact.compact_usage_ledger_locked(root, heartbeat=lock)
    assert receipt is not None, "the production pass folded the closed attempts"
    return receipt


def _rows(root):
    return [json.loads(line) for line in (root / ua.LEDGER_REL).read_text().splitlines()]


def _rewrite(root, rows):
    """Replace the live ledger atomically, as the compactor itself does."""
    temp = root / ua.LEDGER_REL.with_name("tampered.jsonl")
    temp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    os.replace(temp, root / ua.LEDGER_REL)


def _cold(root):
    with memo._LEDGER_READ_CACHE_LOCK:
        memo._LEDGER_READ_CACHE.pop(str(root.resolve()), None)


def _sums(root, *ids):
    projections = [ua.usage_projection(root)]
    for rid in ids:
        projections += [ua.usage_projection(root, root_task_id=rid), ua.usage_projection(root, billing_group_id=rid)]
    return [(p["accounted_usd"], p.get("limit_usd"), p["cost_final"], p["non_final_rows"]) for p in projections]


def _authority(root, rid):
    return (original_group_limit(root, rid), ledger_billing_binding(root, rid),
            _billing_group(SimpleNamespace(DRIVE_ROOT=root), rid, {"task_id": rid}),
            task_billing_fields({"id": rid}, rid, None, root))


def test_legacy_first_cap_survives_reordering_across_models_and_epochs(data_root, monkeypatch):
    """First $20 on a later-sorting model, then $100: the aggregate order puts $100 first."""
    from ouroboros.task_results import write_task_result

    root = ua._drive_root(data_root)
    write_task_result(root, "P", "failed", root_task_id="P")  # legacy: no durable billing_group
    _legacy_attempt(root, "first", model="z-model", cap=20.0)
    _legacy_attempt(root, "later", model="a-model", cap=100.0)
    before, original = _authority(root, "P"), _sums(root, "P")
    assert before[0] == {"limit_usd": 20.0, "source": "ledger_first_row"}
    assert before[2]["billing_group_limit_usd"] == before[3]["billing_group_limit_usd"] == 20.0
    for epoch in (1, 2):
        if epoch == 2:  # another root's history grows, then the whole block folds again
            for index in range(4):
                _legacy_attempt(root, f"n-{index}", model="m", cap=5.0, rid="N")
        sums = _sums(root, "P")
        assert sums[1:] == original[1:]
        _compact(root, monkeypatch)
        blocks = [row for row in _rows(root) if row.get("kind") == "usage_baseline_group"]
        assert blocks[0]["model"] == "a-model" and float(blocks[0]["root_limit_usd"]) == 100.0
        for cold in (False, True):
            if cold:
                _cold(root)
            assert _authority(root, "P") == before
            assert _sums(root, "P") == sums
    assert original_group_limit(root, "N") == {"limit_usd": 5.0, "source": "ledger_first_row"}


def test_initial_unlimited_survives_later_finite_caps_in_one_aggregate(data_root, monkeypatch):
    root = ua._drive_root(data_root)
    _legacy_attempt(root, "first", model="m", cap=None)
    _legacy_attempt(root, "later", model="m", cap=20.0)
    _legacy_attempt(root, "last", model="m", cap=100.0)
    before, sums = _authority(root, "P"), _sums(root, "P")
    assert before[0] == {"limit_usd": None, "source": "ledger_first_row"}
    assert before[1]["billing_group_limit_usd"] is None
    _compact(root, monkeypatch)
    (block,) = [row for row in _rows(root) if row.get("kind") == "usage_baseline_group"]
    assert block["folded_attempt_count"] == 3 and block["root_limit_usd"] == "20.0"  # min lost None
    for cold in (False, True):
        if cold:
            _cold(root)
        assert _authority(root, "P") == before
        assert _sums(root, "P") == sums


def _amend(root, rid, cap, identity):
    from ouroboros.task_results import write_task_result

    write_task_result(root, rid, "running", root_task_id=rid, acceptance_root_cap_amendments=[{
        "accounting_root_task_id": rid, "source_identity": identity, "source_ref": f"test:{identity}",
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "new_cap_usd": cap}])


def test_group_original_survives_late_dispatch_amendment_successor_and_foreign_group(data_root, monkeypatch):
    """The only row carrying P's original $20 is a reserved row that compaction removes."""
    root = ua._drive_root(data_root)
    initial = dict(billing_group_limit_source="initial_task_admission", billing_group_limit_revision="r1")
    with ua.usage_scope(_scope(root, "P", "P", group="P", group_limit=20.0, root_limit=20.0, **initial)):
        first = ua.reserve_attempt(ua.AttemptRequest(model="z-model", provider="openai", reservation_usd=1.0))
        _amend(root, "P", 50.0, "amend-1")  # the owner raises P's allowance before the first dispatch
        ua.mark_dispatched(first)
        ua.settle_attempt(first, {"prompt_tokens": 1, "completion_tokens": 1}, cost_usd=.75, cost_final=True)
    _spend(root, _scope(root, "S", "S", group="P", group_limit=20.0, **initial), 1.25)
    _spend(root, _scope(root, "T", "S", group="P", group_limit=20.0, parent_task_id="S", **initial), .5)
    _spend(root, _scope(root, "F", "F", group="F", group_limit=7.0, root_limit=7.0,
                        billing_group_limit_source="initial_task_admission", billing_group_limit_revision="rf"), 2.0)
    authority = {rid: (original_group_limit(root, rid), ledger_billing_binding(root, rid)) for rid in "PSF"}
    assert authority["P"][0] == {"limit_usd": 20.0, "source": "ledger_first_row"}
    assert authority["P"][1]["billing_group_limit_revision"] == "r1"
    assert authority["S"][1]["billing_group_id"] == "P" and authority["F"][0]["limit_usd"] == 7.0
    assert original_group_limit(root, "S")["source"] == "no_attempt_recorded"  # a successor is not a group
    sums = _sums(root, "P", "S", "F")
    assert ua.usage_projection(root, billing_group_id="P")["limit_usd"] == 50.0  # the dynamic amendment
    _compact(root, monkeypatch)
    live = [row for row in _rows(root) if row.get("kind") != "usage_baseline"]
    assert {row.get("billing_group_limit_usd") for row in live if row.get("billing_group_id") == "P"} == {50.0}
    for cold in (False, True):
        if cold:
            _cold(root)
        assert {rid: (original_group_limit(root, rid), ledger_billing_binding(root, rid)) for rid in "PSF"} == authority
        assert original_group_limit(root, "S")["source"] == "no_attempt_recorded"
        assert _sums(root, "P", "S", "F") == sums
        amended = effective_billing_fields(root, "P", ledger_billing_binding(root, "P"))
        assert (amended["billing_group_limit_usd"], amended["billing_group_limit_source"]) == (50.0, "owner_amendment")
    with pytest.raises(ua.BudgetExceeded):  # the group ceiling still binds every member after compaction
        _spend(root, _scope(root, "S", "S", group="P", group_limit=20.0, **initial), 48.0)


def _strip_carriage(root):
    """The shape an older pass wrote: no header stamp, no carried bindings."""
    rows = _rows(root)
    for row in rows:
        for field in ("binding_authority", "original_root_binding", "original_group_binding"):
            row.pop(field, None)
    _rewrite(root, rows)
    _cold(root)


def test_older_block_is_unknown_never_an_invented_allowance(data_root, monkeypatch):
    from ouroboros._usage_rows import LedgerBindingUnknown
    from ouroboros.task_results import write_task_result

    root = ua._drive_root(data_root)
    write_task_result(root, "P", "failed", root_task_id="P")
    write_task_result(root, "R", "failed", root_task_id="R", billing_group={
        "billing_group_id": "R", "billing_group_limit_usd": 8.0,
        "billing_group_limit_source": "initial_task_admission", "billing_group_limit_revision": "r8"})
    _legacy_attempt(root, "first", model="z-model", cap=20.0)
    _legacy_attempt(root, "later", model="a-model", cap=100.0)
    _legacy_attempt(root, "r-first", model="m", cap=None, rid="R")
    _legacy_attempt(root, "r-later", model="m", cap=8.0, rid="R")
    with ua.usage_scope(_scope(root, "Q", "Q", group="Q", group_limit=9.0, root_limit=9.0,
                               billing_group_limit_source="initial_task_admission", billing_group_limit_revision="rq")):
        ua.reserve_attempt(ua.AttemptRequest(model="m", provider="openai", reservation_usd=1.0))
    _compact(root, monkeypatch)
    sums = _sums(root, "P", "Q", "R")
    _strip_carriage(root)
    assert _sums(root, "P", "Q", "R") == sums
    for epoch in range(2):  # a newer pass carries the unknown forward instead of healing it
        assert original_group_limit(root, "P") == {"limit_usd": None, "source": "ledger_binding_unknown"}
        with pytest.raises(LedgerBindingUnknown):
            ledger_billing_binding(root, "P")
        assert task_billing_fields({"id": "P"}, "P", 30.0, root)["billing_group_id"] == "unavailable:P"
        with pytest.raises(ValueError):
            _billing_group(SimpleNamespace(DRIVE_ROOT=root), "P", {"task_id": "P"})
        assert task_money_snapshot(root, {"id": "P"}, "P") is None
        # A durable group binding stays usable; only the root's unknown ORIGINAL cap is refused.
        assert task_money_snapshot(root, {"id": "R"}, "R") is None
        explicit = task_money_snapshot(root, {"id": "R"}, "R", root_limit=8.0)
        assert explicit["root_axis"]["limit_usd"] == 8.0 and explicit["group_axis"]["limit_usd"] == 8.0
        # A root outside the aggregate (its only chain is still open) keeps its exact binding.
        assert original_group_limit(root, "Q") == {"limit_usd": 9.0, "source": "ledger_first_row"}
        assert ledger_billing_binding(root, "Q")["billing_group_limit_revision"] == "rq"
        for index in range(4):
            _legacy_attempt(root, f"n-{epoch}-{index}", model="m", cap=5.0, rid="N")
        _compact(root, monkeypatch)
        _cold(root)
        assert original_group_limit(root, "N") == {"limit_usd": 5.0, "source": "ledger_first_row"}
        assert _sums(root, "Q", "R")[1:] == sums[3:]


@pytest.mark.parametrize("field", ["original_root_binding", "original_group_binding"])
@pytest.mark.parametrize("tamper", [
    ["not", "a", "binding"],
    "unknown",
    {"root_task_id": "P"},
    {"root_task_id": "P", "root_limit_usd": -1},
    {"root_task_id": "P", "root_limit_usd": "Infinity"},
    {"root_task_id": "P", "root_limit_usd": 20.0, "cost_usd": 1},
    {"root_task_id": "F", "root_limit_usd": 1000.0},
])
def test_unreadable_or_foreign_carried_binding_is_unknown(data_root, monkeypatch, field, tamper):
    from ouroboros._usage_rows import LedgerBindingUnknown

    root = ua._drive_root(data_root)
    _legacy_attempt(root, "first", model="z-model", cap=20.0)
    _legacy_attempt(root, "later", model="a-model", cap=100.0)
    _legacy_attempt(root, "foreign", model="m", cap=1000.0, rid="F")
    _compact(root, monkeypatch)
    rows = _rows(root)
    carrier = next(row for row in rows if row.get("root_task_id") == "P" and field in row)
    carrier[field] = tamper
    _rewrite(root, rows)
    _cold(root)
    if field == "original_group_binding":
        assert original_group_limit(root, "P") == {"limit_usd": None, "source": "ledger_binding_unknown"}
        assert ledger_billing_binding(root, "P")["billing_group_limit_usd"] == 20.0
    else:
        assert original_group_limit(root, "P") == {"limit_usd": 20.0, "source": "ledger_first_row"}
        with pytest.raises(LedgerBindingUnknown):
            ledger_billing_binding(root, "P")
    assert original_group_limit(root, "F") == {"limit_usd": 1000.0, "source": "ledger_first_row"}
    assert ledger_billing_binding(root, "F")["billing_group_limit_usd"] == 1000.0
    assert ua.usage_projection(root, billing_group_id="P")["accounted_usd"] == 1.0


def test_group_binding_naming_a_foreign_group_is_never_adopted(data_root, monkeypatch):
    root = ua._drive_root(data_root)
    _legacy_attempt(root, "first", model="z-model", cap=20.0)
    _legacy_attempt(root, "foreign", model="m", cap=1000.0, rid="F")
    _compact(root, monkeypatch)
    rows = _rows(root)
    carrier = next(row for row in rows if row.get("root_task_id") == "P" and "original_group_binding" in row)
    carrier["original_group_binding"] = {"root_task_id": "P", "billing_group_id": "F", "root_limit_usd": 1000.0}
    _rewrite(root, rows)
    _cold(root)
    assert original_group_limit(root, "P") == {"limit_usd": None, "source": "ledger_binding_unknown"}
    assert original_group_limit(root, "F") == {"limit_usd": 1000.0, "source": "ledger_first_row"}


def test_pass_that_cannot_carry_the_original_binding_aborts_byte_identical(data_root, monkeypatch):
    root = ua._drive_root(data_root)
    _legacy_attempt(root, "first", model="z-model", cap=20.0)
    _legacy_attempt(root, "later", model="a-model", cap=100.0)
    before = (root / ua.LEDGER_REL).read_bytes()
    # A pass whose block rows carry under names no reader recognizes cannot commit.
    monkeypatch.setattr(compact, "CARRIED_ROOT_BINDING", "unrecognized_root_binding")
    monkeypatch.setattr(compact, "CARRIED_GROUP_BINDING", "unrecognized_group_binding")
    monkeypatch.setattr(compact, "_fold_clock", lambda: time.time() + 1_000_000)
    with ua._locked(root) as lock:
        assert compact.compact_usage_ledger_locked(root, heartbeat=lock) is None
    assert (root / ua.LEDGER_REL).read_bytes() == before
    events = (root / "logs" / "events.jsonl").read_text()
    assert "original binding authority mismatch" in events
    assert original_group_limit(root, "P") == {"limit_usd": 20.0, "source": "ledger_first_row"}
