"""Settings, onboarding, and Claude-runtime gateway endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import socket
import sys
from typing import Any, Dict, Optional, Sequence

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from ouroboros.config import (
    DATA_DIR,
    SETTINGS_DEFAULTS as _SETTINGS_DEFAULTS,
    apply_settings_to_env as _apply_settings_to_env,
    load_settings,
    save_settings,
)
from ouroboros.secret_masking import (
    PASSWORD_CLASS_KEYS as _PASSWORD_CLASS_KEYS,
    SECRET_SETTING_KEYS as _SECRET_SETTING_KEYS,
    looks_masked_secret as _looks_masked_secret,
    mask_password_class as _mask_password_class,
    mask_secret_value as _mask_secret_value,
)
from ouroboros.gateway._helpers import json_error, json_exception, request_drive_root
from ouroboros.onboarding_wizard import build_onboarding_html
from ouroboros.platform_layer import is_container_env
from ouroboros.provider_models import MINIMAX_REGION_ENDPOINTS, resolve_minimax_base_url
from ouroboros.server_runtime import (
    apply_runtime_provider_defaults,
    classify_runtime_provider_change,
    has_startup_ready_provider,
)
from ouroboros.settings_setup_contract import (
    BUDGET_SETTING_KEYS,
    build_setup_contract,
    parse_budget_setting,
)
from ouroboros.utils import append_jsonl, atomic_write_json, utc_now_iso

log = logging.getLogger(__name__)
DEFAULT_PORT = int(os.environ.get("OUROBOROS_SERVER_PORT", "8765"))

_CUSTOM_SECRET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

def _get_lan_ip() -> str:
    """Return LAN IP via UDP socket trick; no packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # RFC 5737 TEST-NET-1, no packet sent
            return s.getsockname()[0]
    except OSError:
        return ""


_WILDCARD_HOSTS = frozenset({"0.0.0.0", ""})


def _is_wildcard_host(host: str) -> bool:
    return host in _WILDCARD_HOSTS


def _trust_nonlocal_bind_without_password_enabled() -> bool:
    raw = os.environ.get("OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD", "")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _build_network_meta(bind_host: str, bind_port: int) -> dict:
    """Build /api/settings network metadata."""
    from ouroboros.server_auth import get_network_auth_startup_warning, is_loopback_host
    # Strip IPv6 brackets before loopback classification.
    unbracketed = bind_host[1:-1] if bind_host.startswith("[") and bind_host.endswith("]") else bind_host
    loopback = is_loopback_host(unbracketed)
    if loopback:
        return {
            "bind_host": bind_host,
            "bind_port": bind_port,
            "lan_ip": "",
            "reachability": "loopback_only",
            "recommended_url": "",
            "warning": "Server is bound to localhost — not accessible from other devices.",
        }
    wildcard = _is_wildcard_host(bind_host)
    if wildcard:
        if is_container_env():
            lan_ip = ""
        else:
            lan_ip = _get_lan_ip()
    elif bind_host in ("::", "[::]"):
        # AF_INET startup cannot advertise an IPv6 wildcard LAN IP reliably.
        lan_ip = ""
    else:
        # Use unbracketed form so URL construction can re-bracket IPv6 uniformly.
        lan_ip = unbracketed

    auth_warning = get_network_auth_startup_warning(bind_host) or ""
    if lan_ip:
        host_in_url = f"[{lan_ip}]" if ":" in lan_ip else lan_ip
        reachability = "lan_reachable"
        recommended_url = f"http://{host_in_url}:{bind_port}"
        warning = auth_warning
    else:
        reachability = "host_ip_unknown"
        recommended_url = f"http://your-host-ip:{bind_port}"
        warning = " ".join(
            part for part in [
                "Could not detect LAN IP automatically." if wildcard else "",
                auth_warning,
            ]
            if part
        )
    return {
        "bind_host": bind_host,
        "bind_port": bind_port,
        "lan_ip": lan_ip,
        "reachability": reachability,
        "recommended_url": recommended_url,
        "warning": warning,
    }


def _mask_mcp_servers_payload(servers: Any) -> list:
    if not isinstance(servers, list):
        return []
    try:
        from ouroboros.mcp_client import canonical_server_id as _mcp_canonical_id
    except Exception:
        _mcp_canonical_id = lambda value: str(value or "").strip()  # type: ignore[assignment]
    out = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        clone = dict(entry)
        if clone.get("id"):
            clone["id"] = _mcp_canonical_id(clone.get("id"))
        token = str(clone.get("auth_token") or "")
        if token:
            clone["auth_token"] = _mask_secret_value(token)
            clone["auth_configured"] = True
        else:
            clone["auth_token"] = ""
            clone["auth_configured"] = False
        out.append(clone)
    return out


def _rehydrate_mcp_servers_payload(incoming: Any, current: Any) -> list:
    if not isinstance(incoming, list):
        return []
    try:
        from ouroboros.mcp_client import canonical_server_id as _mcp_canonical_id
    except Exception:
        _mcp_canonical_id = lambda value: str(value or "").strip()  # type: ignore[assignment]
    current_by_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(current, list):
        for entry in current:
            if isinstance(entry, dict):
                cur_id = _mcp_canonical_id(entry.get("id"))
                if cur_id:
                    current_by_id[cur_id] = entry
    out = []
    for entry in incoming:
        if not isinstance(entry, dict):
            continue
        clone = dict(entry)
        clone.pop("auth_configured", None)
        if clone.get("id"):
            clone["id"] = _mcp_canonical_id(clone.get("id"))
        token = str(clone.get("auth_token") or "")
        if _looks_masked_secret(token):
            existing = current_by_id.get(_mcp_canonical_id(clone.get("id")))
            clone["auth_token"] = str((existing or {}).get("auth_token") or "")
        out.append(clone)
    return out


_IMMEDIATE_KEYS = frozenset({
    "TOTAL_BUDGET",
    "OUROBOROS_SOFT_TIMEOUT_SEC",
    "OUROBOROS_HARD_TIMEOUT_SEC",
    "OUROBOROS_TOOL_TIMEOUT_SEC",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "OUROBOROS_UPDATE_CHANNEL",
})

_RESTART_REQUIRED_KEYS = frozenset({
    "OUROBOROS_MAX_WORKERS",
    "OUROBOROS_SERVER_HOST",
    "LOCAL_MODEL_SOURCE",
    "LOCAL_MODEL_FILENAME",
    "LOCAL_MODEL_PORT",
    "LOCAL_MODEL_N_GPU_LAYERS",
    "LOCAL_MODEL_CONTEXT_LENGTH",
    "LOCAL_MODEL_CHAT_FORMAT",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
    # Region selects the MiniMax base URL (api.minimax.io vs api.minimaxi.com),
    # so it changes routing exactly like the base-URL keys above it.
    "MINIMAX_REGION",
    "GIGACHAT_SCOPE",
    "GIGACHAT_BASE_URL",
    "GIGACHAT_VERIFY_SSL_CERTS",
    # Background cognition reads these at consciousness __init__, so a change
    # only takes effect after restart (Phase 4 Evolution settings group).
    "OUROBOROS_BG_WAKEUP_MIN",
    "OUROBOROS_BG_WAKEUP_MAX",
    "OUROBOROS_BG_MAX_ROUNDS",
})


