"""Real disposable pooled children; never invoke the installation's Panic."""
from __future__ import annotations

import multiprocessing
import os
import signal
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import platform_layer as platform
from ouroboros.process_containment import pid_is_zombie
from supervisor import worker_process
from tests._shared import stop_socket_sharer

pytestmark = pytest.mark.serial


def _faulted_entry(mode, incoming, outgoing, repo_dir, drive_root, session_id, stop_socket):
    from ouroboros import process_custody, utils

    platform.create_new_session()
    if mode == "startup_log":
        original = utils.append_jsonl

        def blocked_entry_log(path, value, *args, **kwargs):
            if value.get("type") == "worker_starting":
                outgoing.put("before_lifeline")
                threading.Event().wait()
            return original(path, value, *args, **kwargs)

        utils.append_jsonl = blocked_entry_log
        worker_process.worker_main(mode, incoming, outgoing, repo_dir, drive_root, session_id, stop_socket)
        return

    def callback():
        if mode == "callback_raises":
            raise RuntimeError("failed optional cleanup")
        if mode == "callback_hangs":
            threading.Event().wait()

    if mode != "no_lifeline":
        process_custody.start_parent_lifeline(stop_socket=stop_socket, before_exit=callback, poll_sec=.05)
    outgoing.put("ready")
    threading.Event().wait()


def _owner_fault_entry(mode, *args):
    from ouroboros import claudexor_daemon
    from tests.test_batch1_exact_consumers import _pooled_test_entry

    def unavailable_owner(**_kwargs):
        if mode == "owner_raises":
            raise RuntimeError("owner unavailable")
        threading.Event().wait()

    claudexor_daemon.get_owned_daemon = unavailable_owner
    # Actual worker_main -> actual registry -> supported run_command Popen.
    _pooled_test_entry(mode, *args)


def _ordinary_close_entry(wid, incoming, outgoing, repo_dir, drive_root, session_id, stop_socket):
    import subprocess
    import sys

    from ouroboros import claudexor_daemon, process_custody

    platform.create_new_session()
    daemon = claudexor_daemon.OwnedClaudexorDaemon()
    daemon._proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                    **platform.subprocess_new_group_kwargs())
    process_custody.start_parent_lifeline(stop_socket=stop_socket,
                                        before_exit=lambda: daemon.panic_stop(request_only=True), poll_sec=.05)
    outgoing.put(daemon._proc.pid)
    threading.Event().wait()


@pytest.mark.parametrize("mode", ["startup_log", "no_lifeline", "suspended", "callback_raises", "callback_hangs"])
def test_native_worker_stop_does_not_require_lifeline_or_callback(tmp_path, monkeypatch, mode):
    if mode == "suspended" and os.name == "nt":
        pytest.skip("SIGSTOP is a POSIX failure injection")
    monkeypatch.setattr(worker_process, "worker_main", _faulted_entry)
    ctx = multiprocessing.get_context("spawn")
    incoming, outgoing = ctx.Queue(), ctx.Queue()
    proc = worker_process.spawn_worker_process(ctx, mode, incoming, outgoing, tmp_path, tmp_path)
    try:
        assert outgoing.get(timeout=20) == ("before_lifeline" if mode == "startup_log" else "ready")
        if mode == "suspended":
            os.kill(proc.pid, signal.SIGSTOP)  # our still-custodied native child
        start = time.monotonic()
        receipt = platform.request_process_tree_kill(proc)
        assert time.monotonic() - start < .5
        assert receipt["requested"] and receipt["root_backstop"] == "armed_native_owner"
        assert receipt["confirmation"] == "unconfirmed"
        proc.join(timeout=3)
        assert not proc.is_alive(), receipt
        assert time.monotonic() - start < 3
        if mode in {"startup_log", "no_lifeline", "suspended"}:
            assert receipt["native_request"]["requested"]
            if os.name != "nt":
                assert proc.exitcode == -signal.SIGKILL
    finally:
        try:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
            worker_process.close_worker_stop_channel(proc)
            assert proc._ouroboros_stop_socket.fileno() == -1
        finally:  # a failed cleanup assertion must not leak the queues or the socket sharer
            for queue in (incoming, outgoing):
                queue.close()
                queue.cancel_join_thread()
            stop_socket_sharer()


