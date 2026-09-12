"""Coordinator-first gateway restart request tool and direct-command guard."""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RESTARTCTL = Path("/Users/mutlupolatcan/.hermes/services/gateway-restart-coordinator/restartctl.py")
AUTHORIZED_HOMES = frozenset({"general", "coder"})
_SCANNED_TOOLS = frozenset({"terminal", "execute_code", "code_exec", "write_file", "patch"})
_BLOCK_MESSAGE = (
    "Direct model-driven gateway restart is disabled for this profile. "
    "Use request_gateway_restart so the external Restart Coordinator can serialize, "
    "verify, coalesce, recover, and roll back safely. Human /restart remains an "
    "emergency fallback when the coordinator is unavailable or operator_required."
)
_HERMES_RESTART = re.compile(r"(?is)\bgateway\s+restart\b")
_LAUNCHCTL_GATEWAY_MUTATION = re.compile(
    r"(?is)\blaunchctl\s+(?:kickstart|bootout|bootstrap|remove|stop|start|kill)\b[^\n\r]*?"
    r"ai\.hermes\.gateway-(?!restart-coordinator\b)[a-z0-9_-]+"
)
_DIRECT_GATEWAY_SIGNAL = re.compile(
    r"(?is)\b(?:kill|pkill)\b[^\n\r]*?(?:SIG)?(?:USR1|TERM)[^\n\r]*?(?:hermes|gateway|[0-9]{2,})"
)

SCHEMA = {
    "name": "request_gateway_restart",
    "description": (
        "Enqueue one validated gateway restart request through the external Restart Coordinator. "
        "This tool never restarts a process directly and derives requester identity from HERMES_HOME "
        "plus live gateway ancestry."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "target_profile": {
                "type": "string",
                "enum": ["general", "assistant", "researcher", "coder", "writer", "producer", "marketing", "health", "finance"],
            },
            "artifact_path": {"type": "string", "minLength": 1},
            "artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "expected_version": {"type": "string", "minLength": 1},
            "expected_pid": {"oneOf": [{"type": "integer", "minimum": 1}, {"const": "dependency_new_pid"}]},
            "rollback_path": {"type": "string", "minLength": 1},
            "rollback_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "health_url": {"type": "string", "pattern": "^http://(?:127\\.0\\.0\\.1|localhost)(?::[0-9]+)?/"},
            "semantic_canary": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string", "minLength": 1}, "equals": {}},
                "required": ["path", "equals"],
            },
            "dependency_task_id": {"type": ["string", "null"]},
            "barrier": {"type": ["string", "null"]},
        },
        "required": [
            "task_id", "target_profile", "artifact_path", "artifact_sha256",
            "expected_version", "expected_pid", "rollback_path", "rollback_sha256",
            "health_url", "semantic_canary",
        ],
    },
}


def _profile_from_home() -> str:
    home = Path(os.environ.get("HERMES_HOME", "")).resolve()
    return home.name if home.parent.name == "profiles" else ""


def _requirements_available() -> bool:
    return _profile_from_home() in AUTHORIZED_HOMES and RESTARTCTL.is_file()


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_strings(item)


def _command_text(text: str) -> str:
    """Normalize common argv/code punctuation without executing or parsing shell input."""
    return re.sub(r"[\[\](),'\"]+", " ", text)


_DOC_CANDIDATE_ROOT = Path(
    "/Users/mutlupolatcan/.hermes/profiles/general/artifacts/"
    "astra-instruction-revision/general-canary-source"
)


def _is_inert_doc_candidate(tool_name: str, args: Any) -> bool:
    """Approved OPS-239 staging-only exception; execution tools remain guarded."""
    if _profile_from_home() != "general" or not isinstance(args, Mapping):
        return False
    if tool_name not in {"write_file", "patch"}:
        return False
    if tool_name == "patch" and args.get("mode", "replace") != "replace":
        return False
    raw = args.get("path")
    if not isinstance(raw, str):
        return False
    target = Path(raw)
    if not target.is_absolute() or target.suffix != ".md" or ".." in target.parts:
        return False
    try:
        root = _DOC_CANDIDATE_ROOT
        target.relative_to(root)
        target.resolve().relative_to(root.resolve())
        # Inspect every path component; no symlink escape or executable/hardlink target.
        if any(part.is_symlink() for part in (target, *target.parents)):
            return False
        if target.exists():
            info = target.stat()
            if not target.is_file() or info.st_mode & 0o111 or info.st_nlink != 1:
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


_PROMOTION_MANIFEST = Path(__file__).with_name("ops239-promotion.json")
_PROMOTION_MANIFEST_SHA = "519c725723238402b65798e99c75619c14359aabf9517d70447354df6cb29e59"