def _classify_settings_changes(
    old: Dict[str, Any],
    new: Dict[str, Any],
) -> list:
    """Return changed keys requiring process restart; others hot-reload next task."""
    return [
        k for k in _RESTART_REQUIRED_KEYS
        if str(new.get(k, "") or "") != str(old.get(k, "") or "")
    ]


def _merge_settings_payload(current: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    merged = {k: v for k, v in current.items()}
    for key in _SETTINGS_DEFAULTS:
        # Owner-only keys: loopback HTTP settings cannot set them. Runtime mode is
        # a privilege scope; context mode is a cognitive-horizon knob the agent
        # must not lower itself (BIBLE P1). Both flow through dedicated owner endpoints.
        #
        # NOTE (v6.21.0): OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS is also owner-controlled,
        # but intentionally rides this generic owner path (it is NOT merge-skipped) so
        # the Settings UI can set it without dedicated-endpoint ceremony. The agent
        # cannot self-elevate it: shell (_detect_mutative_toggle_self_change), browser
        # JS (_blocks_mutative_toggle_js), and data_write to settings.json
        # (DATA_WRITE_BLOCKED) all block agent-originated changes, and it defaults to
        # ON in advanced/pro anyway (self-enable is only meaningful in light, which
        # sandboxes live-repo writes regardless). Owner-decided tradeoff; do not
        # "promote" it to the skip-list without owner sign-off (it would break the UI).
        # NOTE: OUROBOROS_POST_TASK_EVOLUTION (the V4 envelope enable) intentionally
        # rides this generic owner path too (like ALLOW_MUTATIVE_SUBAGENTS), so the
        # Phase 4 Evolution settings UI can toggle it On/Off. The agent cannot
        # self-enable it: shell (_detect_evolution_owner_control_self_change), browser JS
        # (_blocks_post_task_evolution_js), the POST /api/settings route guard, and
        # data_write to settings.json (DATA_WRITE_BLOCKED) all block agent-originated
        # changes, and SAFETY.md forbids it. Owner-decided tradeoff; do not merge-skip it
        # (it would break the UI toggle).
        if key in {
            "OUROBOROS_RUNTIME_MODE",
            "OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS",
            "OUROBOROS_CONTEXT_MODE",
            # Derived auto-downgrade state (v6.80.0). Merge-skipped in BOTH directions:
            # setting it would fake an owner narrowing, and CLEARING it would turn a
            # system auto-downgrade into an owner-declared scope-review skip.
            "OUROBOROS_CONTEXT_MODE_AUTO_LOW",
            # CW1 (v6.34.0): the scope-review floor is owner-only and flows ONLY through
            # its dedicated audited endpoint (api_owner_scope_review_floor). Since v6.80.0
            # the value is enforcement-inert — BIBLE P3 scope-review applicability follows
            # the owner context mode — but the frozen contract surface and the owner-only
            # write path stay, so a generic settings write still cannot author it.
            "OUROBOROS_SCOPE_REVIEW_FLOOR",
            # v6.54.3: LLM-safety-supervisor coverage (full/light/off) is likewise an
            # immune-system control — a generic settings write must not lower it. It
            # flows ONLY through the dedicated audited owner endpoint
            # (api_owner_safety_mode); save_settings additionally ratchets lowering.
            "OUROBOROS_SAFETY_MODE",
        }:
            continue
        if key not in body:
            continue
        # A masked placeholder means "field untouched", never a new secret — and
        # never a credential worth persisting even when nothing is stored yet.
        # Clearing stays explicit: the UI sends "" for its Clear action.
        if key in _SECRET_SETTING_KEYS and _looks_masked_secret(body[key]):
            continue
        merged[key] = body[key]
    for key, value in body.items():
        text_key = str(key or "").strip().upper()
        if text_key in _SETTINGS_DEFAULTS or text_key == "OUROBOROS_RUNTIME_MODE":
            continue
        if not _CUSTOM_SECRET_KEY_RE.match(text_key):
            continue
        if text_key.startswith("OUROBOROS_"):
            continue
        if _looks_masked_secret(value):
            continue
        merged[text_key] = value
    return merged


def _current_bind_host(request: Request) -> str:
    return str(getattr(getattr(request.app, "state", None), "bind_host", "") or "")


def _port_file(request: Request) -> pathlib.Path:
    configured = getattr(getattr(request.app, "state", None), "port_file", None)
    return pathlib.Path(configured) if configured is not None else pathlib.Path(DATA_DIR) / "state" / "server_port"


def _default_port(request: Request) -> int:
    return int(getattr(getattr(request.app, "state", None), "default_port", DEFAULT_PORT) or DEFAULT_PORT)


def _start_supervisor_if_needed_for_request(request: Request, settings: dict) -> bool:
    callback = getattr(getattr(request.app, "state", None), "start_supervisor_if_needed", None)
    return bool(callback(settings)) if callable(callback) else False


def _owner_audit(request: Request, action: str, payload: Dict[str, Any]) -> None:
    try:
        drive_root = request_drive_root(request)
    except Exception:
        drive_root = pathlib.Path(DATA_DIR)
    try:
        client = getattr(request, "client", None)
        append_jsonl(
            drive_root / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "owner_api_action",
                "action": str(action or ""),
                "client_host": str(getattr(client, "host", "") or ""),
                **{
                    key: value
                    for key, value in dict(payload or {}).items()
                    if "key" not in str(key).lower() and "secret" not in str(key).lower()
                },
            },
        )
    except Exception:
        log.debug("Failed to write owner API audit event", exc_info=True)


# The context mode and its derived authority bit are authored together, by the owner endpoint
# or by the system auto-downgrade — never by a generic save (see prepare_settings_for_persist).
_CONTEXT_MODE_KEYS = ("OUROBOROS_CONTEXT_MODE", "OUROBOROS_CONTEXT_MODE_AUTO_LOW")


def _owner_write_settings(
    settings: Dict[str, Any],
    *,
    authored_keys: Sequence[str] = (),
    allow_context_lowering: bool = False,
    allow_safety_lowering: bool = False,
) -> None:
    """Write owner-controlled settings without applying the runtime-mode ratchet.

    Skipping that ONE ratchet is the whole reason this writer exists; everything else comes from
    ``config.prepare_settings_for_persist``, the single point both persisting writers pass through.
    An endpoint that genuinely authors a disk-authored key (context mode, safety mode, the derived
    auto-low flag) must name it in ``authored_keys`` — otherwise a POST about an unrelated key would
    author a mode decision out of the defaults merge that ``_owner_read_settings_raw`` performs."""
    from ouroboros import config as _config

    _config._guard_live_settings_write()
    to_write = _config.prepare_settings_for_persist(
        dict(settings), authored_keys=authored_keys,
        allow_context_lowering=allow_context_lowering, allow_safety_lowering=allow_safety_lowering)
    _config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = _config._acquire_settings_lock()
    try:
        atomic_write_json(_config.SETTINGS_PATH, to_write, trailing_newline=False)
    finally:
        _config._release_settings_lock(fd)


