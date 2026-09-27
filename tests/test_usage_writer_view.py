"""Real-writer work bounds, generation races and an independent raw-money oracle."""
from __future__ import annotations

import contextlib
import decimal
import json
import os
from decimal import Decimal

import pytest

from ouroboros import _usage_rows_memo as memo
from ouroboros import usage_accounting as ua
from ouroboros import usage_ledger as ledger


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "1000000")
    ua.ensure_legacy_imported(tmp_path)
    yield tmp_path
    with memo._LEDGER_READ_CACHE_LOCK:
        memo._LEDGER_READ_CACHE.pop(str(tmp_path.resolve()), None)


def request(root, **values):
    return ua.AttemptRequest(**{"drive_root": root, "model": "stub", "provider": "local",
                                "task_id": "child", "root_task_id": "dominant",
                                "reservation_usd": 1.0, **values})


def seed(root, count, *, cost="0.0000015"):
    rows = []
    for index in range(count):
        base = dict(attempt_id=f"seed-{index}", kind="attempt", root_task_id="dominant",
                    provider="local", model="stub", reservation_upper_bound_usd="0.25",
                    pricing_known=True, ts="2020-01-01T00:00:00Z")
        for state in ("reserved", "dispatched", "settled"):
            row = {**base, "state": state, "seq": len(rows) + 1}
            if state == "settled":
                row.update(cost_usd=cost, cost_final=True)
            rows.append(row)
    (root / ua.LEDGER_REL).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def raw_oracle(root):
    """Independent precision-200 fold of RAW literals, never the cache renderer."""
    finals = {}
    for line in (root / ua.LEDGER_REL).read_text().splitlines():
        row = json.loads(line, parse_float=Decimal)
        finals[row["attempt_id"]] = row
    totals = [Decimal(0)] * 5
    with decimal.localcontext() as context:
        context.prec = 200
        for row in finals.values():
            if row.get("kind") in {"usage_baseline", "legacy_metadata"}:
                continue
            bound = Decimal(str(row.get("reservation_upper_bound_usd") or 0))
            cost = row.get("cost_usd")
            if row["state"] == "settled" and cost is not None:
                cost = Decimal(str(cost))
                totals[0] += cost
                totals[1 if row.get("cost_final") else 2] += cost
            elif row["state"] == "reserved":
                totals[3] += bound
            elif row["state"] in {"dispatched", "unresolved", "settled"}:
                totals[4] += bound
    return tuple(totals)


