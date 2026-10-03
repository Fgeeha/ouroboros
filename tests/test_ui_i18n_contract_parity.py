"""The interface-language envelopes have a browser twin: every TypedDict in
ouroboros/gateway/ui_i18n_contracts.py has a JSDoc typedef with the same fields in
web/modules/ui_i18n_types.js (the twin pair beside gateway/contracts.py + api_types.js),
and the generic settings writer refuses the language key — it has one writer."""
from __future__ import annotations

import pathlib
import re
import typing

from ouroboros.gateway import ui_i18n_contracts as contracts
from tests.test_gateway_parity import _js_typedef_fields

REPO = pathlib.Path(__file__).resolve().parents[1]
TWIN = REPO / "web" / "modules" / "ui_i18n_types.js"


def _py_fields(cls) -> set[str]:
    return set(typing.get_type_hints(cls, include_extras=True))


def test_every_interface_language_envelope_has_a_field_identical_browser_twin():
    text = TWIN.read_text(encoding="utf-8")
    names = [name for name in contracts.__all__ if name.startswith("UiI18n")]
    assert names, "the twin pair lost its envelopes"
    for name in names:
        cls = getattr(contracts, name)
        assert re.search(rf"@typedef \{{Object\}} {name}\b", text), f"ui_i18n_types.js lacks {name}"
        assert _js_typedef_fields(text, name) == _py_fields(cls), name
    # The main mirror points at the twin instead of duplicating it.
    api_types = (REPO / "web" / "modules" / "api_types.js").read_text(encoding="utf-8")
    assert "ui_i18n_types.js" in api_types.splitlines()[0]
    assert not re.search(r"@typedef \{Object\} UiI18n", api_types)
    client = (REPO / "web" / "modules" / "api_client.js").read_text(encoding="utf-8")
    assert "import('./api_types.js').UiI18n" not in client, "JSDoc types resolve against the twin, not the index"
    assert "import('./ui_i18n_types.js').UiI18nResponse" in client


def test_the_language_key_has_one_writer():
    """The generic settings save skips endpoint-authored keys; the language is one of them."""
    from ouroboros.config import ENDPOINT_AUTHORED_SETTINGS

    assert "OUROBOROS_UI_LANGUAGE" in ENDPOINT_AUTHORED_SETTINGS
    from ouroboros.gateway import settings as gateway_settings

    assert "OUROBOROS_UI_LANGUAGE" in gateway_settings._ENDPOINT_AUTHORED_SETTINGS  # noqa: SLF001 — the skip set the merger reads


def test_the_server_lifespan_is_an_async_context_manager():
    """The generator's boot hook once pulled the lifespan decorator onto a helper; Starlette
    then ran a deprecated async-generator lifespan. The decorator stays on `lifespan`."""
    import inspect

    import server

    assert not inspect.isasyncgenfunction(server.lifespan)
    wrapped = getattr(server.lifespan, "__wrapped__", None)
    assert wrapped is not None and inspect.isasyncgenfunction(wrapped), "lifespan must be @asynccontextmanager-wrapped"
