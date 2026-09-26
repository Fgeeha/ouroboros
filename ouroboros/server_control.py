"""Process-control helpers for the self-editable server entrypoint."""

from __future__ import annotations

import os
import json
import pathlib
import sys
from typing import Any


def restart_current_process(
    host: str,
    port: int,
    *,
    repo_dir: pathlib.Path,
    log: Any,
    owner_initiated: bool = False,
) -> None:
    """Re-exec this server process.

    ``owner_initiated`` marks the restart the OWNER asked for (the chat Restart
    button, and the control endpoints that restart on the owner's behalf). Only
    that restart drops the inherited runtime-mode ratchet pin, so the child
    re-pins from ``load_settings()``; an agent- or supervisor-initiated restart
    keeps inheriting it exactly as before.
    """
    env = os.environ.copy()
    desired_host = str(host)
    try:
        from ouroboros.config import load_settings
        desired_host = (
            str(os.environ.get("OUROBOROS_SERVER_HOST") or "").strip()
            or str(load_settings().get("OUROBOROS_SERVER_HOST") or "").strip()
            or desired_host
        )
    except Exception:
        desired_host = str(host)
    # Keep real env/argv overrides, but do not turn a Settings-derived host into
    # an env pin: the next owner save must still apply on the following restart.
    env["OUROBOROS_SERVER_PORT"] = str(port)
    env.pop("OUROBOROS_MANAGED_BY_LAUNCHER", None)
    env.pop("OUROBOROS_MANAGED_REPO_DIR", None)
    if owner_initiated:
        # The ratchet pin is exported so a CHILD inherits the parent's baseline
        # and cannot widen its own scope. Carried across an owner restart it also
        # pinned the mode the owner just raised in Settings and pressed Restart to
        # apply: the replacement re-pinned the OLD baseline from this env and the
        # new mode never took effect, on this restart or any later one. Dropping
        # the key here makes the child re-pin from load_settings() — the file only
        # the owner can author. Agent/supervisor restarts keep inheriting it.
        from ouroboros.config import BOOT_RUNTIME_MODE_ENV_KEY

        env.pop(BOOT_RUNTIME_MODE_ENV_KEY, None)
    raw_argv = sys.argv
    try:
        saved = json.loads(os.environ.get("OUROBOROS_SERVER_REEXEC_ARGV_JSON", "") or "[]")
        if isinstance(saved, list) and saved and all(isinstance(item, str) and item for item in saved):
            raw_argv = saved
    except Exception:
        raw_argv = sys.argv
    argv = [sys.executable, *raw_argv]
    log.info("Re-executing direct server mode on %s:%d", desired_host, port)
    try:
        os.execvpe(sys.executable, argv, env)
    except Exception:
        log.exception("Direct re-exec failed; attempting spawned restart fallback.")
        try:
            from ouroboros.config import DATA_DIR
            from ouroboros.process_custody import spawn_supervised

            spawn_supervised(
                argv,
                drive_root=pathlib.Path(DATA_DIR),
                # daemon, NOT session: the replacement IS the next server
                # generation. A session-scoped entry carries this dying
                # generation's session id, so the new server's startup reap
                # would see it as a foreign-session process and SIGKILL itself.
                # daemon scope is always a reaper survivor (launcher-managed
                # lifecycle), which is correct for a long-lived top-level server.
                purpose="server_restart_fallback",
                scope="daemon",
                cwd=str(repo_dir),
                env=env,
            )
            log.info("Spawned replacement server process after exec failure.")
        except Exception:
            log.exception("Spawned restart fallback failed; exiting with restart code only.")


