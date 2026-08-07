"""One contract for masking settings secrets on the wire.

The Settings API answers a GET with a placeholder instead of the stored
credential, so any client can post that placeholder back — the UI does exactly
that when the owner saves without touching a secret field. Producing the mask
and recognising it therefore have to live in the same file: once they drift, an
unrecognised placeholder is persisted as the credential and every consumer
(environment apply, provider catalogs, capability probes) sends it as an
``Authorization`` value, which the provider rejects.

The frontend mirror is ``looksMaskedSecret`` in ``web/modules/utils.js``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)

# Provider/bridge credentials the Settings API masks before it answers a GET.
# The same set gates the read-side sanitizer in ``config.load_settings`` and the
# write-side merge in ``gateway.settings._merge_settings_payload``.
SECRET_SETTING_KEYS = frozenset({
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_PASSWORD",
    "ANTHROPIC_API_KEY",
    "MINIMAX_API_KEY",
    "GITHUB_TOKEN",
    "OUROBOROS_NETWORK_PASSWORD",
})

# Password-class secrets are usually short human-chosen strings: an 8-char
# prefix can BE most of the password. They mask to a constant placeholder;
# long machine-generated API keys keep the recognizable 8-char prefix.
PASSWORD_CLASS_KEYS = frozenset({
    "OUROBOROS_NETWORK_PASSWORD",
    "GIGACHAT_PASSWORD",
    "GIGACHAT_CREDENTIALS",
})

PASSWORD_CLASS_MASK = "***set***"


def mask_password_class(value: Any) -> str:
    return PASSWORD_CLASS_MASK if str(value or "").strip() else ""


def mask_secret_value(value: Any) -> str:
    text = str(value or "")
    return text[:8] + "..." if len(text) > 8 else "***"


def looks_masked_secret(value: Any) -> bool:
    """Whether ``value`` is a display placeholder rather than a real secret.

    Deliberately narrow so a short but genuine credential is never mistaken for
    a placeholder. Only three shapes qualify, and none of them can be a working
    credential: the fixed password-class marker, an all-asterisk run (``**``,
    ``***``, ``********`` — every masker a client can emit), and the
    ``prefix...`` truncation shape produced by ``mask_secret_value``.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if text == PASSWORD_CLASS_MASK or text.endswith("..."):
        return True
    return len(text) >= 2 and set(text) == {"*"}


def strip_masked_secrets(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Blank stored placeholders so they can never be applied as credentials.

    Read-side repair for installs an older round-trip already poisoned: the
    placeholder is dropped on load, the Settings field shows empty, and the
    owner re-enters the real key instead of an endpoint seeing ``Bearer ***``.
    """
    for key in SECRET_SETTING_KEYS:
        if looks_masked_secret(settings.get(key)):
            log.warning("Ignoring masked placeholder stored in %s; treating it as unset.", key)
            settings[key] = ""
    return settings
