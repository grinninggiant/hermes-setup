"""Bind a ledger-owned submission to native, guarded gateway injection.

This module neither watches restart status nor claims work. The owner must supply
an authoritative, fast synchronous freshness predicate; it runs again at ingress.
"""
import hashlib
import inspect
import re


def fence_authorized_inbound(store, *, owner_home, event, gateway, session_store, **_telemetry):
    """Native pre-dispatch observer; no directive and no implicit source authority.

    Registration must bind owner_home/store to the owning plugin, not ambient
    runtime state. Errors propagate to the caller; this helper alone cannot make
    native observer exception suppression a durable cancellation guarantee.
    """
    from pathlib import Path
    owner = Path(owner_home).resolve()
    source = event.source
    if getattr(event, "internal", False) or getattr(source, "profile_route_rejected", False):
        return None
    if getattr(source, "profile", None) not in (None, "", owner.name):
        return None
    if not Path(store.path).resolve().is_relative_to(owner):
        raise ValueError("foreign_continuation_store")
    if Path(gateway._resolve_profile_home_for_source(source)).resolve() != owner:
        return None
    authorized = gateway._is_user_authorized_for_source(source)
    if inspect.iscoroutine(authorized):
        authorized.close()
    if authorized is not True:
        return None
    try:
        if session_store is not gateway.session_store:
            raise ValueError("foreign_session_store")
        key = gateway._session_key_for_source(source)
        entry = session_store.lookup_by_session_key(key)
        if entry is not None:
            store.fence(entry.session_id, authorized=True)
    except Exception:
        store.block_dispatch()
        raise
    return None


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
        if not store.dispatch_safe():
            return False
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
