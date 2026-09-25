// Display-only gate for the agent's reasoning rows (UI preference
// `show_reasoning`, default off). The backend keeps emitting and storing the
// stamped frames either way, so turning it on reveals them on replay too.
// localStorage mirrors the server preference for renders before the fetch lands.
const REASONING_STORAGE_KEY = 'ouro.show_reasoning';

let reasoningVisible = (() => {
    try { return localStorage.getItem(REASONING_STORAGE_KEY) === '1'; } catch { return false; }
})();

export const REASONING_VISIBILITY_EVENT = 'ouro:reasoning-visibility';

export function isReasoningVisible() {
    return reasoningVisible;
}

/** Single writer of the flag. It notifies so a control bound before the
    preference arrives (and the Logs filter chips) can resync; outside a DOM
    (node tests) it is a plain assignment. */
export function setReasoningVisible(value) {
    reasoningVisible = value === true;
    try { localStorage.setItem(REASONING_STORAGE_KEY, reasoningVisible ? '1' : '0'); } catch { /* storage blocked: server value still wins on boot */ }
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function'
        && typeof CustomEvent === 'function') {
        window.dispatchEvent(new CustomEvent(REASONING_VISIBILITY_EVENT, {
            detail: { visible: reasoningVisible },
        }));
    }
    return reasoningVisible;
}

/** Settings display toggle. It applies and persists on its own (`save`) and must
    never touch the settings draft, so its change event stops before the
    page-level dirty listener sees it. */
export function bindReasoningToggle(root, save) {
    const box = root.querySelector('#ui-show-reasoning');
    if (!box) return;
    const sync = () => { box.checked = reasoningVisible; };
    box.addEventListener('change', (event) => {
        event.stopPropagation();
        save(setReasoningVisible(box.checked));
    });
    window.addEventListener(REASONING_VISIBILITY_EVENT, sync);
    sync();
}

/** Which frames the reasoning branch claims. Display on: every reasoning frame
    but a subagent's, which keeps its lineage card line. Display off: every one
    but a reasoning-only round (`narration: true`), which reads as before. */
export function claimsReasoningFrame(evt, isSubagent) {
    if (evt?.reasoning !== true) return false;
    return reasoningVisible ? !isSubagent : evt.narration !== true;
}
