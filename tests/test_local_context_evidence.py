"""Local capacity is a serving-instance fact, independent of training and health."""
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from ouroboros import capability_evidence as ce, config, local_model
from ouroboros.llm import LLMClient
from ouroboros.llm_local import local_context_limits


@pytest.fixture
def manager(monkeypatch, tmp_path):
    manager = local_model.LocalModelManager()
    manager._proc = SimpleNamespace(pid=123, poll=lambda: None)
    manager._status = "ready"
    monkeypatch.setattr(local_model, "get_manager", lambda: manager)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return manager


@pytest.mark.parametrize("unknown", [{}, {"context_length": None}, {"context_length": 0},
                                      {"context_length": -1}, {"context_length": "unknown"},
                                      ConnectionError("unavailable")])
def test_unknown_metadata_does_not_poison_cache_or_health(manager, monkeypatch, unknown):
    health = Mock(side_effect=[unknown, {"context_length": 131072}])
    monkeypatch.setattr(manager, "health_check", health)
    assert manager.get_context_length() == 0
    assert manager.status_dict()["context_length"] == 0
    assert manager.is_running
    assert manager.get_context_length() == 131072
    assert manager.get_context_length() == 131072
    assert health.call_count == 2


@pytest.mark.parametrize("window", [4096, 16384, "131072"])
def test_positive_reported_metadata_is_retained(manager, monkeypatch, window):
    health = Mock(return_value={"context_length": window})
    monkeypatch.setattr(manager, "health_check", health)
    assert manager.get_context_length() == int(window)
    assert manager.get_context_length() == int(window)
    health.assert_called_once()


def test_healthy_server_with_no_window_still_becomes_ready(manager, monkeypatch):
    import requests
    from ouroboros import server_process

    response = Mock()
    response.json.return_value = {"data": [{"id": "local-fixture"}]}
    session = Mock()
    session.get.return_value = response
    # Mock only HTTP; exercise the actual health parser and readiness consumer.
    session_scope = MagicMock()
    session_scope.__enter__.return_value = session
    monkeypatch.setattr(requests, "Session", lambda: session_scope)
    monkeypatch.setattr(server_process, "record_service_binding", lambda *a, **k: None)
    manager._status = "loading"
    manager._wait_for_healthy(timeout=1)
    assert manager.is_running
    assert manager.get_context_length() == 0
    assert manager.serving_context_evidence()["confirmed"] is False
    assert manager.status_dict()["error"] is None


@pytest.mark.parametrize("allow_fetch", [False, True])
@pytest.mark.parametrize("legacy_window", [None, 4096, 131072])
def test_unknown_local_probe_ignores_legacy_cache_and_cloud_metadata(
    manager, monkeypatch, tmp_path, allow_fetch, legacy_window,
):
    manager._context_length = 131072  # Training metadata is not a served window.
    fp = ce.route_fingerprint(provider="openrouter", model="remote/name")
    if legacy_window is not None:
        ce._store_evidence(tmp_path, "probes", fp, ce.CapabilityEvidence(
            legacy_window, ce.STATUS_CONFIRMED, ce.SOURCE_LOCAL_HEALTH, fp,
            "remote/name", "openrouter", ts=ce.utc_now_iso(),
        ).to_json())
    path = ce._store_path(tmp_path)
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: pytest.fail("cloud metadata"))
    monkeypatch.setattr(ce, "_generative_probe_window", lambda *a, **k: pytest.fail("generation probe"))
    evidence = ce.probe(tmp_path, provider="openrouter", model="remote/name", use_local=True,
                        allow_fetch=allow_fetch, allow_generative=True)
    assert evidence.window_tokens == 0
    assert evidence.status == ce.STATUS_UNPROBEABLE
    assert not ce.is_known(evidence)
    assert local_context_limits(65536) == (0, 2048)
    assert manager.is_running
    assert (path.read_bytes() if path.exists() else None) == before


