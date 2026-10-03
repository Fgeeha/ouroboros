// Settings → Appearance → Language: the option rows, the status line, and the binder's
// flows (choose a listed language, type an unusual one, import, export, regenerate), with
// the gateway client, the toast and the device languages injected. The control writes the
// install-wide setting at once and never through the Settings draft.
import test from 'node:test';
import assert from 'node:assert/strict';
import { bindLanguageSettings, describeStatus, languageOptions, ENGLISH_VALUE, OTHER_VALUE } from '../modules/settings_language.js';

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('the option rows: English, the device suggestion, every memory on disk, the odd current tag, Other…', () => {
    const rows = languageOptions({
        payload: { languages: [{ language: 'ru', entries: 1200, pending: 3, label: 'Русский' }, { language: 'en' }] },
        navigatorLanguages: ['de-DE', 'en-US'],
        current: 'qya',
    });
    assert.deepEqual(rows.map((row) => row.value), [ENGLISH_VALUE, 'de-DE', 'ru', 'qya', OTHER_VALUE]);
    assert.equal(rows[0].label, 'English (source)');
    assert.ok(rows[1].label.endsWith("· this device's language"), rows[1].label);
    assert.equal(rows[2].label, 'Русский · 1200 ready, 3 pending');
    assert.equal(rows[4].label, 'Other…');
    // A device language that already has a memory is listed once, as the memory.
    const once = languageOptions({ payload: { languages: [{ language: 'de' }] }, navigatorLanguages: ['de'] });
    assert.deepEqual(once.map((row) => row.value), [ENGLISH_VALUE, 'de', OTHER_VALUE]);
    // Garbage in the payload never becomes an option.
    const clean = languageOptions({ payload: { languages: [{ language: 'Russian please' }, {}] } });
    assert.deepEqual(clean.map((row) => row.value), [ENGLISH_VALUE, OTHER_VALUE]);
});

test('the status line states the source, or the language with its counts and a damaged file', () => {
    assert.equal(describeStatus(null), '');
    assert.equal(describeStatus({ english: true, chosen: false }), 'English, the source text; no language chosen yet.');
    assert.equal(describeStatus({ english: true, chosen: true }), 'English, the source text.');
    assert.equal(describeStatus({ english: false, language: 'ru', profile: { label: 'Русский' }, stats: { entries: 1200, pending: 3, stale: 2 } }),
        'Русский: 1200 translated · 3 pending · 2 stale');
    assert.equal(describeStatus({ english: false, language: 'qya', stats: { entries: 0 }, memory_error: 'bad json' }),
        'qya: 0 translated · stored file unreadable, English shown');
});

// ---------------------------------------------------------------------------
// A flat DOM double: the binder reads [data-i18n-*] nodes, listens to a few events,
// fills the <select> through document.createElement, and toggles hidden/disabled.
// ---------------------------------------------------------------------------

class Stub {
    constructor(tag, attrs = {}) {
        this.tagName = tag.toUpperCase();
        this.attrs = { ...attrs };
        this.children = [];
        this.listeners = new Map();
        this.hidden = false;
        this.disabled = false;
        this.value = '';
        this.textContent = '';
        this.files = [];
        this.focused = 0;
        this.clicked = 0;
    }

    matches(selector) {
        const attr = /^\[([^\]=]+)\]$/.exec(selector.trim());
        if (attr) return attr[1] in this.attrs;
        if (selector.startsWith('#')) return this.attrs.id === selector.slice(1);
        return this.tagName === selector.toUpperCase();
    }

    querySelector(selector) {
        for (const child of this.children) {
            if (child.matches(selector)) return child;
            const deep = child.querySelector(selector);
            if (deep) return deep;
        }
        return null;
    }

    addEventListener(type, fn) { this.listeners.set(type, fn); }

    removeEventListener(type) { this.listeners.delete(type); }

    fire(type, extra = {}) {
        const fn = this.listeners.get(type);
        return fn ? fn({ type, target: this, preventDefault() { this.prevented = true; }, ...extra }) : undefined;
    }

    replaceChildren(...nodes) { this.children = nodes; }

    focus() { this.focused += 1; }

    click() { this.clicked += 1; }
}

