"""Stable host instructions and complete actor-first coordination appendix."""

from __future__ import annotations

import json
from hashlib import sha256


HOST_INSTRUCTIONS = (
    "You are a delegated worker running inside the workspace assigned by your host. Your "
    "assignment and explicit owner/task constraints govern your work. Do not run git "
    "commit, tag, push, rebase, reset or any other history-moving command: your host "
    "captures changes against its recorded baseline and decides whether to integrate "
    "them. A private delegated snapshot can preserve committed changes in that diff, "
    "but a moved HEAD is disclosed as an instruction violation; it does not authorize "
    "a commit or apply. A self_worktree capture separately requires an unchanged HEAD. "
    "Do not review or accept your own change, do not "
    "touch the host's runtime controls, skills, or memory. If your environment "
    "offers a way to ask your host a clarifying "
    "question, you may use it: your host may answer from its task context; a question "
    "that carries an engine expiry times out benignly if unanswered — continue with "
    "stated assumptions rather than blocking — while one without an expiry waits until "
    "answered. If your harness cannot ask mid-run, do NOT end the run to ask — "
    "state your assumption and continue."
)

UNPROVEN_BOUNDARY_INSTRUCTION = (
    " An OS-enforced filesystem boundary was REQUESTED for this run but is NOT guaranteed: "
    "your engine applies one only where it has a mechanism for this host, and your host "
    "reads back from your own attempt records what was actually applied. Work as if there "
    "is no boundary — stay inside this root, do not read the operator's home directory, "
    "credential stores, or the harness runtime tree, and do NOT describe yourself in your "
    "answer as sandboxed or confined. If your own environment shows you whether a boundary "
    "was in force, say so plainly."
)


_ACCESS_PRECEDENCE = (
    "this line governs native process access, while explicit task constraints "
    "and the assigned edit target still bind. When a private delegated snapshot "
    "exists, the later DELEGATED EXECUTION BINDING is the assigned edit target "
    "and supersedes path fields in the inherited contract."
)

ACCESS_INSTRUCTIONS = {
    "readonly": (
        " ACCESS: you may read and run read-only commands inside this root, and make no "
        "edits or writes; " + _ACCESS_PRECEDENCE
    ),
    "workspace_write": (
        " ACCESS: you may edit inside this root and must not write outside this root; " + _ACCESS_PRECEDENCE
    ),
    "full": (
        " ACCESS: full native process access is requested for this assignment, "
        "with source edits delivered through the assigned root; effective access "
        "is established by the run receipt, and the private snapshot is not an OS "
        "sandbox; " + _ACCESS_PRECEDENCE
    ),
}


def access_instruction(access: str) -> str:
    """The ONE canonical sentence for a run's typed access profile, or "".

    A parent's prose ban ("Read-only no edits/commands...") in a work order once
    duplicated and contradicted the profile the host had already derived, and the
    run died unable to reach its own read surface. `DelegatedRunShape.access` is
    the authority, so the host states it in exactly one sentence and says which
    text wins. Deliberately not a paragraph and not a list of prohibitions: a
    longer rule becomes prose competing with the typed profile, which is the
    defect. The parent's prose is never parsed, only outranked. An unrecognized
    profile renders nothing rather than inventing a rule.
    """
    return ACCESS_INSTRUCTIONS.get(str(access or "").strip(), "")


def execution_binding_instruction(execution_root: str, authority_root: str) -> str:
    """State the host's effective write root after private snapshot provisioning.

    The task contract names the stable authority/project root so the host can
    reconcile and apply a result. That path is not the child write surface once
    a private delegated snapshot exists. The binding is appended after the
    inherited assignment so an owner-facing absolute path cannot silently win by
    omission or by appearing earlier in the work order.
    """
    execution = str(execution_root or "").strip()
    authority = str(authority_root or "").strip()
    if not execution:
        return ""
    return (
        "\n\nDELEGATED EXECUTION BINDING (canonical host fact; supersedes path fields "
        "in the inherited contract for this run): "
        f"the sole writable execution root for this run is {execution}. "
        "Use relative paths or absolute paths under that root for every shell, "
        "file, and patch operation. The stable authority/project root "
        f"{authority or '(unknown)'} is a read-only identity/reference until the "
        "parent explicitly integrates the captured result. Do not write, patch, "
        "stage, reset, clean, or commit the authority root. If the harness cannot "
        "honor this binding, stop with a typed execution-root mismatch instead of "
        "falling back to the authority root."
    )


def _rebind_json_block(text: str, marker: str, execution_root: str, authority_root: str) -> str:
    start = text.find(marker)
    if start < 0:
        return text
    json_start = text.find("\n", start)
    if json_start < 0:
        return text
    json_start += 1
    try:
        value, used = json.JSONDecoder().raw_decode(text[json_start:])
    except (TypeError, ValueError):
        return text
    if not isinstance(value, dict):
        return text

    def rewrite(node):
        if isinstance(node, dict):
            for key, child in list(node.items()):
                if key in {"workspace_root", "write_root"} and isinstance(child, str) and child:
                    node[key] = execution_root
                else:
                    rewrite(child)
            node.setdefault("authority_target_root", authority_root)
        elif isinstance(node, list):
            for child in node:
                rewrite(child)

    rewrite(value)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text[:json_start] + rendered + text[json_start + used:]


def bind_execution_assignment(text: str, execution_root: str, authority_root: str) -> str:
    """Rewrite child-facing structured path fields before appending the binding."""
    bound = _rebind_json_block(text, "HOST TASK CONTRACT AUTHORITY", execution_root, authority_root)
    return _rebind_json_block(bound, "HOST AUTHORITY BINDING", execution_root, authority_root)


def apply_execution_binding(instructions: str, prompt: str, compiled: bool,
                            execution_root: str, authority_root: str) -> tuple[str, str]:
    binding = execution_binding_instruction(execution_root, authority_root)
    instructions = bind_execution_assignment(instructions, execution_root, authority_root) + binding
    if compiled:
        prompt = bind_execution_assignment(prompt, execution_root, authority_root) + binding
    return instructions, prompt


def append_coordination_context(
    base_instructions: str,
    coordination_context: str,
) -> str:
    """Append the exact advisory context without changing instruction roles."""

    context = str(coordination_context or "")
    if not context:
        return base_instructions
    coordination_sha = sha256(context.encode("utf-8")).hexdigest()
    appendix = (
        "\n\nHOST COORDINATION CONTEXT (advisory appendix; canonical work-order "
        f"authority remains unchanged; sha256={coordination_sha}):\n{context}"
    )
    return base_instructions + appendix