def _owner_read_settings_raw() -> Dict[str, Any]:
    """Read settings for owner endpoints without applying runtime-mode ratchets."""
    from ouroboros import config as _config

    merged = dict(_SETTINGS_DEFAULTS)
    try:
        if _config.SETTINGS_PATH.exists():
            raw = json.loads(_config.SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged.update(raw)
    except Exception:
        log.debug("Failed to read raw owner settings; using defaults", exc_info=True)
    return merged


def _has_running_agent_tasks() -> bool:
    try:
        from supervisor.workers import PENDING, RUNNING, _get_chat_agent
        if PENDING or RUNNING:
            return True
        agent = _get_chat_agent()
        return bool(getattr(agent, "_busy", False))
    except Exception:
        return False


async def api_owner_runtime_mode(request: Request) -> JSONResponse:
    """Persist the owner-selected runtime mode for the next boot."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    from ouroboros import config as _config

    raw_mode = str((body or {}).get("mode") or "").strip().lower()
    if raw_mode not in set(_config.VALID_RUNTIME_MODES):
        return json_error("'mode' must be one of: light, advanced, pro", 400)
    old_settings = _owner_read_settings_raw()
    previous_mode = _config.normalize_runtime_mode(old_settings.get("OUROBOROS_RUNTIME_MODE"))
    active_mode = _config.get_runtime_mode()
    next_mode = _config.normalize_runtime_mode(raw_mode)
    restart_required = active_mode != next_mode
    current = dict(old_settings)
    current["OUROBOROS_RUNTIME_MODE"] = next_mode
    _owner_write_settings(current)
    _owner_audit(
        request,
        "runtime_mode",
        {
            "runtime_mode": next_mode,
            "previous_runtime_mode": previous_mode,
            "active_runtime_mode": active_mode,
            "restart_required": restart_required,
        },
    )
    return JSONResponse({
        "ok": True,
        "runtime_mode": next_mode,
        "restart_required": restart_required,
    })


async def api_owner_auto_grant(request: Request) -> JSONResponse:
    """Persist the owner auto-grant toggle outside generic settings writes."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return json_error("'enabled' must be a boolean", 400)
    enabled = bool(body.get("enabled"))
    current = _owner_read_settings_raw()
    current["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] = "true" if enabled else "false"
    _owner_write_settings(current)
    os.environ["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] = current["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"]
    _owner_audit(request, "auto_grant", {"enabled": enabled})
    return JSONResponse({"ok": True, "enabled": enabled})


def _active_main_route(
    settings: Dict[str, Any],
    *,
    model_override: str = "",
    use_local_override: Optional[bool] = None,
) -> Dict[str, Any]:
    """(provider, model, base_url, use_local) for the active main model route.

    ``model_override`` / ``use_local_override`` let the task loop probe the ACTUAL
    active route at point-of-use (CW2) — a per-task ``switch_model`` / model override
    or a mid-loop local-route change — rather than only the settings-derived route."""
    from ouroboros import config as _config
    from ouroboros.provider_models import provider_for_model

    model = str(model_override or settings.get("OUROBOROS_MODEL") or _config.SETTINGS_DEFAULTS.get("OUROBOROS_MODEL") or "").strip()
    provider = provider_for_model(model)
    base_url = ""
    if provider == "openai":
        base_url = str(settings.get("OPENAI_BASE_URL") or "")
    elif provider == "openai-compatible":
        base_url = str(settings.get("OPENAI_COMPATIBLE_BASE_URL") or "")
    elif provider == "cloudru":
        base_url = str(settings.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL") or "")
    elif provider == "gigachat":
        base_url = str(settings.get("GIGACHAT_BASE_URL") or "")
    elif provider == "minimax":
        base_url = resolve_minimax_base_url(settings.get("MINIMAX_REGION") or "")
    # CW7 (v6.34.0): honour the USE_LOCAL_MAIN routing setting — a local-routed main
    # lane must report provider='local' so the Max gate consults the local n_ctx
    # (Capability Evidence local-health) instead of the remote OUROBOROS_MODEL metadata.
    use_local_main = str(settings.get("USE_LOCAL_MAIN") or "").strip().lower() in ("1", "true", "yes", "on")
    use_local = use_local_main or model.endswith(" (local)") or provider == "local"
    if use_local_override is not None:
        use_local = bool(use_local_override)
    if use_local:
        provider = "local"
    return {"provider": provider, "model": model, "base_url": base_url, "use_local": use_local}


def _max_context_block(settings: Dict[str, Any], *, allow_generative: bool = False):
    """Capability-Evidence gate for Max context mode (BIBLE P1/P3): Max requires the
    active main route to carry CONFIRMED/ASSERTED ≥1M evidence, else fail-closed.
    Returns None when Max is permitted, or a plain-language block payload dict:
      {error, needs_ack:{route, route_fp, evidence}, window_tokens:int, verified:bool}
    verified=True means the window is KNOWN and below 1M; False means it could not be
    confirmed (no provider metadata, or the probe could not reach the provider)."""
    try:
        from ouroboros.capability_evidence import probe, confirms_at_least, ONE_MILLION, STATUS_FAILED
        from ouroboros.config import DATA_DIR

        route = _active_main_route(settings)
        # Thread the in-flight key into the probe ONLY for the active route's own
        # provider (openai-compatible or minimax; first-run onboarding, where the key
        # is not yet on disk). Threading another provider's key would reach
        # LLMClient.probe_oversized_context and replace that provider's resolved key
        # on the generative probe path (cross-provider key bleed, since the
        # generative probe also runs for openai/openrouter/cloudru).
        route_api_key = None
        if route.get("provider") == "openai-compatible":
            route_api_key = str(settings.get("OPENAI_COMPATIBLE_API_KEY") or "") or None
        elif route.get("provider") == "minimax":
            route_api_key = str(settings.get("MINIMAX_API_KEY") or "") or None
        ev = probe(DATA_DIR, provider=route["provider"], model=route["model"],
                   base_url=route["base_url"], use_local=route["use_local"], allow_fetch=True,
                   allow_generative=allow_generative, api_key=route_api_key)
        if confirms_at_least(ev, ONE_MILLION):
            return None
        win = int(ev.window_tokens or 0)
        verified = win > 0  # a known window that simply is not ≥1M
        # The probe REACHED the provider but it was down (owner decision P4:
        # "no connection -> error", not a silent downgrade).
        probe_failed = (ev.status == STATUS_FAILED)
        if probe_failed:
            msg = (
                f"Couldn't reach the provider to verify {route['model']}'s context "
                "window (no connection). The model was not changed — check the "
                "connection and try again."
            )
        elif verified:
            msg = (
                f"Model {route['model']} has a confirmed context window of "
                f"~{win // 1000}K tokens — below the 1M needed for Max context mode."
            )
        else:
            msg = (
                f"Couldn't confirm a 1M context window for {route['model']} "
                "(no provider metadata for this route)."
            )
        return {
            "error": msg,
            "needs_ack": {**route, "route_fp": ev.route_fp, "evidence": ev.to_json()},
            "window_tokens": win,
            "verified": verified,
            "probe_failed": probe_failed,
        }
    except Exception as exc:  # probe machinery could not run => fail-closed (downgrade, not a connectivity error)
        return {
            "error": f"Couldn't verify this model's capability for Max context mode: {exc}",
            "needs_ack": {}, "window_tokens": 0, "verified": False, "probe_failed": False,
        }


# Settings keys a review slot's route can resolve its base URL through. Changing one
# changes the ROUTE FINGERPRINT for an unchanged model, so it must retrigger the
# capability notice exactly as a slot change does (see _review_capability_notices).
_REVIEW_ROUTE_BASE_URL_KEYS = frozenset({
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
    "GIGACHAT_BASE_URL",
    "MINIMAX_REGION",
})


def _review_slot_route(settings: Dict[str, Any], model: str) -> Dict[str, Any]:
    """(provider, model, base_url, use_local) for a REVIEW slot's own route.

    Deliberately NOT ``_active_main_route``: a review slot is pinned by its own model
    id and must never inherit the main lane's USE_LOCAL_MAIN routing."""
    from ouroboros.provider_models import provider_for_model

    provider = provider_for_model(str(model or ""))
    base_url = ""
    if provider == "openai":
        base_url = str(settings.get("OPENAI_BASE_URL") or "")
    elif provider == "openai-compatible":
        base_url = str(settings.get("OPENAI_COMPATIBLE_BASE_URL") or "")
    elif provider == "cloudru":
        base_url = str(settings.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL") or "")
    elif provider == "gigachat":
        base_url = str(settings.get("GIGACHAT_BASE_URL") or "")
    elif provider == "minimax":
        base_url = resolve_minimax_base_url(settings.get("MINIMAX_REGION") or "")
    use_local = provider == "local" or str(model or "").endswith(" (local)")
    return {
        "provider": "local" if use_local else provider,
        "model": str(model or ""),
        "base_url": base_url,
        "use_local": use_local,
    }


def _unrecognised_review_models(models: Any) -> list:
    """Review-slot model ids the provider catalog does not know (evidence-based).

    An OpenRouter-routed id that is ABSENT from a SUCCESSFULLY fetched OpenRouter
    catalog is reported loudly: a truncated slot value (e.g. ``-5``) otherwise looks
    valid on save and only surfaces later as three waves of ``400 <id> is not a valid
    model ID``, which destroys the review quorum. Nothing is rejected or rewritten —
    the fetch may be unavailable, so this is a WARNING, never a save gate."""
    try:
        from ouroboros.llm import LLMClient
        from ouroboros.provider_models import provider_for_model

        candidates = [str(m or "").strip() for m in (models or []) if str(m or "").strip()]
        openrouter = [m for m in candidates if provider_for_model(m) == "openrouter"]
        if not openrouter:
            return []
        LLMClient.openrouter_context_length(openrouter[0], allow_fetch=True)
        if not getattr(LLMClient, "_CAPABILITIES_FETCH_OK", False):
            return []  # no authoritative catalog -> cannot claim anything is unknown
        known = getattr(LLMClient, "_CONTEXT_LENGTH_CACHE", {}) or {}
        return [m for m in openrouter if m not in known]
    except Exception:
        return []


def _review_capability_notices(settings: Dict[str, Any]) -> list:
    """Owner-facing Capability Evidence notices for the configured review slots.

    The Max-context gate only ever probed the MAIN route, so a PINNED scope reviewer
    could not become "known" through any path and silently ran with the conservative
    sub-floor window — exactly the failure the owner hit. Saving settings now probes
    the review + scope-review slots too and returns the SAME
    ``needs_ack:{route, route_fp, evidence}`` contract the Max gate already uses, and
    ``settings.js`` renders it through the SAME confirm -> owner-capability-ack flow.
    Advisory only: a slot without evidence stays fail-closed at review time (the pin is
    routing intent, never evidence) — this just makes "known" reachable.

    ONLY the scope-review surface is probed: it is the one surface whose >=1M evidence
    gates anything, so probing the triad slots was network work whose result was
    discarded. The caller gates this on a ROUTE-AFFECTING change (the scope slot itself
    or any base URL that route resolves through), not every settings save: capability is
    a property of provider+base_url+model, so a hot base-URL change produces a route with
    no evidence exactly as a model change does.

    The slot is read from the CANDIDATE settings, not from ``get_scope_review_models()``:
    that reads process env, which is not necessarily the value being saved, so the notice
    could describe the outgoing route instead of the incoming one."""
    notices: list = []
    try:
        from ouroboros.capability_evidence import ONE_MILLION, confirms_at_least, probe
        from ouroboros.config import DATA_DIR, get_scope_review_models

        candidates = [
            m for m in str(
                settings.get("OUROBOROS_SCOPE_REVIEW_MODELS")
                or settings.get("OUROBOROS_SCOPE_REVIEW_MODEL")
                or ""
            ).replace(",", " ").split() if m
        ] or list(get_scope_review_models() or [])
        seen: set = set()
        for model in candidates:
            if str(model) in seen:
                continue
            seen.add(str(model))
            route = _review_slot_route(settings, str(model))
            ev = probe(
                DATA_DIR, provider=route["provider"], model=route["model"],
                base_url=route["base_url"], use_local=route["use_local"],
                allow_fetch=True, allow_generative=False,
            )
            if not confirms_at_least(ev, ONE_MILLION):
                notices.append({
                    "surface": "scope_review",
                    "needs_ack": {**route, "route_fp": ev.route_fp, "evidence": ev.to_json()},
                    "window_tokens": int(ev.window_tokens or 0),
                    "verified": int(ev.window_tokens or 0) > 0,
                })
    except Exception:
        log.debug("review capability probe skipped", exc_info=True)
    return notices


def _active_route_confirms_max(
    settings: Optional[Dict[str, Any]] = None,
    *,
    model: str = "",
    use_local: Optional[bool] = None,
    allow_fetch: bool = False,
) -> Optional[bool]:
    """Return True/False for known route capacity and None when it is unknown.

    CW2 (v6.34.0): does the active main route carry confirmed/asserted >=1M
    Capability Evidence RIGHT NOW? ``model`` / ``use_local`` pin the probe to the
    loop's ACTUAL active route (a task model override or a local main lane, CW7) —
    local routes are probed for their local n_ctx, never skipped. Complements the
    settings-save gate (checks at write time) and the reactive provider-overflow
    fallback (recovers after a rejection). Fail-closed on any error.

    ``allow_fetch`` (v6.39, H): the read-only hot path passes False (no network).
    The ONCE-PER-TASK start-of-loop gate passes True — a LAZY probe-on-first-use so
    a genuine >=1M route is actually confirmed when CONTEXT_MODE=max is the default
    and the owner never toggled Low->Max in the UI (the only path that previously
    wrote evidence). The fetch is self-limiting: ``probe`` returns cached evidence
    within its TTL (confirmed 24h / failed 10m) without refetching, and writes the
    SHARED global DATA_DIR store, so concurrent subagents share one probe rather than
    stampeding. Unknown is deliberately distinct from confirmed sub-1M so the
    ordinary task path may attempt Max once and react only to real overflow."""
    try:
        from ouroboros.capability_evidence import ONE_MILLION, probe
        from ouroboros.config import DATA_DIR

        s = settings if isinstance(settings, dict) else _owner_read_settings_raw()
        route = _active_main_route(s, model_override=model, use_local_override=use_local)
        ev = probe(
            DATA_DIR, provider=route["provider"], model=route["model"],
            base_url=route["base_url"], use_local=route["use_local"], allow_fetch=allow_fetch,
        )
        known = (
            str(getattr(ev, "status", "") or "") in {"confirmed", "asserted"}
            and not bool(getattr(ev, "stale", False))
            and int(getattr(ev, "window_tokens", 0) or 0) > 0
        )
        if not known:
            return None
        return int(ev.window_tokens or 0) >= ONE_MILLION
    except Exception:
        return None


def _apply_max_context_auto_downgrade(
    current: Dict[str, Any],
    old_effective_settings: Dict[str, Any],
) -> tuple:
    """Narrow Max->Low IN PLACE when a model change lands on an unverified route.

    Returns ``(notice, probe_error)``: at most one is set. ``probe_error`` means the
    provider could not be reached at all and the caller must 503 WITHOUT saving.

    Max-mode is fail-closed (BIBLE P1/P3). The low->max TOGGLE is gated by
    api_owner_context_mode, but a model/provider CHANGE while already in Max must not
    silently keep Max on an unverified (sub-1M / unknown) route. Owner decision
    (v6.33.0 WS11): changing models stays FRICTION-FREE — the model change ALWAYS
    succeeds; if the new route can't be confirmed >=1M the context mode
    AUTO-DOWNGRADES to Low with a plain notice (never a 409 that blocks the save).
    Every uncertainty resolves CLOSED (-> Low)."""
    from ouroboros.config import get_context_mode

    try:
        in_max = get_context_mode() == "max"
    except Exception:
        in_max = True  # cannot determine the mode -> assume max, re-gate
    if not in_max:
        return None, None
    def _route_key(r):
        return (r["provider"], r["model"], r["base_url"], r["use_local"])
    try:
        route_changed = (
            _route_key(_active_main_route(current))
            != _route_key(_active_main_route(old_effective_settings))
        )
    except Exception:
        route_changed = True  # cannot compare routes -> assume changed, re-gate
    if not route_changed:
        return None, None
    block = _max_context_block(current, allow_generative=True)  # fail-closed internally
    if block is None:
        return None, None
    if block.get("probe_failed"):
        # Owner decision P4: a genuine NO-CONNECTION during the probe is an ERROR, not
        # a silent downgrade — and the model is NOT saved. (A sub-1M/unprobeable route
        # still auto-downgrades.)
        return None, str(
            block.get("error")
            or "Couldn't reach the provider to verify the model's context window."
        )
    current["OUROBOROS_CONTEXT_MODE"] = "low"
    os.environ["OUROBOROS_CONTEXT_MODE"] = "low"
    # This narrowing is SYSTEM-initiated on an AGENT-REACHABLE path (a plain
    # {"OUROBOROS_MODEL": ...} POST names neither the context key nor settings.json, so
    # the self-lowering shell guard cannot see it). Since v6.80.0 the context mode also
    # decides whether the BIBLE P3 blocking scope review applies, so the auto-low is
    # marked as derived: get_owner_context_mode keeps reporting the OWNER's selection,
    # and scope review stays ON. Context sizing is unchanged.
    current["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "true"
    os.environ["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "true"
    return (
        str(block.get("error") or "")
        + " Context mode switched to Low. To use Max with this model, confirm it "
          "supports a 1M-token context window."
    ), None


async def api_owner_context_mode(request: Request) -> JSONResponse:
    """Persist the owner-selected context mode (low/max).

    Owner-only like runtime mode, but NOT boot-pinned: it hot-applies on the next
    task (mirrors the auto-grant toggle), so no restart is required.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    from ouroboros import config as _config

    raw_mode = str((body or {}).get("mode") or "").strip().lower()
    if raw_mode not in set(_config.VALID_CONTEXT_MODES):
        return json_error("'mode' must be one of: low, max", 400)
    next_mode = _config.normalize_context_mode(raw_mode)
    previous_mode = _config.get_context_mode()
    if previous_mode == "max" and next_mode == "low" and _has_running_agent_tasks():
        return json_error(
            "Context mode can only be lowered while Ouroboros is idle. "
            "Wait until no queued or running work remains, then switch Low/Max.",
            409,
        )
    current = _owner_read_settings_raw()
    # Hard-block ENABLING max unless the active route's >=1M is confirmed/acked.
    if next_mode == "max" and previous_mode != "max":
        block = _max_context_block(current, allow_generative=True)
        if block is not None:
            return JSONResponse({"ok": False, "context_mode": previous_mode, **block}, status_code=409)
    current["OUROBOROS_CONTEXT_MODE"] = next_mode
    # An explicit owner selection re-authors the value: the derived auto-downgrade flag
    # is cleared, so `low` chosen HERE really does mean "scope review not performed".
    current["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    # This endpoint IS the author of both keys, so they persist even at the shipped default.
    _owner_write_settings(current, authored_keys=_CONTEXT_MODE_KEYS, allow_context_lowering=True)
    os.environ["OUROBOROS_CONTEXT_MODE"] = next_mode
    os.environ["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    _owner_audit(
        request,
        "context_mode",
        {"context_mode": next_mode, "previous_context_mode": previous_mode},
    )
    return JSONResponse({"ok": True, "context_mode": next_mode})


_SCOPE_REVIEW_FLOOR_DEPRECATION_NOTICE = (
    "OUROBOROS_SCOPE_REVIEW_FLOOR is DEPRECATED since v6.80.0 and no longer affects "
    "anything: your value is stored, but whether the BIBLE P3 blocking scope review runs "
    "is decided solely by the owner-only context mode (max = blocking scope gate, low = "
    "whole-repository scope review declaredly not performed). Use POST "
    "/api/owner/context-mode to change that."
)


async def api_owner_scope_review_floor(request: Request) -> JSONResponse:
    """Persist the owner-selected P3 scope-review floor (blocking_1m | advisory).

    Owner-only + audited (CW1, v6.34.0): merge-skipped from the generic /api/settings
    path, so ONLY this dedicated endpoint may author it.

    DEPRECATED and ENFORCEMENT-INERT since v6.80.0: nothing in the runtime consults the
    stored value — scope-review applicability comes solely from
    ``config.get_owner_context_mode()``. The endpoint is kept because the gateway contract
    surface is frozen and because an owner customization is never destroyed: the write is
    accepted, stored, audited, and answered with an explicit deprecation notice naming the
    control that actually decides."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = str((body or {}).get("floor") or "").strip().lower()
    if raw not in {"blocking_1m", "advisory"}:
        return json_error("'floor' must be one of: blocking_1m, advisory", 400)
    current = _owner_read_settings_raw()
    previous = str(current.get("OUROBOROS_SCOPE_REVIEW_FLOOR") or "blocking_1m").strip().lower()
    current["OUROBOROS_SCOPE_REVIEW_FLOOR"] = raw
    _owner_write_settings(current)
    os.environ["OUROBOROS_SCOPE_REVIEW_FLOOR"] = raw
    _owner_audit(
        request,
        "scope_review_floor",
        {
            "scope_review_floor": raw,
            "previous_scope_review_floor": previous,
            "deprecated": True,
        },
    )
    return JSONResponse({
        "ok": True,
        "scope_review_floor": raw,
        "deprecation_notice": _SCOPE_REVIEW_FLOOR_DEPRECATION_NOTICE,
    })


async def api_owner_safety_mode(request: Request) -> JSONResponse:
    """Persist the owner-selected LLM-safety-supervisor coverage (full | light | off).

    Owner-only + audited (v6.54.3): safety coverage is an immune-system control, so
    it is merge-skipped from the generic /api/settings path and its lowering is
    ratcheted in save_settings — ONLY this dedicated, audited endpoint may lower it.
    The deterministic registry sandbox, protected paths, and light-mode guards run
    in every mode (BIBLE P3: the LLM supervisor is a layer, not the floor)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    from ouroboros import config as _config

    raw_mode = str((body or {}).get("mode") or "").strip().lower()
    if raw_mode not in set(_config.VALID_SAFETY_MODES):
        return json_error("'mode' must be one of: full, light, off", 400)
    current = _owner_read_settings_raw()
    previous = _config.normalize_safety_mode(current.get("OUROBOROS_SAFETY_MODE"))
    current["OUROBOROS_SAFETY_MODE"] = raw_mode
    _owner_write_settings(
        current, authored_keys=("OUROBOROS_SAFETY_MODE",), allow_safety_lowering=True)
    os.environ["OUROBOROS_SAFETY_MODE"] = raw_mode
    _owner_audit(
        request,
        "safety_mode",
        {"safety_mode": raw_mode, "previous_safety_mode": previous},
    )
    return JSONResponse({"ok": True, "safety_mode": raw_mode})


async def api_acknowledge_capability(request: Request) -> JSONResponse:
    """Record a route-fingerprinted owner acknowledgement of a model's context
    window (Capability Evidence: ASSERTED). Auditable and NON-generic — it covers
    only the exact provider+model+base_url+headers/options it was issued for, and
    is invalidated by any route change. CI/headless may supply the same ack via
    config, but it must carry the same fingerprint (no repo-wide trust flag)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    provider = str((body or {}).get("provider") or "").strip()
    model = str((body or {}).get("model") or "").strip()
    if not provider or not model:
        return json_error("'provider' and 'model' are required", 400)
    try:
        window_tokens = int((body or {}).get("window_tokens") or 0)
    except (TypeError, ValueError):
        window_tokens = 0
    if window_tokens <= 0:
        return json_error("'window_tokens' must be a positive integer", 400)
    try:
        from ouroboros.capability_evidence import record_owner_ack
        record = record_owner_ack(
            request_drive_root(request),
            provider=provider, model=model,
            base_url=str((body or {}).get("base_url") or ""),
            window_tokens=window_tokens,
            headers=(body or {}).get("headers") if isinstance((body or {}).get("headers"), dict) else None,
            options=(body or {}).get("options") if isinstance((body or {}).get("options"), dict) else None,
            note=str((body or {}).get("note") or ""),
        )
        _owner_audit(request, "capability_ack", {"route_fp": record.get("route_fp"), "window_tokens": window_tokens, "model": model})
        return JSONResponse({"ok": True, "ack": record})
    except Exception as exc:
        return json_exception(exc)


def _claude_code_status_payload() -> Dict[str, Any]:
    """Return app-managed Claude runtime status, versions, readiness, and stderr."""
    from ouroboros.platform_layer import resolve_claude_runtime

    rt = resolve_claude_runtime()
    label = rt.status_label()

    stderr_tail = ""
    try:
        from ouroboros.gateways.claude_code import get_last_stderr as gw_stderr
        stderr_tail = gw_stderr(max_chars=2000)
    except Exception:
        pass

    message_map = {
        "ready": f"Claude runtime ready (SDK {rt.sdk_version}, CLI {rt.cli_version})",
        "no_api_key": f"Claude runtime available (SDK {rt.sdk_version}) but ANTHROPIC_API_KEY is not set. Add it in Settings.",
        "error": f"Claude runtime error: {rt.error}",
        "degraded": f"Claude runtime degraded (SDK {rt.sdk_version}, CLI {'found' if rt.cli_path else 'missing'}). Try Repair.",
        "missing": "Claude runtime not available. Use Repair in Settings or reinstall the app.",
    }

    return {
        "status": label,
        "installed": bool(rt.sdk_version),
        "ready": rt.ready,
        "busy": False,
        "version": rt.sdk_version,
        "cli_version": rt.cli_version,
        "cli_path": rt.cli_path,
        "interpreter_path": rt.interpreter_path,
        "app_managed": rt.app_managed,
        "legacy_detected": rt.legacy_detected,
        "legacy_sdk_version": rt.legacy_sdk_version,
        "api_key_set": rt.api_key_set,
        "message": message_map.get(label, f"Claude runtime: {label}"),
        "error": rt.error,
        "stderr_tail": stderr_tail,
    }


async def api_settings_get(request: Request) -> JSONResponse:
    settings, _, _ = apply_runtime_provider_defaults(load_settings())
    safe = {k: v for k, v in settings.items()}
    for key in _SECRET_SETTING_KEYS:
        if safe.get(key):
            safe[key] = (
                _mask_password_class(safe[key])
                if key in _PASSWORD_CLASS_KEYS
                else _mask_secret_value(safe[key])
            )
    safe["MCP_SERVERS"] = _mask_mcp_servers_payload(safe.get("MCP_SERVERS") or [])
    for key, value in list(safe.items()):
        if key in _SECRET_SETTING_KEYS or key in _SETTINGS_DEFAULTS:
            continue
        if _CUSTOM_SECRET_KEY_RE.match(str(key)) and value:
            safe[key] = _mask_secret_value(value)
    try:
        port = int(_port_file(request).read_text().strip()) if _port_file(request).exists() else _default_port(request)
    except (ValueError, OSError):
        port = _default_port(request)
    meta = _build_network_meta(_current_bind_host(request), port)
    meta["custom_secret_keys"] = sorted(
        key for key in settings
        if key not in _SECRET_SETTING_KEYS
        and key not in _SETTINGS_DEFAULTS
        and _CUSTOM_SECRET_KEY_RE.match(str(key))
        and settings.get(key)
    )
    meta["setup_contract"] = build_setup_contract("web")
    safe["_meta"] = meta
    return JSONResponse(safe)


async def api_onboarding(request: Request) -> Response:
    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings, allow_elevation=True)
    if has_startup_ready_provider(settings):
        return Response(status_code=204)
    return HTMLResponse(build_onboarding_html(settings, host_mode="web"))


async def api_claude_code_status(request: Request) -> JSONResponse:
    try:
        payload = await asyncio.to_thread(_claude_code_status_payload)
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "installed": False,
            "busy": False,
            "message": "Failed to read Claude Agent SDK status.",
            "error": str(e),
        }, status_code=500)


async def api_claude_code_install(request: Request) -> JSONResponse:
    """Repair/update Claude runtime using the app-managed interpreter."""
    try:
        import subprocess as _sp
        import sys as _sys

        interpreter = _sys.executable
        try:
            from ouroboros.platform_layer import resolve_claude_runtime
            rt = resolve_claude_runtime()
            if rt.interpreter_path:
                interpreter = rt.interpreter_path
        except Exception:
            pass

        # Import SDK baseline at call time: one SSOT, clean endpoint error if broken.
        from ouroboros.launcher_bootstrap import _CLAUDE_SDK_BASELINE as sdk_baseline

        result = await asyncio.to_thread(
            lambda: _sp.run(
                [interpreter, "-m", "pip", "install", "--upgrade", sdk_baseline],
                capture_output=True, text=True, timeout=120,
            )
        )
        if result.returncode == 0:
            payload = await asyncio.to_thread(_claude_code_status_payload)
            payload["repaired"] = True
            return JSONResponse(payload)
        return JSONResponse({
            "status": "error",
            "installed": False,
            "ready": False,
            "busy": False,
            "message": "Claude runtime repair failed.",
            "error": (result.stderr or result.stdout or "")[:500],
        }, status_code=500)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "installed": False,
            "ready": False,
            "busy": False,
            "message": "Claude runtime repair failed.",
            "error": f"{type(e).__name__}: {e}",
        }, status_code=500)


async def api_settings_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return json_error("JSON body must be an object.", 400)
        channel_key = "OUROBOROS_UPDATE_CHANNEL"
        if channel_key in body:
            from ouroboros.update_channels import UPDATE_CHANNEL_BRANCHES

            raw_channel = str(body.get(channel_key) or "").strip().lower()
            if raw_channel not in UPDATE_CHANNEL_BRANCHES:
                return json_error(
                    f"{channel_key} must be one of: stable, qa, development.", 400
                )
            body = dict(body)
            body[channel_key] = raw_channel
        # Reject a malformed post-task evolution cadence at the API boundary: the
        # read-time getter only normalizes, and the Settings UI validates its own Save,
        # but a direct API client must not be able to persist e.g. every_n:0 or garbage.
        cadence_key = "OUROBOROS_POST_TASK_EVOLUTION_CADENCE"
        if cadence_key in body:
            from ouroboros import config as _config
            raw_cadence = str(body.get(cadence_key) or "").strip()
            if raw_cadence and not _config.is_valid_post_task_evolution_cadence(raw_cadence):
                return json_error(f"{cadence_key} must be one of: off, llm, every_n:<positive int>.", 400)
        parsed_budget: dict[str, float] = {}
        for budget_key in BUDGET_SETTING_KEYS:
            if budget_key not in body:
                continue
            budget_value, budget_error = parse_budget_setting(budget_key, body.get(budget_key))
            if budget_error:
                return json_error(budget_error, 400)
            if budget_value is not None:
                parsed_budget[budget_key] = budget_value
        if parsed_budget:
            body = dict(body)
            body.update(parsed_budget)
        old_settings = load_settings()
        from ouroboros.config import get_runtime_mode, normalize_runtime_mode as _norm_runtime_mode

        raw_old_settings = _owner_read_settings_raw()
        pending_runtime_mode = _norm_runtime_mode(
            raw_old_settings.get("OUROBOROS_RUNTIME_MODE", old_settings.get("OUROBOROS_RUNTIME_MODE"))
        )
        current_runtime_mode = get_runtime_mode()
        old_effective_settings = dict(old_settings)
        old_effective_settings["OUROBOROS_RUNTIME_MODE"] = current_runtime_mode
        if "MCP_SERVERS" in body:
            body = dict(body)
            body["MCP_SERVERS"] = _rehydrate_mcp_servers_payload(
                body.get("MCP_SERVERS"),
                old_settings.get("MCP_SERVERS"),
            )
        current = _merge_settings_payload(old_effective_settings, body)
        minimax_region = str(current.get("MINIMAX_REGION") or "").strip().lower()
        if minimax_region and minimax_region not in MINIMAX_REGION_ENDPOINTS:
            return json_error("MINIMAX_REGION must be global_en or cn_zh.", 400)
        current["MINIMAX_REGION"] = minimax_region
        # Generic settings saves operate on the current boot baseline. A pending
        # next-boot mode written by /api/owner/runtime-mode is preserved on disk
        # below, but never hot-applied to this process/env.
        current["OUROBOROS_RUNTIME_MODE"] = current_runtime_mode
        # Trim opaque path text so configured/empty state is deterministic.
        current["OUROBOROS_SKILLS_REPO_PATH"] = str(
            current.get("OUROBOROS_SKILLS_REPO_PATH") or ""
        ).strip()
        try:
            from ouroboros.server_auth import is_loopback_host
            desired_host = str(current.get("OUROBOROS_SERVER_HOST") or "").strip()
            desired_password = str(current.get("OUROBOROS_NETWORK_PASSWORD") or "").strip()
            trust_unauth = _trust_nonlocal_bind_without_password_enabled()
            allowed_saved_hosts = {"", "127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0", "::", "[::]"}
            if desired_host and desired_host not in allowed_saved_hosts:
                return json_error(
                    "Server Bind Host in Settings supports localhost or wildcard "
                    "binds only (127.0.0.1 or 0.0.0.0). Specific LAN IP binds "
                    "are manual/env-only so the desktop launcher can keep using "
                    "a reliable loopback health check.",
                    400,
                )
            if desired_host and not is_loopback_host(desired_host) and not desired_password and not trust_unauth:
                return json_error(
                    "Setting a non-localhost Server Bind Host through the web UI "
                    "requires a Network Password in the same save. For manual "
                    "trusted-lab/Docker setups, stop Ouroboros and edit "
                    "settings.json or environment variables directly.",
                    400,
                )
            current_effective_host = (
                str(_current_bind_host(request) or "").strip()
                or str(os.environ.get("OUROBOROS_SERVER_HOST") or "").strip()
            )
            old_password = str(old_settings.get("OUROBOROS_NETWORK_PASSWORD") or "").strip()
            if (
                current_effective_host
                and not is_loopback_host(current_effective_host)
                and old_password
                and not desired_password
                and not trust_unauth
            ):
                return json_error(
                    "Cannot clear Network Password while the running server is "
                    "still bound to a non-localhost interface. First save a "
                    "loopback Server Bind Host and restart, then clear the password.",
                    400,
                )
        except Exception:
            log.warning("Could not validate network bind settings", exc_info=True)
        current, provider_defaults_changed, provider_default_keys = apply_runtime_provider_defaults(current)
        if str(current.get("LOCAL_MODEL_SOURCE", "") or "").strip() and not has_startup_ready_provider(current):
            return json_error("Local-only setups must route at least one model to the local runtime.", 400)
        # Fail-closed Max narrowing on a model/route change (see the helper): the save
        # always succeeds, but an unverified route drops context sizing to Low, and an
        # unreachable provider is a 503 that does NOT persist the model.
        _max_downgrade_notice, _max_probe_error = _apply_max_context_auto_downgrade(
            current, old_effective_settings
        )
        if _max_probe_error:
            return json_error(_max_probe_error, 503)
        all_changed = [
            k for k in current
            if str(current.get(k, "") or "") != str(old_effective_settings.get(k, "") or "")
        ]
        restart_keys = _classify_settings_changes(old_effective_settings, current)

        settings_to_save = dict(current)
        settings_to_save["OUROBOROS_RUNTIME_MODE"] = pending_runtime_mode
        # The Max->Low auto-downgrade above is an owner-endpoint, system-initiated
        # lowering (the new model can't sustain Max), so it is allowed past the
        # cognitive-horizon guard; an ordinary save never lowers context mode.
        # The generic POST authors these keys ONLY when the auto-downgrade above actually fired;
        # a save about a model slot must not author a context mode out of the defaults merge.
        _owner_write_settings(
            settings_to_save,
            authored_keys=_CONTEXT_MODE_KEYS if _max_downgrade_notice else (),
            allow_context_lowering=bool(_max_downgrade_notice))
        _apply_settings_to_env(current)
        _start_supervisor_if_needed_for_request(request, current)

        if any(k in all_changed for k in ("MCP_ENABLED", "MCP_SERVERS", "MCP_TOOL_TIMEOUT_SEC")):
            try:
                from ouroboros.mcp_client import (
                    reconfigure_from_settings as _mcp_reconfigure,
                    refresh_all_background as _mcp_refresh_background,
                )
                _mcp_reconfigure(current)
                _mcp_refresh_background(reason="settings")
            except Exception:
                log.warning("MCP reconfigure after settings change failed", exc_info=True)

        # Skills repo/runtime changes require extension loader reconciliation.
        try:
            from ouroboros.extension_loader import reload_all as _reload_extensions
            new_path = str(current.get("OUROBOROS_SKILLS_REPO_PATH") or "").strip()
            old_path = str(old_effective_settings.get("OUROBOROS_SKILLS_REPO_PATH") or "").strip()
            new_runtime_mode = str(current.get("OUROBOROS_RUNTIME_MODE") or "").strip()
            old_runtime_mode = str(old_effective_settings.get("OUROBOROS_RUNTIME_MODE") or "").strip()
            if new_path != old_path or new_runtime_mode != old_runtime_mode:
                # Use load_settings so extensions do not capture a stale snapshot.
                from ouroboros.config import load_settings as _load_settings
                reload_drive_root = pathlib.Path(
                    request.app.state.drive_root
                    if hasattr(request.app, "state") and hasattr(request.app.state, "drive_root")
                    else request_drive_root(request)
                )
                if (
                    (bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules)
                    and reload_drive_root == pathlib.Path.home() / "Ouroboros" / "data"
                    and not os.environ.get("OUROBOROS_DATA_DIR")
                ):
                    log.info("Skipping extension reload_all against real DATA_DIR during pytest settings save")
                else:
                    _reload_extensions(
                        reload_drive_root,
                        _load_settings,
                        repo_path=new_path or None,
                    )
        except Exception:
            log.error("Extension reload after settings change failed", exc_info=True)

        try:
            from supervisor.state import refresh_budget_from_settings
            refresh_budget_from_settings(current)
        except Exception:
            pass
        try:
            from supervisor.queue import refresh_timeouts_from_settings
            refresh_timeouts_from_settings(current)
        except Exception:
            pass
        try:
            from supervisor.message_bus import refresh_budget_limit
            raw_budget = current.get("TOTAL_BUDGET")
            new_budget = float(raw_budget) if raw_budget is not None else 0.0
            refresh_budget_limit(new_budget)
        except Exception:
            pass

        warnings = []
        if provider_defaults_changed:
            change_kind = classify_runtime_provider_change(old_effective_settings, current)
            if change_kind == "direct_normalize":
                warnings.append(
                    "Normalized direct-provider routing because OpenRouter is not configured for the active provider."
                )
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().configure_from_settings(current)
        except Exception:
            pass
        try:
            from ouroboros.server_auth import is_loopback_host
            desired_host = str(current.get("OUROBOROS_SERVER_HOST") or "").strip()
            desired_password = str(current.get("OUROBOROS_NETWORK_PASSWORD") or "").strip()
            if desired_host and not is_loopback_host(desired_host) and not desired_password:
                if _trust_nonlocal_bind_without_password_enabled():
                    warnings.append(
                        "OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1 allows this "
                        "non-localhost bind without Ouroboros's internal Network Password. "
                        "Use only behind ingress auth, VPN, private networking, or an auth proxy."
                    )
                else:
                    warnings.append(
                        "Server Bind Host is non-localhost and Network Password is empty; "
                        "after restart the app will be reachable on the network without a password."
                    )
        except Exception:
            pass
        _repo_slug = current.get("GITHUB_REPO", "")
        _gh_token = current.get("GITHUB_TOKEN", "")
        if _gh_token and any(k in all_changed for k in ("GITHUB_REPO", "GITHUB_TOKEN")):
            from supervisor.git_ops import configure_personal_remote
            remote_ok, remote_msg, resolved_slug = configure_personal_remote(
                _repo_slug,
                _gh_token,
                auto_fork=not bool(str(_repo_slug or "").strip()),
                confirm_replace_origin=bool(body.get("GITHUB_REPLACE_ORIGIN_CONFIRMED")),
            )
            if not remote_ok:
                log.warning("Remote configuration failed on settings save: %s", remote_msg)
                warnings.append(f"Remote config failed: {remote_msg}")
            elif resolved_slug and resolved_slug != _repo_slug:
                current["GITHUB_REPO"] = resolved_slug
                settings_to_save["GITHUB_REPO"] = resolved_slug
                _owner_write_settings(settings_to_save)
                os.environ["GITHUB_REPO"] = resolved_slug
        immediate_changed = [k for k in all_changed if k in _IMMEDIATE_KEYS]
        next_task_changed = [
            k for k in all_changed
            if k not in _IMMEDIATE_KEYS and k not in _RESTART_REQUIRED_KEYS
        ]
        resp: Dict[str, Any] = {"status": "saved"}
        if not all_changed:
            resp["no_changes"] = True
        if restart_keys:
            resp["restart_required"] = True
            resp["restart_keys"] = restart_keys
        if immediate_changed:
            resp["immediate_changed"] = True
        if next_task_changed:
            resp["next_task_changed"] = True
        if warnings:
            resp["warnings"] = warnings
        if _max_downgrade_notice:
            resp["context_mode"] = "low"
            resp["context_mode_downgraded"] = True
            resp["notice"] = _max_downgrade_notice
        if any(k.startswith("OUROBOROS_SCOPE_REVIEW_MODEL") or k == "OUROBOROS_REVIEW_MODELS"
               for k in all_changed):
            _unknown = _unrecognised_review_models(
                str(current.get("OUROBOROS_SCOPE_REVIEW_MODELS") or "").replace(",", " ").split()
                + str(current.get("OUROBOROS_REVIEW_MODELS") or "").replace(",", " ").split()
            )
            if _unknown:
                warnings.append(
                    "Unrecognised review model id(s) the provider catalog does not list: "
                    + ", ".join(sorted(set(_unknown)))
                    + ". Review calls to these slots will fail with 'not a valid model ID' "
                    "and can break the review quorum — check for a truncated value."
                )
                resp["warnings"] = warnings
        # Capability is a property of the whole ROUTE (provider + base_url + model), and
        # the lazy scope probe memoises by that fingerprint. A base-URL change therefore
        # produces an unprobed route exactly as a model change does; gating notices on
        # the model alone left the next scope review at the conservative sub-floor with
        # the advertised owner-ack path unreachable.
        if any(
            k.startswith("OUROBOROS_SCOPE_REVIEW_MODEL") or k in _REVIEW_ROUTE_BASE_URL_KEYS
            for k in all_changed
        ):
            _capability_notices = _review_capability_notices(current)
            if _capability_notices:
                resp["review_capability_notices"] = _capability_notices
        return JSONResponse(resp)
    except Exception as e:
        return json_exception(e, 400)
