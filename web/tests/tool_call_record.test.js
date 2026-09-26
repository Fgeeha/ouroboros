import assert from 'node:assert/strict';
import test from 'node:test';

import { summarizeLogEvent } from '../modules/log_events.js';

// #1316: a durable tool_call_started row (a Logs backfill) records that host
// processing began; replayed alone it must not read as a live "Running" call.
test('a tool start reads as started, its wait end as a timeout, its result as a result', () => {
    const base = { task_id: 't', tool: 'run_command', invocation_id: 'i1', args: { cmd: 'make' } };
    const started = summarizeLogEvent({ ...base, type: 'tool_call_started', timeout_sec: 60 });
    assert.equal(started.headline, 'Started run_command');
    assert.doesNotMatch(started.headline, /Running/);
    assert.equal(summarizeLogEvent({ ...base, type: 'tool_call_timeout', timeout_sec: 60 }).headline,
        'run_command timed out');
    assert.equal(summarizeLogEvent({ ...base, type: 'tool_call', result_preview: 'ok' }).headline,
        'run_command result');
});
