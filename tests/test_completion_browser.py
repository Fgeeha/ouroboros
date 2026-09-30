"""Native completion receipts preserve actual work/error cards, live and replayed."""
import json
import os
import uuid
from pathlib import Path

import pytest

from tests.system_e2e.harness import (
    ArtifactOracle, keyless_settings, message_text, start_server, wait_durable_result, wait_until,
)
from tests.test_chat_addressing_browser import ToolCallOnlyModel, _OBSERVE_CARDS
from tests.test_owner_wait_integration import wait_clone as clone_fixture
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET

wait_clone = clone_fixture
pytestmark = [pytest.mark.serial, pytest.mark.ui_browser]


@pytest.mark.parametrize("engine,width", [("chromium", 1440), ("webkit", 390)])
@pytest.mark.parametrize("case", ["finish_only", "read_then_finish", "invalid_finish"])
def test_native_completion_receipt_keeps_work_and_errors_visible(
    wait_clone, tmp_path, monkeypatch, engine, width, case,
):
    from playwright.sync_api import sync_playwright
    from tests.system_e2e.harness import KeylessIsolatedServer

    marker = "NATIVE_COMPLETION_" + uuid.uuid4().hex
    answer = "The requested result is complete."
    finish = {"tool": "finish_task", "arguments": {"action": "finish", "answer": answer}}
    steps = {
        "finish_only": [finish],
        "read_then_finish": [
            {"tool": "read_file", "arguments": {"root": "system_repo", "path": "VERSION"}}, finish,
        ],
        "invalid_finish": [
            {"tool": "finish_task", "arguments": {"action": "finish", "answer": ""}},
            {"final": answer},
        ],
    }[case]
    calls = []

    def response(body):
        assert any(marker in message_text(m) for m in body.get("messages", []) if m.get("role") == "user")
        index = len(calls)
        calls.append(body)
        assert index < len(steps), "completion bought an unexpected extra model turn"
        return steps[index]

    home = tmp_path / "home"
    home.mkdir()
    original_env = KeylessIsolatedServer._env
    monkeypatch.setattr(KeylessIsolatedServer, "_env", lambda server: {
        **original_env(server), "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
    })
    evidence = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or tmp_path / "evidence")
    evidence = evidence / f"completion-{case}-{engine}-{width}"
    evidence.mkdir(parents=True, exist_ok=True)
    with ToolCallOnlyModel([response] * len(steps)) as stub:
        server = start_server(wait_clone, tmp_path / "instance", keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        oracle = ArtifactOracle(server.data_root)
        try:
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={"width": width, "height": 900}, has_touch=width < 980)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
                try:
                    page.goto(server.base_url, wait_until="domcontentloaded")
                    page.wait_for_function("() => window.__testSockets?.[0]?.readyState === WebSocket.OPEN")
                    page.evaluate(_OBSERVE_CARDS)
                    page.evaluate("""() => {
                        window.__completionMetrics = [];
                        window.__testSockets[0].addEventListener('message', event => {
                            const frame = JSON.parse(event.data);
                            if (frame.type === 'log' && frame.data?.type === 'task_metrics_event')
                                window.__completionMetrics.push(frame.data);
                        });
                    }""")
                    page.locator("#chat-input").fill(marker)
                    page.locator("#chat-send").click()
                    task = wait_until(lambda: next((row["task"] for row in oracle.events("task_received")
                        if row.get("task", {}).get("_is_direct_chat") and marker in row["task"].get("text", "")), None), 90)
                    assert task, "composer did not admit the native direct turn"
                    task_id = task["id"]
                    result = wait_durable_result(oracle, task_id, timeout=90)
                    page.wait_for_function(
                        "id => window.__completionMetrics.some(row => row.task_id === id)",
                        arg=task_id, timeout=30000,
                    )
                    metrics = [row for row in oracle.supervisor_rows("task_metrics_event") if row.get("task_id") == task_id]
                    assert metrics[-1]["completion_tool_calls"] == (0 if case == "invalid_finish" else 1)
                    assert metrics[-1]["tool_calls"] == (2 if case == "read_then_finish" else 1)
                    assert metrics[-1]["tool_errors"] == (1 if case == "invalid_finish" else 0)
                    assert len(calls) == len(steps)
                    page.get_by_text(answer, exact=True).wait_for(timeout=30000)
                    card = page.locator(f'.chat-live-card[data-task-id="{task_id}"]')
                    if case == "finish_only":
                        assert card.count() == 0
                        assert task_id not in page.evaluate("() => window.__mountedCards")
                    else:
                        card.wait_for(timeout=30000)
                        assert card.locator("[data-live-title]").inner_text().strip()
                    page.screenshot(path=str(evidence / "live.png"), full_page=True, animations="disabled")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_text(answer, exact=True).wait_for(timeout=30000)
                    assert page.locator(f'.chat-live-card[data-task-id="{task_id}"]').count() == (0 if case == "finish_only" else 1)
                    page.screenshot(path=str(evidence / "reloaded.png"), full_page=True, animations="disabled")
                    assert not errors, errors
                    (evidence / "receipt.json").write_text(json.dumps({
                        "case": case, "engine": engine, "width": width,
                        "candidate_identity": wait_clone.identity, "source_head": wait_clone.state.head.decode("ascii").strip(),
                        "served_checkout": str(wait_clone.path), "task": task, "result": result,
                        "task_metrics": metrics, "observed_model_calls": len(calls), "errors": errors,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    page.screenshot(path=str(evidence / "failure.png"), full_page=True, animations="disabled")
                    (evidence / "failure-dom.html").write_text(page.content(), encoding="utf-8")
                    raise
                finally:
                    browser.close()
        finally:
            server.stop()
            assert server.proc.poll() is not None