@pytest.mark.parametrize("mode", ["owner_raises", "owner_hangs"])
def test_supported_command_is_stopped_even_when_another_owner_fails(tmp_path, monkeypatch, mode):
    from tests.test_batch1_exact_consumers import _await_published_command_child

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setattr(worker_process, "worker_main", _owner_fault_entry)
    ctx = multiprocessing.get_context("spawn")
    incoming, outgoing = ctx.Queue(), ctx.Queue()
    proc = worker_process.spawn_worker_process(ctx, mode, incoming, outgoing, tmp_path, tmp_path)
    seen = {}
    try:
        incoming.put({"id": "pooled", "type": "task"})
        # The published-child guarantee: the child's own marker can precede its registry entry.
        child_pid = _await_published_command_child(proc, outgoing, tmp_path / "workspace", seen)
        if os.name != "nt":
            assert os.getpgid(child_pid) == child_pid != os.getpgid(proc.pid)
        receipt = platform.request_process_tree_kill(proc)
        proc.join(timeout=3)
        assert not proc.is_alive(), receipt
        deadline = time.monotonic() + 3
        while platform.pid_is_alive(child_pid) and not pid_is_zombie(child_pid) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not platform.pid_is_alive(child_pid) or pid_is_zombie(child_pid), (receipt, proc.exitcode, seen)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
        child_pid = seen.get("pid", 0)  # the marker's own pid, even when its ACK never came
        if child_pid and platform.pid_is_alive(child_pid) and not pid_is_zombie(child_pid):
            platform.force_kill_pid(child_pid)  # only the continuously observed fixture child
        worker_process.close_worker_stop_channel(proc)
        for queue in (incoming, outgoing):
            queue.close()
            queue.cancel_join_thread()
        stop_socket_sharer()


@pytest.mark.parametrize("broken_channel", [False, True])
def test_server_completes_native_requests_before_settlement_or_exit(tmp_path, monkeypatch, broken_channel):
    from tests.test_server_control_panic_daemon import _run_panic

    parent, child = socket.socketpair()
    parent.setblocking(False)
    native_done = threading.Event()
    proc = SimpleNamespace(pid=123, exitcode=None, _ouroboros_stop_socket=parent,
                           _popen=SimpleNamespace(kill=native_done.set))
    proc._ouroboros_stop_request = lambda: worker_process.request_worker_stop(proc)
    diagnostics = []

    def settle():
        assert native_done.is_set(), "settlement could destroy the parent before its native request"
        return True

    try:
        if broken_channel:
            # The SENDING end: its send fails on every platform. A closed peer is not enough on
            # Windows, where socketpair is loopback TCP and the next send may still be buffered.
            parent.close()
        _run_panic(monkeypatch, tmp_path, daemon_stop=settle, children=(proc,), diagnostics=diagnostics)
        assert native_done.is_set()
        receipt = diagnostics[0]["requests"]["child-123"]
        assert receipt["requested"] is not broken_channel
        assert receipt["native_request"]["requested"]
        assert receipt["confirmation"] == "unconfirmed"  # an adapter signal is NOT observed death
    finally:
        proc.exitcode = 1
        worker_process.close_worker_stop_channel(proc)
        child.close()


