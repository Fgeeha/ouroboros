"""Provider secrets must survive a masked round-trip through /api/settings.

The Settings UI loads masked secrets, so a plain "Save Settings" with an
untouched field posts the mask back. Persisting that mask replaces the working
credential with a placeholder, and every consumer (env apply, provider catalog,
capability probe) then sends it as an ``Authorization`` value — which is what
an OpenAI-compatible gateway rejects with "expected to start with 'sk-'".

Values here are fixtures, never real credentials.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.secret_masking import looks_masked_secret

REAL_KEY = "sk-original-secret"
# Every placeholder shape the UI or the API can put in a secret field.
MASKS = ("**", "***", "********", "***set***", "sk-origi...")


@pytest.fixture()
def settings_client(tmp_path):
    """TestClient over the real GET/POST settings endpoints with an in-memory disk."""
    import server as srv

    on_disk: Dict[str, Any] = {
        "OPENAI_COMPATIBLE_API_KEY": REAL_KEY,
        "OPENAI_COMPATIBLE_BASE_URL": "http://127.0.0.1:4000/v1",
        "OPENROUTER_API_KEY": "sk-or-original",
        "OPENAI_API_KEY": "sk-openai-original",
        "ANTHROPIC_API_KEY": "sk-ant-original",
        "GITHUB_TOKEN": "ghp-original",
    }
    saved: Dict[str, Any] = {}

    def fake_load_settings():
        return copy.deepcopy(on_disk)

    def fake_save_settings(settings, *_a, **_k):
        saved.clear()
        saved.update(settings)
        on_disk.update(settings)

    patches = [
        patch.object(srv, "_start_supervisor_if_needed", lambda *a, **k: None),
        patch.object(srv, "_apply_settings_to_env", lambda *a, **k: None),
        patch.object(srv, "apply_runtime_provider_defaults", lambda s: (dict(s), False, [])),
        patch.object(srv, "load_settings", side_effect=fake_load_settings),
        patch.object(srv, "save_settings", side_effect=fake_save_settings),
        patch.object(srv._gateway_settings, "apply_runtime_provider_defaults", lambda s: (dict(s), False, [])),
        patch.object(srv._gateway_settings, "_apply_max_context_auto_downgrade", lambda *a, **k: ("", "")),
        patch.object(srv._gateway_settings, "_owner_read_settings_raw", side_effect=fake_load_settings),
        patch.object(srv._gateway_settings, "_owner_write_settings", side_effect=fake_save_settings),
        patch("ouroboros.server_auth.get_configured_network_password", return_value=""),
    ]
    for p in patches:
        p.start()
    try:
        app = Starlette(routes=[
            Route("/api/settings", endpoint=srv.api_settings_get, methods=["GET"]),
            Route("/api/settings", endpoint=srv.api_settings_post, methods=["POST"]),
        ])
        app.state.drive_root = tmp_path
        app.state.repo_dir = tmp_path
        with TestClient(app) as client:
            yield client, on_disk, saved
    finally:
        for p in patches:
            p.stop()


def test_get_masks_the_stored_key(settings_client):
    client, _on_disk, _saved = settings_client
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert REAL_KEY not in resp.text
    assert looks_masked_secret(resp.json()["OPENAI_COMPATIBLE_API_KEY"])


def test_reposting_the_served_mask_keeps_the_real_key(settings_client):
    """The exact UI flow: load, save without touching the field."""
    client, on_disk, saved = settings_client
    mask = client.get("/api/settings").json()["OPENAI_COMPATIBLE_API_KEY"]

    resp = client.post("/api/settings", json={"OPENAI_COMPATIBLE_API_KEY": mask})

    assert resp.status_code == 200, resp.text
    assert saved["OPENAI_COMPATIBLE_API_KEY"] == REAL_KEY
    assert on_disk["OPENAI_COMPATIBLE_API_KEY"] == REAL_KEY


@pytest.mark.parametrize("mask", MASKS)
def test_no_mask_shape_replaces_the_stored_key(settings_client, mask):
    client, on_disk, saved = settings_client

    resp = client.post("/api/settings", json={"OPENAI_COMPATIBLE_API_KEY": mask})

    assert resp.status_code == 200, resp.text
    assert saved["OPENAI_COMPATIBLE_API_KEY"] == REAL_KEY
    assert on_disk["OPENAI_COMPATIBLE_API_KEY"] == REAL_KEY


@pytest.mark.parametrize("mask", MASKS)
def test_a_mask_is_never_persisted_even_with_nothing_stored(settings_client, mask):
    """Without this, an empty install stores the placeholder as the credential."""
    client, on_disk, saved = settings_client
    on_disk["OPENAI_COMPATIBLE_API_KEY"] = ""

    resp = client.post("/api/settings", json={"OPENAI_COMPATIBLE_API_KEY": mask})

    assert resp.status_code == 200, resp.text
    assert saved["OPENAI_COMPATIBLE_API_KEY"] == ""


def test_a_new_key_replaces_the_old_one(settings_client):
    client, on_disk, saved = settings_client

    resp = client.post("/api/settings", json={"OPENAI_COMPATIBLE_API_KEY": "sk-new-secret"})

    assert resp.status_code == 200, resp.text
    assert saved["OPENAI_COMPATIBLE_API_KEY"] == "sk-new-secret"
    assert on_disk["OPENAI_COMPATIBLE_API_KEY"] == "sk-new-secret"


def test_explicit_clear_removes_the_key(settings_client):
    """The Clear button posts an empty string; that must really delete it."""
    client, on_disk, saved = settings_client

    resp = client.post("/api/settings", json={"OPENAI_COMPATIBLE_API_KEY": ""})

    assert resp.status_code == 200, resp.text
    assert saved["OPENAI_COMPATIBLE_API_KEY"] == ""
    assert on_disk["OPENAI_COMPATIBLE_API_KEY"] == ""


def test_absent_key_keeps_the_stored_secret(settings_client):
    client, on_disk, saved = settings_client

    resp = client.post("/api/settings", json={"OUROBOROS_MODEL": "openai-compatible::local-reason"})

    assert resp.status_code == 200, resp.text
    assert saved["OPENAI_COMPATIBLE_API_KEY"] == REAL_KEY


@pytest.mark.parametrize("key", [
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "OPENAI_COMPATIBLE_API_KEY",
])
def test_every_provider_secret_round_trips(settings_client, key):
    """One contract for all masked secrets, not a per-provider patch."""
    client, on_disk, saved = settings_client
    original = on_disk[key]
    mask = client.get("/api/settings").json()[key]
    assert looks_masked_secret(mask)

    assert client.post("/api/settings", json={key: mask}).status_code == 200
    assert saved[key] == original

    assert client.post("/api/settings", json={key: "brand-new-value-123"}).status_code == 200
    assert saved[key] == "brand-new-value-123"


@pytest.mark.parametrize("mask", MASKS)
def test_a_stored_mask_never_reaches_the_environment(tmp_path, monkeypatch, mask):
    """Defence for installs already poisoned by an older round-trip: a masked
    value on disk reads back as unset instead of becoming a Bearer credential."""
    import json

    from ouroboros import config as cfg

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "OPENAI_COMPATIBLE_API_KEY": mask,
        "OPENAI_COMPATIBLE_BASE_URL": "http://127.0.0.1:4000/v1",
    }), encoding="utf-8")
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)

    loaded = cfg.load_settings()
    assert loaded["OPENAI_COMPATIBLE_API_KEY"] == ""

    # apply_settings_to_env writes process env directly, so restore it afterwards.
    saved_env = dict(os.environ)
    try:
        cfg.apply_settings_to_env(loaded)
        assert os.environ.get("OPENAI_COMPATIBLE_API_KEY", "") == ""
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


@pytest.mark.parametrize("value", ["**", "***", "********", "***set***", "sk-origi...", "abcd..."])
def test_looks_masked_secret_accepts_every_placeholder(value):
    assert looks_masked_secret(value) is True


@pytest.mark.parametrize("value", ["", "sk-a", "*", "sk-original-secret", "p@ssw0rd", "a*b*c"])
def test_looks_masked_secret_rejects_real_values(value):
    assert looks_masked_secret(value) is False
