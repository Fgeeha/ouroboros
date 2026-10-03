"""The actual main/watchdog composition retains cleanup before either transfer."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.serial


@pytest.mark.parametrize("managed", [True, False], ids=["launcher", "direct"])
@pytest.mark.parametrize("uvicorn_returns", [True, False], ids=["returned", "held"])
def test_restart_waits_for_cleanup_and_transfers_once(monkeypatch, tmp_path, managed, uvicorn_returns):
    import server
    from ouroboros.delegate_recovery import PLANNED_RESTART_TRANSACTION_ENV

    cleanup_entered, release_cleanup, transfer, release_server = (threading.Event() for _ in range(4))
    calls, failures = [], []
    class DrainEvent(threading.Event):
        def wait(self, timeout=None):
            return super().wait(0.02 if timeout in (5, 30) else timeout)

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self, *, sockets):
            assert sockets[0].getsockname() == ("127.0.0.1", 9123)
            server._restart_requested.set()
            if not uvicorn_returns:
                assert release_server.wait(5)

    @contextmanager
    def listener(*args, **kwargs):
        yield SimpleNamespace(getsockname=lambda: ("127.0.0.1", 9123))

    def cleanup(**kwargs):
        calls.append(("cleanup", kwargs))
        cleanup_entered.set()
        assert release_cleanup.wait(5)
        calls.append(("cleanup_finished", None))

    def reexec(host, port, **kwargs):
        assert calls[-1][0] == "cleanup_finished"
        assert (host, port) == ("127.0.0.1", 9123)
        assert kwargs["owner_initiated"] is True
        assert server.os.environ[PLANNED_RESTART_TRANSACTION_ENV] == "planned-same-attempt"
        calls.append(("reexec", None))

    def exit_process(code):
        assert code == server.RESTART_EXIT_CODE
        calls.append(("exit", code))
        transfer.set()

    monkeypatch.setattr(server, "threading", SimpleNamespace(Event=DrainEvent, Thread=threading.Thread))
    monkeypatch.setattr(server, "_restart_requested", threading.Event())
    owner = threading.Event()
    owner.set()
    monkeypatch.setattr(server, "_owner_restart_requested", owner)
    monkeypatch.setattr(server, "_planned_delegate_restart_transaction_id", "planned-same-attempt")
    monkeypatch.setattr(server, "_LAUNCHER_MANAGED", managed)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "_event_loop", None)
    monkeypatch.setattr(server, "load_settings", lambda: {})
    monkeypatch.setattr(server, "verify_settings_integrity", lambda: None)
    monkeypatch.setattr(server, "parse_server_args", lambda *a: SimpleNamespace(host="127.0.0.1", port=9123, host_explicit=False))
    monkeypatch.setattr(server, "find_free_port", lambda host, port: port)
    monkeypatch.setattr(server, "get_network_auth_startup_warning", lambda host: "")
    monkeypatch.setattr(server, "validate_network_auth_configuration", lambda host: "")
    monkeypatch.setattr(server, "bound_service_socket", listener)
    monkeypatch.setattr(server, "write_port_file", lambda *a: None)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_SignalStopServer", FakeServer)
    monkeypatch.setattr(server, "_emergency_process_cleanup", cleanup)
    monkeypatch.setattr(server, "_restart_current_process_impl", reexec)
    monkeypatch.setattr("supervisor.update_merge.read_update_tx_strict", lambda: ("absent", {}))
    monkeypatch.setattr(server.os, "_exit", exit_process)
    monkeypatch.delenv(PLANNED_RESTART_TRANSACTION_ENV, raising=False)

    def run():
        try:
            server.main()
        except BaseException as exc:
            failures.append(exc)

    main = threading.Thread(target=run)
    main.start()
    try:
        assert cleanup_entered.wait(5)
        assert not transfer.is_set() and main.is_alive()
        release_cleanup.set()
        assert transfer.wait(5)
    finally:
        release_cleanup.set()
        release_server.set()
        main.join(5)
    assert not main.is_alive() and failures == []
    assert calls == [("cleanup", {"port_sweep": False}), ("cleanup_finished", None),
                     *([] if managed else [("reexec", None)]), ("exit", server.RESTART_EXIT_CODE)]


def test_restart_fallback_runs_pin_handoff_before_retained_executor_cleanup(monkeypatch, tmp_path):
    import server

    calls = []
    requested = threading.Event()
    requested.set()
    monkeypatch.setattr(server, "_restart_requested", requested)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server._historical_audit, "stop", lambda: None)
    monkeypatch.setattr(server, "_managed_update_pending_kwargs", lambda: {"preserve_pending": True})
    monkeypatch.setattr(server, "_restart_cleanup_kwargs", lambda: {})
    monkeypatch.setattr("supervisor.workers.kill_workers", lambda **kw: calls.append(("workers", kw)))
    monkeypatch.setattr(server, "_stop_owned_daemon_for_new_pin", lambda: calls.append(("pin", {})))
    monkeypatch.setattr("ouroboros.tools.shell.kill_all_tracked_subprocesses", lambda: calls.append(("shell", {})))
    monkeypatch.setattr("ouroboros.workspace_executor.kill_all_foreground", lambda root, **kw: calls.append(("foreground", kw)))
    monkeypatch.setattr("ouroboros.tools.services.kill_all_services", lambda root, **kw: calls.append(("services", kw)))
    monkeypatch.setattr("multiprocessing.active_children", lambda: [])
    monkeypatch.setattr("ouroboros.extension_companion.panic_kill_all", lambda: calls.append(("companions", {})))
    monkeypatch.setattr("ouroboros.platform_layer.kill_process_on_port", lambda port: pytest.fail("restart must not kill an unrelated listener by port"))
    server._emergency_process_cleanup(port_sweep=False)
    assert [name for name, kw in calls] == ["workers", "pin", "shell", "foreground", "services", "companions"]
    assert calls[0][1]["preserve_pending"] is True
    assert calls[3][1] == calls[4][1] == {"wait": False}


@pytest.mark.parametrize("failure", ["", "fallback", "cleanup", "reexec"],
                         ids=["reexec-ok", "spawn-fallback-ok", "cleanup-error", "reexec-error"])
@pytest.mark.parametrize("uvicorn_returns", [False, True], ids=["held", "returned"])
def test_direct_watchdog_physically_transfers_or_fails(tmp_path, failure, uvicorn_returns):
    """Thread outcomes are physical exec or nonzero exit, never success/hang after an error."""
    script = tmp_path / "direct_reexec.py"
    receipt = tmp_path / "successor.json"
    script.write_text('''
from contextlib import contextmanager
import json, os, pathlib, sys, threading
from types import SimpleNamespace

receipt = pathlib.Path(sys.argv[1])
failure, uvicorn_returns = sys.argv[2], sys.argv[3] == "returned"
if os.environ.get("OURO_TEST_SUCCESSOR"):
    from ouroboros.delegate_recovery import PLANNED_RESTART_TRANSACTION_ENV
    receipt.write_text(json.dumps({
        "pid": os.getpid(), "previous_pid": int(os.environ["OURO_TEST_SUCCESSOR"]),
        "transaction": os.environ.get(PLANNED_RESTART_TRANSACTION_ENV),
        "port": os.environ.get("OUROBOROS_SERVER_PORT"),
        "cleanup": os.environ.get("OURO_TEST_CLEANUP"),
    }), encoding="utf-8")
    sys.exit(0)

import server
import supervisor.update_merge
class DrainEvent(threading.Event):
    def wait(self, timeout=None):
        return super().wait(0.02 if timeout == 30 else timeout)
class HeldServer:
    def __init__(self, config):
        self.should_exit = False
    def run(self, **kwargs):
        server._restart_requested.set()
        if not uvicorn_returns:
            threading.Event().wait(20)
            raise AssertionError("the direct watchdog never replaced the held server")
@contextmanager
def listener(*args, **kwargs):
    yield SimpleNamespace(getsockname=lambda: ("127.0.0.1", 9123))
def cleanup(**kwargs):
    assert kwargs == {"port_sweep": False}
    if failure == "cleanup":
        raise OSError("fixture cleanup failure")
    os.environ["OURO_TEST_CLEANUP"] = "completed"
    os.environ["OURO_TEST_SUCCESSOR"] = str(os.getpid())
def reexec_failure(*args, **kwargs):
    raise OSError("fixture reexec failure")

server.threading = SimpleNamespace(Event=DrainEvent, Thread=threading.Thread)
server._restart_requested = threading.Event()
server._event_loop = None
server._LAUNCHER_MANAGED = False
server._planned_delegate_restart_transaction_id = "physical-handoff"
server.verify_settings_integrity = lambda: None
server.load_settings = lambda: {}
server.parse_server_args = lambda *args: SimpleNamespace(host="127.0.0.1", port=9123, host_explicit=False)
server.find_free_port = lambda host, port: port
server.get_network_auth_startup_warning = lambda host: ""
server.validate_network_auth_configuration = lambda host: ""
server.bound_service_socket = listener
server.write_port_file = lambda *args: None
server.uvicorn.Config = lambda *args, **kwargs: None
server._SignalStopServer = HeldServer
server._emergency_process_cleanup = cleanup
if failure in {"reexec", "fallback"}:
    from ouroboros import server_control, process_custody
    server_control.os.execvpe = reexec_failure
    if failure == "reexec":
        process_custody.spawn_supervised = reexec_failure
    else:
        spawn = process_custody.spawn_supervised
        def joined_spawn(*args, **kwargs):
            child = spawn(*args, **kwargs)
            assert child.wait(timeout=10) == 0
            return child
        process_custody.spawn_supervised = joined_spawn
supervisor.update_merge.read_update_tx_strict = lambda: ("absent", {})
sys.exit(server.main())
''', encoding="utf-8")
    env = os.environ.copy()
    env.pop("OUROBOROS_SERVER_REEXEC_ARGV_JSON", None)
    env.pop("OURO_TEST_SUCCESSOR", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, str(script), str(receipt), failure,
                             "returned" if uvicorn_returns else "held"], env=env,
                            capture_output=True, text=True, timeout=15)
    if failure in {"cleanup", "reexec"}:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Restart failed; cleanup or transfer is unconfirmed" in result.stderr
        assert "fixture " + failure + " failure" in result.stderr
        assert not receipt.exists()
        return
    assert result.returncode == (42 if failure == "fallback" else 0), result.stdout + result.stderr
    observed = json.loads(receipt.read_text(encoding="utf-8"))
    assert observed["transaction"] == "physical-handoff"
    assert observed["port"] == "9123" and observed["cleanup"] == "completed"
    if os.name != "nt":
        assert (observed["pid"] == observed["previous_pid"]) is (failure != "fallback")
