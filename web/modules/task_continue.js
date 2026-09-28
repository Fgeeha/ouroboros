// Owner Batch4: "Continue" on a card whose root task was interrupted.
//
// The server decides what a settled card may offer (`continuation_offer` on the
// task detail: a Continue after a technical interruption, or the successor this
// task already has). The press carries ONE random action nonce kept in local
// storage per task, so a retry or a reload after a lost answer returns the SAME
// admission instead of starting a second task; a timeout is never read as "not
// continued". The card then points to its successor (a stale card never offers
// a second Continue — new work goes through the conversation).

import { continueTask, fetchTaskDetail } from './api_client.js';
import { ensureLiveActionsEl } from './chat_activity.js';
import { taskDoneIsTerminal } from './log_events.js';
import { showToast } from './toast.js';

const NONCE_KEY = 'ouro_continue_nonce:';
const inFlight = new Set();
const sessionNonces = new Map();
// Cards whose full task detail was read once for the server's offer.
const offerReads = new WeakSet();

/** One Continue action per task. Unavailable storage limits retention to this page. */
export function continueNonce(taskId, storage, retainedNonce = '') {
    const key = `${NONCE_KEY}${taskId}`;
    let nonce = retainedNonce || sessionNonces.get(key);
    try {
        storage ??= globalThis.localStorage;
        nonce ||= storage?.getItem?.(key);
    } catch { /* even accessing localStorage can throw */ }
    nonce ||= (globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`)
        .replace(/[^A-Za-z0-9_-]/g, '');
    sessionNonces.set(key, nonce);
    try { storage?.setItem?.(key, nonce); } catch { /* best effort */ }
    return nonce;
}

/** @returns {{kind: 'offer'|'retry'|'successor'|'none', successorId?: string, cause?: string, actionNonce?: string}} */
export function continueOfferView(detail) {
    const offer = detail?.continuation_offer;
    if (!offer || typeof offer !== 'object') return { kind: 'none' };
    if (offer.state === 'bound') return offer.action_nonce
        ? { kind: 'retry', actionNonce: String(offer.action_nonce) } : { kind: 'none' };
    if (offer.successor_task_id) return { kind: 'successor', successorId: String(offer.successor_task_id) };
    return offer.eligible ? { kind: 'offer', cause: String(offer.cause || '') } : { kind: 'none' };
}

/**
 * Press Continue: one request under the persisted nonce; the answer names the
 * successor (a replay of an earlier press answers the same one).
 * @returns {Promise<string>} the successor task id, or '' when refused
 */
export async function continueTaskAction(taskId, { request = continueTask, storage, toast = showToast, actionNonce = '' } = {}) {
    const id = String(taskId || '').trim();
    if (!id || inFlight.has(id)) return '';
    inFlight.add(id);
    try {
        const ack = await request(id, continueNonce(id, storage, actionNonce));
        const successor = String(ack?.successor_task_id || '');
        toast(ack?.held
            ? 'Continue accepted. Waiting for earlier work to settle; use Resume in Activity to recheck.'
            : 'Continue accepted.', 'ok');
        return successor;
    } catch (exc) {
        const body = exc?.body || {};
        if (body.reason_code === 'already_continued' && body.successor_task_id && body.state !== 'bound') {
            toast('Already continued.', 'info');
            return String(body.successor_task_id);
        }
        if (body.state === 'bound' && body.action_nonce) continueNonce(id, storage, body.action_nonce);
        // The same nonce stays: pressing again retries this very admission.
        toast(`Continue not confirmed: ${exc?.message || exc}`, 'error');
        return '';
    } finally {
        inFlight.delete(id);
    }
}

function renderSuccessor(button, successorId) {
    button.textContent = 'Continued';
    button.disabled = true;
    button.dataset.continueSuccessor = successorId;
}

/**
 * Keep one settled card's Continue action in step with the server's offer.
 *
 * The full task detail (`GET /api/tasks/{id}`) and a settled task's replayed
 * history rows state the offer, so a card rebuilt after a reload shows it
 * without being opened. A live event row (task_done, task_eval, …) says nothing
 * about it and never removes a shown action; when it is the terminal of a ROOT
 * card that did not complete cleanly (a best-effort completion may be a
 * technical limit), the card reads the full detail once to learn the offer.
 */
export function syncContinueAction(record, detail, { read = fetchTaskDetail } = {}) {
    if (!detail || typeof detail !== 'object') return false;
    if (!('continuation_offer' in detail)) {
        const status = String(detail.task_terminal_status || detail.status || '').toLowerCase();
        const clean = ['completed', 'done'].includes(status) && detail.outcome_axes?.execution?.status !== 'best_effort';
        if (record?.root && !record.isSubagent && !offerReads.has(record) && taskDoneIsTerminal(detail) && !clean) {
            offerReads.add(record);
            Promise.resolve().then(() => read(record.groupId))
                .then((full) => {
                    const offer = full?.continuation_offer;
                    if (!offer || typeof offer !== 'object' || Array.isArray(offer)) {
                        offerReads.delete(record);  // null is the production reader's failure result
                    } else if (record.root?.isConnected) syncContinueAction(record, full, { read });
                })
                .catch(() => { offerReads.delete(record); });  // a later terminal row retries
        }
        return false;
    }
    const view = continueOfferView(detail);
    const actions = view.kind === 'none' ? null : ensureLiveActionsEl(record);
    const existing = record?.root?.querySelector?.('[data-continue-task]');
    if (!actions) {
        existing?.remove();
        return false;
    }
    const button = existing || document.createElement('button');
    if (!existing) {
        button.type = 'button';
        button.className = 'btn btn-xs';
        button.dataset.continueTask = String(record.groupId || '');
        actions.appendChild(button);
    }
    if (view.kind === 'successor') {
        renderSuccessor(button, view.successorId);
        return !existing;
    }
    button.textContent = view.kind === 'retry' ? 'Retry Continue' : 'Continue';
    button.disabled = false;
    button.title = view.kind === 'retry' ? 'Retry the same unconfirmed Continue action' : `Start a new task that continues this interrupted one (${view.cause || 'technical interruption'})`;
    button.onclick = async (event) => {
        event.stopPropagation();
        button.disabled = true;
        const successor = await continueTaskAction(record.groupId, { actionNonce: view.actionNonce || '' });
        if (successor) renderSuccessor(button, successor);
        else button.disabled = false;
    };
    return !existing;
}
