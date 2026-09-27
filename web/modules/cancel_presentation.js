import { plainCauseText } from './utils.js';

// Twin of supervisor.cancel_publication.cancel_cause_clauses. IDs stay in
// cancel_origin/lineage details; a compact sentence never guesses an actor.
export const CANCEL_SOURCE_PHRASES = {
    http_single: 'Stopped from the app (Stop now)',
    http_cascade: 'Stopped from the app (Stop now)',
    http_graceful: 'Stopped from the app (Wrap up)',
    cascade_descendant: 'Stopped with the task tree it belongs to',
    owner_restart: 'Stopped by the Restart command',
    snapshot_restore: 'The server stopped while this task was still running',
    agent_tool: 'Stopped by Ouroboros',
};
// Twin of CANCEL_SOURCE_LABELS: a producer's own fixed label restates its phrase.
export const CANCEL_SOURCE_LABELS = {
    http_graceful: 'owner requested finalize-then-stop',
    owner_restart: 'Owner restart',
    snapshot_restore: 'server_shutdown',
};
// Twin of CANCEL_REASON_MAX_CHARS / CANCEL_REASON_PREVIEW_NOTE: the record keeps
// the whole reason, so a shortened clause names itself a preview.
export const CANCEL_REASON_MAX_CHARS = 160;
export const CANCEL_REASON_PREVIEW_NOTE = ' (preview; the full reason is kept with the task)';

function reasonPreview(value) {
    const whole = plainCauseText(value, 0);
    const shown = plainCauseText(value, CANCEL_REASON_MAX_CHARS);
    return shown === whole ? shown : `${shown}${CANCEL_REASON_PREVIEW_NOTE}`;
}

export function cancelCauseClauses(origin, record = {}) {
    const source = String(origin.source || '');
    // Ouroboros's own cancel_task stamps the run that asked, not a swept root.
    const asker = source === 'agent_tool';
    const asked = String(origin.requested_by || '');
    const self = String(record.task_id || record.id || record.subagent_task_id || '');
    const parent = String(record.parent_task_id || '');
    const root = String(record.root_task_id || '');
    const actor = origin.request_origin?.kind === 'agent_task' && origin.request_origin.task_id;
    const relation = asked && asked !== self && asked === parent
        ? (asker ? 'Requested by its parent task' : 'Stopped with its parent task')
        : asked && asked !== self && parent && asked === root
            ? (asker ? 'Requested by an ancestor task' : 'Stopped with an ancestor task') : '';
    const stated = String(origin.reason || '');
    const label = Object.hasOwn(CANCEL_SOURCE_LABELS, source) ? CANCEL_SOURCE_LABELS[source] : null;
    return [
        Object.hasOwn(CANCEL_SOURCE_PHRASES, source) ? CANCEL_SOURCE_PHRASES[source] : source,
        stated.split(/\s+/).filter(Boolean).join(' ') === label ? '' : reasonPreview(stated),
        origin.scope === 'cascade' ? 'this task and its sub-tasks' : '',
        relation,
        actor && !(asker && relation && String(actor) === asked) ? 'Requested by a task' : '',
    ];
}
