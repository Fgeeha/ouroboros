"""Production controls and real admission/census APIs on a disposable local stand.

The fixture serves candidate modules, injects only a queue persistence failure,
then reloads the browser. It does not emulate Continue acknowledgements.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest

from tests._budget_pause_exact_helpers import _install_queue
from tests.test_owner_continue import _interrupted

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
WEB = Path(__file__).resolve().parents[1] / 'web'
HTML = '''<!doctype html><html class="ouro-ui"><head><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/ui.css"><link rel="stylesheet" href="/style.css"></head><body>
<main><div id="card" class="chat-live-card"><h3>Interrupted report</h3></div>
<button id="header-restart" class="btn">Restart</button><button id="settings-restart" class="btn">Restart now</button>
<div id="activity"></div></main><script type="module">
import {syncContinueAction} from '/modules/task_continue.js';
import {confirmAndSendRestart} from '/modules/chat_activity.js';
import {openConfirmDialog} from '/modules/confirm_dialog.js';
import {initActivity} from '/modules/activity.js';
const ws={on(){return ()=>{};},send(){throw Error('test must cancel Restart');}};
const detail=await (await fetch('/api/tasks/pred-1')).json();
syncContinueAction({root:document.getElementById('card'),groupId:'pred-1'},detail);
window.activity=initActivity({mount:document.getElementById('activity'),ws});
await window.activity.refresh();
for(const id of ['header-restart','settings-restart']) document.getElementById(id).onclick=()=>confirmAndSendRestart({openConfirmDialog,ws});
window.ready=true;
</script></body></html>'''


def test_failed_admission_reload_retry_and_tree_pause_consumers(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright, expect
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ouroboros.gateway.task_continue import api_task_continue
    from ouroboros.gateway.state import _chat_activities_snapshot_safe
    from ouroboros.owner_continue import continuation_offer
    from ouroboros.owner_pause import install_fence, set_fence_state
    from ouroboros.task_results import load_task_result, write_task_result

    q, _, workers = _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path)
    write_task_result(tmp_path, 'pausing-root', 'running', root_task_id='pausing-root')
    fence, _ = install_fence(tmp_path, 'pausing-root', request_id='tree-pause')
    workers.PENDING.append({'id': 'pausing-root', 'root_task_id': 'pausing-root', 'type': 'task',
                            'title': 'Saved root waiting for child', '_budget_pause': {'reason': 'owner'}})
    workers.RUNNING['child'] = {'task': {'id': 'child', 'root_task_id': 'pausing-root', 'parent_task_id': 'pausing-root'}}
    write_task_result(tmp_path, 'child', 'running', root_task_id='pausing-root', parent_task_id='pausing-root')
    async def detail(_request):
        row = load_task_result(tmp_path, 'pred-1')
        return JSONResponse({**row, 'continuation_offer': continuation_offer(row, 'pred-1')})
    async def state(_request):
        return JSONResponse({'active_chat_activities': _chat_activities_snapshot_safe(tmp_path),
                             'active_chat_activities_complete': True, 'bg_consciousness_enabled': False})
    async def tasks(_request):
        return JSONResponse({'queue': {'pending': [{'id': t['id'], 'task': t} for t in workers.PENDING],
                                      'running': [{'id': tid, **r} for tid, r in workers.RUNNING.items()]}})
    async def schedules(_request):
        return JSONResponse({'tasks': []})
    app = Starlette(routes=[Route('/api/tasks/pred-1', detail), Route('/api/state', state),
                           Route('/api/tasks', tasks), Route('/api/schedules', schedules),
                           Route('/api/tasks/{task_id}/continue', api_task_continue, methods=['POST'])])
    client = TestClient(app)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path == '/fixture':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(HTML.encode())
            else:
                super().do_GET()
    requests = []
    def respond(route):
        req = route.request
        path = req.url.split('/api/', 1)[1]
        if req.method == 'POST':
            requests.append(req.post_data_json)
            response = client.post('/api/' + path, json=req.post_data_json)
        else:
            response = client.get('/api/' + path)
        route.fulfill(status=response.status_code, content_type='application/json', body=response.content)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(WEB)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = tmp_path / 'screenshots'
    evidence.mkdir()
    persist = q.persist_queue_snapshot
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1200, 'height': 850})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.route('**/api/**', respond)
                page.goto(f'http://127.0.0.1:{server.server_port}/fixture')
                page.wait_for_function('window.ready')
                expect(page.locator('[data-activity-section="queue"]')).to_contain_text('pausing')
                for door in ('header-restart', 'settings-restart'):
                    page.locator('#' + door).click()
                    expect(page.get_by_text('1 task is still pausing', exact=False)).to_be_visible()
                    page.screenshot(path=str(evidence / f'{door}-pausing.png'))
                    page.get_by_role('button', name='Cancel', exact=True).click()
                monkeypatch.setattr(q, 'persist_queue_snapshot', lambda **_k: False)
                page.get_by_role('button', name='Continue', exact=True).click()
                expect(page.get_by_role('button', name='Continue', exact=True)).to_be_enabled()
                claim = load_task_result(tmp_path, 'pred-1')['continued_by']
                assert claim['state'] == 'bound' and load_task_result(tmp_path, claim['successor_task_id']) is None
                # Lose all page/local identity; reload restores the exact server action.
                page.evaluate('localStorage.clear()')
                page.reload()
                page.wait_for_function('window.ready')
                retry = page.get_by_role('button', name='Retry Continue', exact=True)
                expect(retry).to_be_enabled()
                page.screenshot(path=str(evidence / 'bound-retry.png'))
                monkeypatch.setattr(q, 'persist_queue_snapshot', persist)
                retry.click()
                expect(page.locator('[data-continue-successor]')).to_have_text('Continued as ' + claim['successor_task_id'])
                assert [r['action_nonce'] for r in requests] == [claim['action_nonce']] * 2
                assert sum(t['id'] == claim['successor_task_id'] for t in workers.PENDING) == 1
                workers.RUNNING.clear()
                set_fence_state(tmp_path, 'pausing-root', fence_id=fence['fence_id'], state='paused')
                page.reload()
                page.wait_for_function('window.ready')
                expect(page.locator('[data-activity-section="queue"]')).to_contain_text('paused')
                page.locator('#header-restart').click()
                expect(page.get_by_text('1 task is still pausing', exact=False)).to_have_count(0)
                page.get_by_role('button', name='Cancel', exact=True).click()
                page.screenshot(path=str(evidence / 'admitted-and-paused.png'))
                assert not errors
            finally:
                browser.close()
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
    print('BATCH4_REPAIR_UI_EVIDENCE', evidence)
