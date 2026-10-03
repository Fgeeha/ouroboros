"""Host-recorded plan-review facts for the learning surfaces (reflection, late settlement).

The post-task reflection asks the mind to account for every review finding, yet its
evidence held only the commit/advisory ledger and the acceptance panels: what the PLAN
review found, how the author answered, whether the element survived in the plan the
author finally selected, and which reviewers never answered all vanished at task end.
This leaf reads the recorded plan-review state and projects those facts, bounded and
with a source pointer. It scores nothing and decides nothing: which advice mattered is
the mind's judgement (BIBLE P5/P13); the host only keeps the facts in front of it.

One builder for the whole slice (``plan_review_reflection_slice``), one pure core over
an already-loaded state (``facts_from_state``) so tests need no drive, and one bounded
row for a panel that settled after the task ended (``late_settlement_reflection_entry``).
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List, Mapping, Optional

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

# Bounds are fit limits for a Light-route prompt and a context row, not judgements:
# every cut is named in the slice itself (``omitted``), never silent.
PLAN_REVIEW_REFLECTION_CHARS = 6_000
LATE_SETTLEMENT_ROW_CHARS = 1_500
_TEXT_CHARS = 200
_WAVES_SHOWN = 8
LATE_SETTLEMENT_TASK_TYPE = "acceptance_late_settlement"


def _cut(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _spec_texts(spec: Mapping[str, Any]) -> Dict[str, str]:
    """``id -> text`` for every id-bearing element of a normalized spec, by the spec's own map."""
    try:
        from ouroboros.tools.plan_spec import _text_by_id

        texts = dict(_text_by_id(spec))
    except Exception:
        texts = {}
    goal = str(spec.get("goal") or "")
    if goal:
        texts.setdefault("goal", goal)
    return texts


