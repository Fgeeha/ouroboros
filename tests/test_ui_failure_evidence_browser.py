"""Small real-engine acceptance for the explicit synthetic failure recorder."""

from __future__ import annotations

import json
import zipfile

import pytest

from tests.ui_failure_evidence import FailureEvidence


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ui_failure_evidence_real_browser(tmp_path, monkeypatch, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    secret = "owner-secret-sentinel-do-not-export-771c0f"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    html = """<!doctype html><title>Synthetic evidence specimen</title>
      <style>body{font:20px system-ui;background:#eef1f6;margin:32px}
      #chat-messages{height:260px;overflow:auto;background:white;padding:20px}
      .content{height:900px;background:linear-gradient(#ddf,#dff)}</style>
      <h1>Browser failure evidence</h1><p id="chat-status">Thinking...</p>
      <div id="chat-messages"><div class="content">Synthetic scroll target</div></div>"""
    with playwright.sync_playwright() as pw:
        for mode in ("disabled", "pass", "failure", "capture_error"):
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
            destination = tmp_path / mode
            evidence = FailureEvidence(page, browser, None if mode == "disabled" else destination,
                                       f"synthetic::{mode}", engine)
            actions = []
            sentinel = AssertionError("controlled browser sentinel")
            if mode == "capture_error":
                def fail_capture(**kwargs):
                    raise OSError("synthetic screenshot writer error")
                monkeypatch.setattr(page, "screenshot", fail_capture)
            caught = None
            try:
                with evidence:
                    actions.append("goto")
                    page.goto("https://ui-evidence.invalid/")
                    actions.append("move")
                    page.mouse.move(200, 220)
                    actions.append("wheel")
                    page.mouse.wheel(0, 200)
                    actions.append("wait")
                    page.wait_for_function("document.querySelector('#chat-messages').scrollTop > 0")
                    evidence.checkpoint("sentinel_after_existing_actions")
                    if mode in {"failure", "capture_error"}:
                        raise sentinel
            except AssertionError as exc:
                caught = exc
            assert actions == ["goto", "move", "wheel", "wait"]
            assert not browser.is_connected()
            if mode in {"disabled", "pass"}:
                assert caught is None and not destination.exists()
                continue
            assert caught is sentinel
            data = json.loads((evidence.bundle / "evidence.json").read_text(encoding="utf-8"))
            geometry = json.loads((evidence.bundle / "geometry.json").read_text(encoding="utf-8"))
            assert data["primary_exception"]["message"] == str(sentinel)
            assert data["failure_stage"] == "sentinel_after_existing_actions"
            assert geometry["feed"]["scroll_top"] > 0
            assert geometry["hit_chain"]
            assert any(row["type"] == "wheel" for row in geometry["events"])
            assert "Synthetic scroll target" in (evidence.bundle / "page.html").read_text(encoding="utf-8")
            if mode == "failure":
                assert (evidence.bundle / "screenshot.png").read_bytes().startswith(b"\x89PNG")
            else:
                assert data["capture_errors"][0]["operation"] == "screenshot"
            with zipfile.ZipFile(evidence.bundle / "trace.zip") as trace:
                assert any(name.endswith(".trace") for name in trace.namelist())
                assert all(secret.encode() not in trace.read(name) for name in trace.namelist())
            assert all(secret.encode() not in path.read_bytes() for path in evidence.bundle.iterdir())
