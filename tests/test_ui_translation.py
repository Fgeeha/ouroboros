"""The translation generator (ouroboros/ui_translation.py): the catalog it enumerates, the
queue it drains, the validation every model answer passes, the worker's single flight,
and the free-text language resolution — all with a fake light model, never a provider."""
from __future__ import annotations

import json
import threading

import pytest

from ouroboros import i18n_memory as memory
from ouroboros import ui_translation as gen


class FakeLight:
    """``LLMClient.chat`` double: answers from a queue of (content, stop) pairs and records
    every request so a test can read the prompt the generator built."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.answers:
            raise AssertionError("the generator called the model more often than the test planned")
        content, stop = self.answers.pop(0)
        return {"content": content}, {"response_finish_reason": stop}


@pytest.fixture(autouse=True)
def _isolated_generator(monkeypatch):
    """Every test gets its own worker, catalog registrations and gateway hook list, so a
    registration or a thread from one test never reaches another test (or the API tests)."""
    from ouroboros.gateway import ui_i18n

    monkeypatch.setattr(ui_i18n, "_LANGUAGE_HOOKS", [])
    monkeypatch.setattr(gen, "_WORKER", gen._Worker())
    monkeypatch.setattr(gen, "_IN_FLIGHT", {})
    monkeypatch.setattr(gen, "_EXTRA_CATALOGS", dict(gen._EXTRA_CATALOGS))
    yield
    gen.set_client_factory(None)
    gen.stop_background(timeout=5.0)


@pytest.fixture
def light(monkeypatch):
    """A credentialed light slot without a provider: the route is fixed, the client is injected."""
    monkeypatch.setattr(gen, "_light_route", lambda: ("fake/light", False, True))
    monkeypatch.setattr(gen, "_timeout_sec", lambda: 30.0)
    return monkeypatch


def _ru(tmp_path, **header):
    memory.update_memory(tmp_path, "ru", lambda doc: None, create=True,
                         plural_select={"map": {"1": "one", "2": "few", "5": "many", "0": "many"}, "period": None},
                         plural_categories=["one", "few", "many", "other"], **header)


def _answer(translations):
    return json.dumps({"translations": translations}, ensure_ascii=False), "stop"


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------


def test_catalog_enumerates_every_host_table_by_code_with_its_english():
    entries = gen.catalog_entries()
    assert entries["code:task.headline.done"]["text"] == "Done"
    assert entries["code:question.status.waiting"]["text"] == "Waiting for your answer"
    assert entries["code:task.cause.author_stop"]["text"].startswith("Ouroboros stopped")
    assert entries["code:routing.refusal.target_unknown"]["text"] == "that task is no longer running"
    assert all(key.startswith("code:") for key in entries)
    hashes = gen.catalog_source_hashes()
    assert hashes["code:task.headline.done"] == memory.source_hash("Done")
    assert set(hashes) == set(entries)


def test_registered_catalogs_join_the_enumeration(monkeypatch):
    monkeypatch.setattr(gen, "_EXTRA_CATALOGS", {})
    gen.register_catalog("tg.phase", lambda: {"working": "Working on it"}, "Telegram's card phase word")
    entries = gen.catalog_entries()
    assert entries["code:tg.phase.working"] == {"text": "Working on it",
                                                 "context": {"table": "tg.phase", "note": "Telegram's card phase word"}}


def test_enqueue_catalog_queues_missing_and_stale_codes_but_never_an_owner_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "_EXTRA_CATALOGS", {})
    _ru(tmp_path)

    def _seed(doc):
        memory.set_owner_entry(doc, "code:task.headline.done", {"text": "Готово"})
        memory.apply_generated(doc, {
            "code:task.headline.error": {"text": "Сбой"},
            "code:task.headline.warn": {"text": "Готово с оговорками"},
        }, model="fake", source_hashes={"code:task.headline.error": memory.source_hash("Failed"),
                                        "code:task.headline.warn": memory.source_hash("an older English")})
        return doc

    memory.update_memory(tmp_path, "ru", _seed)
    queued = gen.enqueue_catalog(tmp_path, "ru")
    pending = dict(memory.take_pending(tmp_path, "ru", 10_000))
    assert queued == len(pending) == len(gen.catalog_entries()) - 2, "every code but the pin and the fresh generated one"
    assert "code:task.headline.done" not in pending, "an owner pin is never regenerated"
    assert "code:task.headline.error" not in pending, "a generated entry whose English did not move stays"
    assert pending["code:task.headline.warn"]["context"]["stale"] is True
    assert pending["code:task.headline.warn"]["context"]["source"] == "Done with warnings"
    assert pending["code:task.cause.author_stop"]["context"]["table"] == "task.cause"
    assert gen.enqueue_catalog(tmp_path, "ru") == 0 or memory.pending_count(tmp_path, "ru") >= 0  # idempotent on the queue


# ---------------------------------------------------------------------------
# one batch
# ---------------------------------------------------------------------------


def test_translate_batch_writes_validated_answers_as_generated_entries_and_broadcasts(tmp_path, light):
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [
        {"key": "Settings", "context": {"role": "button", "page": "settings"}},
        {"key": "{n} notes", "context": {"source": "{n} notes", "params": ["n"]}},
        {"key": "code:task.headline.done", "context": {"source": "Done", "table": "task.headline"}},
        {"key": "New task in {name}", "context": {"source": "New task in {name}", "params": ["name"]}},
    ])
    client = FakeLight([_answer([
        {"id": 0, "text": "Настройки"},
        {"id": 1, "forms": {"one": "{n} заметка", "few": "{n} заметки", "many": "{n} заметок", "other": "{n} заметки"}},
        {"id": 2, "text": "Готово"},
        {"id": 3, "text": "Новая задача в {name}"},
    ])])
    frames = []
    light.setattr(gen, "_broadcast", frames.append)
    facts = gen.translate_batch(tmp_path, "ru", client=client)
    assert facts == {"taken": 4, "applied": 4, "requeued": 0, "dropped": 0, "error": ""}
    doc = memory.load_memory(tmp_path, "ru")
    assert doc["entries"]["Settings"]["text"] == "Настройки"
    assert doc["entries"]["Settings"]["provenance"] == "generated"
    assert doc["entries"]["Settings"]["model"] == "fake/light"
    assert doc["entries"]["{n} notes"]["forms"]["many"] == "{n} заметок"
    assert doc["entries"]["code:task.headline.done"]["source_hash"] == memory.source_hash("Done")
    assert memory.fmt("{n} notes", {"n": 5}, "ru", drive_root=tmp_path) == "5 заметок"
    assert memory.tr("code:task.headline.done", "ru", "Done", drive_root=tmp_path) == "Готово"
    assert memory.pending_count(tmp_path, "ru") == 0
    assert frames == [{"type": "ui_i18n_updated", "language": "ru", "revision": doc["revision"], "applied": 4}]
    # The prompt: a stable prefix (rules, plural categories) and the batch with its context.
    request = client.calls[0]
    assert request["model"] == "fake/light" and request["model_role"] == "light"
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == gen.UI_TRANSLATION_MAX_TOKENS
    system, user = request["messages"][0]["content"], request["messages"][1]["content"]
    assert "one, few, many, other" in system and "`{n}`" in system
    rows = json.loads(user.split("\n", 1)[1])
    assert rows[0] == {"id": 0, "text": "Settings", "plural": False, "placeholders": [],
                       "context": {"role": "button", "page": "settings"}}
    assert rows[1]["plural"] is True and rows[1]["placeholders"] == ["{n}"]
    assert rows[2]["text"] == "Done" and "source" not in rows[2]["context"]


def test_invalid_answers_are_requeued_with_their_attempt_and_dropped_after_the_bound(tmp_path, light):
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [
        {"key": "Delete {name}", "context": {"source": "Delete {name}"}},   # foreign placeholder
        {"key": "Files", "context": {}},                                      # markup
        {"key": "{n} errors", "context": {"source": "{n} errors"}},           # a category the language lacks
        {"key": "Save", "context": {}},                                       # missing from the answer
        {"key": "Open", "context": {}},                                       # fine
    ])
    client = FakeLight([_answer([
        {"id": 0, "text": "Удалить {title}"},
        {"id": 1, "text": "<b>Файлы</b>"},
        {"id": 2, "forms": {"one": "{n} ошибка", "two": "{n} ошибки", "other": "{n} ошибок"}},
        {"id": 4, "text": "Открыть"},
    ])])
    light.setattr(gen, "_broadcast", lambda frame: None)
    facts = gen.translate_batch(tmp_path, "ru", client=client)
    assert facts["applied"] == 1 and facts["requeued"] == 4 and facts["dropped"] == 0
    pending = dict(memory.take_pending(tmp_path, "ru", 100))
    assert set(pending) == {"Delete {name}", "Files", "{n} errors", "Save"}
    assert all(item["attempts"] == 1 for item in pending.values())
    assert pending["Files"]["last_error"] == "no valid answer in the batch"
    # Two more failed batches drop the keys, with a warning, never a silent loop.
    for item in pending.values():
        item["attempts"] = gen.MAX_ATTEMPTS - 1
    memory.requeue_pending(tmp_path, "ru", list(pending.items()))
    client = FakeLight([_answer([])])
    facts = gen.translate_batch(tmp_path, "ru", client=client)
    assert facts["dropped"] == 4 and facts["requeued"] == 0
    assert memory.pending_count(tmp_path, "ru") == 0


def test_a_batch_cut_by_the_output_budget_is_a_failed_batch(tmp_path, light):
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [{"key": "Settings", "context": {}}, {"key": "Files", "context": {}}])
    client = FakeLight([(json.dumps({"translations": [{"id": 0, "text": "Настройки"}]}), "length")])
    facts = gen.translate_batch(tmp_path, "ru", client=client)
    assert facts["error"] == "output_truncated" and facts["applied"] == 0 and facts["requeued"] == 2
    assert memory.load_memory(tmp_path, "ru")["entries"] == {}, "a partial answer is never half-trusted"
    assert memory.pending_count(tmp_path, "ru") == 2


def test_a_provider_failure_requeues_the_batch_and_names_the_cause(tmp_path, light):
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [{"key": "Settings", "context": {}}])

    class Broken:
        def chat(self, **kwargs):
            raise ConnectionError("provider down")

    facts = gen.translate_batch(tmp_path, "ru", client=Broken())
    assert facts["requeued"] == 1 and facts["error"].startswith("ConnectionError")
    pending = dict(memory.take_pending(tmp_path, "ru", 10))
    assert pending["Settings"]["attempts"] == 1


def test_without_a_credentialed_light_model_the_queue_is_kept_and_the_state_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "_light_route", lambda: ("", False, False))
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [{"key": "Settings", "context": {}}])
    facts = gen.translate_batch(tmp_path, "ru")
    assert facts["error"] == "language_needs_model" and facts["requeued"] == 1
    assert memory.pending_count(tmp_path, "ru") == 1


def test_owner_pins_survive_a_generated_answer_for_the_same_key(tmp_path, light):
    _ru(tmp_path)
    memory.update_memory(tmp_path, "ru", lambda doc: (memory.set_owner_entry(doc, "Settings", {"text": "Параметры"}), doc)[1])
    memory.record_missing(tmp_path, "ru", [{"key": "Settings", "context": {}}])
    light.setattr(gen, "_broadcast", lambda frame: None)
    facts = gen.translate_batch(tmp_path, "ru", client=FakeLight([_answer([{"id": 0, "text": "Настройки"}])]))
    assert facts["applied"] == 0
    assert memory.load_memory(tmp_path, "ru")["entries"]["Settings"]["text"] == "Параметры"


# ---------------------------------------------------------------------------
# the worker and the hooks
# ---------------------------------------------------------------------------


def test_the_hook_queues_the_catalog_on_a_language_choice_and_the_worker_drains_it(tmp_path, light):
    light.setenv("OUROBOROS_UI_LANGUAGE", "ru")
    light.setattr(gen, "_EXTRA_CATALOGS", {})
    _ru(tmp_path)
    catalog = gen.catalog_entries()
    answers = []
    # Every batch answers every id with a marked translation.
    for start in range(0, len(catalog), gen.BATCH_KEYS):
        count = min(gen.BATCH_KEYS, len(catalog) - start)
        answers.append(_answer([{"id": i, "text": f"RU#{i}"} for i in range(count)]))
    client = FakeLight(answers)
    done = threading.Event()
    frames = []

    def record(frame):
        frames.append(frame)
        if memory.pending_count(tmp_path, "ru") == 0:
            done.set()

    light.setattr(gen, "_broadcast", record)
    light.setattr(gen, "_WORKER", gen._Worker())
    gen.set_client_factory(lambda: client)
    try:
        gen.on_language_event("language_set", tmp_path, "ru")
        assert done.wait(10), gen.generator_status()
        gen._WORKER.thread.join(timeout=5)
    finally:
        gen.set_client_factory(None)
    doc = memory.load_memory(tmp_path, "ru")
    assert len(doc["entries"]) == len(catalog)
    assert doc["entries"]["code:task.headline.done"]["text"].startswith("RU#")
    assert all(frame["type"] == "ui_i18n_updated" for frame in frames)
    assert gen.generator_status()["state"] == "idle" and gen.generator_status()["applied"] == len(catalog)
    assert len(client.calls) == len(answers)


def test_the_worker_stops_when_the_language_changes_under_it(tmp_path, light):
    light.setenv("OUROBOROS_UI_LANGUAGE", "ru")
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [{"key": f"Label {i}", "context": {}} for i in range(3)])

    class Switching:
        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            light.setenv("OUROBOROS_UI_LANGUAGE", "de")  # the owner picked another language mid-batch
            return {"content": json.dumps({"translations": [{"id": i, "text": f"X{i}"} for i in range(3)]})}, {"response_finish_reason": "stop"}

    client = Switching()
    light.setattr(gen, "_broadcast", lambda frame: None)
    light.setattr(gen, "_WORKER", gen._Worker())
    gen.set_client_factory(lambda: client)
    try:
        gen.on_language_event("missing", tmp_path, "ru")
        gen._WORKER.thread.join(timeout=10)
    finally:
        gen.set_client_factory(None)
    assert client.calls == 1, "the batch in flight finishes; no second batch starts for a language that is no longer chosen"
    assert memory.load_memory(tmp_path, "ru")["entries"]["Label 0"]["text"] == "X0"


def test_english_events_and_boot_without_a_language_do_nothing(tmp_path, light):
    light.setenv("OUROBOROS_UI_LANGUAGE", "")
    light.setattr(gen, "_WORKER", gen._Worker())
    gen.on_language_event("language_set", tmp_path, "en")
    gen.on_language_event("language_set", tmp_path, "")
    gen.start_background(tmp_path)
    assert gen._WORKER.thread is None
    assert not (tmp_path / "state" / "i18n").exists()


def test_boot_re_check_queues_the_catalog_for_the_chosen_language(tmp_path, light):
    light.setenv("OUROBOROS_UI_LANGUAGE", "ru")
    light.setattr(gen, "_EXTRA_CATALOGS", {})
    light.setattr(gen, "_WORKER", gen._Worker())
    _ru(tmp_path)
    scheduled = []
    light.setattr(gen._WORKER, "schedule", lambda root: scheduled.append(root))
    from ouroboros.gateway import ui_i18n

    light.setattr(ui_i18n, "_LANGUAGE_HOOKS", [])  # this test's registration must not outlive it
    gen.start_background(tmp_path)
    assert scheduled == [tmp_path]
    assert memory.pending_count(tmp_path, "ru") == len(gen.catalog_entries())
    assert gen.on_language_event in ui_i18n._LANGUAGE_HOOKS


# ---------------------------------------------------------------------------
# a free-text language → a profile
# ---------------------------------------------------------------------------


def test_resolve_language_request_returns_a_profile_for_a_known_and_an_invented_language(tmp_path, light):
    known = FakeLight([(json.dumps({"tag": "QYA", "label": "Quenya", "direction": "ltr",
                                    "instruction": "Tolkien's High-elven", "lexicon": "task = car"}), "stop")])
    profile = gen.resolve_language_request("Quenya, the Elvish language", drive_root=tmp_path, client=known)
    assert profile == {"tag": "qya", "label": "Quenya", "direction": "ltr",
                       "instruction": "Tolkien's High-elven", "lexicon": "task = car"}
    assert "Quenya, the Elvish language" in known.calls[0]["messages"][0]["content"]

    invented = FakeLight([(json.dumps({"tag": "not a tag!", "label": "Vaelic Tongue", "direction": "rtl",
                                       "lexicon": "x" * (memory.MAX_LEXICON_CHARS + 100)}), "stop")])
    profile = gen.resolve_language_request("invent a language", drive_root=tmp_path, client=invented)
    assert profile["tag"] == "art-x-vaelic-t" and profile["direction"] == "rtl"
    assert len(profile["lexicon"]) == memory.MAX_LEXICON_CHARS, "the lexicon is bounded, never refused"

    english = FakeLight([(json.dumps({"tag": "en-GB", "label": "English"}), "stop")])
    assert gen.resolve_language_request("English please", drive_root=tmp_path, client=english)["tag"] == "en"


def test_resolve_language_request_failures_are_typed(tmp_path, light):
    with pytest.raises(gen.LanguageResolveError) as empty:
        gen.resolve_language_request("   ", drive_root=tmp_path)
    assert empty.value.code == "language_not_a_tag" and empty.value.status == 400

    class Broken:
        def chat(self, **kwargs):
            raise ConnectionError("down")

    with pytest.raises(gen.LanguageResolveError) as failed:
        gen.resolve_language_request("Quenya", drive_root=tmp_path, client=Broken())
    assert failed.value.code == "language_resolve_failed" and failed.value.status == 502

    light.setattr(gen, "_light_route", lambda: ("", False, False))
    with pytest.raises(gen.LanguageResolveError) as none:
        gen.resolve_language_request("Quenya", drive_root=tmp_path)
    assert none.value.code == "language_needs_model" and none.value.status == 400

    garbage = FakeLight([("I would rather not.", "stop")])
    light.setattr(gen, "_light_route", lambda: ("fake/light", False, True))
    profile = gen.resolve_language_request("Klingon", drive_root=tmp_path, client=garbage)
    assert profile["tag"] == "art-x-klingon" and profile["label"] == "Klingon", "no JSON → the owner's own word becomes an invented tag"


def test_the_batch_at_the_model_still_counts_as_pending_for_the_gateway(tmp_path, light):
    light.setenv("OUROBOROS_UI_LANGUAGE", "ru")
    _ru(tmp_path)
    memory.record_missing(tmp_path, "ru", [{"key": "Settings", "context": {}}, {"key": "Files", "context": {}}])
    light.setattr(gen._WORKER, "status", {**gen._WORKER.status, "language": "ru"})
    seen = {}

    class Observing:
        def chat(self, **kwargs):
            from ouroboros.gateway.ui_i18n import memory_payload

            seen["queue"] = memory.pending_count(tmp_path, "ru")
            seen["in_flight"] = gen.generator_status()["in_flight"]
            seen["payload_pending"] = memory_payload(tmp_path, "ru")["stats"]["pending"]
            return {"content": json.dumps({"translations": [{"id": 0, "text": "Настройки"}, {"id": 1, "text": "Файлы"}]})}, {}

    light.setattr(gen, "_broadcast", lambda frame: None)
    gen.translate_batch(tmp_path, "ru", client=Observing())
    assert seen["queue"] == 0, "the file queue no longer holds the batch"
    assert seen["in_flight"] == 2 and seen["payload_pending"] == 2, "the gateway still reports the two keys as pending"
    assert gen.generator_status()["in_flight"] == 0
    assert memory.pending_count(tmp_path, "ru") == 0
