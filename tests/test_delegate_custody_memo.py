"""The custody row memo answers exactly what a full chain replay answers (#804).

Every test compares the memo-backed readers with the same reader fed a full
``_iter_rows`` pass over the rotated chain, after appends, rotations, torn
tails and the anomalies the fingerprint rule must refuse to advance over.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib

import pytest

from ouroboros import delegate_custody as custody
from ouroboros import delegate_custody_memo as memo
from ouroboros.utils import append_jsonl
from supervisor.state import rotate_jsonl_log_if_needed


def _rotate(root: pathlib.Path) -> None:
    rotate_jsonl_log_if_needed(root, "events.jsonl", "events", max_bytes=1)


def _strip(row):
    return {k: v for k, v in row.items() if k not in ("request", memo.REQUEST_LOCATOR_KEY)}


def _full_rows(root):
    return [_strip(r) for r in custody._iter_rows(custody.event_log_path(root))]


def _as_dicts(state):
    return {run_id: dataclasses.asdict(entry) for run_id, entry in state.items()}


def _assert_equivalent(root: pathlib.Path) -> None:
    full = list(custody._iter_rows(custody.event_log_path(root)))
    assert [_strip(r) for r in custody.custody_rows(root)] == [_strip(r) for r in full]
    assert _as_dicts(custody.replay(root)) == _as_dicts(custody.replay(root, rows=full))
    assert custody.pending_invocations(root) == custody.pending_invocations(root, rows=full)
    for row in full:
        run_id = str(row.get("run_id") or "")
        if run_id:
            assert custody.run_timing(root, run_id) == _timing_from_rows(full, run_id)
        token = str(row.get("invocation_id") or "")
        if token:
            assert custody.invocation_record(root, token) == custody.invocation_record(root, token, rows=full)


def _timing_from_rows(rows, run_id):
    started_ts, max_seconds = "", 0
    for row in rows:
        if row.get("run_id") != run_id or row.get("type") != custody.STARTED:
            continue
        started_ts = started_ts or str(row.get("ts") or "")
        max_seconds = max_seconds or int(row.get("max_seconds") or 0)
    return started_ts, max_seconds


def _started(root, run_id, task_id, shape=None, **extra):
    assert custody.record_started(root, custody.RunCustody(
        run_id=run_id, task_id=task_id, route_id="codex", model="m", ledger_root=str(root), **extra),
        shape=shape)


def _requested(root, token, task_id, request):
    assert custody.emit(root, custody.START_REQUESTED, {
        "invocation_id": token, "task_id": task_id, "route": "codex", "request": request})


def test_memo_matches_full_replay_across_appends_and_rotations(tmp_path):
    root = tmp_path
    assert custody.custody_rows(root) == ()
    _started(root, "run-1", "task-a", shape={"max_seconds": 90})
    _assert_equivalent(root)
    _rotate(root)
    _requested(root, "inv-a", "task-a", {"prompt": "legacy inline body"})
    assert custody.emit(root, custody.SETTLED, {"run_id": "run-1", "task_id": "task-a", "state": "succeeded"})
    _assert_equivalent(root)
    _rotate(root)
    _rotate(root)  # an empty rotation: the touched live file holds nothing
    _started(root, "run-2", "task-b", invocation_id="inv-a")
    append_jsonl(custody.event_log_path(root), {"ts": "t", "type": "llm_usage", "task_id": "task-b"})
    _assert_equivalent(root)
    assert custody.replay(root)["run-1"].settled and not custody.replay(root)["run-2"].settled
    assert custody.run_timing(root, "run-1")[1] == 90
    assert [r["invocation_id"] for r in custody.pending_invocations(root)] == []
    diagnostics = memo.memo_diagnostics(root)
    assert diagnostics["rows"] == 4 and len(diagnostics["segments"]) >= 3


def test_rotation_between_calls_advances_without_refold(tmp_path):
    root = tmp_path
    _started(root, "run-1", "task-a")
    custody.custody_rows(root)
    _started(root, "run-2", "task-a")
    custody.custody_rows(root)
    generation = memo.memo_diagnostics(root)["generation"]
    assert generation == 2
    _rotate(root)
    _started(root, "run-3", "task-a")
    _assert_equivalent(root)
    # An advance bumps the generation; a refold would start a fresh memo at 1.
    assert memo.memo_diagnostics(root)["generation"] == generation + 1
    assert len(memo.memo_diagnostics(root)["segments"]) == 2


def test_torn_live_tail_waits_then_folds_exactly_once(tmp_path):
    root = tmp_path
    _started(root, "run-1", "task-a")
    custody.custody_rows(root)
    row = json.dumps({"ts": "t", "type": custody.SETTLED, "run_id": "run-1", "task_id": "task-a", "state": "failed"})
    path = custody.event_log_path(root)
    with path.open("ab") as handle:
        handle.write(row[:20].encode("utf-8"))
    assert not custody.replay(root)["run-1"].settled
    with path.open("ab") as handle:
        handle.write(row[20:].encode("utf-8") + b"\n")
    assert custody.replay(root)["run-1"].settled
    assert [r["type"] for r in custody.custody_rows(root)].count(custody.SETTLED) == 1
    _assert_equivalent(root)


def test_torn_archive_tail_is_consumed_and_counted(tmp_path):
    root = tmp_path
    _started(root, "run-1", "task-a")
    _rotate(root)
    archive = sorted((root / "archive").glob("events_*.jsonl"))[-1]
    with archive.open("ab") as handle:
        handle.write(b'{"type": "delegate_run_settled", "run_id": "run-1", "task_id": "task-a"')  # never terminated
    _started(root, "run-2", "task-a")
    _assert_equivalent(root)
    assert memo.memo_diagnostics(root)["torn_archive_lines"] == 1
    assert not custody.replay(root)["run-1"].settled
    # The next call does not re-read or re-count the torn bytes.
    custody.custody_rows(root)
    assert memo.memo_diagnostics(root)["torn_archive_lines"] == 1


def test_same_size_rewrite_of_consumed_bytes_refolds(tmp_path):
    root = tmp_path
    _started(root, "run-1", "task-a")
    assert custody.replay(root)["run-1"].task_id == "task-a"
    path = custody.event_log_path(root)
    original = path.read_bytes()
    rewritten = original.replace(b'"task-a"', b'"task-b"')
    assert len(rewritten) == len(original)
    path.write_bytes(rewritten)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert custody.replay(root)["run-1"].task_id == "task-b"
    _assert_equivalent(root)


@pytest.mark.parametrize("anomaly", ["archive_deleted", "archive_inserted_before", "live_truncated"])
def test_chain_anomalies_refold_to_the_full_replay(tmp_path, anomaly):
    root = tmp_path
    _started(root, "run-1", "task-a")
    _rotate(root)
    _started(root, "run-2", "task-a")
    _rotate(root)
    _started(root, "run-3", "task-a")
    _assert_equivalent(root)
    archives = sorted((root / "archive").glob("events_*.jsonl"))
    if anomaly == "archive_deleted":
        archives[0].unlink()
    elif anomaly == "archive_inserted_before":
        early = root / "archive" / "events_20000101T000000.jsonl"
        early.write_text(json.dumps({"ts": "t", "type": custody.STARTED, "run_id": "run-0",
                                     "task_id": "task-z"}) + "\n", encoding="utf-8")
    else:
        custody.event_log_path(root).write_text("", encoding="utf-8")
    _assert_equivalent(root)
    if anomaly == "archive_inserted_before":
        assert "run-0" in custody.replay(root)
    if anomaly == "live_truncated":
        assert "run-3" not in custody.replay(root)


def test_unreadable_archive_directory_bypasses_the_memo(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    root = tmp_path
    _started(root, "run-1", "task-a")
    _rotate(root)
    _started(root, "run-2", "task-a")
    archive_dir = root / "archive"
    archive_dir.chmod(0o000)
    try:
        rows = custody.custody_rows(root)
        lenient = list(custody._iter_rows(custody.event_log_path(root)))
        assert [r["run_id"] for r in rows] == [r["run_id"] for r in lenient] == ["run-2"]
        assert memo.memo_diagnostics(root)["cold"]
    finally:
        archive_dir.chmod(0o755)
    _assert_equivalent(root)
    assert {r["run_id"] for r in custody.custody_rows(root)} == {"run-1", "run-2"}


def test_warm_reads_open_no_archive_segment(tmp_path, monkeypatch):
    root = tmp_path
    _started(root, "run-1", "task-a")
    _rotate(root)
    _started(root, "run-2", "task-a")
    opened = []
    original_open = pathlib.Path.open

    def counting_open(self, *args, **kwargs):
        if self.parent.name == "archive":
            opened.append(self.name)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)
    custody.custody_rows(root)
    assert len(opened) == 1  # the cold fold reads the archive once
    custody.custody_rows(root)
    custody.run_timing(root, "run-1")
    custody.replay(root)
    custody.pending_invocations(root)
    assert len(opened) == 1  # every warm read is served from the memo


def test_replay_returns_independent_copies(tmp_path):
    root = tmp_path
    _started(root, "run-1", "task-a", resource_ref={"root": "skill_payload"})
    first = custody.replay(root)["run-1"]
    first.resource_ref["root"] = "tampered"
    first.verified_source_ranges.append((0, 1))
    first.settled = True
    second = custody.replay(root)["run-1"]
    assert second.resource_ref == {"root": "skill_payload"}
    assert second.verified_source_ranges == [] and not second.settled
    assert second is not first


def test_legacy_inline_request_survives_compaction(tmp_path, monkeypatch):
    from ouroboros import observability

    root = tmp_path
    inline = {"prompt": "legacy inline body", "instructions": "x" * 2000}
    _requested(root, "inv-legacy", "task-a", inline)
    monkeypatch.setattr(observability, "read_blob_ref",
                        lambda *a, **k: pytest.fail("an inline body must not read a blob"))
    rows = custody.custody_rows(root)
    assert "request" not in rows[0] and memo.REQUEST_LOCATOR_KEY in rows[0]
    assert custody.invocation_record(root, "inv-legacy")["request"] == inline
    pending = custody.pending_invocations(root)
    assert pending[0]["request"] == inline
    assert "request_locator" not in pending[0] and "request_ref" not in pending[0]
    _rotate(root)  # the locator follows the row into its archive by inode
    assert custody.invocation_record(root, "inv-legacy")["request"] == inline


def test_reset_forgets_the_memo(tmp_path):
    root = tmp_path
    _started(root, "run-1", "task-a")
    custody.custody_rows(root)
    assert not memo.memo_diagnostics(root)["cold"]
    memo.reset_custody_memo(root)
    assert memo.memo_diagnostics(root)["cold"]
    custody.custody_rows(root)
    memo.reset_custody_memo()
    assert memo.memo_diagnostics(root)["cold"]
