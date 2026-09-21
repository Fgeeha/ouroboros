"""Real SPA census consumers, socket lifecycle, CSS and navigation preservation.

Uses the existing static-document fixture; never starts or bootstraps Ouroboros.
All runtime replies are synthetic. Run with either fixture browser engine.
"""
from __future__ import annotations

import copy
import json
from urllib.parse import urlparse

import pytest

from tests import test_subscription_setup_browser as setup_browser

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
WORK = '.nav-project-row[data-project-id="p-work"]'
FRAMES = '() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))'
OBSERVE = """() => {
    window.sidebarSockets = [];
    const Socket = window.WebSocket;
    window.WebSocket = class extends Socket {
        constructor(...args) { super(...args); sidebarSockets.push(this); }
    };
    window.sidebarReads = [];
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        const response = await nativeFetch(...args);
        if (new URL(args[0], location.href).pathname === '/api/state') {
            const nativeJson = response.json.bind(response);
            response.json = async () => {
                const body = await nativeJson();
                // Record completion after the actual consumers' synchronous
                // projection and its paint, including an unchanged projection.
                requestAnimationFrame(() => requestAnimationFrame(() => sidebarReads.push(body._testRevision)));
                return body;
            };
        }
        return response;
    };
}"""


def activity(identity, project='', phase='working', **extra):
    return dict(activity_id=identity, project_id=project, chat_id=42 if project else 1,
                kind='managed_task' if project else 'direct_chat', phase=phase, **extra)


