// Settings → Appearance → Language: the one control for the install's interface language.
//
// The choice is an install-wide SETTING (OUROBOROS_UI_LANGUAGE), unlike the client-local
// theme beside it: it writes through POST /api/ui/i18n/language on change, never through
// the Settings draft, so it marks nothing dirty. The select lists English, the device's
// language as a suggestion, every language that already has a memory on disk, and
// "Other…", which opens a free field: a code (`pt-BR`), a name, or a description of a
// language to invent. A tag saves at once; a name or a description is resolved by the
// light model into a tag and a profile (the gateway answers `language_needs_model` when
// no model is configured, and the note says so).
import { apiClient } from './api_client.js';
import { showToast } from './toast.js';
import { applyPayload, currentLanguage, englishTag, isEnglish, pluralSelectMap, setLanguage } from './i18n.js';

export const OTHER_VALUE = '__other__';
export const ENGLISH_VALUE = 'en';

function displayName(tag, fallback = tag) {
    if (typeof Intl === 'undefined' || typeof Intl.DisplayNames !== 'function') return fallback;
    try {
        const name = new Intl.DisplayNames([tag, 'en'], { type: 'language' }).of(tag);
        return name && name !== tag ? name : fallback;
    } catch {
        return fallback;
    }
}

function validTag(value) {
    const text = String(value || '').trim();
    return /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,8}$/.test(text) ? text : '';
}

function readiness(summary) {
    if (!summary) return '';
    if (summary.malformed) return 'file unreadable';
    const parts = [];
    if (summary.entries) parts.push(`${summary.entries} ready`);
    if (summary.pending) parts.push(`${summary.pending} pending`);
    return parts.join(', ');
}

/**
 * The option rows of the select, pure: English first, then the device's suggestion, then
 * every language on disk (with its readiness), the current tag when it is none of those,
 * and Other… last. Returns [{value, label}].
 */
export function languageOptions({ payload = null, navigatorLanguages = [], current = '' } = {}) {
    const rows = [{ value: ENGLISH_VALUE, label: 'English (source)' }];
    const seen = new Set([ENGLISH_VALUE]);
    const onDisk = new Map();
    for (const item of payload?.languages || []) {
        const tag = validTag(item?.language);
        if (tag && !englishTag(tag)) onDisk.set(tag, item);
    }
    const device = (navigatorLanguages || []).map(validTag).find((tag) => tag && !englishTag(tag) && !onDisk.has(tag));
    if (device) {
        seen.add(device);
        rows.push({ value: device, label: `${displayName(device, device)} · this device's language` });
    }
    for (const [tag, item] of onDisk) {
        seen.add(tag);
        const base = item?.label && item.label !== tag ? item.label : displayName(tag, tag);
        const ready = readiness(item);
        rows.push({ value: tag, label: ready ? `${base} · ${ready}` : base });
    }
    const chosen = validTag(current);
    if (chosen && !seen.has(chosen) && !englishTag(chosen)) {
        seen.add(chosen);
        rows.push({ value: chosen, label: displayName(chosen, chosen) });
    }
    rows.push({ value: OTHER_VALUE, label: 'Other…' });
    return rows;
}

/** The status line under the control, pure. */
export function describeStatus(payload) {
    if (!payload) return '';
    if (payload.english) return payload.chosen ? 'English, the source text.' : 'English, the source text; no language chosen yet.';
    const label = payload.profile?.label && payload.profile.label !== payload.language
        ? payload.profile.label : displayName(payload.language, payload.language);
    const stats = payload.stats || {};
    const parts = [`${stats.entries || 0} translated`];
    if (stats.pending) parts.push(`${stats.pending} pending`);
    if (typeof stats.stale === 'number' && stats.stale) parts.push(`${stats.stale} stale`);
    if (payload.memory_error) parts.push('stored file unreadable, English shown');
    const generator = payload.generator || {};
    if (generator.state === 'running') parts.push('translating…');
    else if (generator.state === 'no_model') parts.push('no model configured to translate; set one up in Models');
    else if (generator.state === 'failed') parts.push(`translation paused: ${generator.error || 'the model call failed'}`);
    return `${label}: ${parts.join(' · ')}`;
}

/**
 * Wire the `[data-i18n-settings]` block. Returns a disposer. `client`, `toast` and
 * `navigatorLanguages` are injectable for tests.
 */
