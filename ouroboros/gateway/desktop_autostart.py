"""Desktop autostart endpoints: read and set the Windows sign-in entry.

Transport only; the entry, its states and its target live in
``ouroboros.windows_autostart``. The registry is not settings.json, so these
routes stay outside the owner settings write seam; a change is still audited.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import windows_autostart
from ouroboros.gateway._helpers import json_error, request_json_or
from ouroboros.gateway.owner_settings import _owner_audit

log = logging.getLogger(__name__)


async def api_desktop_autostart_get(request: Request) -> JSONResponse:
    try:
        return JSONResponse({"state": windows_autostart.autostart_state()})
    except OSError as exc:
        log.warning("Windows sign-in entry could not be read: %s", exc)
        return json_error("Windows startup entry could not be read", 500)


async def api_desktop_autostart_post(request: Request) -> JSONResponse:
    body = await request_json_or(request, None)
    if not isinstance(body, dict) or set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
        return json_error('request body must be {"enabled": true} or {"enabled": false}', 400)
    if windows_autostart.launcher_path() is None:
        return json_error("autostart is available only in the packaged Windows desktop app", 409)
    try:
        state = windows_autostart.set_autostart(body["enabled"])
    except OSError as exc:
        log.warning("Windows sign-in entry could not be changed: %s", exc)
        return json_error("Windows startup entry could not be changed", 500)
    _owner_audit(request, "desktop_autostart", {"enabled": body["enabled"], "state": state})
    return JSONResponse({"state": state})
