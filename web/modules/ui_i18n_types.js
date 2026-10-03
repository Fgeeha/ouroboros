/** Dependency-free JSDoc mirror of `ouroboros.gateway.ui_i18n_contracts` — the `/api/ui/i18n*` envelopes
 *  `api_types.js` indexes; the memory file they describe is ouroboros/i18n_memory.py schema 1. */
/**
 * A language's profile inside its translation memory (ouroboros/i18n_memory.py).
 * @typedef {Object} UiI18nProfile
 * @property {string} label  // display name the owner typed or the generator chose
 * @property {string} instruction  // free-text brief for the generator (an invented language's description)
 * @property {'ltr'|'rtl'} direction
 * @property {string} lexicon  // generator-written vocabulary/rules for rare or invented languages
 */

/**
 * One translation: `text`, or plural `forms` keyed by CLDR category.
 * @typedef {Object} UiI18nEntry
 * @property {string=} text
 * @property {Object.<string,string>=} forms
 * @property {'generated'|'owner'|'imported'} provenance
 * @property {string=} source_hash
 * @property {string=} model
 * @property {string=} at
 * @property {string=} attempt_id
 * @property {string=} pack
 * @property {string=} pack_version
 * @property {string=} context
 */

/**
 * @typedef {Object} UiI18nStats
 * @property {number} entries
 * @property {number} generated
 * @property {number} owner
 * @property {number} imported
 * @property {number|null} stale  // null = the current English of code keys was not available
 * @property {number} pending  // queued misses awaiting the generator
 */

/**
 * One language present on disk, for the Settings select.
 * @typedef {Object} UiI18nLanguageSummary
 * @property {string} language
 * @property {string} label
 * @property {number} entries
 * @property {number} pending
 * @property {boolean} malformed
 */

/**
 * GET /api/ui/i18n and the body of a successful language POST.
 * @typedef {Object} UiI18nResponse
 * @property {boolean=} ok
 * @property {string} language  // BCP-47 tag; "" = not chosen (English source renders)
 * @property {boolean} chosen
 * @property {boolean} english  // not chosen or chosen English: entries are empty by construction
 * @property {number} revision
 * @property {UiI18nProfile|null} profile
 * @property {Object|null} plural_select  // {map: {"0": "other", …}, period: number|null}, written by the browser
 * @property {string[]|null} plural_categories
 * @property {Object.<string,UiI18nEntry>} entries
 * @property {UiI18nStats} stats
 * @property {string} updated_at
 * @property {string} memory_error  // nonempty when the stored file is malformed (English fallback in effect)
 * @property {UiI18nLanguageSummary[]} languages
 */

/**
 * POST /api/ui/i18n/language.
 * @typedef {Object} UiI18nLanguageRequest
 * @property {string} language  // BCP-47 tag ("" = not chosen, "en" = chosen English)
 * @property {string=} label
 * @property {Object=} profile
 * @property {Object=} plural_select  // Intl.PluralRules select(n) for 0..100 (+ period)
 * @property {string[]=} plural_categories
 */

/**
 * POST /api/ui/i18n/missing: strings the renderer could not translate.
 * @typedef {Object} UiI18nMissingRequest
 * @property {string} language  // must equal the install's current language (409 otherwise)
 * @property {Object[]} items  // {key, context?}; shape-filtered, at most 200 per call
 */

/**
 * @typedef {Object} UiI18nMissingResponse
 * @property {number} accepted
 * @property {number} dropped
 * @property {number} pending
 */
