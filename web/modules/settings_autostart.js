import { apiClient } from './api_client.js';
import { setInlineStatus } from './ui_primitives.js';

/* Start with Windows (Appearance tab). Not a server setting: the Windows sign-in
   entry of the host running Ouroboros, applied on click — so, like the
   notification block, no `s-` field and no part of the /api/settings draft.
   Windows can change the entry too, so the block re-reads it whenever Settings
   is shown. Only the states below reveal it; `unavailable` (every run except the
   packaged Windows desktop copy) and anything unknown keep it hidden. */

const NOTES = {
    on: '',
    off: '',
    other_copy: 'Windows starts Ouroboros at sign-in from a different entry (another copy, or one set up by hand). Turn this on to start this copy instead.',
    disabled_in_windows: 'Turned off in Windows Startup apps. Turn this on to start Ouroboros at sign-in again.',
};

export function bindAutostartControl(page) {
    const section = page.querySelector('[data-autostart-settings]');
    const box = section?.querySelector('[data-autostart-toggle]');
    const status = section?.querySelector('[data-autostart-status]');
    if (!section || !box) return () => {};
    let destroyed = false;
    let busy = false;
    let generation = 0;

    const paint = (state) => {
        const known = Object.hasOwn(NOTES, state);
        section.hidden = !known;
        box.checked = state === 'on';
        box.disabled = false;
        const note = known ? NOTES[state] : '';
        setInlineStatus(status, note, note ? 'warn' : 'muted');
    };

    const refresh = async () => {
        if (busy || destroyed) return;
        const current = ++generation;
        try {
            const { state } = await apiClient.desktopAutostart();
            if (!destroyed && !busy && current === generation) paint(state);
        } catch (error) {
            // Availability unknown stays hidden; a visible block reports the failed read.
            if (destroyed || busy || current !== generation || section.hidden) return;
            box.disabled = true;
            setInlineStatus(status, `Could not read the Windows startup entry: ${error.message}`, 'danger');
        }
    };

    const onChange = async () => {
        if (busy || destroyed) return;
        const wanted = box.checked;
        busy = true;
        generation += 1; // a read that started before the click must not repaint over it
        box.disabled = true;
        setInlineStatus(status, '', 'muted');
        try {
            const { state } = await apiClient.setDesktopAutostart(wanted);
            busy = false;
            if (!destroyed) paint(state);
        } catch (error) {
            // A refusal may land between the two registry writes: show what Windows now holds.
            let state;
            try { ({ state } = await apiClient.desktopAutostart()); } catch { /* current state is unknown */ }
            busy = false;
            if (destroyed) return;
            if (state === undefined) {
                box.checked = !wanted; // last observed value, not a claim about the current registry
                box.disabled = true;
                setInlineStatus(status, `Could not change the Windows startup entry: ${error.message}. Current Windows state could not be read; reopen Settings to retry.`, 'danger');
            } else {
                paint(state);
                setInlineStatus(status, `Could not change the Windows startup entry: ${error.message}`, 'danger');
            }
        }
    };

    const onPageShown = (event) => {
        if (event.detail?.page === 'settings') void refresh();
    };
    const dispose = () => {
        destroyed = true;
        box.removeEventListener('change', onChange);
        window.removeEventListener('ouro:page-shown', onPageShown);
        window.removeEventListener('pagehide', onPageHide);
    };
    const onPageHide = (event) => {
        if (!event.persisted) dispose();
    };

    box.addEventListener('change', onChange);
    window.addEventListener('ouro:page-shown', onPageShown);
    window.addEventListener('pagehide', onPageHide);
    void refresh();
    return dispose;
}
