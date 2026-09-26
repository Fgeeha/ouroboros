from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest


SKILL_ROOT = Path(__file__).parents[1] / "skills" / "telegram"
package = types.ModuleType("telegram_native_test")
package.__path__ = [str(SKILL_ROOT)]
sys.modules["telegram_native_test"] = package
lib_package = types.ModuleType("telegram_native_test.lib")
lib_package.__path__ = [str(SKILL_ROOT / "lib")]
sys.modules["telegram_native_test.lib"] = lib_package
SPEC = importlib.util.spec_from_file_location(
    "telegram_native_test.lib.miniapp_registration",
    SKILL_ROOT / "lib" / "miniapp_registration.py",
)
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class FakeAPI:
    def __init__(self, root: Path, *, port: int = 8765) -> None:
        self.data_dir = root / "data"
        self.state_dir = root / "skill-state"
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "settings.json").write_text(
            json.dumps({"TELEGRAM_CHAT_ID": "12345"}),
            encoding="utf-8",
        )
        self.port = port
        self.routes: list[tuple[str, tuple[str, ...], Any]] = []
        self.companions: list[str] = []
        self.sections: list[tuple[str, str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str]] = []

    @staticmethod
    def _assert_tool_name(name: str) -> None:
        candidate = str(name or "").strip()
        if not candidate or len(candidate) > 64 or not candidate.replace("_", "").isalnum():
            raise ValueError(f"tool name must be alnum/underscore only: {candidate!r}")

    def get_runtime_info(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "state_dir": str(self.state_dir),
            "server_port": self.port,
        }

    def get_state_dir(self) -> str:
        return str(self.state_dir)

    def register_route(self, name: str, handler: Any, methods: tuple[str, ...]) -> None:
        self._assert_tool_name(name)
        self.routes.append((name, methods, handler))

    def register_settings_section(self, section_id: str, title: str, schema: dict[str, Any]) -> None:
        self._assert_tool_name(section_id)
        self.sections.append((section_id, title, schema))

    def register_companion_process(self, name: str) -> None:
        self._assert_tool_name(name)
        self.companions.append(name)

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


def test_register_writes_nonsecret_runtime_config_and_companion(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path, port=9012)
    plugin.register(api)
    config_path = api.state_dir / plugin._CONFIG_NAME
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config == {
        "schema": 2,
        "core_port": 9012,
        "owner_chat_id": 12345,
        "button_text": "Ouroboros",
        "tunnel": "cloudflare_quick",
    }
    if sys.platform != "win32":
        assert config_path.stat().st_mode & 0o777 == 0o600
    assert api.companions == ["miniapp_gateway"]
    assert api.routes == []
    assert api.sections == []