@pytest.mark.parametrize('width,theme,reduced', [
    (1360, 'dark', False), (1360, 'light', True),
    (390, 'light', False), (390, 'dark', True),
])
def test_sidebar_activity_census_and_navigation(subscription_ui, width, theme, reduced):
    ui, page = subscription_ui, subscription_ui['page']
    page.set_viewport_size(dict(width=width, height=844))
    page.emulate_media(color_scheme=theme, reduced_motion='reduce' if reduced else 'no-preference')
    page.add_init_script('(' + OBSERVE + ')()')
    projects = [dict(id='p-' + key, name=name, chat_id=42 + i, lifecycle='active', visible_revision=0)
                for i, (key, name) in enumerate([
                    ('work', 'Working room'), ('queue', 'Queued room'), ('wait', 'Waiting room'),
                    ('empty', 'Empty room'), ('delete', 'Deleting room')])]
    projects[0]['visible_revision'] = 4
    projects[-1]['lifecycle'] = 'deleting'
    work = activity('work', 'p-work')
    wait = activity('wait', 'p-wait', task_attempt=1, model_waits={
        'access': dict(state='waiting', task_attempt=1),
    })
    initial = [work, activity('queue', 'p-queue', 'queued'), wait,
               activity('deleting', 'p-delete'), activity('main', phase='thinking')]
    body = dict(sha='sidebar-fixture', supervisor_ready=True, active_chat_activities_complete=True,
                active_chat_activities=initial, projects=projects, project_chat_ids=[p['chat_id'] for p in projects],
                _testRevision=1)
    mode, sockets, held = {'fault': ''}, [], []

    def connect(socket):
        sockets.append(socket)
        socket.send(json.dumps({'type': 'heartbeat'}))

    def state_response(route):
        if mode['fault'] == 'hold':
            held.append((route, copy.deepcopy(body)))
        elif mode['fault'] == 'http':
            route.fulfill(status=503, json={'error': 'fixture failure'})
        elif mode['fault'] == 'network':
            route.abort()
        else:
            route.fulfill(json=copy.deepcopy(body))

    origin = urlparse(ui['url']).netloc
    page.route('**/*', lambda r: r.fallback() if urlparse(r.request.url).netloc == origin else r.abort())
    page.route_web_socket('**/ws', connect)
    page.route('**/api/state', state_response)
    page.expose_function('sidebarHeldCount', lambda: len(held))
    page.goto(ui['url'] + '/', wait_until='domcontentloaded')
    page.wait_for_function('() => window.__ouroWs?.ws?.readyState === WebSocket.OPEN')
    page.wait_for_function('() => sidebarReads.includes(1)')
    page.locator(WORK).wait_for(state='attached')
    if width < 700:
        page.locator('#page-chat [data-mobile-nav-toggle]').click()
        page.wait_for_function("() => document.querySelector('#primary-sidebar').getBoundingClientRect().left >= 0")

    def marker(project):
        return page.locator(f'[data-project-id="{project}"] .nav-activity-marker')

    def observe(project, state, moving=False):
        target = marker(project)
        page.wait_for_function("([selector, state]) => document.querySelector(selector)?.dataset.state === state",
                               arg=[f'[data-project-id="{project}"] .nav-activity-marker', state])
        assert target.get_attribute('data-motion') == ('1' if moving else '0')
        paint = target.evaluate("""el => ({gap:getComputedStyle(el).gap, dots:[...el.children].map(s => {
            const c=getComputedStyle(s); return {width:c.width,height:c.height,animation:c.animationName,
                duration:c.animationDuration,delay:c.animationDelay};})})""")
        assert paint['gap'] == '3px'
        assert len(paint['dots']) == 3
        for index, dot in enumerate(paint['dots']):
            assert (dot['width'], dot['height']) == ('4px', '4px')
            assert dot['animation'] == ('typing-bounce' if moving and not reduced else 'none')
            if moving and not reduced:
                assert dot['duration'] == '1.4s'
                assert float(dot['delay'].removesuffix('s')) == pytest.approx(index * .2)

    def refresh(**changes):
        body.update(changes)
        body['_testRevision'] += 1
        sockets[-1].send(json.dumps({'type': 'projects_changed'}))
        page.wait_for_function('rev => sidebarReads.includes(rev)', arg=body['_testRevision'])

    def capture(suffix):
        setup_browser.capture(page, f'sidebar-{theme}-{width}-{reduced}-{suffix}')

    observe('p-work', 'working', True)
    observe('p-queue', 'queued')
    observe('p-wait', 'waiting')
    assert page.locator('[data-project-id="p-wait"]').get_attribute('title') == 'Waiting room · Waiting for access'
    assert marker('p-empty').is_hidden()
    assert page.locator('[data-project-id="p-empty"]').is_enabled()
    assert page.locator('[data-project-id="p-delete"]').is_disabled()
    assert page.locator('#nav-main-activity').get_attribute('data-motion') == '1'
    assert page.locator(WORK + ' .nav-unread-dot').count() == 1
    assert page.locator('#nav-projects-count').inner_text() == '1'
    assert 'Unread' in page.locator(WORK).get_attribute('aria-label')
    for project, token in [('p-wait', '--amber'), ('p-queue', '--text-secondary')]:
        assert marker(project).evaluate("""(el, token) => getComputedStyle(el.firstElementChild).backgroundColor
            === (() => {const probe=document.createElement('span'); probe.style.color='var('+token+')';
                el.append(probe); const color=getComputedStyle(probe).color; probe.remove();return color;})()""", token)
    geometry = page.locator('#nav-projects-activity').evaluate("""el => {
        const a=el.getBoundingClientRect(), b=el.previousElementSibling.getBoundingClientRect();
        return {gap:a.left-b.right, top:a.top, labelTop:b.top, height:b.height};}""")
    assert 0 < geometry['gap'] <= 10, geometry
    assert geometry['labelTop'] <= geometry['top'] <= geometry['labelTop'] + geometry['height']
    capture('states')

    # Aggregate is adjacent to Projects and survives collapse; activity does not sort rows.
    order = page.locator('.nav-project-row').evaluate_all('els => els.map(el=>el.dataset.projectId)')
    page.locator('#nav-projects-toggle').click()
    assert page.locator('#nav-projects-list').is_hidden()
    assert page.locator('#nav-projects-activity').is_visible()
    capture('collapsed')
    page.locator('#nav-projects-toggle').click()

    # Keyboard opens the real portalled menu. Preserve the actual menu, focused
    # menu item, row, unread node and marker through activity-only repaints.
    kebab = page.locator(WORK).locator('..').locator('.nav-project-kebab')
    kebab.focus()
    page.keyboard.press('Enter')
    menu = page.locator('body > .project-row-menu')
    menu.wait_for()
    page.keyboard.press('End')
    page.evaluate("""() => { window.sidebarKept = {
        row:document.querySelector('[data-project-id="p-work"]'), menu:document.querySelector('.project-row-menu'),
        focus:document.activeElement, unread:document.querySelector('[data-project-id="p-work"] .nav-unread-dot'),
        marker:document.querySelector('[data-project-id="p-work"] .nav-activity-marker')}; }""")
    owner_wait = activity('work', 'p-work', required_question=dict(wait_for_answer=True, owner_wait_state='waiting'))
    refresh(active_chat_activities=[owner_wait])
    observe('p-work', 'waiting')
    assert page.locator(WORK).get_attribute('title').endswith('Waiting for your answer')
    mixed = [owner_wait, activity('independent', 'p-work', 'finalizing')]
    refresh(active_chat_activities=mixed)
    observe('p-work', 'working', True)
    assert page.locator(WORK).get_attribute('aria-label').endswith('Finalizing · Waiting for your answer')
    assert page.evaluate("""() => sidebarKept.row === document.querySelector('[data-project-id="p-work"]')
        && sidebarKept.menu === document.querySelector('.project-row-menu') && sidebarKept.focus === document.activeElement
        && sidebarKept.unread.isConnected && sidebarKept.marker.isConnected""")
    assert page.locator('.nav-project-row').evaluate_all('els => els.map(el=>el.dataset.projectId)') == order
    capture('mixed-menu')
    page.keyboard.press('Escape')
    assert menu.count() == 0
    assert kebab.evaluate('el=>el===document.activeElement')

    # Resumed owner wait is computation again. A partial omission retains the
    # old row as unknown; a current positive row remains independently moving.
    owner_wait['required_question']['owner_wait_state'] = 'resumed'
    refresh(active_chat_activities=[owner_wait])
    observe('p-work', 'working', True)
    refresh(active_chat_activities=[activity('positive', 'p-queue', 'thinking')], active_chat_activities_complete=False)
    observe('p-work', 'unknown')
    observe('p-queue', 'working', True)
    assert 'Activity status unavailable' in page.locator(WORK).get_attribute('title')
    refresh(active_chat_activities=[], active_chat_activities_complete=True, supervisor_ready=False)
    observe('p-work', 'unknown')
    observe('p-queue', 'unknown')
    refresh(active_chat_activities=initial, supervisor_ready=True)
    observe('p-work', 'working', True)

    # Missing census, HTTP failure, and transport failure all stop retained
    # observations. Exercise the actual state readers, not an exported reducer.
    body.pop('active_chat_activities')
    refresh()
    observe('p-work', 'unknown')
    for fault in ['http', 'network']:
        refresh(active_chat_activities=initial)
        observe('p-work', 'working', True)
        mode['fault'] = fault
        sockets[-1].send(json.dumps({'type': 'projects_changed'}))
        observe('p-work', 'unknown')
        mode['fault'] = ''

    # Hold an old response, apply a newer one, then release the old read. The
    # accepted snapshot sequencer must prevent stale absence-based clearing.
    refresh(active_chat_activities=initial)
    mode['fault'] = 'hold'
    body['active_chat_activities'] = []
    body['_testRevision'] += 1
    with page.expect_request(lambda r: urlparse(r.url).path == '/api/state'):
        sockets[-1].send(json.dumps({'type': 'projects_changed'}))
    page.wait_for_function('() => sidebarHeldCount().then(n => n > 0)')
    mode['fault'] = ''
    refresh(active_chat_activities=initial)
    for route, old in held:
        route.fulfill(json=old)
        page.wait_for_function('rev => sidebarReads.includes(rev)', arg=old['_testRevision'])
    held.clear()
    page.evaluate(FRAMES)
    observe('p-work', 'working', True)

    # A real socket close and reconnect stop motion immediately and preserve
    # unknown until the new connection's held state responses actually arrive.
    mode['fault'] = 'hold'
    connections = len(sockets)
    sockets[-1].close()
    observe('p-work', 'unknown')
    assert page.locator('#nav-main-activity').get_attribute('data-state') == 'unknown'
    page.wait_for_function('n => sidebarSockets.length > n && sidebarSockets.at(-1).readyState === WebSocket.OPEN', arg=connections)
    page.evaluate(FRAMES)
    observe('p-work', 'unknown')
    mode['fault'] = ''
    for route, snapshot in held:
        route.fulfill(json=snapshot)
    held.clear()
    refresh(active_chat_activities=initial)
    observe('p-work', 'working', True)
    # WebSocket error uses the same production disconnect handler.
    page.evaluate("sidebarSockets.at(-1).dispatchEvent(new Event('error'))")
    observe('p-work', 'unknown')
    page.wait_for_function('n => sidebarSockets.length > n && sidebarSockets.at(-1).readyState === WebSocket.OPEN', arg=connections + 1)
    refresh(active_chat_activities=[], active_chat_activities_complete=True, supervisor_ready=True)
    assert marker('p-work').is_hidden()
    assert page.locator('#nav-main-activity').is_hidden()
    assert page.locator('#nav-projects-activity').is_hidden()
    assert page.locator(WORK + ' .nav-unread-dot').count() == 1
    assert page.locator('#nav-projects-count').inner_text() == '1'
    capture('complete-empty')

    # Merely observing state has not marked any project read. Keyboard opening
    # an empty room still navigates normally; the active room gets aria-current.
    assert not any(path == '/api/ui/preferences' and payload.get('project_seen_revision')
                   for path, payload in ui['posts'])
    empty = page.locator('[data-project-id="p-empty"]')
    empty.focus()
    page.keyboard.press('Enter')
    page.locator('#project-panel').wait_for(state='visible')
    assert empty.get_attribute('aria-current') == 'page'
    assert page.locator('#project-panel-title').inner_text() == 'Empty room'
    if width < 700:
        assert not page.locator('#primary-sidebar').evaluate("el=>el.classList.contains('open')")
    page.locator('#project-panel-close').click()
    page.locator('#project-panel').wait_for(state='hidden')
    assert page.locator('[data-nav-page="chat"]').get_attribute('aria-current') == 'page'
