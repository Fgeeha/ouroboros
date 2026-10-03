"""Host sign-in registration, read from the OS rather than settings.json.

Only packaged launchers with automatic-intent support may register. Adapters
change the NEXT sign-in, never start/stop the current runtime or release a saved
pause. launchd has no KeepAlive; systemd uses the shipped, non-restarting unit.
Linux selects one registration: native packages use systemd, portable builds XDG.
"""
from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from xml.parsers.expat import ExpatError

from ouroboros import windows_autostart
from ouroboros.platform_layer import BUNDLE_DIR_ENV, is_unstable_macos_app_path
from ouroboros.utils import write_bytes_atomic, write_text_atomic

LABEL = "com.ouroboros.agent"
UNIT = "ouroboros.service"
NATIVE_LAUNCHER = Path("/opt/ouroboros/Ouroboros")
NATIVE_UNIT = Path("/usr/lib/systemd/user/ouroboros.service")
AUTOMATIC_ARGS = ["--launch-intent", "automatic"]


def launcher_target() -> tuple[Path | None, str]:
    """The launcher's immutable build facts, not the managed repo's version."""
    presentation = os.environ.get("OUROBOROS_PRESENTATION")
    allowed = ("desktop_window", "browser_fallback") if sys.platform == "linux" else ("desktop_window",)
    bundle_text = os.environ.get(BUNDLE_DIR_ENV, "")
    if (sys.platform not in ADAPTERS or presentation not in allowed or not bundle_text
            or os.environ.get("OUROBOROS_MANAGED_BY_LAUNCHER") != "1"):
        return None, "Available only when the host runs the packaged desktop app."
    bundle = Path(bundle_text)
    if sys.platform == "win32":
        exe = windows_autostart.launcher_path()
    elif sys.platform == "darwin":
        app = next((p for p in bundle.parents if p.suffix == ".app"), None)
        if app and is_unstable_macos_app_path(app):
            return None, "Move Ouroboros to Applications before enabling sign-in startup."
        exe = app / "Contents/MacOS/Ouroboros" if app else None
    else:
        exe = bundle.parent / "Ouroboros" if bundle.name == "_internal" else None
        appimage = os.environ.get("APPIMAGE", "")
        if appimage:
            exe = Path(appimage)
        elif os.environ.get("APPDIR"):
            return None, "Open Ouroboros from its stable AppImage file first."
    if exe is None or not exe.is_absolute() or not exe.is_file():
        return None, "Available only when the host runs the packaged desktop app."
    try:
        version = tuple(int(part) for part in os.environ.get("OUROBOROS_APP_VERSION", "").lstrip("v").split("."))
    except ValueError:
        version = ()
    if len(version) != 3 or version < (7, 2, 0):
        return None, "Sign-in startup needs a newer app build (7.2.0 or later)."
    return exe, ""


def _run(argv: list[str], *, codes: tuple[int, ...] = (0,)) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{argv[0]} did not answer within 5 seconds") from exc
    if result.returncode not in codes:
        raise OSError(result.stderr.strip() or f"{argv[0]} exited {result.returncode}")
    return result.stdout.strip()


def _windows(_exe: Path, enabled: bool | None) -> str:
    return windows_autostart.autostart_state() if enabled is None else windows_autostart.set_autostart(enabled)


