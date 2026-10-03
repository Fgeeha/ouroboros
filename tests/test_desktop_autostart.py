"""Sign-in contract across packaged hosts; OS commands never reach the real host."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from ouroboros import desktop_autostart as startup
from ouroboros.launcher_bootstrap import automatic_launch_allowed, parse_launch_options


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    monkeypatch.setenv("OUROBOROS_PRESENTATION", "desktop_window")
    monkeypatch.setenv("OUROBOROS_APP_VERSION", "7.2.0")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(startup.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(startup, "NATIVE_UNIT", tmp_path / "ouroboros.service")
    monkeypatch.setattr(startup.os, "getuid", lambda: 501, raising=False)
    state = SimpleNamespace(calls=[], override=None, unit="disabled")

    def run(argv, **kwargs):
        state.calls.append(argv)
        if argv[0] == "launchctl":
            if argv[1] == "enable":
                state.override = "enabled"
                return ""
            assert argv == ["launchctl", "print-disabled", "gui/501"]
            # Live shape (darwin 25): `=> disabled` / `=> enabled`.
            rows = ['"com.apple.Siri.agent" => disabled']  # a neighbour's override is not ours
            rows += [f'"{startup.LABEL}" => {state.override}'] if state.override else []
            return "disabled services = {\n" + "".join(f"\t\t{row}\n" for row in rows) + "\t}"
        assert argv[0:2] == ["systemctl", "--user"] and argv[-1] == "ouroboros.service"
        if argv[2] == "is-enabled":
            return state.unit
        assert argv[2] in {"enable", "disable"}  # never --now, start, stop or linger
        state.unit = "enabled" if argv[2] == "enable" else "disabled"
        return ""

    monkeypatch.setattr(startup, "_run", run)

    def package(platform, *, native=False, appimage=False):
        monkeypatch.setattr(startup, "sys", SimpleNamespace(platform=platform))
        bundle = (tmp_path / "Ouroboros.app/Contents/Resources" if platform == "darwin"
                  else tmp_path / "Ouroboros/_internal")
        exe = (bundle.parent / "MacOS/Ouroboros" if platform == "darwin"
               else bundle.parent / "Ouroboros")
        bundle.mkdir(parents=True, exist_ok=True)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.touch()
        monkeypatch.setenv("OUROBOROS_BUNDLE_DIR", str(bundle))
        if native:
            monkeypatch.setattr(startup, "NATIVE_LAUNCHER", exe)
            startup.NATIVE_UNIT.touch()
        if appimage:
            exe = tmp_path / "My Ouroboros.AppImage"
            exe.touch()
            monkeypatch.setenv("APPIMAGE", str(exe))
            monkeypatch.setenv("APPDIR", str(bundle.parent))
        return exe

    return package, state


def test_macos_writes_one_launchagent_and_respects_os_override(host, tmp_path):
    package, os_state = host
    exe = package("darwin")
    path = tmp_path / "Library/LaunchAgents/com.ouroboros.agent.plist"
    assert startup.autostart_status() == {"state": "off"}
    assert not path.exists() and os_state.calls == []  # GET writes nothing
    assert startup.autostart_status(True) == {"state": "on"}
    entry = plistlib.loads(path.read_bytes())
    assert entry == {"Label": "com.ouroboros.agent", "ProgramArguments": [str(exe), "--launch-intent", "automatic"], "RunAtLoad": True}
    assert not (tmp_path / ".config").exists()
    assert os_state.calls[0] == ["launchctl", "enable", "gui/501/com.ouroboros.agent"]
    os_state.override = "disabled"  # System Settings or `launchctl disable`, as current macOS prints it
    assert startup.autostart_status()["state"] == "disabled_by_os"
    assert startup.autostart_status(True)["state"] == "on"
    for printed, expected in (("true", "disabled_by_os"), ("false", "on"), (None, "on")):  # older spelling; no row
        os_state.override = printed
        assert startup.autostart_status()["state"] == expected
    entry["ProgramArguments"] = ["/Applications/Other.app/Contents/MacOS/Ouroboros"]
    path.write_bytes(plistlib.dumps(entry))
    assert startup.autostart_status()["state"] == "other_copy"
    assert startup.autostart_status(False) == {"state": "off"}
    assert not path.exists()


@pytest.mark.parametrize("appimage", [False, True])
def test_linux_portable_registers_only_its_stable_target(host, tmp_path, appimage):
    package, os_state = host
    exe = package("linux", appimage=appimage)
    path = tmp_path / ".config/autostart/ouroboros.desktop"
    assert startup.autostart_status()["state"] == "off"
    assert startup.autostart_status(True)["state"] == "on"
    text = path.read_text(encoding="utf-8")
    assert text == f'[Desktop Entry]\nType=Application\nName=Ouroboros\nExec="{exe}" --launch-intent automatic\nTerminal=false\n'
    assert os_state.calls == [] and not (tmp_path / "Library").exists()
    path.write_text(text + "Hidden=true\n", encoding="utf-8")
    assert startup.autostart_status()["state"] == "disabled_by_os"
    assert startup.autostart_status(True)["state"] == "on"
    path.write_text(text.replace(str(exe), "/other/Ouroboros"), encoding="utf-8")
    assert startup.autostart_status()["state"] == "other_copy"
    assert startup.autostart_status(False)["state"] == "off"
    assert not path.exists()


def test_linux_native_uses_shipped_unit_and_replaces_xdg_registration(host, tmp_path):
    package, os_state = host
    package("linux", native=True)
    path = tmp_path / ".config/autostart/ouroboros.desktop"
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nExec=/old/Ouroboros\n", encoding="utf-8")
    assert startup.autostart_status()["state"] == "other_copy"
    assert startup.autostart_status(True)["state"] == "on"
    assert not path.exists()
    assert ["systemctl", "--user", "enable", "ouroboros.service"] in os_state.calls
    os_state.unit = "masked"
    assert startup.autostart_status()["state"] == "disabled_by_os"
    os_state.unit = "enabled"
    assert startup.autostart_status(False)["state"] == "off"
    assert ["systemctl", "--user", "disable", "ouroboros.service"] in os_state.calls


def test_linux_portable_disables_existing_native_registration_before_writing(host, tmp_path):
    package, os_state = host
    package("linux", appimage=True)
    startup.NATIVE_UNIT.touch()
    os_state.unit = "enabled"
    assert startup.autostart_status()["state"] == "other_copy"
    assert startup.autostart_status(True)["state"] == "on"
    assert os_state.unit == "disabled"
    assert (tmp_path / ".config/autostart/ouroboros.desktop").exists()


@pytest.mark.parametrize("native", [False, True])
def test_linux_refuses_a_second_registration_when_the_first_cannot_be_removed(host, tmp_path, monkeypatch, native):
    package, os_state = host
    package("linux", native=native, appimage=not native)
    startup.NATIVE_UNIT.touch()
    path = tmp_path / ".config/autostart/ouroboros.desktop"
    if native:
        path.parent.mkdir(parents=True)
        path.mkdir()  # unlink must fail before systemctl enable can run
    else:
        os_state.unit = "enabled"
        monkeypatch.setattr(startup, "_run", lambda argv, **kwargs: "enabled")
    with pytest.raises(OSError):
        startup.autostart_status(True)
    assert ["systemctl", "--user", "enable", "ouroboros.service"] not in os_state.calls
    assert not path.is_file()


def test_linux_browser_fallback_still_configures_the_packaged_host(host, monkeypatch):
    package, _ = host
    package("linux")
    monkeypatch.setenv("OUROBOROS_PRESENTATION", "browser_fallback")
    assert startup.autostart_status(True)["state"] == "on"


@pytest.mark.parametrize("version", ["7.1.9", "", "unknown", "7.2.0-rc.1"])
def test_old_or_unproven_launcher_cannot_register(host, monkeypatch, version):
    package, os_state = host
    package("darwin")
    monkeypatch.setenv("OUROBOROS_APP_VERSION", version)
    for enable in (None, True, False):
        status = startup.autostart_status(enable)
        assert status["state"] == "unavailable" and "newer app build" in status["reason"]
    assert os_state.calls == []


@pytest.mark.parametrize("kind", ["source_launcher", "source_server", "unstable_macos", "missing_appimage"])
def test_unavailable_hosts_never_register(host, tmp_path, monkeypatch, kind):
    package, os_state = host
    exe = package("linux" if kind == "missing_appimage" else "darwin", appimage=kind == "missing_appimage")
    if kind == "source_launcher":
        monkeypatch.setenv("OUROBOROS_BUNDLE_DIR", str(tmp_path / "source"))
    elif kind == "source_server":
        monkeypatch.delenv("OUROBOROS_MANAGED_BY_LAUNCHER")
    elif kind == "unstable_macos":
        monkeypatch.setattr(startup, "is_unstable_macos_app_path", lambda path: True)
    else:
        exe.unlink()
    result = startup.autostart_status(True)
    assert result["state"] == "unavailable" and os_state.calls == []
    if kind == "unstable_macos":
        assert "Move Ouroboros to Applications" in result["reason"]


def test_runtime_context_reports_host_lifecycle_even_for_a_remote_sender(host, tmp_path, monkeypatch):
    from ouroboros import context

    package, _ = host
    package("linux")
    startup.autostart_status(True)
    env = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, drive_path=lambda name: tmp_path / name)
    monkeypatch.setattr(context, "_runtime_budget_info", lambda *args: {})
    rendered = context.build_runtime_section(env, {"metadata": {"client_surface": {"channel": "web"}}})
    data = json.JSONDecoder().raw_decode(rendered.split("\n\n", 1)[1])[0]
    assert data["runtime_env"]["autostart"] == "on"
    assert data["runtime_env"]["keep_running_after_close"] is False


def test_a_sign_in_start_keeps_a_saved_pause(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from supervisor import worker_chat_lane
    from tests._budget_pause_exact_helpers import _install_queue, _parked

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    _parked(tmp_path, monkeypatch)
    assert queue.persist_queue_snapshot(reason="sign_out")
    workers.PENDING.clear()
    intent = parse_launch_options(["--launch-intent", "automatic"]).launch_intent
    assert automatic_launch_allowed(intent, tmp_path, logging.getLogger(__name__))
    assert queue.restore_pending_from_snapshot() == 1
    monkeypatch.setattr(worker_chat_lane, "_pool", lambda: workers)
    worker_chat_lane.auto_resume_after_restart()
    assert workers.PENDING[0]["_budget_pause"]["exact_continuation"]
    assert budget_pause.budget_pause_row(tmp_path, "pause-task")["state"] == budget_pause.STATE_PAUSED


def test_native_unit_uses_automatic_intent():
    """Stage-0 dependency: its executor owns the shipped unit, not this change."""
    unit = Path(__file__).resolve().parents[1] / "packaging/systemd/ouroboros.service"
    assert "ExecStart=/opt/ouroboros/Ouroboros --launch-intent automatic" in unit.read_text(encoding="utf-8")