def test_live_serving_capacity_reaches_main_and_review_consumers(manager, monkeypatch, tmp_path):
    from ouroboros.context_fit import resolve_context_fit_route
    from ouroboros.reviewer_window import resolve_reviewer_window

    monkeypatch.setattr(config, "runtime_settings", lambda: {"OUROBOROS_MODEL_CONTEXT_WINDOWS": {}})
    monkeypatch.setattr("ouroboros.reviewer_window._LAZY_ROUTE_LOCKS", {})
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_WINDOWS", "{}")
    manager._context_length = 131072
    for window in (16384, 8192, 4096, 0):
        manager._serving_context_length = window
        route, evidence = resolve_context_fit_route(
            {"model": "local-fixture", "use_local_model": True}, allow_fetch=False)
        assert route["use_local"] is True
        assert evidence.window_tokens == window
        assert ce.is_known(evidence) is (window > 0)
        reviewer = resolve_reviewer_window("local-fixture", use_local=True)
        assert reviewer.window_tokens == window
        assert reviewer.status == evidence.status
        assert local_context_limits(65536) == (window, window // 4 if window else 2048)
    assert not ce._store_path(tmp_path).exists()


def test_local_owner_assertion_remains_separate(manager, tmp_path):
    ce.record_owner_ack(tmp_path, provider="local", model="local-fixture", window_tokens=32768)
    evidence = ce.probe(tmp_path, provider="local", model="local-fixture", use_local=True, allow_fetch=False)
    assert evidence.window_tokens == 32768
    assert evidence.status == ce.STATUS_ASSERTED
    assert evidence.source == ce.SOURCE_OWNER_ACK
    assert manager.serving_context_evidence()["confirmed"] is False


def test_serving_measurement_keeps_input_and_instance_binding(manager, monkeypatch):
    import requests
    from ouroboros.local_model_server import input_fingerprint

    manager._context_length = 131072
    manager._serving_context_length = 16384
    manager._measurement_route = True
    payload = {"model": "local-model", "messages": [{"role": "user", "content": "source"}]}
    measured = {"supported": True, "input_is_exact": True, "input_tokens": 42,
                "context_window": 16384, "process_id": 123,
                "native_input_sha256": input_fingerprint(payload)}
    response = Mock()
    response.json.return_value = measured
    session_scope = MagicMock()
    session_scope.__enter__.return_value.post.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: session_scope)
    assert manager.measure_prepared_input(payload) == measured
    changed_payload = {**payload, "messages": [{"role": "user", "content": "changed"}]}
    assert manager.measure_prepared_input(changed_payload)["supported"] is False
    manager._proc = SimpleNamespace(pid=456, poll=lambda: None)
    assert manager.measure_prepared_input(payload)["supported"] is False


