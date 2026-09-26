"""The paid panel's full evidence stays readable after its author's drive is gone."""

import dataclasses
import hashlib
import json
import queue
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import artifacts, model_wait, review_operation
from ouroboros.headless import copy_child_task_result, prepare_task_drive, remove_subagent_task_drive
from ouroboros.review_native_episode import inspection_registry
from ouroboros.review_projection import publish_acceptance_checkpoint
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from ouroboros.task_results import load_task_result, write_task_result
from supervisor import events_chat_delivery as chat
from supervisor.terminal_delivery import delivery_id_for, pending_deliveries, register_pending_delivery
from tests.test_review_operation_collection import _send_ctx, fresh_sends  # noqa: F401
from tests.test_review_operation_lifetime import until


@pytest.mark.parametrize("emitted", ["answer A", "answer B"])
def test_full_operation_sources_survive_author_drive_cleanup(tmp_path, monkeypatch, fresh_sends, emitted):
    task_id = "source-author"
    canonical = tmp_path / "canonical"
    child = prepare_task_drive(canonical, task_id, "empty")
    repo = tmp_path / "repo"
    repo.mkdir()
    events, entered, release = queue.Queue(), threading.Event(), threading.Event()
    critique = json.dumps({"verdict": "FAIL", "findings": [],
                           "summary": "Original critique of A: inspect the missing edge case.\n" * 300})
    ctx = SimpleNamespace(task_id=task_id, task_attempt=1, drive_root=child, budget_drive_root=canonical,
                          task_metadata={}, pending_events=[], event_queue=events)
    write_task_result(canonical, task_id, "running", chat_id=3, child_drive_root=str(child))
    write_task_result(child, task_id, "running", chat_id=3)
    request = ReviewRequest(surface="task_acceptance", task_id=task_id, goal="original goal",
                            subject="answer A", evidence={"requirement": "original requirement"},
                            retry_key="source-closure", drain_deadline=time.monotonic())
    slot = ReviewSlot(slot_id="one", model="model/a", timeout_sec=30)
    checkpoints = []

    class HeldModel:
        def chat(self, **_kwargs):
            # Inspect the canonical source at the physical-send seam, not after dispatch.
            pointer = next(iter(load_task_result(canonical, task_id)["review_operations"].values()))
            raw = artifacts.read_actor_source_bytes(canonical, task_id, pointer["source_ref"])
            source = json.loads(raw)
            assert source["request"] == dataclasses.asdict(request)
            assert source["slot_roster"] == [dataclasses.asdict(slot)]
            assert source["operations"]["one"]
            checkpoints.append((pointer["source_ref"], raw))
            entered.set()
            assert release.wait(15), "fixture did not release the paid reviewer"
            return {"content": critique}, {"prompt_tokens": 1, "completion_tokens": 1}

    try:
        with model_wait.task_model_wait_scope(
            task={"id": task_id, "chat_id": 3, "_attempt": 1}, drive_root=canonical,
            event_queue=events, worker_slot_held=False,
        ) as author:
            first = run_review_request(request, slots=[slot], drive_root=child, usage_ctx=ctx, llm=HeldModel())
            assert entered.wait(5)
            run = {**dataclasses.asdict(first), "authority": "host_root", "binding_hash": "b" * 64,
                   "candidate_hash": hashlib.sha256(b"answer A").hexdigest()}
            publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=task_id)
        assert author.closed
        operation = next(op for op in review_operation._LIVE.values() if op.task_id == task_id)
        assert not operation.closed
        # A mutable result is deliberately different from either possible sent answer.
        write_task_result(child, task_id, "completed", chat_id=3, result="mutable result C")
        copy_child_task_result(canonical, {"id": task_id, "drive_root": str(child)})
        event = {"type": "send_message", "task_id": task_id, "chat_id": 3, "text": emitted,
                 "delivery_id": delivery_id_for(task_id, emitted), "format": "markdown"}
        assert register_pending_delivery(canonical, event)
        sends = []
        monkeypatch.setattr(chat, "_bound_project_chat_id", lambda *_args: 42)
        chat._handle_send_message(event, _send_ctx(canonical, sends))
        assert sends == [(42, emitted)]
        assert remove_subagent_task_drive(canonical, task_id, live=lambda _task: False)
        assert not child.exists()
        # The only pending panel is now its canonical publication; the author has no trace.
        assert not getattr(ctx, "_execution_trace", None)
        release.set()
        until(lambda: operation.closed)
        stored = load_task_result(canonical, task_id)
        panel = stored["review_projection"]["panels"][0]
        late = panel["late_settlement"]
        assert late["reviewed_revision"] == ("delivered" if emitted == "answer A" else "different")
        assert late["reviewed_is_emitted"] is (emitted == "answer A")
        assert stored["result"] == "mutable result C"
        assert len(checkpoints) == 1, "collection must not buy a second review"
        notices = [row for row in list(events.queue) if row.get("system_type") == "acceptance_late_settlement"]
        assert len(notices) == 1
        assert notices[0]["progress_meta"]["late_evidence"]["source_ref"] == panel["applied_source_ref"]
        assert [row["delivery_id"] for row in pending_deliveries(canonical)] == ["acceptance-late:source-closure"]

        # Follow the advertised read_file handles using the existing inspection consumer.
        registry, reader, _ = inspection_registry(str(repo), canonical, task_id)
        refs = [checkpoints[0][0], panel["applied_source_ref"], late["emitted_answer"]["delivered"][0]["source_ref"]]
        documents = []
        for ref in refs:
            result = registry.execute_result("read_file", {**ref["read"]["arguments"], "max_lines": 2000})
            assert result.status == "ok", result.text
            assert reader.last_read_view["opened_root"] == "artifact_store"
            raw = artifacts.read_actor_source_bytes(canonical, task_id, ref)
            assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
            document = json.loads(raw)
            # read_file presents line numbers; ensure its visible output includes each full source line.
            assert all(line in result.text for line in raw.decode().splitlines())
            documents.append(document)
        checkpoint, settled, receipt = documents
        assert artifacts.read_actor_source_bytes(canonical, task_id, refs[0]) == checkpoints[0][1]
        assert checkpoint["request"]["subject"] == settled["request"]["subject"] == "answer A"
        assert checkpoint["slot_roster"] == settled["slot_roster"]
        assert settled["actors"][0]["raw_text"] == critique
        from ouroboros.observability import read_blob_ref, read_call_manifest_ref, read_call_payload

        _, response, response_ref = read_call_payload(
            canonical, task_id=task_id, call_id=settled["actors"][0]["operation_id"] + "_response")
        assert response["message"]["content"] == critique
        retained_response = late["reviewer_outputs"][0]["response_ref"]
        assert response_ref["manifest_ref"] == retained_response["manifest_ref"]
        manifest = read_call_manifest_ref(canonical, retained_response["manifest_ref"], task_id=task_id)
        assert read_blob_ref(canonical, manifest["full_payload_ref"])["message"]["content"] == critique
        assert (receipt["text"], receipt["chat_id"]) == (emitted, 42)
        assert receipt["basis"] == "send_handler_returned"  # producer evidence, not a human-read receipt

        # Settlement itself launches no cognition. A later explicitly admitted
        # wake receives the fact and can open the original subject and critique.
        from ouroboros import agent as agent_module, consciousness_wake as wake
        from supervisor import workers
        from tests.test_consciousness_wake_lane import _lane, _wait_for
        from ouroboros.tools.core_file_tools import _read_file
        from ouroboros.tools.tool_context import ToolContext

        _lane(monkeypatch, canonical)
        finished, consumed = [], []

        class Cognition:
            def handle_task(self, task):
                assert panel["applied_source_ref"]["path"] in task["text"]
                assert "late review settled for task source-author" in task["text"]
                tool_ctx = ToolContext(repo_dir=repo, drive_root=canonical, task_id=task["id"], task_metadata=task["metadata"])
                line = next(line for line in task["text"].splitlines() if "late review settled for task source-author" in line)
                advertised = json.loads(line.split("exact source ", 1)[1].rsplit(" sha256 ", 1)[0])
                consumed.append(_read_file(tool_ctx, **advertised["arguments"], max_lines=2000))
                return []

        monkeypatch.setattr(agent_module, "make_agent", lambda **_kw: Cognition())
        observation = wake.observe_wake(canonical, boundary=None, since=0, now=time.time(), reason="heartbeat")

        def render(events):
            return wake.render_wake_message(pathlib.Path(__file__).resolve().parents[1], reason="heartbeat",
                last_wake_at=0, now=time.time(), level="act", disabled_tools=[], spent_usd=0, daily_usd=20,
                running=0, max_tasks=2, interval=3300, events=events)

        admitted = workers.handle_wake_direct(42, render(observation.full_text()), wake.wake_task_metadata("act", "heartbeat"),
            bind_input=lambda task: wake.bind_wake_observation(canonical, task, observation, render),
            on_finished=lambda _tid, ok: finished.append(ok))
        assert admitted["admitted"] and _wait_for(lambda: bool(finished)) and finished == [True]
        assert len(consumed) == 1 and "Original critique of A" in consumed[0] and "answer A" in consumed[0]
        assert len(checkpoints) == 1
        assert artifacts.collect_task_artifact_records(canonical, task_id) == []
    finally:
        release.set()
        until(lambda: not any(op.task_id == task_id for op in review_operation._LIVE.values()))