def _is_exact_doc_promotion(tool_name: str, args: Any) -> bool:
    """Explicitly approved content-and-target pairs, including baseline rollback."""
    if tool_name != "write_file" or _profile_from_home() != "general":
        return False
    if not isinstance(args, Mapping) or not isinstance(args.get("content"), str):
        return False
    if not isinstance(args.get("path"), str):
        return False
    try:
        payload = _PROMOTION_MANIFEST.read_bytes()
        if hashlib.sha256(payload).hexdigest() != _PROMOTION_MANIFEST_SHA:
            return False
        manifest = json.loads(payload)
        target = Path(args["path"])
        root = Path(manifest["target_root"])
        if not target.is_absolute() or target.suffix != ".md" or ".." in target.parts:
            return False
        relative = target.relative_to(root).as_posix()
        target.resolve().relative_to(root.resolve())
        if any(part.is_symlink() for part in (target, *target.parents)):
            return False
        if target.exists():
            info = target.stat()
            if not target.is_file() or info.st_mode & 0o111 or info.st_nlink != 1:
                return False
        digest = hashlib.sha256(args["content"].encode("utf-8")).hexdigest()
        return digest in manifest["files"].get(relative, [])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


_CONFIG_SAFETY_DOC_TARGETS = {
    "/Users/mutlupolatcan/.hermes/profiles/general/artifacts/astra-instruction-revision/config-safety-general-canary/skills/hermes-config-editing/SKILL.md": frozenset({
        "4b454404c5fed7c63d573cd431ebf65a19aeee9de602105ee0e2406f88af5255",
        "c4a597ed809a5f2b37e8ae876099898c46691d33f397906e5363a47e1c7f3dd4",
    }),
    "/Users/mutlupolatcan/.hermes/shared-skills/canonical/hermes-config-editing/SKILL.md": frozenset({
        "4b454404c5fed7c63d573cd431ebf65a19aeee9de602105ee0e2406f88af5255",
        "c4a597ed809a5f2b37e8ae876099898c46691d33f397906e5363a47e1c7f3dd4",
    }),
}


def _is_config_safety_doc_promotion(tool_name: str, args: Any) -> bool:
    """OPS-239 human-approved two-target, content-pinned document promotion."""
    if tool_name != "write_file" or _profile_from_home() != "general":
        return False
    if not isinstance(args, Mapping) or not isinstance(args.get("content"), str):
        return False
    raw = args.get("path")
    if not isinstance(raw, str) or raw not in _CONFIG_SAFETY_DOC_TARGETS:
        return False
    try:
        target = Path(raw)
        if not target.is_absolute() or target.suffix != ".md" or ".." in target.parts:
            return False
        if str(target.resolve()) != raw:
            return False
        if any(part.is_symlink() for part in (target, *target.parents)):
            return False
        if target.exists():
            info = target.stat()
            if not target.is_file() or info.st_mode & 0o111 or info.st_nlink != 1:
                return False
        digest = hashlib.sha256(args["content"].encode("utf-8")).hexdigest()
        return digest in _CONFIG_SAFETY_DOC_TARGETS[raw]
    except (OSError, ValueError, RuntimeError):
        return False


def _pre_tool_call(tool_name: str = "", args: Any = None, **_: Any):
    if _is_config_safety_doc_promotion(tool_name, args):
        return None
    if _is_exact_doc_promotion(tool_name, args):
        return None
    if _is_inert_doc_candidate(tool_name, args):
        return None
    if tool_name not in _SCANNED_TOOLS:
        return None
    for text in _iter_strings(args):
        normalized = _command_text(text)
        patterns = (_HERMES_RESTART, _LAUNCHCTL_GATEWAY_MUTATION, _DIRECT_GATEWAY_SIGNAL)
        if any(pattern.search(normalized) for pattern in patterns):
            return {"action": "block", "message": _BLOCK_MESSAGE}
    return None


def _request_gateway_restart(payload: dict[str, Any]) -> str:
    if not _requirements_available():
        return json.dumps({"status": "rejected", "reason": "restart_coordinator_unavailable"}, sort_keys=True)
    fd, name = tempfile.mkstemp(prefix="hermes-restart-request-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        child_env = {
            "HERMES_HOME": os.environ.get("HERMES_HOME", ""),
            "HOME": os.environ.get("HOME", ""),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/Users/mutlupolatcan/.local/bin",
        }
        completed = subprocess.run(
            [sys.executable, str(RESTARTCTL), "request", name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=child_env,
        )
        raw = (completed.stdout if completed.returncode == 0 else completed.stderr).strip()
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict) and isinstance(decoded.get("status"), str):
                    return json.dumps(decoded, sort_keys=True)
            except json.JSONDecodeError:
                pass
        return json.dumps(
            {"status": "rejected", "reason": "restart_facade_failed", "exit_code": completed.returncode},
            sort_keys=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({"status": "rejected", "reason": type(exc).__name__}, sort_keys=True)
    finally:
        Path(name).unlink(missing_ok=True)


def _handler(args: dict[str, Any], **_: Any) -> str:
    return _request_gateway_restart(dict(args))


def register(ctx) -> None:
    if not callable(getattr(ctx, "register_hook", None)):
        raise RuntimeError("gateway-restart-request requires pre_tool_call hook support")
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_tool(
        name="request_gateway_restart",
        toolset="gateway_restart",
        schema=SCHEMA,
        handler=_handler,
        check_fn=_requirements_available,
        description=SCHEMA["description"],
        emoji="↻",
    )