def execute_panic_stop(
    consciousness: Any,
    kill_workers_fn,
    *,
    data_dir: pathlib.Path,
    panic_exit_code: int,
    log: Any,
    bound_port: int | None = None,
) -> None:
    """Full emergency stop: kill everything, write panic flag, hard-exit.

    ``bound_port`` is the main port the server actually bound. The caller owns
    that fact and passes it in; this leaf does not reach back into the server
    module for it. Omitted (or falsy), the sweep falls back to the default
    install port — see the sweep below.

    The owned Claudexor daemon is stopped by ``get_owned_daemon().stop()``:
    authenticated same-home CLI shutdown handles attached and prior-generation
    daemons, with measured custody/Popen signalling as fallback. Delegated work
    ends through that explicit stop. The shared daemon survives ordinary close
    outside the Windows launcher Job, so that Job is not a Panic backstop.
    Unconfirmed shutdown remains disclosed with custody retained; names or
    recycled descriptor ports never authorize signalling an unrelated process.
    """
    import threading
    from ouroboros.startup_historical_audit import audit

    audit.stop()
    requests = []

    def request(name, fn):
        # No join here: one helper's lock, disk, CLI or exit wait must not stand
        # between Panic and another positively owned process's stop request.
        done = threading.Event()
        def run():
            try:
                fn()
            except Exception:
                pass  # custody stays unconfirmed; no fabricated completion
            finally:
                done.set()
        threading.Thread(target=run, name=f"panic-{name}", daemon=True).start()
        requests.append((name, done))

    import multiprocessing

    from ouroboros.claudexor_daemon import get_owned_daemon
    from ouroboros.extension_companion import panic_kill_all
    from ouroboros.gateway.host_service import host_service_port
    from ouroboros.local_model import get_manager
    from ouroboros.platform_layer import force_kill_pid, kill_process_on_port
    from ouroboros.tools.services import kill_all_services
    from ouroboros.tools.shell import kill_all_tracked_subprocesses
    from ouroboros.workspace_executor import kill_all_foreground

    request("consciousness", consciousness.stop)
    request("local-model", lambda: get_manager().panic_stop())
    request("daemon", lambda: get_owned_daemon().panic_stop())
    request("commands", kill_all_tracked_subprocesses)
    request("executors", lambda: kill_all_foreground(data_dir, wait=False))
    request("services", lambda: kill_all_services(data_dir, wait=False))
    request("companions", panic_kill_all)
    request("workers", lambda: kill_workers_fn(
        force=True, archive_service_logs=False, reconcile_delegate_custody=False))
    # Multiprocessing handles are positively owned; never expand to PID/name scans.
    for child in multiprocessing.active_children():
        request(f"child-{child.pid}", lambda pid=child.pid: force_kill_pid(pid))
    request("main-port", lambda: kill_process_on_port(bound_port or 8765))
    request("host-port", lambda: kill_process_on_port(host_service_port()))

    # All stop paths have been launched before any persistence wait. The flag can
    # progress even if a helper is stuck; no successful thread launch proves death.
    flag_written = _bounded(lambda: _write_panic_flag(data_dir), 2.0)
    _bounded(lambda: _persist_panic_controls(data_dir), 2.0)
    unsettled = [name for name, done in requests if not done.is_set()]
    _bounded(lambda: log.critical("PANIC STOP: flag persisted=%s; unfinished stop helpers=%s; "
                                  "hard exit %d, unresolved custody retained.",
                                  flag_written, unsettled, panic_exit_code), 0.5)
    os._exit(panic_exit_code)


def _bounded(fn, timeout_sec: float) -> bool:
    """Run one best-effort Panic write on a daemon thread and wait at most
    ``timeout_sec``: a stalled disk or lock can never hold the exit. True only
    when it finished without raising."""
    import threading

    done = threading.Event()

    def _run() -> None:
        try:
            fn()
            done.set()
        except Exception:
            pass

    threading.Thread(target=_run, name="panic-bounded-write", daemon=True).start()
    return done.wait(timeout_sec)


def _write_panic_flag(data_dir: pathlib.Path) -> None:
    panic_flag = data_dir / "state" / "panic_stop.flag"
    panic_flag.parent.mkdir(parents=True, exist_ok=True)
    panic_flag.write_text("panic", encoding="utf-8")


def _persist_panic_controls(data_dir: pathlib.Path) -> None:
    """Panic is an owner stop: disable evolution and consciousness as KNOWN controls
    (short lock, never unlocked), record the evolution stop intent, close the campaign
    without git cleanup and drop a queued promotion. Each step is independent."""
    from ouroboros.post_task_evolution import drop_pending_request
    from supervisor import state
    from supervisor.evolution_lifecycle import complete_evolution_campaign, record_evolution_stop_intent

    def _panic_controls(st: dict) -> None:
        st.update(evolution_mode_enabled=False, bg_consciousness_enabled=False,
                  evolution_owner_stopped=True, post_task_autostop=False)
        st.pop("evolution_stop_source", None)  # an owner stop: no agent source may un-stick it

    for step in (
        lambda: state.update_state(_panic_controls, confirm=PANIC_CONTROL_KEYS, lock_timeout_sec=0.5),
        lambda: record_evolution_stop_intent("panic", "panic stop"),
        # cleanup_worktree=False: Panic never runs git stash/reset; the flag + boot reconcile own it.
        lambda: complete_evolution_campaign("panic stop", status="stopped", cleanup_worktree=False),
        lambda: drop_pending_request(data_dir),
    ):
        try:
            step()
        except Exception:
            pass


PANIC_CONTROL_KEYS = ("evolution_mode_enabled", "bg_consciousness_enabled", "evolution_owner_stopped",
                      "evolution_stop_source", "post_task_autostop")


def _record_unconfirmed_daemon_stop(data_dir: pathlib.Path, exc: BaseException) -> None:
    from ouroboros.utils import append_jsonl, utc_now_iso

    append_jsonl(data_dir / "logs" / "supervisor.jsonl", {
        "ts": utc_now_iso(), "type": "process_stop_unconfirmed",
        "purpose": "claudexor_daemon", "reason": f"stop raised {type(exc).__name__}",
    })
