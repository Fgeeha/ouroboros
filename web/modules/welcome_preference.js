import { apiClient } from './api_client.js';

export const DEFAULT_WELCOME_TEXT = 'Ouroboros has awakened';

export function welcomeText(value) {
    if (value?.mode === 'hidden') return null;
    if (value?.mode === 'custom') return typeof value.text === 'string' && value.text.trim()
        ? value.text : null;
    return value?.mode === 'default' ? DEFAULT_WELCOME_TEXT : null;
}

// This control saves a presentation preference independently of /api/settings.
// A failed read never paints the default as though it were the owner's saved choice.
export function bindWelcomePreference(root, client = apiClient) {
    const host = root.querySelector('[data-welcome-settings]');
    if (!host) return () => {};
    const mode = host.querySelector('[data-welcome-mode]');
    const text = host.querySelector('[data-welcome-text]');
    const save = host.querySelector('[data-welcome-save]');
    const status = host.querySelector('[data-welcome-status]');
    let alive = true;
    let dirty = false;
    let busy = false;
    let loaded = false;
    let editVersion = 0;
    const show = (message, tone = 'muted') => {
        status.textContent = message;
        status.dataset.tone = tone;
    };
    const sync = () => {
        text.disabled = mode.value !== 'custom';
        save.disabled = busy || !loaded;
    };
    const changed = () => { dirty = true; editVersion++; sync(); };
    mode.addEventListener('change', changed);
    text.addEventListener('input', changed);
    const load = async () => {
        try {
            const value = (await client.uiPreferences())?.welcome;
            if (!alive || dirty) return;
            if (!value || !['default', 'hidden', 'custom'].includes(value.mode)
                || typeof value.text !== 'string') throw new Error('Welcome preference unavailable');
            loaded = true;
            mode.value = value.mode;
            text.value = value.text;
            show('Saved for this installation.');
            sync();
        } catch (error) {
            if (alive) show(`Could not read the welcome preference: ${error.message}`, 'warn');
        }
    };
    const onSave = async () => {
        if (busy || !loaded) return;
        if (mode.value === 'custom' && !text.value.trim()) {
            show('Enter a message, or choose Hidden.', 'warn');
            return;
        }
        busy = true;
        sync();
        const submitted = { mode: mode.value, text: text.value };
        const submittedVersion = editVersion;
        try {
            const result = await client.saveUiPreferences({ welcome: submitted });
            if (!alive) return;
            if (!result?.ok || !result?.welcome) throw new Error('Save was not confirmed');
            if (editVersion === submittedVersion) dirty = false;
            show(dirty ? 'Saved; newer edits have not been saved.' : 'Welcome preference saved.');
            window.dispatchEvent(new CustomEvent('ouro:welcome-changed', { detail: result.welcome }));
        } catch (error) {
            if (alive) show(`Welcome preference was not saved: ${error.message}`, 'warn');
        } finally {
            busy = false;
            if (alive) sync();
        }
    };
    save.addEventListener('click', onSave);
    sync();
    void load();
    return () => {
        alive = false;
        mode.removeEventListener('change', changed);
        text.removeEventListener('input', changed);
        save.removeEventListener('click', onSave);
    };
}