@pytest.mark.parametrize("prefix", [2, 2500])
def test_real_warm_writers_never_walk_history_or_one_large_root(root, monkeypatch, prefix):
    seed(root, prefix)
    old = ua.reserve_attempt(request(root))
    ua.mark_dispatched(old)
    ua.mark_unresolved(old, "late provider")
    ua.record_subscription_session("retained", drive_root=root, route="subscription", spend_usd=None)
    validation = []
    validate = ledger._validate_records

    def sparse(rows, **kwargs):
        validation.append((len(rows), len(kwargs.get("states", {})), len(kwargs.get("late_receipt_ids", set()))))
        return validate(rows, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("warm monetary consumer walked historical records")

    monkeypatch.setattr(ledger, "_validate_records", sparse)
    for name in ("_final_rows", "_summary", "_ledger_resume_state", "_read_records_locked"):
        monkeypatch.setattr(ua, name, forbidden)
    for _ in range(1400 if prefix == 2 else 4):  # >4096 appends, no periodic refold
        assert ua.execute_physical_attempt(request(root), lambda: {"usage": {}}) == {"usage": {}}
    ua.settle_attempt(old, cost_usd=.003, cost_final=True)  # delayed oldest ID
    ua.record_unmetered_external_dispatch("absent", drive_root=root)
    ua.record_subscription_session("retained", drive_root=root, route="subscription", model="new-observation")
    assert max(max(counts) for counts in validation) <= 1
    with memo._writer_locked(root) as view:
        assert view.cash == raw_oracle(root)
        assert view.finals[old.attempt_id]["settle_reason"] == "late_receipt"


@pytest.mark.parametrize("rewrite", ["replace", "shrink", "same_size"])
def test_generation_reprepare_and_money_admission(root, monkeypatch, rewrite):
    rows = seed(root, 4, cost="1.0000000")
    ua.record_unmetered_external_dispatch("warm", drive_root=root)
    if rewrite == "shrink":
        rows = rows[:3]
    for row in rows:
        if row["state"] == "settled":
            row["cost_usd"] = "9.0000000"
    raw = "".join(json.dumps(row) + "\n" for row in rows)
    path = root / ua.LEDGER_REL
    if rewrite == "same_size":
        original = path.read_text()
        raw = original.replace('"cost_usd": "1.0000000"', '"cost_usd": "9.0000000"')
        assert len(raw) == len(original)
        path.write_text(raw)
    else:
        replacement = root / "replacement"
        replacement.write_text(raw)
        os.replace(replacement, path)
    with pytest.raises(ua.BudgetExceeded):
        ua.reserve_attempt(request(root, global_limit_usd=5))
    with memo._writer_locked(root) as view:
        assert view.cash == raw_oracle(root)


def test_replacement_between_preparation_and_acquisition_never_installs_old_cash(root, monkeypatch):
    seed(root, 2, cost="1")
    prepared = memo._prepare_writer
    calls = []

    def race(root):
        result = prepared(root)
        calls.append(result)
        if len(calls) == 1:
            target = root / "replacement"
            target.write_text((root / ua.LEDGER_REL).read_text().replace('"cost_usd": "1"', '"cost_usd": "9"'))
            os.replace(target, root / ua.LEDGER_REL)
        return result

    monkeypatch.setattr(memo, "_prepare_writer", race)
    with pytest.raises(ua.BudgetExceeded):
        ua.reserve_attempt(request(root, global_limit_usd=3))
    assert len(calls) == 2


def test_older_preparation_cannot_replace_a_newer_installed_view(root, monkeypatch):
    seed(root, 2)
    prepared = memo._prepare_writer
    newer = []

    def race(root):
        old = prepared(root)
        fresh = prepared(root)
        memo._ledger_cache_put(str(root.resolve()), fresh)
        newer.append(fresh)
        return old

    monkeypatch.setattr(memo, "_prepare_writer", race)
    ua.reserve_attempt(request(root))
    assert memo._LEDGER_READ_CACHE[str(root.resolve())] is newer[0]


def test_cold_parse_and_actual_in_reserve_compaction_prepare_outside_lock(root, monkeypatch):
    from ouroboros import usage_compaction as compaction

    seed(root, 10)
    locked = False
    original_lock = ua._locked
    original_prepare = memo._prepare_writer
    calls = []

    @contextlib.contextmanager
    def tracked_lock(*args, **kwargs):
        nonlocal locked
        with original_lock(*args, **kwargs) as beat:
            locked = True
            try:
                yield beat
            finally:
                locked = False

    def prepare(root):
        assert not locked
        calls.append(True)
        return original_prepare(root)

    receipts = []

    def compact_once(root, *, heartbeat):
        if not receipts:
            receipts.append(compaction.compact_usage_ledger_locked(root, heartbeat=heartbeat))

    monkeypatch.setattr(ua, "_locked", tracked_lock)
    monkeypatch.setattr(memo, "_prepare_writer", prepare)
    monkeypatch.setattr(compaction, "maybe_compact_usage_ledger_locked", compact_once)
    ua.reserve_attempt(request(root))
    assert receipts[0] is not None
    assert len(calls) == 2
    assert json.loads((root / ua.LEDGER_REL).read_text().splitlines()[0])["kind"] == "usage_baseline"


def test_invalid_second_row_cannot_advance_view_or_consume_late_receipt(root):
    held = ua.reserve_attempt(request(root))
    ua.mark_dispatched(held)
    ua.mark_unresolved(held, "unknown")
    before = (root / ua.LEDGER_REL).read_bytes()
    with memo._writer_locked(root) as view:
        resume = view.resume
        row = {**view.finals[held.attempt_id], "state": "settled", "cost_usd": .1,
               "cost_final": True, "settle_reason": "late_receipt"}
        with pytest.raises(ua.UsageLedgerCorrupt):
            view.append(root, [row, row])
        assert held.attempt_id in resume.late_receipt_ids
    assert (root / ua.LEDGER_REL).read_bytes() == before
    ua.settle_attempt(held, cost_usd=.1, cost_final=True)
    with pytest.raises(ua.UsageAccountingError):
        ua.settle_attempt(held, cost_usd=.2, cost_final=True)


def test_fsync_uncertainty_discards_all_derived_state(root, monkeypatch):
    seed(root, 3)
    actual = ledger._append_bytes_fsync

    def uncertain(path, payload):
        actual(path, payload)
        raise OSError("fsync outcome uncertain")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_append_bytes_fsync", uncertain)
        with pytest.raises(OSError):
            ua.reserve_attempt(request(root))
    assert str(root.resolve()) not in memo._LEDGER_READ_CACHE
    with memo._writer_locked(root) as view:
        assert view.cash == raw_oracle(root)
        assert view.summary()["reserved_usd"] == 1


def test_public_resume_remains_a_retained_snapshot(root):
    held = ua.reserve_attempt(request(root))
    with ua._locked(root):
        old = ua._ledger_resume_state(root, ua._read_records_locked(root))
    ua.mark_dispatched(held)
    with ua._locked(root):
        _, newer = ua._read_new_records_locked(root, old)
    assert old.states[held.attempt_id] == "reserved"
    assert newer.states[held.attempt_id] == "dispatched"


def test_raw_literal_and_ambient_precision_match_independent_oracle(root):
    rows = seed(root, 2)
    rows[2]["cost_usd"] = "1.9542475"
    rows[5]["cost_usd"] = "0.513341"
    # Raw JSON numeric precision would be lost by ordinary float decoding.
    raw = "".join(json.dumps(row) + "\n" for row in rows).replace('"1.9542475"', '1.95424750000000000000000000000000000000000000000001')
    (root / ua.LEDGER_REL).write_text(raw)
    with decimal.localcontext() as context:
        context.prec = 4
        with memo._writer_locked(root) as view:
            assert view.cash == raw_oracle(root)
            assert view.summary()["settled_usd"] == 2.467589
            assert view.summary() == {key: ua._summary(list(view.finals.values()))[key] for key in view.summary()}
    with pytest.raises(ua.BudgetExceeded):
        ua.reserve_attempt(request(root, global_limit_usd=2.467589, reservation_usd=0))


@pytest.mark.parametrize("tail", [b'{"seq":', b'{"seq":999,"state":"settled"}\n'])
def test_torn_tail_repair_keeps_cash_and_future_writer(root, tail):
    seed(root, 4)
    with open(root / ua.LEDGER_REL, "ab") as handle:
        handle.write(tail)
    ua.execute_physical_attempt(request(root), lambda: {"usage": {}})
    assert (root / ua.QUARANTINE_REL).exists()
    with memo._writer_locked(root) as view:
        assert view.cash == raw_oracle(root)


def test_baseline_residue_skips_empty_pass_but_age_crossing_still_compacts(root, monkeypatch):
    from ouroboros import config, usage_compaction as compact
    seed(root, 10)
    monkeypatch.setattr(config, "USAGE_LEDGER_COMPACT_BYTES", 1)
    monkeypatch.setattr(config, "USAGE_LEDGER_COMPACT_RETRY_GROWTH_BYTES", 1)
    with ledger._locked(root) as beat:
        assert compact.compact_usage_ledger_locked(root, heartbeat=beat)
    compact._COMPACT_ATTEMPTS.clear()
    calls = []
    original = compact.maybe_compact_usage_ledger_locked
    def tracked(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(compact, "maybe_compact_usage_ledger_locked", tracked)
    for _ in range(10):  # exceed the existing durable source-size growth floor
        held = ua.reserve_attempt(request(root))
        ua.mark_dispatched(held)
        ua.settle_attempt(held, {}, cost_usd=.25, cost_final=True)
    assert not calls  # baseline groups alone never justify a whole pass
    clock = compact._fold_clock()
    monkeypatch.setattr(compact, "_fold_clock", lambda: clock + compact.USAGE_LEDGER_FOLD_MIN_AGE_SEC + 1)
    before = (root / ua.LEDGER_REL).stat().st_ino
    ua.reserve_attempt(request(root))
    assert calls == [1]
    assert (root / ua.LEDGER_REL).stat().st_ino != before


def test_exact_near_cap_admission_keeps_six_places_and_nanodollar_allowance():
    from ouroboros._usage_money import exceeds_limit
    with decimal.localcontext() as context:
        context.prec = 3
        def cash(value):
            return (Decimal(value), Decimal(value), Decimal(0), Decimal(0), Decimal(0))
        # Unrounded 2.4675885 exceeds this cap, but admission deliberately uses
        # the existing rounded bucket contract. This is not an unrounded policy.
        assert not exceeds_limit(cash("2.4675885"), "2.467588002", "0")
        assert exceeds_limit(cash("2.4675885"), "2.467588001", "0")
        assert not exceeds_limit(cash("2.4675885"), "2.467587999", dispatch=True)
        assert exceeds_limit(cash("2.4675885"), "2.467587998", dispatch=True)
        assert exceeds_limit(cash("10000000000000000000000000000.000002"),
                             "10000000000000000000000000000.000001", dispatch=True)


def test_preparation_changed_retries_outside_lock_and_numeric_metadata_keeps_shape(root, monkeypatch):
    seed(root, 1)
    original = memo._prepare_writer
    prepared, locked = [], []
    original_lock = ua._locked
    @contextlib.contextmanager
    def lock(*args, **kwargs):
        locked.append(1)
        with original_lock(*args, **kwargs) as beat:
            yield beat
    def prepare(path):
        prepared.append(1)
        return memo._PREPARATION_CHANGED if len(prepared) == 1 else original(path)
    monkeypatch.setattr(memo, "_prepare_writer", prepare)
    monkeypatch.setattr(ua, "_locked", lock)
    with memo._writer_locked(root) as view:
        assert len(view.records) == 3
    assert len(prepared) == 2 and len(locked) == 1
    from ouroboros._usage_money import LiteralFloat, durable_literals
    raw = LiteralFloat("0.123456789012345678901234567890")
    stored = durable_literals({"cost_usd": raw, "foreign_metadata": {"ratio": raw, "status": "kept"}})
    assert stored["cost_usd"] == raw.literal
    assert isinstance(stored["foreign_metadata"]["ratio"], float)
    assert stored["foreign_metadata"]["status"] == "kept"


@pytest.mark.parametrize("replacement", [False, True])
def test_strict_read_consumers_prepare_once_outside_money_lock(root, monkeypatch, replacement):
    from ouroboros import _usage_rows
    from ouroboros.model_send_seal import reconcile_model_send_seals
    from ouroboros.server_maintenance import _reconcile_abandoned_usage
    from ouroboros.delegate_custody_usage import observe_failed_review_send

    seed(root, 2500)
    if replacement:
        ua.usage_projection(root)
        target = root / "replacement"
        target.write_bytes((root / ua.LEDGER_REL).read_bytes())
        os.replace(target, root / ua.LEDGER_REL)
    locked, preparations = [], []
    real_lock, prepare, summary = ua._locked, memo._prepare_writer, _usage_rows._summary

    @contextlib.contextmanager
    def lock(*args, **kwargs):
        with real_lock(*args, **kwargs) as beat:
            locked.append(True)
            try:
                yield beat
            finally:
                locked.pop()

    def outside_prepare(path):
        assert not locked, "cold history preparation held the money lock"
        preparations.append(True)
        return prepare(path)

    def outside_summary(rows):
        assert not locked, "full projection fold held the money lock"
        return summary(rows)

    def forbidden(*args, **kwargs):
        pytest.fail("strict read recreated a full locked history parse")

    monkeypatch.setattr(ua, "_locked", lock)
    monkeypatch.setattr(memo, "_prepare_writer", outside_prepare)
    monkeypatch.setattr(ua, "_read_records_locked", forbidden)
    monkeypatch.setattr(ledger, "_read_records_locked", forbidden)
    monkeypatch.setattr(_usage_rows, "_summary", outside_summary)
    assert ua.usage_projection(root, root_task_id="dominant")["attempt_counts"] == {"settled": 2500}
    assert ua.refresh_root_accounting(root, "dominant", strict=True) is not None
    assert ua.skill_review_usage(root, review_skill="absent", review_wave_id="absent")["attempt_ids"] == []
    assert len(ua.read_usage_records(root)) == 7500
    # Exercise the real audit/maintenance consumers, not just their helper.
    assert reconcile_model_send_seals(root)["status"] == "completed"
    _reconcile_abandoned_usage(root)
    observe_failed_review_send(lambda row: None, RuntimeError("no dispatched ids"))
    held = ua.reserve_attempt(request(root))
    ua.mark_dispatched(held)
    ua.settle_attempt(held, {}, cost_usd=.01, cost_final=True)
    assert len(preparations) == 1, "strict readers and writers must share one prepared source"


def test_snapshots_and_transition_results_cannot_mutate_writer_state(root):
    held = ua.reserve_attempt(request(root))
    ua.mark_dispatched(held)
    settled = ua._transition(held, "settled", prompt_tokens=2, cost_usd=.01, cost_final=True)
    before = ua.read_usage_records(root)
    # Include future nested metadata, whose authority and shape stay untouched.
    with memo._writer_locked(root) as view:
        view.append(root, [{"attempt_id": "future", "kind": "external_unmetered", "state": "settled",
                           "cost_usd": None, "future_metadata": {"values": [1, 2]},
                           "review_skill": "test", "review_wave_id": "wave", "review_slot_id": "s",
                           "cost_evidence": {"knowledge": "unknown", "future": [1, 2]}}])
    review = ua.skill_review_usage(root, review_skill="test", review_wave_id="wave")
    review["attempts"][0]["cost_evidence"]["future"].append(999)
    assert ua.skill_review_usage(root, review_skill="test", review_wave_id="wave")["attempts"][0]["cost_evidence"]["future"] == [1, 2]
    final = ua.read_usage_records(root, final_only=True)
    final[-1]["future_metadata"]["values"].append(999)
    settled["cost_usd"] = 999
    with ua._locked(root):
        public = ua._read_records_locked_cached(root)
    public[-1]["future_metadata"]["values"].clear()
    with memo._writer_locked(root) as view:
        assert view.finals[held.attempt_id]["cost_usd"] == .01
        assert view.finals["future"]["future_metadata"] == {"values": [1, 2]}
        assert view.finals["future"]["cost_evidence"]["future"] == [1, 2]
    assert len(before) == 3 and before[-1]["cost_usd"] == .01


def test_captured_display_generation_is_stable_during_writer_and_rebuild(root):
    held = ua.reserve_attempt(request(root))
    rows, _, old, generation = ua._memoized_final_rows(root)
    ua.mark_dispatched(held)
    assert rows[0]["state"] == "reserved"
    assert old.resume.states == {} and old.resume.late_receipt_ids == set()
    newer_rows, _, newer, _ = ua._memoized_final_rows(root)
    assert newer is not old and newer_rows[0]["state"] == "dispatched"
    assert old.generation == generation
    # An equal file fingerprint does not equate private rebuilt state objects.
    with memo._LEDGER_READ_CACHE_LOCK:
        memo._LEDGER_READ_CACHE.pop(str(root.resolve()))
    _, _, rebuilt, _ = ua._memoized_final_rows(root)
    assert rebuilt is not newer


@pytest.mark.parametrize("reason", ["platform_refused", "permission_denied", "identity_unreadable"])
def test_display_snapshot_cannot_mask_actual_platform_refusal(root, monkeypatch, reason):
    ua.usage_projection(root)
    @contextlib.contextmanager
    def refuse(*args, **kwargs):
        raise ledger.UsageLockUnavailable("real refusal", reason=reason)
        yield
    monkeypatch.setattr(ua, "_locked", refuse)
    with pytest.raises(ledger.UsageLockUnavailable) as error:
        ua.usage_projection(root, allow_stale=True)
    assert error.value.reason == reason


def test_strict_reader_corruption_propagates_instead_of_serving_memo(root):
    seed(root, 2)
    ua.usage_projection(root)
    path = root / ua.LEDGER_REL
    path.write_text(path.read_text().replace('"state": "reserved"', '"state": "impossible"', 1))
    with pytest.raises(ledger.UsageLedgerCorrupt):
        ua.usage_projection(root, root_task_id="dominant")


def test_fold_heap_supersession_and_replay_scope_agree(root, monkeypatch):
    from ouroboros import usage_compaction as compact
    from ouroboros._usage_money import monetary_scope_key
    seed(root, 2)
    held = ua.reserve_attempt(request(root, root_task_id=""))
    ua.mark_dispatched(held)
    ua.mark_unresolved(held, "unknown")
    ua.terminalize_abandoned_attempt(held, reason="closed")
    ua.settle_attempt(held, {}, cost_usd=0, cost_final=True)
    with memo._writer_locked(root) as view:
        assert all(monetary_scope_key(row) == str(row.get("root_task_id") or "") for row in view.finals.values())
        for value in (None, "", "dominant"):
            selected = list(view.finals.values()) if value is None else [
                row for row in view.finals.values() if monetary_scope_key(row) == value]
            assert view.summary(value) == {key: ua._summary(selected)[key] for key in view.summary(value)}
        for now in (0, compact._fold_clock(), compact._fold_clock() + compact.USAGE_LEDGER_FOLD_MIN_AGE_SEC + 1):
            monkeypatch.setattr(compact, "_fold_clock", lambda: now)
            expected = any(at is not None and at <= now for row in view.finals.values()
                           for at in [compact.fold_eligible_at(row)])
            assert view.has_foldable_attempt() == expected