def _macos(exe: Path, enabled: bool | None) -> str:
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if enabled is False:
        path.unlink(missing_ok=True)
    elif enabled is True:
        write_bytes_atomic(path, plistlib.dumps({
            "Label": LABEL, "ProgramArguments": [str(exe), *AUTOMATIC_ARGS], "RunAtLoad": True,
        }))
        # Clear a launchctl override only on an explicit owner enable. Do not
        # bootstrap: RunAtLoad would launch another instance in this session.
        _run(["launchctl", "enable", f"gui/{os.getuid()}/{LABEL}"])
    try:
        entry = plistlib.loads(path.read_bytes())
    except FileNotFoundError:
        return "off"
    except (plistlib.InvalidFileException, ValueError, ExpatError):
        return "other_copy"
    if (not isinstance(entry, dict) or entry.get("Label") != LABEL
            or entry.get("ProgramArguments") != [str(exe), *AUTOMATIC_ARGS]
            or entry.get("RunAtLoad") is not True or entry.get("KeepAlive", False) is not False):
        return "other_copy"
    disabled = _run(["launchctl", "print-disabled", f"gui/{os.getuid()}"])
    return "disabled_by_os" if re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*true\b', disabled) else "on"


def _desktop_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "autostart/ouroboros.desktop"


def _desktop_command(exe: Path) -> str:
    # Desktop Entry's two escaping layers: argv quoting, then string escaping.
    quoted = "".join("\\" + c if c in '\\"`$' else c for c in str(exe)).replace("%", "%%")
    quoted = quoted.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{quoted}" --launch-intent automatic'


def _desktop_state(path: Path, exe: Path) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
        entry = parser["Desktop Entry"]
    except FileNotFoundError:
        return "off"
    except (configparser.Error, KeyError, UnicodeError):
        return "other_copy"
    if entry.get("Exec") != _desktop_command(exe) or entry.get("Type") != "Application":
        return "other_copy"
    return "disabled_by_os" if entry.get("Hidden", "false").lower() == "true" else "on"


def _systemd_state() -> str:
    value = _run(["systemctl", "--user", "is-enabled", UNIT], codes=(0, 1, 4))
    states = {"enabled": "on", "enabled-runtime": "on", "disabled": "off",
              "masked": "disabled_by_os", "masked-runtime": "disabled_by_os", "not-found": "unavailable"}
    if value not in states:
        raise OSError(f"Could not determine systemd sign-in startup: {value or 'no answer'}")
    return states[value]


def _linux(exe: Path, enabled: bool | None) -> str:
    path = _desktop_path()
    native = exe == NATIVE_LAUNCHER
    unit_state = _systemd_state() if native or NATIVE_UNIT.is_file() else "off"
    if enabled is not None:
        if native:
            path.unlink(missing_ok=True)
            _run(["systemctl", "--user", "enable" if enabled else "disable", UNIT])
            return _systemd_state()
        if unit_state == "on":
            _run(["systemctl", "--user", "disable", UNIT])
            if _systemd_state() == "on":
                raise OSError("Disable the host's Ouroboros user service before switching to this copy.")
        if enabled:
            write_text_atomic(path, "[Desktop Entry]\nType=Application\nName=Ouroboros\n"
                              f"Exec={_desktop_command(exe)}\nTerminal=false\n")
        else:
            path.unlink(missing_ok=True)
        unit_state = "off"
    if native:
        return "other_copy" if path.exists() else unit_state
    return "other_copy" if unit_state == "on" else _desktop_state(path, exe)


ADAPTERS = {"win32": _windows, "darwin": _macos, "linux": _linux}


def autostart_status(enabled: bool | None = None) -> dict[str, str]:
    """Read OS state; a boolean additionally requests an owner registration edit."""
    exe, reason = launcher_target()
    if exe is None:
        return {"state": "unavailable", "reason": reason}
    state = ADAPTERS[sys.platform](exe, enabled)
    return {"state": state, **({"reason": "The packaged sign-in service is unavailable."} if state == "unavailable" else {})}


def runtime_facts() -> dict:
    """One task-start observation; OS read failures do not break model context."""
    try:
        status = autostart_status()
    except OSError as exc:
        logging.getLogger(__name__).warning("Autostart state unavailable: %s", exc)
        status = {"state": "unavailable", "reason": str(exc)}
    return {"autostart": status["state"], "keep_running_after_close": False,
            **({"autostart_reason": status["reason"]} if status.get("reason") else {})}
