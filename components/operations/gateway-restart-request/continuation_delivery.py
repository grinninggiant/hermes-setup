"""Bind a ledger-owned submission to native, guarded gateway injection.

This module neither watches restart status nor claims work. The owner must supply
an authoritative, fast synchronous freshness predicate; it runs again at ingress.
"""
import hashlib
import inspect
import re


def schedule_bound(store, ctx, operation_id, owner_id, session_key, *, authority_guard):
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", operation_id):
        raise ValueError("invalid_operation_id")
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("invalid_session_key")
    parameters = inspect.signature(ctx.inject_message).parameters
    if not {"expected_session_id", "dispatch_guard"}.issubset(parameters):
        raise RuntimeError("native_guarded_injection_unavailable")
    if not callable(authority_guard) or inspect.iscoroutinefunction(authority_guard):
        raise ValueError("invalid_authority_guard")
    row = store.get(operation_id)
    route_digest = hashlib.sha256(session_key.encode()).hexdigest()
    if not row or row["owner_id"] != owner_id or row["route_digest"] != route_digest:
        return False

    def authority_now():
        result = authority_guard()
        if inspect.iscoroutine(result):
            result.close()
        return result is True

    try:
        if not authority_now():
            return False
    except Exception:
        return False

    def still_authorized():
        current = store.get(operation_id)
        if not current or current["state"] not in {"dispatching", "submitted"}:
            return False
        if any(current[key] != row[key] for key in
               ("session_id", "route_digest", "checkpoint_digest", "owner_id")):
            return False
        return authority_now()

    return store.submit(operation_id, owner_id, lambda: ctx.inject_message(
        f"[restart-continuation:{operation_id}] Verify restart evidence and continue only the already-authorized work. This scheduling event is not completion.",
        session_key=session_key,
        expected_session_id=row["session_id"],
        dispatch_guard=still_authorized,
    ))