function page() {
    const root = new Stub('div', { 'data-i18n-settings': '' });
    const parts = {
        select: new Stub('select', { 'data-i18n-select': '' }),
        other: new Stub('div', { 'data-i18n-other': '' }),
        otherInput: new Stub('input', { 'data-i18n-other-input': '' }),
        otherApply: new Stub('button', { 'data-i18n-other-apply': '' }),
        note: new Stub('div', { 'data-i18n-note': '' }),
        status: new Stub('div', { 'data-i18n-status': '' }),
        importButton: new Stub('button', { 'data-i18n-import': '' }),
        importFile: new Stub('input', { 'data-i18n-import-file': '' }),
        exportButton: new Stub('button', { 'data-i18n-export': '' }),
        regenerateButton: new Stub('button', { 'data-i18n-regenerate': '' }),
    };
    parts.other.hidden = true;
    parts.other.children = [parts.otherInput, parts.otherApply, parts.note];
    root.children = [parts.select, parts.other, parts.status, parts.importButton, parts.importFile, parts.exportButton, parts.regenerateButton];
    const doc = new Stub('div');
    doc.children = [root];
    return { doc, ...parts };
}

function fakeClient(initial) {
    const calls = [];
    let current = initial;
    return {
        calls,
        set(payload) { current = payload; },
        uiI18n: async () => { calls.push(['uiI18n']); return current; },
        saveUiLanguage: async (body) => {
            calls.push(['saveUiLanguage', body]);
            if (body.language === 'Quenya') throw Object.assign(new Error('bad'), { status: 400, body: { code: 'language_not_a_tag' } });
            current = { language: body.language, english: false, chosen: true, entries: {}, revision: 1,
                profile: { label: body.label || body.language }, stats: { entries: 0, pending: 0 }, languages: current.languages };
            return current;
        },
        importI18n: async (doc) => { calls.push(['importI18n', doc]); return { result: { added: 2, replaced: 1 } }; },
        exportI18nUrl: (language) => `/api/ui/i18n/export?language=${language}`,
        regenerateI18n: async (body) => { calls.push(['regenerateI18n', body]); return { ok: true }; },
    };
}

function withDocument(fn) {
    const had = 'document' in globalThis;
    const saved = globalThis.document;
    globalThis.document = { createElement: (tag) => new Stub(tag), body: null };
    return Promise.resolve(fn()).finally(() => {
        if (had) globalThis.document = saved;
        else delete globalThis.document;
    });
}

const ENGLISH = { language: '', english: true, chosen: false, entries: {}, revision: 0, stats: null,
    languages: [{ language: 'ru', entries: 1200, pending: 3, label: 'Русский' }] };

test('boot lists the languages and shows the English source; export and regenerate wait for a language', () => withDocument(async () => {
    const p = page();
    const client = fakeClient(ENGLISH);
    const toasts = [];
    const dispose = bindLanguageSettings(p.doc, { client, toast: (m, k) => toasts.push([m, k]), navigatorLanguages: ['de'] });
    await settle();
    assert.deepEqual(p.select.children.map((o) => o.value), [ENGLISH_VALUE, 'de', 'ru', OTHER_VALUE]);
    assert.equal(p.select.value, ENGLISH_VALUE);
    assert.equal(p.other.hidden, true);
    assert.equal(p.status.textContent, 'English, the source text; no language chosen yet.');
    assert.equal(p.exportButton.disabled, true);
    assert.equal(p.regenerateButton.disabled, true);
    assert.deepEqual(toasts, []);
    dispose();
    assert.equal(p.select.listeners.size, 0);
    assert.equal(p.importFile.listeners.size, 0);
}));

