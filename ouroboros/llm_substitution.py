"""Which account should NOT answer the next request of a route, and why.

Two facts share this leaf because they answer one question. A per-subject
refusal (auth, quota, an unusable credential) says the account that just failed
should not be preferred again; a served-model mismatch says the account that
just answered did so with the WRONG MODEL. Both are spent by the next matching
dispatch, and neither ranks accounts: the engine remains the only chooser, and
these facts only stop the transport from asking it for the same account again.

## Refusing a round that ANOTHER model answered

A subscription account can answer a request for one model with a different
model: no error, nothing in its quota, and the same account keeps doing it for
tens of minutes while its siblings serve the requested model normally. The
ENGINE states that as a typed fact on its completed result and deprioritises
that account for a while; this leaf owns what the caller does with the fact.

The answer is never the round. It stays retained and acknowledged like any
other answer, because the horizon a round nearly ran under is evidence, but it
adopts no turn state, runs no tool call, and is replaced by a NEW operation
that drops the substituting account's preference. Two cases are refused
outright instead: a pin names ONE account, and an already admitted candidate is
bound to exact bytes a redo would change. When no redo is left, the typed
`model_substituted` refusal reaches the caller, whose configured fallback chain
owns what happens next.

The budget is its own counter, never the transport's no-start preparation
loop: a repair fixes a request that was never sent, while a redo spends a
fresh generation on an answer that already arrived.
"""

from __future__ import annotations

import contextvars
import copy
from typing import Any

from ouroboros import config
from ouroboros.utils import append_jsonl, utc_now_iso

_FAILED_PROFILE = contextvars.ContextVar("claudexor_failed_profile", default=())
_PER_SUBJECT_REFUSALS = frozenset({
    "auth_required", "auth_refresh_failed", "credential_unusable", "provider_refused",
    "rate_limited", "subscription_window_exhausted",
})
_NON_PROVIDER_FAILURES = frozenset({
    "model_operation_cancelled", "model_operation_interrupted",
})


def failed_account_preference(target: dict, parameters: dict) -> str:
    """The account this route must not prefer right now, if one is remembered."""
    failed = _FAILED_PROFILE.get()
    key = (parameters.get("cache_affinity"), target["source"], target["resolved_model"])
    return failed[3] if len(failed) == 4 and failed[:3] == key else ""


def take_failed_account_preference(target: dict, parameters: dict) -> str:
    """Read that account and spend the fact: it names ONE next dispatch."""
    profile = failed_account_preference(target, parameters)
    if profile:
        _FAILED_PROFILE.set(())
    return profile


def suppress_account_preference(target: dict, parameters: dict, profile: str) -> None:
    """Keep the next request of this ROUTE from preferring ``profile`` again.

    Route identity is the requested source and model, so a helper that carries
    no cache affinity suppresses its preference exactly like a task round does.
    """
    _FAILED_PROFILE.set((parameters.get("cache_affinity"), target["source"],
                         target["resolved_model"], profile))


def remember_failed_profile(target: dict, parameters: dict, error: Any) -> None:
    if getattr(error, "stream_rejected", False):
        return  # Local message normalization says nothing about account readiness.
    route = error.route or {}
    key = (parameters.get("cache_affinity"), route.get("source"), route.get("model"))
    if (key == (parameters.get("cache_affinity"), target["source"], target["resolved_model"])
            and key[0] and route.get("credentialProfileId")
            and ((error.status_code == 0 and error.code not in _NON_PROVIDER_FAILURES)
                 or error.code in _PER_SUBJECT_REFUSALS)):
        _FAILED_PROFILE.set((*key, route["credentialProfileId"]))


def substitution_fact(result: dict) -> dict | None:
    """The engine's typed fact that ANOTHER model answered this request.

    Only the engine compares model identity: the host never matches model
    strings of its own, so a dated snapshot, an alias or another transport
    cannot turn into a host-invented mismatch here.
    """
    fact = result.get("modelMismatch")
    if not isinstance(fact, dict):
        return None
    requested, observed = str(fact.get("requested") or ""), str(fact.get("observed") or "")
    return {"requested": requested, "observed": observed} if requested and observed else None


def _redo_allowed(payload: dict) -> str:
    """Empty when the same round may be asked again, else why it may not be.

    A pin names ONE account, so re-asking it is the same account and the same
    answer; the caller's configured model fallback owns that case. An admitted
    candidate is bound to exact bytes, and a redo drops the account preference,
    so re-asking would break that admission instead of honoring it.
    """
    from ouroboros.usage_accounting import (
        current_physical_attempt_predicate, physical_attempt_headroom,
    )

    if (payload.get("account") or {}).get("mode") == "pin":
        return "pinned_account"
    if current_physical_attempt_predicate() is not None:
        return "admitted_candidate"
    from ouroboros.model_wait import dispatch_deadline_remaining_sec

    remaining = dispatch_deadline_remaining_sec()
    if remaining is not None and remaining <= 0:
        # The round already spent its window. Another send would be paid for
        # out of time the caller no longer has.
        return "deadline_spent"
    headroom = physical_attempt_headroom()
    if headroom is not None and headroom < 1:
        # A bounded actor (a density probe, a packet review) owns those sends
        # for its own rail. A redo that spent the last one would leave the
        # repair that rail is about to make unable to run.
        return "send_budget_spent"
    return ""