@pytest.mark.parametrize("window", [0, 16384])
def test_running_local_dispatch_uses_serving_capacity_without_cloud_fallback(manager, monkeypatch, window):
    manager._context_length = 131072 if window else 4096
    manager._serving_context_length = window
    client = LLMClient(api_key="unused")
    sent = []

    def create(**payload):
        sent.append(payload)
        return SimpleNamespace(model_dump=lambda: {
            "choices": [{"message": {"role": "assistant", "content": "local answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 6000, "completion_tokens": 2, "total_tokens": 6002},
        })

    monkeypatch.setattr(client, "_get_local_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(client, "_resolve_remote_target", lambda *a, **k: pytest.fail("cloud fallback"))
    text = "Keep this source intact. " * 1000  # Exceeds the fictitious 4096 allowance.
    message, usage = client.chat(messages=[{"role": "user", "content": text}],
                                 model="local-fixture", max_tokens=65536, use_local=True)
    assert message["content"] == "local answer"
    assert usage["provider"] == "local"
    assert len(sent) == 1
    assert sent[0]["messages"][-1]["content"] == text
    assert sent[0]["max_tokens"] == (4096 if window else 2048)
    assert not ce._load(config.DATA_DIR)["probes"]


def test_actual_local_connection_error_is_not_a_synthetic_overflow(manager, monkeypatch):
    manager._context_length = 0
    monkeypatch.setattr(manager, "health_check", Mock(side_effect=ConnectionError("health unavailable")))
    client = LLMClient(api_key="unused")
    error = ConnectionError("local transport unavailable")
    create = Mock(side_effect=error)
    monkeypatch.setattr(client, "_get_local_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(client, "_resolve_remote_target", lambda *a, **k: pytest.fail("cloud fallback"))
    with pytest.raises(ConnectionError) as caught:
        client.chat(messages=[{"role": "user", "content": "source " * 5000}],
                    model="local-fixture", max_tokens=65536, use_local=True)
    assert caught.value is error
    create.assert_called_once()


_STAND_IN_SERVER = '''
"""Loopback stand-in for ouroboros.local_model_server; it loads no model."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ouroboros.local_model_server import input_fingerprint

PORT, N_CTX = (int(sys.argv[sys.argv.index(flag) + 1]) for flag in ("--port", "--n_ctx"))


class Handler(BaseHTTPRequestHandler):
    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # /v1/models reports training metadata only.
        self.reply({"data": [{"id": "local-fixture", "meta": {"n_ctx_train": 131072}}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(os.environ["LOCAL_STAND_IN_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "pid": os.getpid(), "body": body}) + "\\n")
        if self.path == "/extras/measure_chat":
            self.reply({"supported": True, "input_is_exact": True, "input_tokens": 100,
                        "context_window": N_CTX, "process_id": os.getpid(),
                        "native_input_sha256": input_fingerprint(body), "output_limit_enforced": True,
                        "reasoning_included_in_limit": True, "reason": None})
        else:
            self.reply({"id": "fixture", "object": "chat.completion", "created": 0, "model": "local-model",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "local answer"}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 2, "total_tokens": 102}})

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def _spawned_worker(requests, results, data_dir):
    """A separate spawned process which, like worker_main, owns no local server."""
    import os
    import pathlib

    os.environ["OUROBOROS_IN_WORKER"] = "1"
    config.DATA_DIR = pathlib.Path(data_dir)
    for port, chat in iter(requests.get, None):
        os.environ["LOCAL_MODEL_PORT"] = str(port)
        try:
            manager = local_model.get_manager()
            probe = ce.probe(config.DATA_DIR, provider="local", model="local-fixture",
                             use_local=True, allow_fetch=False)
            reply = None
            if chat:
                message, usage = LLMClient(api_key="unused").chat(
                    messages=[{"role": "user", "content": "hello"}], model="local-fixture",
                    max_tokens=65536, use_local=True)
                reply = (message.get("content"), usage.get("provider"))
            results.put({"pid": os.getpid(), "owns_server": manager._proc is not None,
                         "evidence": manager.serving_context_evidence(),
                         "limits": local_context_limits(65536),
                         "probe": (probe.window_tokens, probe.status, probe.source), "reply": reply})
        except Exception as error:  # Report consumer failures instead of hanging the parent.
            results.put({"error": repr(error)})


@pytest.fixture
def offline_manager(monkeypatch, tmp_path):
    manager = local_model.LocalModelManager()
    monkeypatch.setattr(local_model, "_manager", manager)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(manager, "check_runtime", lambda: True)
    yield manager
    manager.stop_server()


@pytest.mark.parametrize("source", ["", "   ", "fixture/source"])
def test_autostart_guard_admits_only_a_configured_source(offline_manager, monkeypatch, caplog, source):
    from ouroboros.local_model_autostart import auto_start_local_model

    download = Mock(return_value="/models/fixture.gguf")
    start = Mock()
    monkeypatch.setattr(offline_manager, "download_model", download)
    monkeypatch.setattr(offline_manager, "start_server", start)
    auto_start_local_model({"LOCAL_MODEL_SOURCE": source, "LOCAL_MODEL_FILENAME": "fixture.gguf",
                            "LOCAL_MODEL_PORT": 9123, "LOCAL_MODEL_N_GPU_LAYERS": 7,
                            "LOCAL_MODEL_CONTEXT_LENGTH": 8192, "LOCAL_MODEL_CHAT_FORMAT": "chatml"})
    if source.strip():
        download.assert_called_once_with("fixture/source", "fixture.gguf")
        start.assert_called_once_with("/models/fixture.gguf", port=9123, n_gpu_layers=7, n_ctx=8192,
                                      chat_format="chatml", source="fixture/source", filename="fixture.gguf")
        assert "LOCAL_MODEL_SOURCE is empty" not in caplog.text
    else:
        download.assert_not_called()
        start.assert_not_called()
        assert "LOCAL_MODEL_SOURCE is empty" in caplog.text


def test_spawned_worker_reads_the_serving_instance_its_server_process_owns(
    offline_manager, monkeypatch, tmp_path,
):
    """Workers are spawned processes; the owned server lives in the server process."""
    import json
    import multiprocessing
    import os
    import socket
    import subprocess
    import time

    from ouroboros.local_model_autostart import auto_start_local_model
    from ouroboros.server_process import read_service_bindings

    stand_in = tmp_path / "stand_in_server.py"
    stand_in.write_text(_STAND_IN_SERVER, encoding="utf-8")
    real_popen, real_run = subprocess.Popen, subprocess.run

    def popen(cmd, **kwargs):  # Only the vendor server is replaced; the launched argv is kept.
        assert cmd[1:3] == ["-m", "ouroboros.local_model_server"]
        return real_popen([cmd[0], str(stand_in), *cmd[3:]], **kwargs)

    def run(cmd, **kwargs):  # llama-cpp-python is not installed here.
        if cmd[1:] == ["-c", "import llama_cpp"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(local_model, "subprocess", SimpleNamespace(**{
        **vars(subprocess), "Popen": popen, "run": run}))
    monkeypatch.setattr(offline_manager, "download_model", lambda source, filename: str(tmp_path / filename))
    log_path = tmp_path / "stand_in.jsonl"
    monkeypatch.setenv("LOCAL_STAND_IN_LOG", str(log_path))

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def autostart(port, n_ctx):
        auto_start_local_model({"LOCAL_MODEL_SOURCE": "fixture/source", "LOCAL_MODEL_FILENAME": "fixture.gguf",
                                "LOCAL_MODEL_PORT": port, "LOCAL_MODEL_CONTEXT_LENGTH": n_ctx})
        deadline = time.monotonic() + 30
        while not offline_manager.is_running and time.monotonic() < deadline:
            time.sleep(0.05)
        assert offline_manager.is_running, offline_manager.status_dict()
        return offline_manager.serving_context_evidence()

    ctx = multiprocessing.get_context("spawn")
    requests, results = ctx.Queue(), ctx.Queue()
    worker = ctx.Process(target=_spawned_worker, args=(requests, results, str(tmp_path)), daemon=True)
    worker.start()

    def ask(port, chat=False):
        requests.put((port, chat))
        seen = results.get(timeout=60)
        assert "error" not in seen, seen
        assert seen["pid"] == worker.pid != os.getpid() and not seen["owns_server"]
        return seen

    unknown_probe = (0, ce.STATUS_UNPROBEABLE, ce.SOURCE_NONE)
    try:
        port = free_port()
        owned = autostart(port, 16384)
        seen = ask(port, chat=True)
        assert seen["evidence"]["confirmed"] is True, seen
        assert (seen["evidence"]["context_window"], seen["evidence"]["process_id"]) == (
            16384, owned["process_id"])
        assert seen["limits"] == (16384, 4096)
        assert seen["probe"] == (16384, ce.STATUS_CONFIRMED, ce.SOURCE_LOCAL_HEALTH)
        assert seen["reply"] == ("local answer", "local")
        sent = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert [row["path"] for row in sent] == ["/extras/measure_chat", "/v1/chat/completions"]
        assert {row["pid"] for row in sent} == {owned["process_id"]}
        assert sent[-1]["body"]["max_tokens"] == 4096

        elsewhere = ask(free_port())  # This worker would dispatch to another endpoint.
        assert elsewhere["evidence"]["confirmed"] is False and elsewhere["limits"] == (0, 2048)
        assert elsewhere["probe"] == unknown_probe

        offline_manager.stop_server()
        stopped = ask(port)
        assert stopped["evidence"]["confirmed"] is False and stopped["limits"] == (0, 2048)
        assert stopped["probe"] == unknown_probe

        restarted = autostart(port, 8192)  # The same worker holds no stale window.
        assert restarted["process_id"] != owned["process_id"]
        seen = ask(port)
        assert (seen["evidence"]["context_window"], seen["evidence"]["process_id"]) == (
            8192, restarted["process_id"])
        assert seen["limits"] == (8192, 2048) and seen["probe"][:2] == (8192, ce.STATUS_CONFIRMED)

        bindings_path = tmp_path / "state" / "server_port.bindings.json"
        published = bindings_path.read_text(encoding="utf-8")
        reused = json.loads(published)  # The pid now names a process born after the published one.
        fingerprint = reused["local_model"]["fingerprint"]
        fingerprint.update({key: "0" for key in ("start_time", "creation_time") if key in fingerprint})
        bindings_path.write_text(json.dumps(reused), encoding="utf-8")
        foreign = ask(port)
        assert foreign["evidence"]["confirmed"] is False and foreign["probe"] == unknown_probe
        bindings_path.write_text(published, encoding="utf-8")

        offline_manager._proc.kill()  # A crash leaves the published binding behind.
        offline_manager._proc.wait(timeout=10)
        assert read_service_bindings(tmp_path)["local_model"]["pid"] == restarted["process_id"]
        crashed = ask(port)
        assert crashed["evidence"]["confirmed"] is False and crashed["limits"] == (0, 2048)
        assert crashed["probe"] == unknown_probe
    finally:
        requests.put(None)
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
        for channel in (requests, results):
            channel.close()
            channel.cancel_join_thread()