def test_retirement_closes_channel_but_keeps_owed_native_owner(tmp_path, monkeypatch):
    """An ordinary cleanup failure cannot discard an already armed native request."""
    parent, child = socket.socketpair()
    parent.setblocking(False)
    native_done = threading.Event()
    proc = SimpleNamespace(pid=123, exitcode=None, _ouroboros_stop_socket=parent,
                           _popen=SimpleNamespace(kill=native_done.set))
    try:
        worker_process.request_worker_stop(proc)
        worker_process.close_worker_stop_channel(proc)
        assert parent.fileno() == -1
        assert native_done.wait(2)
    finally:
        proc.exitcode = 1
        worker_process.close_worker_stop_channel(proc)
        child.close()


def test_hung_server_owner_callback_cannot_skip_native_worker_request(tmp_path, monkeypatch):
    from tests.test_server_control_panic_daemon import _run_panic

    entered, release, native_done = threading.Event(), threading.Event(), threading.Event()
    parent, child = socket.socketpair()
    parent.setblocking(False)
    proc = SimpleNamespace(pid=123, exitcode=None, _ouroboros_stop_socket=parent,
                           _popen=SimpleNamespace(kill=native_done.set))
    proc._ouroboros_stop_request = lambda: worker_process.request_worker_stop(proc)

    def hang(**_kwargs):
        entered.set()
        release.wait(3)  # fixture backstop; assert production proceeds well before it
        return []

    def settle():
        assert entered.is_set() and native_done.is_set()
        release.set()
        return True

    try:
        started = time.monotonic()
        _run_panic(monkeypatch, tmp_path, panic_request=hang, daemon_stop=settle, children=(proc,))
        assert time.monotonic() - started < 2
        assert release.is_set() and native_done.is_set()
    finally:
        release.set()
        proc.exitcode = 1
        worker_process.close_worker_stop_channel(proc)
        child.close()


def test_ordinary_stop_channel_retirement_does_not_panic_owned_daemon(tmp_path, monkeypatch):
    from supervisor import worker_pool_lifecycle, workers

    monkeypatch.setattr(worker_process, "worker_main", _ordinary_close_entry)
    monkeypatch.setattr(worker_pool_lifecycle, "_record_worker_pids", lambda: None)
    ctx = multiprocessing.get_context("spawn")
    incoming, outgoing = ctx.Queue(), ctx.Queue()
    proc = worker_process.spawn_worker_process(ctx, 0, incoming, outgoing, tmp_path, tmp_path)
    slot = SimpleNamespace(proc=proc, in_q=incoming)
    monkeypatch.setattr(workers, "WORKERS", {0: slot})
    daemon_pid, daemon_birth = 0, ""
    try:
        daemon_pid = outgoing.get(timeout=20)
        daemon_birth = platform.process_start_time(daemon_pid)  # pinned while its owner still runs
        assert daemon_birth
        worker_process.close_worker_stop_channel(proc)  # EOF is not the explicit Panic byte
        proc.join(timeout=3)
        assert not proc.is_alive()
        assert platform.pid_is_alive(daemon_pid) and not pid_is_zombie(daemon_pid)
        assert worker_pool_lifecycle.retire_worker(0, slot)
        assert proc._ouroboros_stop_socket.fileno() == -1 and not workers.WORKERS
    finally:
        try:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
            if (daemon_birth and platform.pid_is_alive(daemon_pid) and not pid_is_zombie(daemon_pid)
                    and platform.process_start_time(daemon_pid) == daemon_birth):
                platform.force_kill_pid(daemon_pid)  # the fixture's own daemon: its PID AND its birth
                deadline = time.monotonic() + 3
                while (platform.pid_is_alive(daemon_pid) and not pid_is_zombie(daemon_pid)
                       and time.monotonic() < deadline):
                    time.sleep(.01)
                assert not platform.pid_is_alive(daemon_pid) or pid_is_zombie(daemon_pid)
        finally:  # a failed death proof must not leak the stop channel, queues or socket sharer
            worker_process.close_worker_stop_channel(proc)
            for queue in (incoming, outgoing):
                queue.close()
                queue.cancel_join_thread()
            stop_socket_sharer()
