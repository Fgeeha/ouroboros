"""Retain a review request's named evidence before binding any paid reader.

The artifact/source promotion owners verify typed references and their owned
JSON closure. Retrieving acceptance additionally names a task record, receipt
union, task-filtered trajectory and artifact manifest. These are immutable
snapshots, not a copy of an execution drive. Opaque prose is never path-rewritten.
The returned request carries both original provenance and actual reader paths;
missing named bytes refuse dispatch, while an absent optional log is disclosed.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import tempfile
from typing import Any


def retain_review_refs(value: Any, source: pathlib.Path, custody: pathlib.Path, task_id: str) -> Any:
    """Verify and retain a typed closure through the existing promotion owners."""
    from ouroboros.observability import promote_child_task_refs

    retained, facts = promote_child_task_refs(custody, source, task_id, {'review_evidence': value})
    if facts['pending_refs'] or facts['unavailable_refs']:
        raise ValueError(json.dumps(facts, sort_keys=True))
    return retained['review_evidence']


def _retain_named_sources(request: Any, source: pathlib.Path, custody: pathlib.Path) -> list[dict]:
    from ouroboros.artifacts import (copy_artifact_file, read_actor_source_bytes, store_actor_source_bytes,
                                    stream_artifact_file, task_artifact_dir_path)
    from ouroboros.outcome_receipt_store import publish_verification_receipt_union, verification_receipts_path
    from ouroboros.task_results import load_task_result, task_result_path

    task_id, rows = request.task_id, []
    base = task_artifact_dir_path(source, task_id)
    target = task_artifact_dir_path(custody, task_id, create=True)

    def retain(name, original, raw):
        ref = store_actor_source_bytes(custody, task_id, category='context_checkpoints',
                                      source_id='review-retrieval-' + name, data=raw, extension='json')
        if read_actor_source_bytes(custody, task_id, ref) != raw:
            raise ValueError(f'{name}: source readback failed')
        ref = retain_review_refs(ref, source, custody, task_id)
        rows.append({'name': name, 'source_path': str(original), 'source_ref': ref,
                     'retained_path': str(target / ref['path']), 'status': 'retained'})

    result = load_task_result(source, task_id, strict=True)
    if not result:
        raise ValueError('task result source unavailable')
    # A declared artifact is copied through the verified file owner, preserving
    # its name and source path beside a digest-named immutable reader target.
    manifest = [*(request.evidence.get('artifacts') or []), *(result.get('artifacts') or [])]
    paths = {}
    for artifact in manifest:
        if not isinstance(artifact, dict):
            raise ValueError('invalid artifact manifest member')
        if artifact.get('name') == '…':
            raise ValueError('artifact manifest incomplete')
        original = pathlib.Path(artifact.get('path') or base / str(artifact.get('relpath') or artifact.get('name') or ''))
        if not original.is_absolute():
            original = source / original
        if original == verification_receipts_path(source, task_id):
            continue  # receipts have their union owner below
        if str(original) in paths:
            continue
        identity = stream_artifact_file(original, expected=artifact if artifact.get('immutable') else None)
        if artifact.get('sha256') and artifact['sha256'] != identity['sha256']:
            raise ValueError(f'artifact digest mismatch: {original}')
        if artifact.get('size') is not None and int(artifact['size']) != identity['size']:
            raise ValueError(f'artifact size mismatch: {original}')
        artifact_rel = f"source_handles/context_checkpoints/review-artifact-{identity['sha256']}.bin"
        copy_artifact_file(original, target / artifact_rel, expected=identity)
        ref = {'kind': 'task_source', 'root': 'artifact_store', 'path': artifact_rel, **identity,
               'read': {'tool': 'read_file', 'arguments': {'root': 'artifact_store', 'path': artifact_rel}}}
        read_actor_source_bytes(custody, task_id, ref)
        paths[str(original)] = str(target / artifact_rel)
        if artifact.get('path'):
            paths[artifact['path']] = str(target / artifact_rel)
        rows.append({'name': 'artifact:' + str(artifact.get('name') or original.name),
                     'source_path': str(original), 'source_ref': ref, 'retained_path': str(target / artifact_rel),
                     'status': 'retained'})

    def bind_artifacts(value):
        # Only structured artifact path fields are read addresses. Prose and
        # source_path retain the author's exact provenance, even if obsolete.
        if isinstance(value, dict):
            return {key: paths.get(item, item) if key == 'path' and isinstance(item, str)
                    else bind_artifacts(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bind_artifacts(item) for item in value]
        return value

    result = retain_review_refs(bind_artifacts(result), source, custody, task_id)
    retain('task-result', task_result_path(source, task_id), json.dumps(result, ensure_ascii=False).encode())
    request.evidence = bind_artifacts(request.evidence)
    for row in request.evidence.get('artifacts') or []:
        original = pathlib.Path(row.get('path') or base / row['name'])
        original = str(original if original.is_absolute() else source / original)
        if original in paths:
            row['path'] = paths[original]

    receipt = verification_receipts_path(source, task_id)
    canonical_receipt = verification_receipts_path(custody, task_id)
    if receipt.exists() and not publish_verification_receipt_union(custody, task_id, source):
        raise ValueError('verification receipt union unavailable')
    if canonical_receipt.exists():
        retain('verification-receipts', receipt, canonical_receipt.read_bytes())
    else:
        rows.append({'name': 'verification-receipts', 'source_path': str(receipt), 'status': 'not_recorded'})
    trajectory = source / 'logs' / 'tools.jsonl'
    if trajectory.exists():
        with trajectory.open(encoding='utf-8') as stream:
            records = [row for line in stream if line.strip() if (row := json.loads(line)).get('task_id') == task_id]
        records = retain_review_refs(records, source, custody, task_id)
        retain('tool-trajectory', trajectory, json.dumps(records, ensure_ascii=False).encode())
    else:
        rows.append({'name': 'tool-trajectory', 'source_path': str(trajectory), 'status': 'not_recorded'})
    return rows


def _reader_bindings(value: Any, root: pathlib.Path, task_id: str) -> list[dict]:
    """Give each task-owned ref its actual address without rewriting opaque text."""
    from ouroboros.artifacts import task_artifact_dir_path

    rows = {}

    def visit(item, owner):
        if isinstance(item, dict):
            if item.get('kind') == 'task_source':
                path = task_artifact_dir_path(root, owner) / item['path']
                rows[(owner, str(path))] = {'owner_task_id': owner, 'source_path': item['path'],
                    'sha256': item['sha256'], 'size': item['size'],
                    'retained_path': str(path), 'read': {'tool': 'read_file', 'arguments': {
                        'root': 'runtime_data', 'path': str(path.relative_to(root))}}}
            else:
                for key, child in item.items():
                    visit(child, source_owner(key, child, owner))
        elif isinstance(item, list):
            for child in item:
                visit(child, owner)

    visit(value, task_id)
    return list(rows.values())


def retain_review_request_sources(request: Any, *, source_root: Any, custody_root: Any) -> None:
    """Bind typed request refs and named acceptance sources to durable custody.

    Call BEFORE review_operation_scope, serialization, prompt caching or dispatch.
    This is not rendered-packet custody: general typed source closure includes
    earlier loops and native continuation sources. Reuse verifies the original
    snapshots; it never refreshes a request underneath a live paid reader.
    A failure raises before the caller can stamp paid work.
    """
    from ouroboros.artifacts import read_actor_source_bytes, task_artifact_dir_path

    source, custody = pathlib.Path(source_root), pathlib.Path(custody_root)
    retained = request.policy.get('review_source_closure')
    if retained:
        read_root = pathlib.Path(retained['read_root']).resolve()
        if (retained.get('task_id') != request.task_id
                or source.resolve() != read_root
                or not read_root.is_relative_to((task_artifact_dir_path(custody, request.task_id) / 'source_handles' / 'review_inputs').resolve())):
            raise ValueError('review source closure identity mismatch')
        for row in retained['sources']:
            if row['status'] == 'retained':
                read_actor_source_bytes(read_root, request.task_id, row['source_ref'])
        retain_review_refs(dataclasses.asdict(request), read_root, custody, request.task_id)
        return
    # Work on a copy: partial publication cannot leave the caller appearing bound.
    bound = dataclasses.replace(request, **{key: value for key, value in retain_review_refs(
        dataclasses.asdict(request), source, custody, request.task_id).items()})
    parent = task_artifact_dir_path(custody, request.task_id, create=True) / 'source_handles' / 'review_inputs'
    parent.mkdir(parents=True, exist_ok=True)
    read_root = pathlib.Path(tempfile.mkdtemp(prefix='request-', dir=parent))
    bound = dataclasses.replace(bound, **retain_review_refs(dataclasses.asdict(bound), custody, read_root, request.task_id))
    named = _retain_named_sources(bound, source, read_root) if request.surface == 'task_acceptance' else []
    bindings = _reader_bindings(dataclasses.asdict(bound), read_root, request.task_id)
    bound.policy = {**bound.policy, 'native_data_root': str(read_root), 'review_source_closure': {
        'schema_version': 1, 'task_id': request.task_id, 'read_root': str(read_root),
        'sources': named, 'refmap': bindings}}
    retain_review_refs(dataclasses.asdict(bound), read_root, custody, request.task_id)
    for field in dataclasses.fields(request):
        setattr(request, field.name, getattr(bound, field.name))


def promote_source_payload(raw: bytes, *, source_id: str, extension: str, category: str,
                           parent_root: pathlib.Path, child_root: pathlib.Path,
                           task_id: str, state: dict) -> bytes:
    """Close the host-owned review/checkpoint JSON shapes; preserve opaque bytes."""
    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.observability import _rewrite_child_ref_tree

    plan_wave_source = source_id.startswith("plan-review-wave-")
    if extension == "json":
        json_lines = source_id == 'review-retrieval-verification-receipts'
        try:
            payload = [json.loads(line) for line in raw.splitlines() if line.strip()] if json_lines else json.loads(raw)
        except (ValueError, UnicodeError):
            payload = None
        meta = payload.get("artifact_meta") if isinstance(payload, dict) else None
        plan_wave = plan_wave_source and isinstance(meta, dict) and meta.get("kind") == "plan_review_wave"
        # Native history/round sources use the same typed refs, including view
        # receipts. Follow their host-owned JSON shape, never arbitrary prose.
        native_source = isinstance(payload, dict) and 'read_receipts' in payload and (
            'round_sources' in payload or ('round' in payload and 'messages' in payload)
            or ('required_sources_ref' in payload and 'read_provenance' in payload))
        checkpoint = isinstance(payload, dict) and all(key in payload for key in (
            'messages', 'selection_fingerprint', 'observed_view_revision', 'selected_unit_ids'))
        retained_review = source_id.startswith('review-retrieval-')
        if plan_wave or native_source or checkpoint or retained_review or (isinstance(payload, list) and source_id == "acceptance_tool_trajectory") or (
            isinstance(payload, dict) and isinstance(payload.get("request"), dict)
            and payload["request"].get("surface") == "task_acceptance"
        ):
            rewritten = _rewrite_child_ref_tree(payload, parent_root, child_root, task_id, state)
            if checkpoint and rewritten != payload:
                # Capsules bind raw messages/unit identities. Copying their
                # relative source closure may not rewrite the captured transcript.
                raise ValueError('context checkpoint closure requires transcript rebinding')
            if rewritten != payload:
                # Retain the captured digest as well as the re-addressed view.
                # Historical transcripts may still quote the original handle.
                store_actor_source_bytes(parent_root, task_id, category=category,
                    source_id=source_id, data=raw, extension=extension)
                raw = (("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                        for row in rewritten) + "\n") if json_lines else
                       json.dumps(rewritten, ensure_ascii=False, sort_keys=True, default=str)).encode("utf-8")
    return raw


def source_owner(key: str, value: Any, task_id: str) -> str:
    """Only an attested predecessor carrier changes a nested source's owner."""
    if key == 'predecessor_authority' and isinstance(value, dict) and value.get('task_id'):
        from ouroboros.agent_startup_checks import valid_task_result_authority_source
        from ouroboros.artifacts import validate_task_id

        owner = validate_task_id(value['task_id'])
        if not valid_task_result_authority_source(value.get('source'), owner):
            raise ValueError('predecessor source has no task-bound authority')
        return owner
    return task_id