def _discard(invocation: Any, fact: dict, result: dict,
             disposition: str, redo: int, redos: int) -> dict:
    """Keep the paid generation reconstructible, then refuse it as this round's answer.

    The bytes stay retained and acknowledged exactly as an accepted answer's
    would be, because a discarded answer is still evidence of what the horizon
    was. What it does NOT do is become the round: no turn state is adopted from
    it, no tool call runs, and the caller drops this account's preference before
    building the next request, so the engine's selection is free to land
    elsewhere.
    """
    route = result.get("route") or {}
    usage = result.get("usage") or {}
    # A discarded answer is only evidence while its bytes are reachable. Say
    # what custody it actually has instead of assuming retention and the ACK
    # both worked; a durable row that hid a failed one would be a false receipt.
    custody = invocation.acknowledge() if invocation.response_ref else {"state": "absent"}
    recorded = append_jsonl(invocation.root / "logs" / "events.jsonl", {
        "ts": utc_now_iso(), "type": "model_served_mismatch", "task_id": invocation.task_id,
        "model_role": invocation.role, "operation_id": invocation.operation_id,
        "physical_attempt_id": invocation.invocation_id, "disposition": disposition,
        "redo": redo, "redos": redos, **fact, "route": copy.deepcopy(route),
        "discarded_usage": {key: usage.get(key) for key in
                            ("input_tokens", "output_tokens", "cached_input_tokens")},
        "result_custody": copy.deepcopy(custody),
    })
    return {**custody, **({} if recorded else {"event_recorded": False})}


class SubstitutionBudget:
    """One call's budget for re-asking a round that ANOTHER model answered.

    Its own counter, never the preparation loop's: a no-start repair fixes the
    request that was never sent, while this spends a fresh generation on an
    answer that already arrived. Both axes can occur in one call.
    """

    def __init__(self, error: type) -> None:
        self.error = error
        self.redos = config.get_model_substitution_redos()
        self.used = 0
        self.discarded: list[dict] = []
        # The account this call must stop preferring, applied by the CALLER: a
        # context variable set on an offloaded thread never reaches the loop
        # that builds the next request.
        self.avoid = ""

    def stop_preferring(self, target: dict, parameters: dict) -> None:
        """Apply the pending suppression HERE, where the next request is built."""
        if self.avoid:
            suppress_account_preference(target, parameters, self.avoid)
            self.avoid = ""

    def disclose(self, answer: tuple[dict, dict]) -> tuple[dict, dict]:
        """Carry the discarded generations into the accepted answer's usage row.

        The owner learns which horizon this round nearly ran under even when the
        redo recovered silently; money and tokens stay the ledger's, per attempt.
        """
        message, usage = answer
        if self.discarded and isinstance(usage.get("claudexor"), dict):
            usage["claudexor"]["substituted"] = copy.deepcopy(self.discarded)
        return message, usage

    def admit(self, invocation: Any, target: dict, payload: dict,
              parameters: dict, result: dict) -> bool:
        """True once this substituted generation is discarded and the round re-asked.

        False leaves an ordinary answer untouched; a round that may not be
        re-asked raises instead, so the other model's answer never becomes the
        caller's — whatever the reason the redo is unavailable.
        """
        fact = substitution_fact(result)
        if fact is None:
            return False
        reason = _redo_allowed(payload) or (
            "" if self.used < self.redos else "redos_exhausted")
        self.used += 1
        self.avoid = str((result.get("route") or {}).get("credentialProfileId") or "")
        self.discarded.append({**fact, "account": self.avoid, "disposition": reason or "redo"})
        custody = _discard(invocation, fact, result, reason or "redo", self.used, self.redos)
        self.discarded[-1]["result_custody"] = custody
        if reason:
            raise _refusal(self, invocation, fact, result, reason, self.used - 1)
        return True


def _refusal(budget: "SubstitutionBudget", invocation: Any, fact: dict,
             result: dict, reason: str, redos: int) -> Exception:
    """The typed refusal a caller sees once no further redo is available."""
    error = budget.error({
        "code": "model_substituted",
        "message": (f"The route answered with {fact['observed']} instead of the requested "
                    f"{fact['requested']}; the answer was not accepted."),
        "context": {"requested_model": fact["requested"], "observed_model": fact["observed"],
                    "reason": reason, "redos": redos},
    }, model_role=invocation.role, operation_id=invocation.operation_id,
        route=result.get("route") or {})
    if invocation.capture is not None:
        error.physical_attempt_capture = invocation.capture
    return error