def test_bridge_binding_waits_for_private_owner_and_rejects_symlink(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path)
    settings = api.state_dir / "settings.json"
    settings.write_text(json.dumps({"TELEGRAM_CHAT_ID": "-100123"}), encoding="utf-8")
    plugin.register(api)
    config = json.loads((api.state_dir / plugin._CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["owner_chat_id"] == 0

    target = tmp_path / "other.json"
    target.write_text(json.dumps({"TELEGRAM_CHAT_ID": "999"}), encoding="utf-8")
    settings.unlink()
    settings.symlink_to(target)
    with pytest.raises(Exception, match="unsafe"):
        plugin.register(api)


def test_invalid_core_port_fails_before_companion(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path, port=0)
    with pytest.raises(plugin.ConfigurationError, match="port"):
        plugin.register(api)
    assert api.companions == []


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Linux", "aarch64"),
        ("Linux", "amd64"),
        ("Windows", "AMD64"),
    ],
)
def test_supported_platform_matrix(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    monkeypatch.setattr(plugin.platform, "system", lambda: system)
    monkeypatch.setattr(plugin.platform, "machine", lambda: machine)
    assert plugin._platform_error() == ""


def test_unsupported_platform_skips_only_miniapp_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(plugin.platform, "machine", lambda: "mips")
    api = FakeAPI(tmp_path)
    plugin.register(api)
    assert api.companions == []
    status = plugin._read_status(api)
    assert status["state"] == "unavailable"
    assert status["reason_code"] == "unsupported_platform"


def test_empty_windows_machine_still_registers_companion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_native_test.scripts import platform_support

    monkeypatch.setattr(plugin.platform, "system", lambda: "Windows")
    monkeypatch.setattr(plugin.platform, "machine", lambda: "")
    monkeypatch.setattr(platform_support.sysconfig, "get_platform", lambda: "win-amd64")
    api = FakeAPI(tmp_path)
    plugin.register(api)
    assert api.companions == ["miniapp_gateway"]
    assert plugin._read_status(api)["state"] == "starting"


def test_windows_status_uses_heartbeat_without_destructive_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeAPI(tmp_path)
    plugin.register(api)
    monkeypatch.setattr(plugin.platform, "system", lambda: "Windows")
    monkeypatch.setattr(plugin, "process_alive", lambda _pid: True)
    monkeypatch.setattr(
        plugin.os,
        "kill",
        lambda *_args: pytest.fail("Windows status must never call os.kill(pid, 0)"),
    )
    (api.state_dir / plugin._STATUS_NAME).write_text(
        json.dumps(
            {
                "state": "ready",
                "message": "ok",
                "public_url": "https://abc.trycloudflare.com/",
                "pid": 4242,
                "updated_at_epoch": int(time.time()),
            }
        ),
        encoding="utf-8",
    )
    assert plugin._read_status(api)["state"] == "ready"


def test_status_projection_returns_only_bounded_diagnostics(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path)
    plugin.register(api)
    extra_key = "TELEGRAM_BOT_TOKEN"
    sentinel = "must-not-leak"
    (api.state_dir / plugin._STATUS_NAME).write_text(
        json.dumps(
            {
                "state": "ready",
                "message": "Existing SPA is available",
                "public_url": "https://abc.trycloudflare.com/",
                "cloudflared_version": "2026.7.2",
                "pid": os.getpid(),
                "updated_at_epoch": int(time.time()),
                "reason_code": "healthy",
                "attempt": 0,
                "last_ready_at_epoch": int(time.time()),
                "next_retry_at_epoch": 0,
                extra_key: sentinel,
                "init_data": sentinel,
            }
        ),
        encoding="utf-8",
    )
    body = plugin._read_status(api)
    assert body["state"] == "ready"
    assert body["message"] == "Existing SPA is available"
    assert body["public_url"] == "https://abc.trycloudflare.com/"
    assert body["cloudflared_version"] == "2026.7.2"
    assert body["reason_code"] == "healthy"
    assert sentinel not in json.dumps(body)


def test_status_hides_dead_companion_url(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path)
    plugin.register(api)
    (api.state_dir / plugin._STATUS_NAME).write_text(
        json.dumps(
            {
                "state": "ready",
                "message": "old ready state",
                "public_url": "https://dead.trycloudflare.com/",
                "pid": 999_999_999,
                "updated_at_epoch": int(time.time()),
            }
        ),
        encoding="utf-8",
    )
    body = plugin._read_status(api)
    assert body["state"] == "stale"
    assert body["reason_code"] == "heartbeat_stale"
    assert "public_url" not in body


def test_registration_status_becomes_stale_without_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(plugin.time, "time", lambda: now)
    api = FakeAPI(tmp_path)
    plugin.register(api)
    starting = plugin._read_status(api)
    assert starting["state"] == "starting"
    assert "cloudflared_version" not in starting
    monkeypatch.setattr(plugin.time, "time", lambda: now + 46)
    stale = plugin._read_status(api)
    assert stale["state"] == "stale"
    assert stale["reason_code"] == "heartbeat_stale"
