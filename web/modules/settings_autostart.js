import { apiClient } from './api_client.js';
import { setInlineStatus } from './ui_primitives.js';

/* Host startup is an immediate OS edit, separate from the /api/settings draft.
   Re-read when Settings opens: the OS can change the same registration. */

const NOTES = {
    unavailable: 'Sign-in startup is unavailable on this host.',
    on: '',
    off: '',
    other_copy: 'This host starts Ouroboros at sign-in from a different entry (another copy, or one set up by hand). Turn this on to start this copy instead.',
    disabled_by_os: 'Turned off in the host operating system. Turn this on to enable it, or check the host’s startup settings if it stays disabled.',
};

export function bindAutostartControl(page) {
    const section = page.querySelector('[data-autostart-settings]');
    const box = section?.querySelector('[data-autostart-toggle]');
    const status = section?.querySelector('[data-autostart-status]');
    if (!section || !box) return () => {};
    let destroyed = false;
    let busy = false;
    let generation = 0;

    const paint = ({ state, reason }) => {
        const known = Object.hasOwn(NOTES, state);
        section.hidden = !known;
        box.checked = state === 'on';
        box.disabled = state === 'unavailable';
        const note = known ? (reason || NOTES[state]) : '';
        setInlineStatus(status, note, note ? 'warn' : 'muted');
    };

    const refresh = async () => {
        if (busy || destroyed) return;
        const current = ++generation;
        try {
            const snapshot = await apiClient.desktopAutostart();
            if (!destroyed && !busy && current === generation) paint(snapshot);
        } catch (error) {
            // Shown even before any state is known: a lasting read failure stays explained.
            if (destroyed || busy || current !== generation) return;
            section.hidden = false;
            box.disabled = true;
            setInlineStatus(status, `Could not read the host startup entry: ${error.message}`, 'danger');
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
            const snapshot = await apiClient.setDesktopAutostart(wanted);
            busy = false;
            if (!destroyed) paint(snapshot);
        } catch (error) {
            // A refusal may land between the two OS registration writes: show what the OS now holds.
            let snapshot;
            try { snapshot = await apiClient.desktopAutostart(); } catch { /* current state is unknown */ }
            busy = false;
            if (destroyed) return;
            if (snapshot === undefined) {
                box.checked = !wanted; // last observed value, not a claim about the current OS registration
                box.disabled = true;
                setInlineStatus(status, `Could not change the host startup entry: ${error.message}. Current host state could not be read; reopen Settings to retry.`, 'danger');
            } else {
                paint(snapshot);
                setInlineStatus(status, `Could not change the host startup entry: ${error.message}`, 'danger');
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