test('choosing a listed language saves the tag with the plural map and a display label, then paints the status', () => withDocument(async () => {
    const p = page();
    const client = fakeClient(ENGLISH);
    bindLanguageSettings(p.doc, { client, toast: () => {}, navigatorLanguages: [] });
    await settle();
    p.select.value = 'ru';
    p.select.fire('change');
    await settle();
    const save = client.calls.find(([name]) => name === 'saveUiLanguage');
    assert.ok(save, 'the choice writes through the language endpoint at once');
    const body = save[1];
    assert.equal(body.language, 'ru');
    assert.equal(body.plural_select.map['1'], 'one');
    assert.equal(body.plural_select.map['5'], 'many');
    assert.equal(body.plural_select.period, 100);
    assert.ok(body.plural_categories.includes('few'));
    assert.ok(typeof body.label === 'string' && body.label.length > 0, 'a display name travels with the tag');
    assert.equal(p.select.value, 'ru');
    assert.ok(p.status.textContent.startsWith(`${body.label}: 0 translated`), p.status.textContent);
    assert.equal(p.exportButton.disabled, false);
    assert.equal(p.regenerateButton.disabled, false);
}));

test('Other… opens the free field; a tag saves, a description the gateway cannot resolve yet is explained inline', () => withDocument(async () => {
    const p = page();
    const client = fakeClient(ENGLISH);
    bindLanguageSettings(p.doc, { client, toast: () => {}, navigatorLanguages: [] });
    await settle();
    p.select.value = OTHER_VALUE;
    p.select.fire('change');
    assert.equal(p.other.hidden, false);
    assert.equal(p.otherInput.focused, 1);
    assert.equal(client.calls.filter(([name]) => name === 'saveUiLanguage').length, 0, 'opening the field saves nothing');

    p.otherInput.value = 'Quenya';
    p.otherApply.fire('click');
    await settle();
    assert.equal(client.calls.at(-1)[1].language, 'Quenya');
    assert.ok(p.note.textContent.startsWith('Not recognized as a language code yet'), p.note.textContent);
    assert.equal(p.select.value, OTHER_VALUE);
    assert.equal(p.other.hidden, false);

    p.otherInput.value = 'pt-BR';
    const event = { key: 'Enter' };
    p.otherInput.fire('keydown', event);
    await settle();
    const save = client.calls.at(-1);
    assert.equal(save[0], 'saveUiLanguage');
    assert.equal(save[1].language, 'pt-BR');
    assert.equal(p.note.textContent, '');
    assert.equal(p.otherInput.value, '');
    assert.equal(p.select.value, 'pt-BR');
}));

test('import reads the chosen file into the import endpoint and reloads; export and regenerate act on the current language', () => withDocument(async () => {
    const p = page();
    const client = fakeClient({ ...ENGLISH, language: 'ru', english: false, chosen: true, profile: { label: 'Русский' }, stats: { entries: 10, pending: 0 } });
    const toasts = [];
    const opened = [];
    bindLanguageSettings(p.doc, { client, toast: (m, k) => toasts.push([m, k]), navigatorLanguages: [], openUrl: (url) => opened.push(url) });
    await settle();
    assert.equal(p.status.textContent, 'Русский: 10 translated');

    p.importButton.fire('click');
    assert.equal(p.importFile.clicked, 1);
    p.importFile.files = [{ text: async () => JSON.stringify({ schema: 1, language: 'ru', entries: {} }) }];
    p.importFile.fire('change');
    await settle();
    const imported = client.calls.find(([name]) => name === 'importI18n');
    assert.deepEqual(imported[1], { schema: 1, language: 'ru', entries: {} });
    assert.deepEqual(toasts.at(-1), ['Imported 2 new and replaced 1 translations.', 'success']);
    assert.equal(p.importFile.value, '');

    p.importFile.files = [{ text: async () => 'not json' }];
    p.importFile.fire('change');
    await settle();
    assert.equal(toasts.at(-1)[1], 'error');

    p.exportButton.fire('click');
    assert.deepEqual(opened, ['/api/ui/i18n/export?language=ru']);

    p.regenerateButton.fire('click');
    await settle();
    const regen = client.calls.find(([name]) => name === 'regenerateI18n');
    assert.deepEqual(regen[1], { language: 'ru' });
    assert.equal(toasts.at(-1)[1], 'success');
}));

test('a page without the block binds nothing and disposes harmlessly', () => {
    const dispose = bindLanguageSettings(new Stub('div'), { client: {}, toast: () => {} });
    assert.equal(typeof dispose, 'function');
    dispose();
});