def _latest_dispositions(wave: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Last answer per finding id wins (the recorder merges by finding_id in that order)."""
    latest: Dict[str, Dict[str, Any]] = {}
    for item in wave.get("dispositions") or []:
        if isinstance(item, Mapping) and str(item.get("finding_id") or "").strip():
            latest[str(item["finding_id"]).strip()] = dict(item)
    return latest


def _element_change(delta: Optional[Mapping[str, Any]], element_id: str) -> str:
    """How the element fared in the selected plan: same / changed / removed / renumbered→id / added."""
    if not isinstance(delta, Mapping):
        return "n/a"
    ids = delta.get("ids") if isinstance(delta.get("ids"), Mapping) else {}
    for name in ("removed", "changed", "added"):
        if element_id in (ids.get(name) or []):
            return name
    for moved in delta.get("renumbered") or []:
        if isinstance(moved, Mapping) and str(moved.get("from") or moved.get("prev_id") or "") == element_id:
            return f"renumbered→{moved.get('to') or moved.get('id') or '?'}"
    return "same"


def _wave_summary(wave: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "cycle": wave.get("cycle_index"),
        "aggregate": wave.get("aggregate"),
        "closed": bool(wave.get("closed")),
        "counts": wave.get("counts") if isinstance(wave.get("counts"), Mapping) else {},
        "paid": bool(wave.get("paid")),
    }


def facts_from_state(
    state: Mapping[str, Any],
    *,
    critic: Optional[Mapping[str, Any]],
    author_plan: Optional[Mapping[str, Any]] = None,
    claims_source: str = "",
    census: Optional[Mapping[str, Any]] = None,
    source_ref: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Pure projection of one task's plan-review record (no drive, no model call).

    ``critic`` is the wave whose findings the author answered (the reviewed plan);
    ``author_plan`` the plan the author selected without a new wave, if any;
    ``census`` the critic wave's slot census (``plan_wave_slot_census``). Returns
    ``None`` when the task recorded no wave at all.
    """
    waves = [w for w in (state.get("waves") or []) if isinstance(w, Mapping)]
    if not waves and not isinstance(critic, Mapping):
        return None
    critic = critic if isinstance(critic, Mapping) else waves[-1]
    spec = critic.get("spec") if isinstance(critic.get("spec"), Mapping) else {}
    texts = _spec_texts(spec)
    dispositions = _latest_dispositions(critic)
    delta: Optional[Dict[str, Any]] = None
    selected: Optional[Dict[str, Any]] = None
    if isinstance(author_plan, Mapping):
        author = author_plan.get("author_disposition") if isinstance(author_plan.get("author_disposition"), Mapping) else {}
        selected_spec = author_plan.get("spec") if isinstance(author_plan.get("spec"), Mapping) else {}
        try:
            from ouroboros.tools.plan_spec import spec_delta

            delta = spec_delta(dict(spec), dict(selected_spec)) if spec and selected_spec else None
        except Exception:
            log.debug("plan facts: spec delta unavailable", exc_info=True)
            delta = None
        selected = {
            "author_action": str(author.get("action") or ""),
            "disposition": str(author.get("disposition") or author.get("verdict") or ""),
            "enforcement": str(author.get("enforcement") or ""),
            "rationale": _cut(author.get("rationale"), 300),
            "delta": ({k: delta["ids"].get(k, []) for k in ("added", "removed", "changed")}
                      | {"renumbered": len(delta.get("renumbered") or []), "goal_changed": bool(delta.get("goal_changed"))})
            if isinstance(delta, Mapping) else "not computed",
        }
    findings_by_element: Dict[str, List[Dict[str, Any]]] = {}
    questions: List[Dict[str, Any]] = []
    for finding in critic.get("findings") or []:
        if not isinstance(finding, Mapping):
            continue
        fid = str(finding.get("finding_id") or finding.get("id") or "")
        answer = dispositions.get(fid)
        row = {
            "finding_id": fid,
            "class": str(finding.get("class") or ""),
            "model": str(finding.get("model") or finding.get("slot") or ""),
            "summary": _cut(finding.get("summary"), 160),
            "disposition": (f"{answer.get('decision')}: {_cut(answer.get('rationale'), _TEXT_CHARS)}"
                            if isinstance(answer, Mapping) else "unanswered"),
        }
        element = str(finding.get("breaks") or "").strip()
        findings_by_element.setdefault(element or "(no element)", []).append(row)
        if row["class"] == "need_evidence" and not str(finding.get("locator") or "").strip() and element:
            questions.append({"finding_id": fid, "breaks": element,
                              "question": row["summary"], "answer": row["disposition"]})
    elements: List[Dict[str, Any]] = []
    touched = set(findings_by_element)
    if isinstance(delta, Mapping):
        ids = delta.get("ids") if isinstance(delta.get("ids"), Mapping) else {}
        touched |= {str(i) for name in ("removed", "changed", "added") for i in (ids.get(name) or [])}
    for element_id in sorted(touched, key=lambda e: (e == "(no element)", e)):
        elements.append({
            "id": element_id,
            "text": _cut(texts.get(element_id, ""), _TEXT_CHARS),
            "changed_in_selected_plan": _element_change(delta, element_id),
            "findings": findings_by_element.get(element_id, []),
        })
    unchanged = len([i for i in texts if i not in touched])
    unresolved: Dict[str, Any] = {}
    if isinstance(census, Mapping):
        rows = [r for name in ("awaiting", "unresolved", "uncollected") for r in (census.get(name) or [])
                if isinstance(r, Mapping)]
        unresolved = {"count": len(rows), "slots": [str(r.get("slot_id") or "") for r in rows]}
    facts: Dict[str, Any] = {
        "source_ref": dict(source_ref or {}),
        "wave_ref": critic.get("wave_artifact") if isinstance(critic.get("wave_artifact"), Mapping) else {},
        "claims_source": claims_source or "unknown",
        "waves": [_wave_summary(w) for w in waves[-_WAVES_SHOWN:]],
        "waves_total": len(waves),
        "reviewed_plan": {"aggregate": critic.get("aggregate"), "closed": bool(critic.get("closed")),
                          "cycle": critic.get("cycle_index"), "closure_notes": list(critic.get("closure_notes") or [])[:6]},
        "selected_plan": selected,
        "elements": elements,
        "unchanged_elements_without_findings": unchanged,
        "questions": questions,
        "unresolved_reviewers_at_task_end": unresolved,
    }
    return _fit(facts)


def _fit(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Bound the rendered JSON: drop note-class finding texts first, then trailing elements; name every cut."""
    omitted = {"note_findings_summaries": 0, "elements": 0}
    if len(_render(facts)) <= PLAN_REVIEW_REFLECTION_CHARS:
        return facts
    for element in facts["elements"]:
        for row in element["findings"]:
            if row.get("class") == "note" and "summary" in row:
                del row["summary"]
                omitted["note_findings_summaries"] += 1
    while facts["elements"] and len(_render(facts)) > PLAN_REVIEW_REFLECTION_CHARS:
        facts["elements"].pop()
        omitted["elements"] += 1
    facts["omitted"] = {**omitted, "note": "whole rows omitted; read source_ref"}
    return facts


def _render(facts: Mapping[str, Any]) -> str:
    return json.dumps(facts, ensure_ascii=False, indent=2, default=str)


def render_plan_review_section(facts: Mapping[str, Any]) -> str:
    """The prompt section the reflection reads first: facts, with the judgement left to the reader."""
    return ("TASK PLAN REVIEW (host-recorded facts; which advice mattered is yours to judge):\n"
            + _render(facts))


def plan_review_reflection_slice(root: Any, task_id: str, *, task: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Load one task's plan-review record and project it; ``None`` without waves, a
    disclosed ``{"unavailable": …}`` when a recorded source cannot be read."""
    from ouroboros.task_results import current_plan_review_wave, load_plan_review_state
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable, authority_wave, current_author_plan

    if not str(task_id or "").strip():
        return None
    source_ref = {"kind": "task_result", "reader": "get_task_result", "task_id": str(task_id), "field": "plan_review_state"}
    drive = pathlib.Path(str(root))
    try:
        state = load_plan_review_state(drive, str(task_id))
    except (PlanReviewSourceUnavailable, OSError, ValueError) as exc:
        return {"unavailable": str(exc), "source_ref": source_ref}
    if not (state.get("waves") or []):
        return None
    try:
        author_plan = current_author_plan(drive, str(task_id), state)
    except PlanReviewSourceUnavailable as exc:
        author_plan = None
        log.debug("plan facts: author plan unavailable: %s", exc)
    critic: Optional[Mapping[str, Any]] = None
    try:
        hot = None
        if isinstance(author_plan, Mapping) and author_plan.get("review_fingerprint"):
            from ouroboros.task_results import plan_review_wave

            hot = plan_review_wave(state, str(author_plan["review_fingerprint"]))
        hot = hot or current_plan_review_wave(state) or (state.get("waves") or [None])[-1]
        critic = authority_wave(drive, str(task_id), hot) or hot
    except PlanReviewSourceUnavailable as exc:
        return {"unavailable": str(exc), "source_ref": source_ref}
    claims_source = ""
    try:
        from ouroboros.review_evidence_sections import _accept_effective_claims

        contract = (task or {}).get("task_contract") if isinstance((task or {}).get("task_contract"), Mapping) else {}
        claims_source = _accept_effective_claims(None, dict(contract or {}), drive, str(task_id))[1]
    except Exception:
        log.debug("plan facts: claims source unavailable", exc_info=True)
    census = None
    try:
        from ouroboros.tools.plan_review_runtime import plan_wave_slot_census

        census = plan_wave_slot_census(dict(critic)) if isinstance(critic, Mapping) else None
    except Exception:
        log.debug("plan facts: slot census unavailable", exc_info=True)
    return facts_from_state(state, critic=critic, author_plan=author_plan, claims_source=claims_source,
                            census=census, source_ref=source_ref)


def _late_panel(result: Mapping[str, Any], retry_key: str) -> Optional[Dict[str, Any]]:
    projection = result.get("review_projection") if isinstance(result.get("review_projection"), Mapping) else {}
    for panel in projection.get("panels") or []:
        late = panel.get("late_settlement") if isinstance(panel, Mapping) else None
        subject = late.get("reviewed_subject") if isinstance(late, Mapping) and isinstance(late.get("reviewed_subject"), Mapping) else {}
        if isinstance(late, Mapping) and str(subject.get("retry_key") or "") == str(retry_key):
            return dict(panel)
    return None


def late_settlement_reflection_entry(root: Any, task_id: str, retry_key: str) -> Optional[Dict[str, Any]]:
    """One bounded reflection row for a panel that settled after its task ended.

    The reflection that already ran was written over a still-running panel; this row
    puts the settled verdict (the host's own sentence), each reviewer's status and the
    plan findings behind the judged claims into the same log, so the next tasks read
    it through the ordinary recent-reflections window. No model call, no marker: the
    Pattern Register stays closed (``_admits_pattern_register`` reads error evidence).
    """
    from ouroboros.task_results import load_task_result

    drive = pathlib.Path(str(root))
    result = load_task_result(drive, str(task_id)) or {}
    panel = _late_panel(result, retry_key) if isinstance(result, Mapping) else None
    if panel is None:
        return None
    late = panel.get("late_settlement") or {}
    lines = [_cut(str(late.get("note") or "").splitlines()[0] if late.get("note") else "acceptance settled late", 400)]
    for row in late.get("reviewer_outputs") or []:
        if isinstance(row, Mapping) and row.get("slot_id"):
            who = str(row.get("model") or row.get("requested_model") or "")
            lines.append(f"- {row['slot_id']}{f' ({who})' if who else ''}: {row.get('verdict') or row.get('operation_state') or 'unknown'}")
    facts = plan_review_reflection_slice(drive, str(task_id))
    if isinstance(facts, Mapping) and facts.get("elements"):
        lines.append(f"Plan review ({facts.get('claims_source')} claims): "
                     + "; ".join(f"{e['id']} {e['changed_in_selected_plan']}, "
                                 + ", ".join(f"{f['class']} {f['disposition'].split(':', 1)[0]}" for f in e["findings"])
                                 for e in facts["elements"][:6]))
    lines.append(f"Source: get_task_result(task_id={task_id}), review_projection panel {panel.get('panel_id') or '?'}.")
    text = "\n".join(lines)
    if len(text) > LATE_SETTLEMENT_ROW_CHARS:
        text = text[: LATE_SETTLEMENT_ROW_CHARS - 40] + "\n… (bounded; read the source above)"
    return {
        "ts": utc_now_iso(), "task_id": str(task_id), "type": LATE_SETTLEMENT_TASK_TYPE,
        "task_type": LATE_SETTLEMENT_TASK_TYPE, "supplement_id": f"acceptance-late:{retry_key}",
        "goal": _cut(result.get("text") or result.get("description") or "", 200), "reflection": text,
        "source_ref": {"kind": "task_result", "reader": "get_task_result", "task_id": str(task_id),
                       "field": "review_projection", "panel_id": panel.get("panel_id")},
    }


def learn_from_late_settlement(root: Any, result: Mapping[str, Any], retry_key: str) -> bool:
    """Append the late-settlement row through the ordinary routed writer; failures only log."""
    from types import SimpleNamespace

    from ouroboros.reflection import append_reflection_routed

    try:
        root = pathlib.Path(str(root))
        entry = late_settlement_reflection_entry(root, str(result.get("task_id") or result.get("id") or ""), retry_key)
        if entry is None:
            return False
        append_reflection_routed(SimpleNamespace(drive_root=root), dict(result), entry)
        return True
    except Exception:
        log.warning("late settlement reflection row was not written", exc_info=True)
        return False
