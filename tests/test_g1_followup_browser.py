"""Actual Activity consumer and Restore request, served from the candidate bytes."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def test_activity_relationship_hold_and_stale_restore(direct_server_with_data):
    from playwright.sync_api import sync_playwright
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.followup import _handle_schedule_followup
    from ouroboros.task_results import write_task_result
    from ouroboros.cancel_intents import request_cancel
    from supervisor.queue_schedules import load_schedule_store
    from tests.test_g1_followup_policy import BINDING, DEADLINE, ORIGIN

    server = direct_server_with_data
    root = server['data_dir']
    write_task_result(root, ORIGIN, 'running', root_task_id=ORIGIN,
                      billing_group=BINDING, deadline_at=DEADLINE)
    request_cancel(root, ORIGIN, reason='Stop before follow-up registration', source='http_single', requested_by='owner')
    ctx = ToolContext(repo_dir=server['repo_dir'], drive_root=root, task_id=ORIGIN,
                      task_metadata={'root_task_id': ORIGIN, 'delegation_role': 'root'},
                      task_contract={'objective': 'x', 'delegation_role': 'root'})
    result = _handle_schedule_followup(ctx, relation='related', run_at='2098-01-01T00:00:00+00:00',
                                      objective='Continue original work after dependency clears')
    assert result.startswith('FOLLOWUP_SCHEDULED'), result
    [record] = load_schedule_store(root)['tasks']
    old_id = record['followup_hold']['hold_id']
    evidence = Path(__file__).resolve().parents[1] / '.review-drive/batch4-g1-output/browser'
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 960})
            page.goto(server['url'], wait_until='domcontentloaded')
            page.click('[data-nav-page="dashboard"]')
            page.click('[data-dashboard-tab="activity"]')
            section = page.locator('[data-activity-section="schedules"]')
            button = section.get_by_role('button', name='Restore hold')
            button.wait_for(state='visible')
            assert button.get_attribute('data-hold-id') == old_id
            visible = section.inner_text()
            assert 'related' in visible and 'cap $10' in visible and 'deadline' in visible
            assert 'Original work stopped or restarted' in visible and 'root-1' in visible and '2099' in visible
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 960})
                if width < 700:
                    page.wait_for_function("() => document.querySelector('#primary-sidebar').getBoundingClientRect().right <= 1")
                section.scroll_into_view_if_needed()
                assert section.locator('.activity-sub').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
                page.screenshot(path=str(evidence / f'g1-activity-{width}.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 960})
            # A new accepted Stop while the old DOM still offers its old identity.
            request_cancel(root, ORIGIN, reason='New Stop', source='http_single', requested_by='owner',
                           allow_settled_target=True)
            with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/action')) as receipt:
                button.click()
            result = receipt.value.json()
            assert result['status'] == 'stale_hold', result
            assert receipt.value.request.post_data_json['expected_hold_id'] == old_id
            [current] = load_schedule_store(root)['tasks']
            assert current['followup_hold']['hold_id'] != old_id
            page.screenshot(path=str(evidence / 'g1-activity-stale-restore.png'), full_page=True)
            print('G1_ACTIVITY_SCREENSHOTS', evidence)
        finally:
            browser.close()
