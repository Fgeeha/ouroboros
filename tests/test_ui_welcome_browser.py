"""The empty-Main greeting through the real server, Settings and chat history.

The greeting is host-owned empty-state copy, never a chat bubble, a history row or
a model reply. It appears only in Main, only after a successful recent history read
whose own window reports complete coverage while the feed holds no content, and its
copy comes from the install-wide ``welcome`` UI preference (default, hidden or
custom), which Settings → Appearance saves through /api/ui/preferences.

The late-history case holds Main's own history reads in page JavaScript and settles
each one from the REAL server response, rewritten only where the scenario says so,
so the pager, the renderer and the gate all consume production payloads. Nothing is
timed: every wait is a DOM condition or a held request.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET

pytest_plugins = ("tests.test_ui_smoke_playwright",)

DEFAULT = "Ouroboros has awakened"
CUSTOM = "Hello <b>x</b>\nSecond line & more"
HYDRATED = '#chat-messages[data-history-hydrated="true"]'
WELCOME = '#chat-messages > .chat-empty-welcome[data-welcome-state="ready"]'
ANY_WELCOME = ".chat-empty-welcome"
SETTLE_FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"

# Records, for every greeting Main mounts, whether its history read was already
# stamped at that moment: the empty state never precedes a landed read.
_WATCH_WELCOME = """() => {
    window.__welcomeMounts = [];
    new MutationObserver(() => {
        const node = document.querySelector('#chat-messages > .chat-empty-welcome');
        if (node && !node.__seen) {
            node.__seen = true;
            window.__welcomeMounts.push(node.parentElement.dataset.historyHydrated || '');
        }
    }).observe(document, {subtree: true, childList: true});
}"""

# Main's recent-history reads wait here until the test settles them.
_HOLD_MAIN_HISTORY = r"""() => {
    const nativeFetch = window.fetch.bind(window);
    window.__historyHeld = [];
    window.fetch = (input, init) => {
        const url = new URL(typeof input === 'string' ? input : input?.url || '', location.href);
        if (url.pathname !== '/api/chat/history' || url.searchParams.has('chat_id')) return nativeFetch(input, init);
        return new Promise((resolve, reject) => window.__historyHeld.push({input, init, resolve, reject}));
    };
    const json = (body, status) => new Response(JSON.stringify(body),
        {status, headers: {'Content-Type': 'application/json'}});
    window.__settleHistory = async (mode) => {
        const held = window.__historyHeld.shift();
        if (!held) throw new Error('no held history read');
        if (mode === 'error') return held.resolve(json({error: 'Synthetic history outage'}, 500));
        const response = await nativeFetch(held.input, held.init);
        const body = await response.json();
        if (mode === 'partial') body.window = {complete: false, truncated_by: ['quota']};
        if (mode === 'message') body.messages = [...body.messages,
            {role: 'assistant', text: 'Late history message.', ts: new Date().toISOString()}];
        held.resolve(json(body, response.status));
    };
}"""


def _settle(page):
    page.evaluate(SETTLE_FRAMES)


def _welcome_text(page):
    return page.locator(WELCOME).evaluate("node => node.lastElementChild.textContent")


def _stored_welcome(page, url):
    return page.request.get(url + "/api/ui/preferences").json()["welcome"]


def _open_main(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.locator('[data-nav-page="chat"]').click()
    page.wait_for_selector(HYDRATED, state="attached", timeout=30_000)
    _settle(page)


def _open_greeting_settings(page, expect):
    page.locator('[data-nav-page="settings"]').click()
    page.wait_for_selector("#page-settings.active")
    page.locator('[data-settings-tab="appearance"]').click()
    page.wait_for_selector('[data-settings-panel="appearance"].active')
    block = page.locator('[data-settings-panel="appearance"] [data-welcome-settings]')
    expect(block.locator("[data-welcome-save]")).to_be_enabled(timeout=30_000)
    return block


def _save_greeting(block, expect, mode, text=None, status="Welcome preference saved."):
    block.locator("[data-welcome-mode]").select_option(mode)
    if text is not None:
        block.locator("[data-welcome-text]").fill(text)
    block.locator("[data-welcome-save]").click()
    expect(block.locator("[data-welcome-status]")).to_have_text(status)


def _main_greeting_is(page, text):
    """Main is mounted behind Settings; a saved choice reaches it without a reload."""
    page.wait_for_function(
        """text => {
            const node = document.querySelector('#chat-messages > .chat-empty-welcome');
            return text === null ? !node : node?.lastElementChild.textContent === text;
        }""",
        arg=text,
    )


@pytest.mark.serial
@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_empty_main_greeting_contract(direct_server_with_data, engine):
    from playwright.sync_api import expect, sync_playwright

    from ouroboros.projects_registry import create_project

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(data_dir.parent)))
    evidence.mkdir(parents=True, exist_ok=True)
    create_project(data_dir, "welcome-room", name="Welcome room")
    prefs_file = data_dir / "state" / "ui_preferences.json"

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            page.add_init_script(f"({_WATCH_WELCOME})()")
            settings_posts = []
            page.on("request", lambda request: settings_posts.append(request.url)
                    if request.method == "POST" and request.url.endswith("/api/settings") else None)

            # (a) A fresh install's empty Main shows the built-in sentence as host copy,
            # only once its history read has landed, and never as a bubble or a row.
            _open_main(page, url)
            page.wait_for_selector(WELCOME)
            assert _welcome_text(page) == DEFAULT
            assert page.evaluate("() => window.__welcomeMounts") == ["true"]
            assert page.locator("#chat-messages .chat-bubble:not(.typing-bubble)").count() == 0
            history = page.request.get(url + "/api/chat/history").json()
            assert history["messages"] == [] and history["window"]["complete"] is True
            page.screenshot(path=str(evidence / f"welcome-default-{engine}.png"))

            # ... and a Project room never shows it.
            page.locator('.nav-project-row[data-project-id="welcome-room"]').click()
            page.wait_for_selector("#project-panel:not([hidden])")
            page.wait_for_selector('#project-panel [data-history-hydrated="true"]', state="attached")
            _settle(page)
            assert page.locator(f"#project-panel {ANY_WELCOME}").count() == 0
            page.click("#project-panel-close")

            # (b) Custom copy saves outside /api/settings, reaches the open Main live,
            # survives a reload and is rendered as text, not markup.
            block = _open_greeting_settings(page, expect)
            expect(block.locator("[data-welcome-status]")).to_have_text("Saved for this installation.")
            _save_greeting(block, expect, "custom", CUSTOM)
            _main_greeting_is(page, CUSTOM)
            expect(page.locator("#settings-unsaved-indicator")).not_to_have_class(re.compile("is-visible"))
            assert settings_posts == []
            assert _stored_welcome(page, url) == {"mode": "custom", "text": CUSTOM}
            block.screenshot(path=str(evidence / f"welcome-settings-{engine}.png"))

            _open_main(page, url)
            page.wait_for_selector(WELCOME)
            assert _welcome_text(page) == CUSTOM
            assert page.locator(f"{WELCOME} b").count() == 0
            assert page.locator(f"{WELCOME} p").evaluate("node => getComputedStyle(node).whiteSpace") == "pre-wrap"
            page.screenshot(path=str(evidence / f"welcome-custom-{engine}.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            _settle(page)
            box = page.locator(WELCOME).bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
            page.screenshot(path=str(evidence / f"welcome-narrow-{engine}.png"))
            page.set_viewport_size({"width": 1280, "height": 860})

            # (e) Blank custom copy is refused by the control and by the endpoint; a
            # refused save leaves the stored choice and its file exactly as they were.
            stored = prefs_file.read_bytes()
            block = _open_greeting_settings(page, expect)
            _save_greeting(block, expect, "custom", "   ", status="Enter a message, or choose Hidden.")
            for invalid in ({"mode": "custom", "text": "   "}, {"mode": "custom", "text": "x" * 501},
                            {"mode": "loud", "text": ""}, {"mode": "custom"}):
                response = page.request.post(url + "/api/ui/preferences", data=json.dumps({"welcome": invalid}),
                                             headers={"Content-Type": "application/json"})
                assert response.status == 400, response.text()
            assert _stored_welcome(page, url) == {"mode": "custom", "text": CUSTOM}
            assert prefs_file.read_bytes() == stored
            _main_greeting_is(page, CUSTOM)

            # (c) Hidden removes it live and after a reload.
            block.locator("[data-welcome-text]").fill(CUSTOM)
            _save_greeting(block, expect, "hidden")
            _main_greeting_is(page, None)
            _open_main(page, url)
            assert page.locator(ANY_WELCOME).count() == 0

            # (d) Default again restores the built-in sentence.
            block = _open_greeting_settings(page, expect)
            _save_greeting(block, expect, "default")
            _main_greeting_is(page, DEFAULT)
            _open_main(page, url)
            page.wait_for_selector(WELCOME)
            assert _welcome_text(page) == DEFAULT
            assert settings_posts == []

            # (f) Only a successful read that reports complete coverage confirms an empty
            # Main. A failed read shows its failure instead (and retracts a greeting it can
            # no longer vouch for), a partial read shows nothing, and a late read that
            # brings a message removes the greeting again.
            late = browser.new_page(viewport={"width": 1280, "height": 860})
            for script in (_CAPTURE_TEST_SOCKET, _HOLD_MAIN_HISTORY, _WATCH_WELCOME):
                late.add_init_script(f"({script})()")
            held = "() => window.__historyHeld.length > 0"
            failure = """() => document.querySelector('#chat-messages .chat-load-older-note')
                ?.textContent.includes('Synthetic history outage') === true"""

            def settle(mode):
                late.wait_for_function(held)
                late.evaluate(f"() => window.__settleHistory('{mode}')")

            def retry():
                # Retry reads again unless another hydration trigger already does; one
                # page task decides, and every Main read in flight is a held one.
                late.evaluate("""() => { if (!window.__historyHeld.length)
                    document.querySelector('#chat-messages .chat-load-older-btn')?.click(); }""")

            def reconnect():
                # A reconnect with no established build SHA deliberately reloads the page.
                late.wait_for_function("() => typeof window.__ouroWs?._lastSha === 'string'"
                                       " && window.__ouroWs._lastSha.length > 0")
                late.evaluate("() => window.__testSockets.at(-1).close()")

            late.goto(url, wait_until="domcontentloaded", timeout=30_000)
            settle("error")
            late.wait_for_function(f"() => window.__historyHeld.length > 0 || ({failure})()")
            assert late.locator(ANY_WELCOME).count() == 0
            assert late.locator(HYDRATED).count() == 0
            retry()
            settle("complete")
            late.wait_for_selector(WELCOME)
            assert _welcome_text(late) == DEFAULT
            reconnect()
            settle("error")
            late.wait_for_selector(ANY_WELCOME, state="detached")
            late.wait_for_function(failure)
            retry()
            settle("partial")
            late.wait_for_selector('.chat-bubble[data-system-type="reconnect"]')
            _settle(late)
            assert late.locator(ANY_WELCOME).count() == 0
            reconnect()
            settle("complete")
            late.wait_for_selector(WELCOME)
            # The ephemeral reconnect notice is chrome, not conversation.
            late.wait_for_function("() => document.querySelectorAll("
                                   "'.chat-bubble[data-system-type=\"reconnect\"]').length >= 2")
            _settle(late)
            assert late.locator(WELCOME).count() == 1
            reconnect()
            settle("message")
            late.locator(".chat-bubble", has_text="Late history message.").wait_for()
            late.wait_for_selector(ANY_WELCOME, state="detached")
            assert late.evaluate("() => window.__welcomeMounts") == ["true", "true"]
            late.close()

            # (g) The owner's first real message replaces the empty state, live and
            # in the durable history, which never carries the greeting itself.
            _open_main(page, url)
            page.wait_for_selector(WELCOME)
            page.fill("#chat-input", "First owner message")
            page.click("#chat-send")
            page.locator(".chat-bubble.user", has_text="First owner message").wait_for()
            page.wait_for_selector(ANY_WELCOME, state="detached")
            _open_main(page, url)
            page.locator(".chat-bubble.user", has_text="First owner message").wait_for()
            assert page.locator(ANY_WELCOME).count() == 0
            body = page.request.get(url + "/api/chat/history").text()
            assert DEFAULT not in body and "Second line" not in body
        finally:
            browser.close()
