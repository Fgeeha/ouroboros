"""Registration-sweep lifecycle: sharer-aware deferral and discharge.

Split from test_delegated_run_isolation.py (module line cap)."""
from __future__ import annotations

import itertools

from ouroboros import delegate_custody as custody


def test_last_shared_project_sibling_retires_once_in_every_settlement_order(tmp_path):
    class _Gateway:
        def __init__(self):
            self.removals = []

        def remove_project(self, project_id):
            self.removals.append(project_id)

    run_ids = ("run-a", "run-b", "run-c")
    for case, order in enumerate(itertools.permutations(run_ids)):
        root = tmp_path / str(case)
        gateway = _Gateway()
        custody._CUSTODY.clear()
        for index, run_id in enumerate(run_ids):
            custody.record_started(root, custody.RunCustody(
                run_id=run_id,
                task_id=f"task-{run_id}",
                project_id="shared-project",
                project_owned=index == 0,
                ledger_root=str(root),
            ))
            custody.emit(root, custody.LEDGER_RECORDED, {"run_id": run_id})

        for run_id in order:
            row = custody.replay(root)[run_id]
            custody.settle_run(root, gateway, row, {"summary": {"state": "succeeded"}})

        assert gateway.removals == ["shared-project"], order
        replayed = custody.replay(root)
        assert all(not row.project_owned for row in replayed.values()), order
        retired = [
            row for row in custody._iter_rows(custody.event_log_path(root))
            if row.get("type") == custody.PROJECT_RETIRED
        ]
        assert len(retired) == 1, order
    custody._CUSTODY.clear()

def test_registration_sweep_defers_behind_a_live_unowned_sharer(tmp_path):
    """Sharers are ALL runs in a project, owned or not: only the creator
    carries the registration, but the daemon refuses removal while any
    sibling lives - attempting anyway spammed PROJECT_RETIRE_FAILED on
    every sweep tick for the sibling's whole lifetime."""
    dc = custody

    class _Gateway:
        def __init__(self):
            self.removals = []

        def handshake(self, **_kw):
            return {}

        def remove_project(self, pid):
            self.removals.append(pid)

        def close(self):
            pass

    gateway = _Gateway()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-a", task_id="t-a", route_id="r", model="m",
        project_id="prj-shared", project_owned=True, ledger_root=str(tmp_path)))
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-b", task_id="t-b", route_id="r", model="m",
        project_id="prj-shared", project_owned=False, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-a", "task_id": "t-a", "route": "r"})

    # Owner settled, unowned sibling still live: the sweep must not attempt.
    dc._CUSTODY.clear()
    dc.retire_settled_registrations(tmp_path, gateway)
    assert gateway.removals == [], "a live unowned sharer defers the attempt"

    # Sibling settles: the very next sweep discharges the registration.
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-b", "task_id": "t-b", "route": "r"})
    dc._CUSTODY.clear()
    dc.retire_settled_registrations(tmp_path, gateway)
    assert gateway.removals == ["prj-shared"]


# -- I9: the undeletable engine project stops being retried forever ----------
#
# Ouroboros creates sticky plan-review THREADS scoped to a project root and the
# gateway has no thread delete; the engine then refuses DELETE with the typed
# `{code: "project_has_threads", status: 409}` forever. The duty is discharged
# once under the engine's own code, never recorded as a deletion, and only when
# no unsettled run of the project exists (a replayed PROJECT_RETIRED clears
# `project_owned` for EVERY sibling of the project).

import json


def _rows(root):
    path = root / "logs" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _of(root, kind):
    return [row for row in _rows(root) if row.get("type") == kind]


class _RefusingGateway:
    """DELETE answers with a typed refusal; ``on_remove`` runs before it (race hook)."""

    def __init__(self, exc=None, on_remove=None):
        self.exc, self.on_remove, self.removals = exc, on_remove, []

    def handshake(self, **_kw):
        return {}

    def remove_project(self, project_id):
        self.removals.append(project_id)
        if self.on_remove is not None:
            self.on_remove()
        if self.exc is not None:
            raise self.exc

    def close(self):
        pass


def _has_threads():
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    return ClaudexorUnavailable("project_has_threads", "project prj-kept has 2 threads",
                                status_code=409)


def _start(root, run_id, *, owned, project_id="prj-kept", persistent=False):
    custody.record_started(root, custody.RunCustody(
        run_id=run_id, task_id=f"t-{run_id}", route_id="r", model="m",
        project_id=project_id, project_owned=owned, project_persistent=persistent,
        ledger_root=str(root)))


def _settle(root, run_id):
    custody.emit(root, custody.SETTLED, {"run_id": run_id, "task_id": f"t-{run_id}", "route": "r"})


def test_any_other_refusal_stays_a_retryable_typed_failure(tmp_path):
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    refusals = [
        ClaudexorUnavailable("project_busy", "project has live runs", status_code=409),
        ClaudexorUnavailable("daemon_unreachable", "socket died", status_code=0),
        ClaudexorUnavailable("malformed_response", "non-JSON body", status_code=0),
        ClaudexorUnavailable("http_500", "project_has_threads", status_code=500),  # prose, not code
        RuntimeError("project_has_threads"),  # not even a typed refusal
    ]
    _start(tmp_path, "run-a", owned=True)
    _settle(tmp_path, "run-a")
    gateway = _RefusingGateway()
    for index, exc in enumerate(refusals, start=1):
        gateway.exc = exc
        custody._CUSTODY.clear()
        custody.retire_settled_registrations(tmp_path, gateway)
        assert len(gateway.removals) == index, "every failure is retried"
        assert _of(tmp_path, custody.PROJECT_RETIRED) == [], f"no discharge for {exc!r}"
        failed = _of(tmp_path, custody.PROJECT_RETIRE_FAILED)
        assert len(failed) == index
        row = failed[-1]
        assert row["code"] == str(getattr(exc, "code", "") or "")
        assert row["status"] == int(getattr(exc, "status_code", 0) or 0)
        assert str(exc) in row["reason"], "the daemon's text still rides the row"
    custody._CUSTODY.clear()
    assert [c.run_id for c in custody.owned_project_registrations(tmp_path)] == ["run-a"]

    # The daemon accepts at last: an ordinary retirement, not a kept project.
    gateway.exc = None
    custody.retire_settled_registrations(tmp_path, gateway)
    retired = _of(tmp_path, custody.PROJECT_RETIRED)
    assert len(retired) == 1 and "project_kept" not in retired[0]
    custody._CUSTODY.clear()
    assert custody.owned_project_registrations(tmp_path) == []