export function bindLanguageSettings(page, {
    client = apiClient, toast = showToast,
    navigatorLanguages = (typeof navigator !== 'undefined' && navigator.languages) || [],
    openUrl = (url) => { if (typeof window !== 'undefined') window.open(url, '_blank', 'noopener'); },
} = {}) {
    const root = page?.querySelector?.('[data-i18n-settings]');
    if (!root) return () => {};
    const select = root.querySelector('[data-i18n-select]');
    const other = root.querySelector('[data-i18n-other]');
    const otherInput = root.querySelector('[data-i18n-other-input]');
    const otherApply = root.querySelector('[data-i18n-other-apply]');
    const status = root.querySelector('[data-i18n-status]');
    const note = root.querySelector('[data-i18n-note]');
    const importButton = root.querySelector('[data-i18n-import]');
    const importFile = root.querySelector('[data-i18n-import-file]');
    const exportButton = root.querySelector('[data-i18n-export]');
    const regenerateButton = root.querySelector('[data-i18n-regenerate]');
    let payload = null;
    let busy = false;

    const setNote = (text) => { if (note) note.textContent = text || ''; };
    const setStatus = (text) => { if (status) status.textContent = text || ''; };

    function render() {
        if (!select) return;
        const current = payload ? (payload.english ? ENGLISH_VALUE : payload.language) : (isEnglish() ? ENGLISH_VALUE : currentLanguage());
        const rows = languageOptions({ payload, navigatorLanguages, current });
        select.replaceChildren(...rows.map((row) => {
            const option = document.createElement('option');
            option.value = row.value;
            option.textContent = row.label;
            return option;
        }));
        select.value = rows.some((row) => row.value === current) ? current : ENGLISH_VALUE;
        if (other) other.hidden = select.value !== OTHER_VALUE;
        setStatus(describeStatus(payload));
        for (const button of [exportButton, regenerateButton]) {
            if (button) button.disabled = !payload || payload.english;
        }
    }

    async function load() {
        try {
            payload = await client.uiI18n();
            applyPayload(payload);
            render();
        } catch {
            setStatus('Language settings could not be loaded.');
        }
    }

    async function choose(raw) {
        const value = String(raw || '').trim();
        if (!value || busy) return;
        busy = true;
        setNote('');
        const body = { language: value };
        const tag = validTag(value);
        if (tag) {
            const plural = pluralSelectMap(tag);
            if (plural) {
                body.plural_select = { map: plural.map, period: plural.period };
                body.plural_categories = plural.categories;
            }
            const label = displayName(tag, '');
            if (label) body.label = label;
        }
        try {
            payload = await client.saveUiLanguage(body);
            // A name the gateway resolved to a tag has no plural map yet (the browser could
            // not compute one for free text): complete the header with this engine's rules.
            if (!tag && payload && !payload.english && !payload.plural_select) {
                const plural = pluralSelectMap(payload.language);
                if (plural) {
                    payload = await client.saveUiLanguage({
                        language: payload.language,
                        plural_select: { map: plural.map, period: plural.period },
                        plural_categories: plural.categories,
                    });
                }
            }
            applyPayload(payload);
            render();
            if (otherInput) otherInput.value = '';
        } catch (error) {
            const code = error?.body?.code || error?.payload?.code || error?.code || '';
            if (code === 'language_not_a_tag' || code === 'language_needs_model' || code === 'language_resolve_failed') {
                setNote(code === 'language_needs_model'
                    ? 'A language name needs a model: set one up in Models, or type a code such as pt-BR.'
                    : code === 'language_resolve_failed'
                        ? 'Ouroboros could not work out this language right now. Try again, or type a code such as pt-BR.'
                        : 'Not recognized as a language. Type a code such as pt-BR, a language name, or describe a language to invent.');
                if (select) select.value = OTHER_VALUE;
                if (other) other.hidden = false;
            } else {
                toast('Language choice could not be saved.', 'error');
                render();
            }
        } finally {
            busy = false;
        }
    }

    const onSelect = () => {
        if (!select) return;
        if (select.value === OTHER_VALUE) {
            if (other) other.hidden = false;
            otherInput?.focus?.();
            return;
        }
        void choose(select.value);
    };
    const onOtherApply = () => void choose(otherInput?.value);
    const onOtherKey = (event) => { if (event.key === 'Enter') { event.preventDefault(); onOtherApply(); } };
    const onImportClick = () => importFile?.click?.();
    const onImportFile = async () => {
        const file = importFile?.files?.[0];
        if (!file) return;
        try {
            const text = await file.text();
            const doc = JSON.parse(text);
            const result = await client.importI18n(doc);
            toast(`Imported ${result?.result?.added ?? 0} new and replaced ${result?.result?.replaced ?? 0} translations.`, 'success');
            await load();
        } catch (error) {
            toast(error?.body?.error || error?.message || 'The file is not a valid translation memory.', 'error');
        } finally {
            if (importFile) importFile.value = '';
        }
    };
    const onExport = () => { if (payload && !payload.english) openUrl(client.exportI18nUrl(payload.language)); };
    const onRegenerate = async () => {
        if (!payload || payload.english || busy) return;
        busy = true;
        try {
            await client.regenerateI18n({ language: payload.language });
            toast('Generated translations dropped; your own and imported ones stay. Rebuilding.', 'success');
            await load();
        } catch {
            toast('Regeneration could not be started.', 'error');
        } finally {
            busy = false;
        }
    };
    const onLanguageChanged = () => { if (!busy) void load(); };

    select?.addEventListener('change', onSelect);
    otherApply?.addEventListener('click', onOtherApply);
    otherInput?.addEventListener('keydown', onOtherKey);
    importButton?.addEventListener('click', onImportClick);
    importFile?.addEventListener('change', onImportFile);
    exportButton?.addEventListener('click', onExport);
    regenerateButton?.addEventListener('click', onRegenerate);
    if (typeof window !== 'undefined') window.addEventListener('ouro:language-changed', onLanguageChanged);
    void load();
    return () => {
        select?.removeEventListener('change', onSelect);
        otherApply?.removeEventListener('click', onOtherApply);
        otherInput?.removeEventListener('keydown', onOtherKey);
        importButton?.removeEventListener('click', onImportClick);
        importFile?.removeEventListener('change', onImportFile);
        exportButton?.removeEventListener('click', onExport);
        regenerateButton?.removeEventListener('click', onRegenerate);
        if (typeof window !== 'undefined') window.removeEventListener('ouro:language-changed', onLanguageChanged);
    };
}

// `setLanguage` is re-exported so a caller that already has a payload (the POST body) can
// paint without a second read.
export { setLanguage };
