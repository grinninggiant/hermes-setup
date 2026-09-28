"""Native Linear Agent Session platform adapter for Hermes Gateway."""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
from markdown_it import MarkdownIt

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)

try:
    from gateway.platforms.base import GoalStatusNotice
except ImportError:  # Upstream cores carry no platform goal-status seam.
    GoalStatusNotice = Any
from gateway.session import SessionSource, build_session_key  # type: ignore[import-not-found]

from .acceptance import acceptance_criteria, acceptance_gate
from .ledger import DeliveryLedger
from .linear_client import LinearAPIError, LinearClient


def _dump_asyncio_tasks_on_request(database_path: str) -> str | None:
    """Operator diagnostic: touching ``linear-task-dump.request`` beside the ledger writes every
    pending asyncio task's stack on this (gateway) loop to ``linear-task-dump.txt``. Read-only."""
    if not database_path:
        return None
    state_dir = Path(database_path).parent
    request = state_dir / "linear-task-dump.request"
    if not request.exists():
        return None
    request.unlink(missing_ok=True)
    output = state_dir / "linear-task-dump.txt"
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(f"# {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        for task in asyncio.all_tasks():
            fh.write(f"\n=== {task.get_name()} {task!r:.300}\n")
            task.print_stack(limit=40, file=fh)
    os.chmod(output, 0o600)
    logger.warning("[linear] asyncio task dump written to %s", output)
    return str(output)


logger = logging.getLogger(__name__)


def _ledger_busy(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        str(exc) == "Linear ledger is busy"
        or getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )


def _check_admission_deadline(deadline: float | None) -> None:
    # timeout_at only cancels on a loop tick; an awaited coroutine may never yield.
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError("Linear created admission deadline expired")


@asynccontextmanager
async def _admission_lock(lock: asyncio.Lock, deadline: float | None):
    async with asyncio.timeout_at(deadline):
        await lock.acquire()
    try:
        _check_admission_deadline(deadline)
        yield
    finally:
        lock.release()


def _ops200_profile_fix_active() -> bool:
    import sys
    core_file = getattr(sys.modules.get("gateway.run"), "__file__", None)
    return bool(core_file and Path(core_file).resolve().parent.parent.name == (
        "hermes-agent-5cc98f1f2ce11bc4c4368ae7e7fabf9c86e81abe-general"
    ))


_WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,200}$")
_ALLOWED_ACTIONS = {"created", "prompted"}

_CONTROL_EVENT_TYPES = {
    "Issue",
    "IssueRelation",
    "PermissionChange",
    "AppUserNotification",
    "OAuthApp",
}
_CONTEXT_EVENT_TYPES = {
    "Comment",
    "IssueLabel",
    "Project",
    "ProjectUpdate",
    "ProjectLabel",
    # Linear's UI/docs use human labels while payload model names can vary
    # across webhook generations. Accept canonical names and known aliases.
    "Attachment",
    "IssueAttachment",
    "Reaction",
    "CommentReaction",
    "EmojiReaction",
}
_DATA_EVENT_TYPES = _CONTROL_EVENT_TYPES | _CONTEXT_EVENT_TYPES
_LINEAR_HOME_CHANNEL_NOTICE_PREFIX = "📬 No home channel is set for Linear."
_LINEAR_LONG_RUNNING_HEARTBEAT_RE = re.compile(
    r"^⏳ Working — [0-9]+ min(?: — (?:"
    r"iteration [0-9]+/[1-9][0-9]*(?:, (?:"
    r"[A-Za-z][A-Za-z0-9_.:-]{0,127}|receiving stream response|"
    r"waiting for non-streaming API response))?"
    r"|[A-Za-z][A-Za-z0-9_.:-]{0,127}))?$"
)
_LINEAR_CORE_BUDGET_NOTICE_RE = re.compile(
    r"^⚠️ Iteration budget exhausted \([0-9]+/[0-9]+\) — asking model to summarise$"
)
_LINEAR_GATEWAY_RESTART_NOTICE = (
    "⏳ Gateway is restarting and is not accepting new work right now."
)
_LINEAR_RECOVERED_GATEWAY_RESTART_NOTICE = (
    "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"
    + _LINEAR_GATEWAY_RESTART_NOTICE
)
_LINEAR_EPHEMERAL_NOTICES = frozenset(
    {
        _LINEAR_GATEWAY_RESTART_NOTICE,
        _LINEAR_RECOVERED_GATEWAY_RESTART_NOTICE,
    }
)
_OPEN_AGENT_SESSION_STATUSES = frozenset({"pending", "active", "awaitingInput"})
# These are the only native slash commands that may answer a core-owned model
# execution wait.  They still go through the normal core command/auth seam; the
# adapter merely prevents its own turn-state fence from treating the command as
# another model turn.
_NATIVE_APPROVAL_COMMANDS = frozenset({"approve", "deny"})
_CHANNEL_ROUTE_BATCH_SIZE = 10
_CHANNEL_ROUTE_MAX_ATTEMPTS = 5
_CHANNEL_ROUTE_POLL_SECONDS = 1.0
_PROGRESS_TURN_STATE_LIMIT = 256
_TURN_DECISION_BATCH_SIZE = 50
_TURN_ADMISSION_MAX_ATTEMPTS = 3
_STAGED_DELIVERY_MAX_ATTEMPTS = 3
_DEFAULT_GOAL_BUDGET_ROLLOVERS = 3
_ACCEPTANCE_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$")
_ACCEPTANCE_H2_NAMES = {"acceptance", "acceptance criteria", "kabul kriterleri"}
_COMMONMARK = MarkdownIt("commonmark")

_CONTINUATION_REASON_CODES = frozenset(
    {
        "approval",
        "awaiting_input",
        "blocked",
        "decision_mismatch",
        "hermes_decision_mismatch",
        "hermes_session_mismatch",
        "hermes_session_unavailable",
        "issue_binding_missing",
        "issue_mismatch",
        "issue_missing",
        "session_mismatch",
        "stopped",
        "strict_session_mismatch",
        "restart_orphan_success",
        "turn_failed",
        "session_error",
        "error_exit_reason",
        "turn_result_invalid",
        "session_context_mismatch",
        "actor_mismatch",
        "issue_state_invalid",
        "open_blockers",
        "status_invalid",
        "incomplete_exit_reason",
        "unverified",
        "native_goal_not_rejudged",
        "native_goal_paused",
        "native_goal_paused_without_question",
        "late_clarify_unverified",
    }
)

_SILENT_CONTROL_REASONS = frozenset({
    "stopped", "native_goal_paused", "native_goal_not_rejudged",
    "native_goal_paused_without_question", "late_clarify_unverified",
})


def _normalize_terminal_reason_code(value: Any) -> str:
    code = str(value or "").strip()
    return code if code in _CONTINUATION_REASON_CODES else "unverified"


def _read_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


def _read_webhook_credentials(path: str) -> dict[str, str]:
    credentials = _read_env_file(path) if path else {}
    for name in ("LINEAR_WEBHOOK_SECRET", "LINEAR_WEBHOOK_SECRET_PREVIOUS"):
        value = os.environ.get(name)
        if value:
            credentials[name] = value
    return credentials


def _payload_timestamp_seconds(payload: dict[str, Any]) -> float | None:
    raw = payload.get("webhookTimestamp")
    if not isinstance(raw, bool) and raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value > 10_000_000_000:
            value /= 1000.0
        return value
    created_at = payload.get("createdAt")
    if isinstance(created_at, str) and created_at:
        try:
            return dt.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _iso_timestamp(value: str) -> float:
    if not value:
        raise ValueError("missing timestamp")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _organization_id(payload: dict[str, Any]) -> str | None:
    direct = payload.get("organizationId")
    if direct:
        return str(direct)
    organization = payload.get("organization")
    if isinstance(organization, dict) and organization.get("id"):
        return str(organization["id"])
    return None


def _actor(payload: dict[str, Any]) -> tuple[str, str]:
    actor = payload.get("actor")
    if not isinstance(actor, dict):
        actor = payload.get("user")
    if not isinstance(actor, dict):
        actor = {}
    # Missing webhook provenance is not an identity. Do not collapse it into
    # a shared placeholder that can equal another missing identity.
    actor_id = str(actor.get("id") or "")
    actor_name = str(actor.get("name") or actor.get("displayName") or "Linear user")
    return actor_id, actor_name


def _activity_signal(payload: dict[str, Any]) -> str:
    activity = payload.get("agentActivity")
    if not isinstance(activity, dict):
        return ""
    return str(activity.get("signal") or "").strip().lower()


def _activity_body(payload: dict[str, Any]) -> str:
    """Read the structured Linear activity used to form the native prompt."""
    activity = payload.get("agentActivity")
    if not isinstance(activity, dict):
        return ""
    body = activity.get("body")
    if body is None:
        content = activity.get("content")
        if isinstance(content, dict):
            body = content.get("body")
    return str(body or "")


def _native_approval_command(event: MessageEvent) -> bool:
    """Return true only for the exact native approval command names."""
    if not bool(getattr(event, "allow_gateway_control", False)):
        return False
    text = str(getattr(event, "text", "") or "").lstrip()
    if not text.startswith("/"):
        return False
    command = text[1:].split(None, 1)[0].split("@", 1)[0].casefold()
    return command in _NATIVE_APPROVAL_COMMANDS


def _delivery_key(payload: dict[str, Any], raw: bytes) -> str:
    """Return an event-level dedup key; Linear webhookId identifies the subscription."""
    action = str(payload.get("action") or "")
    session = payload.get("agentSession")
    session_id = str(session.get("id") or "") if isinstance(session, dict) else ""
    activity = payload.get("agentActivity")
    activity_id = str(activity.get("id") or "") if isinstance(activity, dict) else ""
    if action == "created" and session_id:
        material = f"created\0{session_id}".encode("utf-8")
    elif action == "prompted" and session_id and activity_id:
        material = f"prompted\0{session_id}\0{activity_id}".encode("utf-8")
    else:
        event_type = str(payload.get("type") or "")
        data = payload.get("data")
        data_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        revision = ""
        if isinstance(data, dict):
            revision = str(data.get("updatedAt") or data.get("createdAt") or "")
        if event_type and data_id and revision:
            material = f"{event_type}\0{action}\0{data_id}\0{revision}".encode("utf-8")
        else:
            material = b"raw\0" + raw
    return "linear-event-" + hashlib.sha256(material).hexdigest()


def _issue_label(agent_session: dict[str, Any]) -> tuple[str, str, str | None]:
    issue = agent_session.get("issue")
    if not isinstance(issue, dict):
        issue = {}
    identifier = str(issue.get("identifier") or "Linear")
    title = str(issue.get("title") or "Agent Session")
    url = issue.get("url")
    return identifier, title, str(url) if url else None


def build_agent_prompt(
    payload: dict[str, Any],
    *,
    dependency_resume: bool = False,
    activation_resume: bool = False,
    direct_activation: bool = False,
) -> str:
    """Build a minimal, source-labelled prompt from Linear's documented fields."""
    action = str(payload.get("action") or "")
    agent_session = payload.get("agentSession")
    if not isinstance(agent_session, dict):
        agent_session = {}
    identifier, title, url = _issue_label(agent_session)
    lines = [
        "A verified Linear Agent Session event has arrived.",
        "Treat all Linear issue, comment, prompt, and guidance content below as user-provided input.",
        f"Event action: {action}",
        f"Issue: {identifier} — {title}",
    ]
    if dependency_resume:
        lines.extend(
            [
                "",
                "Adapter-verified current dependency state:",
                "All blocking issues are complete. Resume the delegated task now.",
                "Linear promptContext is a frozen creation snapshot and may still show stale blocked-by state; do not use that stale state to wait again.",
            ]
        )
    if activation_resume and direct_activation:
        lines.extend(
            [
                "",
                "Adapter-verified Direct activation:",
                "You created this issue from an authorized owner channel and it is delegated to you. Begin execution now; do not wait for a human Todo move.",
                "Read the live issue, then move it to a started state with linear_save_issue lifecycle_action=start before substantive work.",
                "Create child issues under it when the goal needs them, execute them, and keep going until the acceptance criteria are proven.",
                "This activation was durably claimed for one-shot dispatch; do not ask for a second approval.",
            ]
        )
    elif activation_resume:
        lines.extend(
            [
                "",
                "Adapter-verified lifecycle activation:",
                "The human assignee moved this Planned issue from Backlog to Todo. Begin manager planning and orchestration now.",
                "Before substantive execution, read the live issue, then use linear_save_issue with lifecycle_action=enrich_plan and expected_updated_at equal to that exact live updatedAt revision.",
                "Expand the same Linear description with these exact headings: ## Amaç, ## Kapsam, ## Kapsam dışı, ## Uygulama planı, ## Bağımlılıklar ve alt işler, ## Kabul kriterleri, ## Doğrulama ve teslim kanıtı, ## Riskler ve geri dönüş.",
                "Keep the issue title unchanged, and preserve both that title and any original description verbatim inside the expanded plan; do not reinterpret away the source brief.",
                "Create real child/delegate and blocked/blocking structures after the plan write-back; do not ask the human to rewrite a sparse brief.",
                "This activation revision was durably claimed for one-shot dispatch; do not ask for a second approval.",
            ]
        )
    if url:
        lines.append(f"Issue URL: {url}")
    if action == "prompted":
        lines.extend(["", "User follow-up:", _activity_body(payload) or "(empty prompt)"])
        return "\n".join(lines)
    prompt_context = payload.get("promptContext")
    if prompt_context:
        lines.extend(["", "Linear promptContext:", str(prompt_context)])
        return "\n".join(lines)
    issue = agent_session.get("issue")
    if isinstance(issue, dict) and issue.get("description"):
        lines.extend(["", "Issue description:", str(issue["description"])])
    else:
        lines.extend(
            [
                "",
                "No directive text was included with this delegation. Acknowledge receipt and ask for a concrete task; do not invent work.",
            ]
        )
    if action == "created" and not activation_resume:
        lines.extend(
            [
                "",
                "Lifecycle: if you created this issue, move it to a started state with linear_save_issue lifecycle_action=start before substantive work. A human-created issue starts only from Todo, and only its human owner moves it to Done.",
            ]
        )
    return "\n".join(lines)


class _PreAuthLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = max(1, int(limit_per_minute))
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True


class LinearPlatformAdapter(BasePlatformAdapter):
    """Hermes platform whose inbound and outbound surface is a Linear Agent Session."""

    supports_code_blocks = True
    supports_async_delivery = True
    supports_response_streaming = False
    splits_long_messages = False
    SUPPORTS_MESSAGE_EDITING = False
    SUPPORTS_TRANSIENT_PROGRESS = True

    def __init__(self, config: PlatformConfig, platform: Platform) -> None:
        super().__init__(config, platform)
        extra = config.extra
        self.host = str(extra.get("host") or "127.0.0.1")
        self.port = int(extra.get("port") or 8787)
        self.webhook_path = str(extra.get("webhook_path") or "/linear/webhook")
        if not self.webhook_path.startswith("/"):
            self.webhook_path = "/" + self.webhook_path
        self.max_body_bytes = int(extra.get("max_body_bytes") or 262144)
        self.replay_window_seconds = int(extra.get("replay_window_seconds") or 60)
        self.credential_env_file = str(extra.get("credential_env_file") or "")
        self.oauth_file = str(extra.get("oauth_file") or "")
        self.database_path = str(extra.get("database_path") or "")
        self._processing_timeout = int(extra.get("processing_timeout_seconds") or 300)
        self._retention = int(extra.get("dedup_retention_seconds") or 604800)
        self._outbox_poll_seconds = float(extra.get("outbox_poll_seconds") or 1.0)
        self._outbox_base_delay = float(extra.get("outbox_base_delay_seconds") or 2.0)
        self._outbox_max_delay = float(extra.get("outbox_max_delay_seconds") or 300.0)
        configured_rollovers = extra.get(
            "goal_max_budget_rollovers", _DEFAULT_GOAL_BUDGET_ROLLOVERS
        )
        self._goal_max_budget_rollovers = (
            configured_rollovers
            if type(configured_rollovers) is int
            and 0 <= configured_rollovers <= _DEFAULT_GOAL_BUDGET_ROLLOVERS
            else _DEFAULT_GOAL_BUDGET_ROLLOVERS
        )
        self._status_writeback_enabled = extra.get("issue_status_writeback_enabled") is True
        self._data_change_events_enabled = extra.get("data_change_events_enabled") is True
        self._planned_activation_enabled = extra.get("planned_activation_enabled") is True
        allowed_activation_teams = extra.get("activation_allowed_team_ids")
        if not isinstance(allowed_activation_teams, list):
            allowed_activation_teams = []
        self._activation_allowed_team_ids = {
            str(team_id) for team_id in allowed_activation_teams
            if isinstance(team_id, str) and team_id
        }
        planned_owner_ids = extra.get("planned_owner_ids")
        if not isinstance(planned_owner_ids, list):
            planned_owner_ids = []
        self._planned_owner_ids = {
            str(user_id) for user_id in planned_owner_ids
            if isinstance(user_id, str) and user_id
        }
        self._dependency_poll_seconds = max(5.0, float(extra.get("dependency_poll_seconds") or 60.0))
        self._closure_reconciliation_enabled = extra.get("closure_reconciliation_enabled") is True
        allowed_closure_teams = extra.get("closure_allowed_team_ids")
        if not isinstance(allowed_closure_teams, list):
            allowed_closure_teams = []
        self._closure_allowed_team_ids = {
            str(team_id) for team_id in allowed_closure_teams
            if isinstance(team_id, str) and team_id
        }
        configured_states = extra.get("issue_status_mapping") or {}
        self._status_mapping = {
            "queued": str(configured_states.get("queued") or "Todo"),
            "running": str(configured_states.get("running") or "In Progress"),
            "blocked": str(configured_states.get("blocked") or "Blocked"),
        }
        self._status_ranks = {"queued": 10, "blocked": 15, "running": 20}
        self._invalid_signature_limiter = _PreAuthLimiter(
            int(extra.get("preauth_rate_limit_per_minute") or 120)
        )
        self._signing_secrets: tuple[str, ...] = ()
        self._ledger: DeliveryLedger | None = None
        self._linear: LinearClient | None = None
        self._last_connect_error: LinearAPIError | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ack_tasks: dict[str, set[asyncio.Task]] = {}
        self._tool_progress_tasks: set[Any] = set()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._accepting_tool_progress = False
        self._terminal_progress_callback: Callable[..., None] | None = None
        self._progress_transition_lock = threading.RLock()
        self._progress_state_lock = threading.Lock()
        self._progress_turns: dict[str, tuple[str, bool]] = {}
        self._channel_route_task: asyncio.Task | None = None
        self._channel_route_notice_tasks: set[asyncio.Task] = set()
        self._channel_route_wakeup = asyncio.Event()
        self._outbox_task: asyncio.Task | None = None
        self._dependency_task: asyncio.Task | None = None
        self._active_turn_events: dict[str, MessageEvent] = {}
        self._oauth_revoked = False
        self._outbox_wakeup = asyncio.Event()
        self._outbox_drain_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._issue_locks: dict[str, asyncio.Lock] = {}
        self._inflight_session_deliveries: dict[str, set[str]] = {}
        # A refused claim must not retarget the next local turn on retry.
        self._stop_delivery_owners: dict[str, tuple[float, tuple[Any, ...]]] = {}
        self.config.typing_indicator = False

    @property
    def authorization_is_upstream(self) -> bool:
        """Linear signs events and organization identity is pinned to the OAuth installation."""
        return True

    @staticmethod
    def check_requirements() -> bool:
        try:
            import aiohttp  # noqa: F401
            import markdown_it  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def validate_config(config: PlatformConfig) -> bool:
        extra = config.extra
        required = ("oauth_file", "database_path")
        boolean_keys = (
            "issue_status_writeback_enabled",
            "data_change_events_enabled",
            "dependency_wait_enabled",
            "planned_activation_enabled",
            "closure_reconciliation_enabled",
            "native_goal_continuation_enabled",
        )
        if any(key in extra and type(extra[key]) is not bool for key in boolean_keys):
            return False
        configured_rollovers = extra.get(
            "goal_max_budget_rollovers", _DEFAULT_GOAL_BUDGET_ROLLOVERS
        )
        if not (
            type(configured_rollovers) is int
            and 0 <= configured_rollovers <= _DEFAULT_GOAL_BUDGET_ROLLOVERS
        ):
            return False
        allowed = extra.get("closure_allowed_team_ids", [])
        if not (
            isinstance(allowed, list)
            and all(isinstance(team_id, str) and team_id for team_id in allowed)
        ):
            return False
        activation_allowed = extra.get("activation_allowed_team_ids", [])
        planned_owner_ids = extra.get("planned_owner_ids", [])
        if not (
            isinstance(activation_allowed, list)
            and all(isinstance(team_id, str) and team_id for team_id in activation_allowed)
            and isinstance(planned_owner_ids, list)
            and all(isinstance(user_id, str) and user_id for user_id in planned_owner_ids)
        ):
            return False
        if extra.get("planned_activation_enabled") is True and not (
            extra.get("data_change_events_enabled") is True
            and activation_allowed
            and planned_owner_ids
        ):
            return False
        has_secret_source = bool(
            extra.get("credential_env_file") or os.environ.get("LINEAR_WEBHOOK_SECRET")
        )
        if extra.get("closure_reconciliation_enabled") is True:
            closure_safe = bool(
                extra.get("data_change_events_enabled") is True
                and extra.get("issue_status_writeback_enabled") is not True
                and isinstance(allowed, list)
                and allowed
                and all(isinstance(team_id, str) and team_id for team_id in allowed)
            )
            if not closure_safe:
                return False
        return all(bool(extra.get(key)) for key in required) and has_secret_source

    @classmethod
    def from_config(cls, config: PlatformConfig) -> "LinearPlatformAdapter":
        return cls(config, Platform("linear"))

    async def connect_outbound_only(self, *, startup_recovery: bool = False) -> bool:
        """Open the durable outbox and OAuth client without binding the webhook port."""
        self._event_loop = asyncio.get_running_loop()
        self._last_connect_error = None
        try:
            if self._ledger is None:
                self._ledger = DeliveryLedger(
                    self.database_path,
                    processing_timeout_seconds=self._processing_timeout,
                    retention_seconds=self._retention,
                    outbox_claim_timeout_seconds=max(30, int(self._outbox_max_delay)),
                    startup_recovery=startup_recovery,
                    continuation_state_enabled=False,
                )
            if self._linear is None:
                self._linear = LinearClient(self.oauth_file)
                await self._linear.connect()
            return True
        except Exception as exc:
            if isinstance(exc, LinearAPIError):
                self._last_connect_error = exc
            logger.error("[linear] Outbound-only adapter failed to connect: %s", exc, exc_info=True)
            await self._cleanup()
            return False

    @property
    def last_connect_error(self) -> LinearAPIError | None:
        """Return safe retry metadata for the most recent outbound connection failure."""
        return self._last_connect_error

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        self._event_loop = asyncio.get_running_loop()
        try:
            credentials = _read_webhook_credentials(self.credential_env_file)
            current_secret = credentials.get("LINEAR_WEBHOOK_SECRET", "")
            previous_secret = credentials.get("LINEAR_WEBHOOK_SECRET_PREVIOUS", "")
            if len(current_secret) < 16:
                raise RuntimeError("LINEAR_WEBHOOK_SECRET is missing or too short")
            self._signing_secrets = tuple(
                secret for secret in (current_secret, previous_secret) if len(secret) >= 16
            )
            if not await self.connect_outbound_only(startup_recovery=True):
                raise RuntimeError("Linear outbound dependencies failed to connect")
            assert self._linear is not None
            app = web.Application(client_max_size=self.max_body_bytes)
            app.router.add_get("/", self._health)
            app.router.add_get("/health", self._health)
            app.router.add_post(self.webhook_path, self._handle_webhook)
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
            self._running = True
            self._accepting_tool_progress = True
            self._outbox_task = asyncio.create_task(self._outbox_loop())
            self._channel_route_task = asyncio.create_task(self._channel_route_loop())
            # Direct activation recovery is a core durable lifecycle, independent
            # of the optional dependency-wait and planned-activation features.
            self._dependency_task = asyncio.create_task(self._dependency_loop())
            logger.info(
                "[linear] Native adapter listening on %s:%d%s actor=%s organization=%s",
                self.host,
                self.port,
                self.webhook_path,
                self._linear.actor_name,
                self._linear.organization_name,
            )
            return True
        except Exception as exc:
            logger.error("[linear] Native adapter failed to connect: %s", exc, exc_info=True)
            await self._cleanup()
            return False

    async def disconnect(self) -> None:
        self._accepting_tool_progress = False
        self._running = False
        self._channel_route_wakeup.set()
        if self._channel_route_task is not None:
            self._channel_route_task.cancel()
            await asyncio.gather(self._channel_route_task, return_exceptions=True)
            self._channel_route_task = None
        route_tasks = list(self._channel_route_notice_tasks)
        for task in route_tasks:
            task.cancel()
        if route_tasks:
            await asyncio.gather(*route_tasks, return_exceptions=True)
        self._channel_route_notice_tasks.clear()
        if self._dependency_task is not None:
            self._dependency_task.cancel()
            await asyncio.gather(self._dependency_task, return_exceptions=True)
            self._dependency_task = None
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None
        tasks = [task for values in self._ack_tasks.values() for task in values]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ack_tasks.clear()
        progress_task_set = getattr(self, "_tool_progress_tasks", None)
        progress_tasks = list(progress_task_set or ())
        for task in progress_tasks:
            task.cancel()
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        if progress_task_set is not None:
            progress_task_set.clear()
        await self._cleanup()

    def schedule_tool_progress(
        self,
        chat_id: str,
        content: str,
        *,
        turn_key: str,
        progress_kind: str = "tool",
    ) -> asyncio.Task:
        """Own transient progress delivery inside the adapter lifecycle."""
        loop = self._event_loop
        if not self._accepting_tool_progress or loop is None or loop.is_closed():
            raise RuntimeError("Linear adapter event loop is unavailable")

        def done(completed: Any) -> None:
            self._tool_progress_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                result = completed.result()
            except Exception:
                logger.warning("[linear] Tool progress activity failed", exc_info=True)
                return
            if not result.success:
                logger.warning(
                    "[linear] Tool progress activity rejected retryable=%s error=%s",
                    result.retryable,
                    result.error or "unknown",
                )

        def start() -> asyncio.Task:
            if not self._accepting_tool_progress:
                raise RuntimeError("Linear adapter is shutting down")
            task = loop.create_task(
                self.send(
                    chat_id,
                    content,
                    metadata={
                        "transient_progress": True,
                        "transient_progress_key": turn_key,
                        "transient_progress_kind": progress_kind,
                    },
                )
            )
            self._tool_progress_tasks.add(task)
            task.add_done_callback(done)
            return task

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            return start()

        handshake: concurrent.futures.Future[asyncio.Task] = concurrent.futures.Future()
        handshake_lock = threading.Lock()
        handshake_timed_out = False

        def start_from_loop() -> None:
            nonlocal handshake_timed_out
            with handshake_lock:
                if handshake_timed_out:
                    return
                try:
                    handshake.set_result(start())
                except Exception as exc:
                    handshake.set_exception(exc)

        loop.call_soon_threadsafe(start_from_loop)
        try:
            return handshake.result(timeout=1.0)
        except concurrent.futures.TimeoutError as exc:
            with handshake_lock:
                if handshake.done():
                    return handshake.result()
                handshake_timed_out = True
            raise RuntimeError("Linear tool progress task could not be scheduled") from exc
        except Exception as exc:
            raise RuntimeError("Linear tool progress task could not be scheduled") from exc

    def open_progress_turn(self, chat_id: str, turn_key: str) -> None:
        """Open progress only when a trusted pre-tool hook proves a new turn."""
        with self._progress_transition_lock:
            ledger = self._ledger
            allowed = bool(ledger and ledger.open_progress_turn(chat_id, turn_key))
            with self._progress_state_lock:
                self._progress_turns[chat_id] = (turn_key, not allowed)
                while len(self._progress_turns) > _PROGRESS_TURN_STATE_LIMIT:
                    self._progress_turns.pop(next(iter(self._progress_turns)))

    def _progress_is_allowed(self, chat_id: str, turn_key: str) -> bool:
        with self._progress_state_lock:
            current = self._progress_turns.get(chat_id)
            if current is not None:
                return current[0] == turn_key and current[1] is False
        ledger = self._ledger
        return bool(ledger and ledger.progress_is_allowed(chat_id, turn_key))

    def _progress_chat_is_allowed(self, chat_id: str) -> bool:
        """Allow unkeyed heartbeat only before this chat's current turn is fenced."""
        with self._progress_state_lock:
            current = self._progress_turns.get(chat_id)
            if current is not None:
                return current[1] is False
        ledger = self._ledger
        return bool(ledger and ledger.progress_is_allowed(chat_id))

    def _current_progress_turn_key(self, chat_id: str) -> str:
        with self._progress_state_lock:
            current = self._progress_turns.get(chat_id)
            if current is not None:
                return current[0]
        ledger = self._ledger
        return ledger.current_progress_turn_key(chat_id) if ledger is not None else ""

    async def _cleanup(self) -> None:
        self._accepting_tool_progress = False
        self._active_turn_events.clear()
        if self._channel_route_task is not None:
            self._channel_route_task.cancel()
            await asyncio.gather(self._channel_route_task, return_exceptions=True)
            self._channel_route_task = None
        if self._dependency_task is not None:
            self._dependency_task.cancel()
            await asyncio.gather(self._dependency_task, return_exceptions=True)
            self._dependency_task = None
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        if self._linear is not None:
            await self._linear.close()
            self._linear = None
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None
        self._event_loop = None

    async def _health(self, request: web.Request) -> web.Response:
        del request
        healthy = bool(
            self._running
            and self._linear is not None
            and self._linear.organization_id
            and self._ledger is not None
        )
        outbox = self._ledger.outbox_counts() if self._ledger is not None else {}
        waiting = self._ledger.waiting_counts() if self._ledger is not None else {}
        activations = self._ledger.activation_counts() if self._ledger is not None else {}
        manager_activations = (
            self._ledger.manager_activation_counts() if self._ledger is not None else {}
        )
        direct_activations = (
            self._ledger.direct_activation_counts() if self._ledger is not None else {}
        )
        closures = self._ledger.closure_counts() if self._ledger is not None else {}
        channel_routes = (
            self._ledger.channel_route_counts() if self._ledger is not None else {}
        )
        degraded = (
            int(outbox.get("dead", 0))
            or int(waiting.get("failed", 0))
            or int(activations.get("failed", 0))
            or int(activations.get("dispatch_unknown", 0))
            or int(manager_activations.get("failed", 0))
            or int(manager_activations.get("delegation_unknown", 0))
            or int(manager_activations.get("dispatch_unknown", 0))
            or int(direct_activations.get("stuck_active", 0))
            or int(direct_activations.get("stuck_events", 0))
            or int(direct_activations.get("dispatch_unknown", 0))
            or int(direct_activations.get("failed", 0))
            or int(closures.get("failed", 0))
            or int(closures.get("blocked_dispatch", 0))
            or int(channel_routes.get("failed", 0))
            or int(channel_routes.get("ambiguous", 0))
            or self._oauth_revoked
        )
        status = "degraded" if healthy and degraded else ("ok" if healthy else "starting")
        return web.json_response(
            {
                "status": status,
                "adapter": "linear-native",
                "version": "0.8.68",
                "features": {
                    "data_change_events": self._data_change_events_enabled,
                    "data_event_types": sorted(_DATA_EVENT_TYPES),
                    "planned_activation": self._planned_activation_enabled,
                    "status_writeback": self._status_writeback_enabled,
                    "closure_reconciliation": self._closure_reconciliation_enabled,
                },
                "outbox": outbox,
                "waiting": waiting,
                "activations": activations,
                "manager_activations": manager_activations,
                "direct_activations": direct_activations,
                "closures": closures,
                "channel_routes": channel_routes,
                "oauth_revoked": self._oauth_revoked,
            },
            status=200 if healthy else 503,
        )

    def _duplicate_delivery_response(self, delivery_key: str) -> web.Response:
        # A processing claim is not durable admission: keep vendor retries alive.
        done = self._ledger is not None and self._ledger.delivery_is_done(delivery_key)
        if done:
            self._repair_acceptance_thoughts(delivery_key)
        return web.json_response(
            {"status": "duplicate" if done else "processing"}, status=200 if done else 503
        )

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        read_deadline = asyncio.get_running_loop().time() + 4.0
        raw = await request.read()
        signature = request.headers.get("Linear-Signature", "").strip().lower()
        logger.info(
            "[linear] webhook received bytes=%d signature_present=%s",
            len(raw),
            bool(signature),
        )
        signature_valid = bool(signature) and any(
            hmac.compare_digest(
                signature,
                hmac.new(secret.encode("utf-8"), raw, "sha256").hexdigest(),
            )
            for secret in self._signing_secrets
        )
        if not signature_valid:
            if not self._invalid_signature_limiter.allow():
                logger.warning("[linear] webhook rejected reason=invalid_signature status=429")
                return web.json_response({"status": "rate_limited"}, status=429)
            logger.warning("[linear] webhook rejected reason=invalid_signature status=401")
            return web.json_response({"status": "unauthorized"}, status=401)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response({"status": "bad_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"status": "bad_payload"}, status=400)
        timestamp = _payload_timestamp_seconds(payload)
        if timestamp is None or abs(time.time() - timestamp) > self.replay_window_seconds:
            logger.warning("[linear] webhook rejected reason=stale status=401")
            return web.json_response({"status": "stale"}, status=401)
        if self._linear is None or self._ledger is None:
            return web.json_response({"status": "unavailable"}, status=503)
        incoming_org = _organization_id(payload)
        if not incoming_org or not hmac.compare_digest(incoming_org, self._linear.organization_id or ""):
            logger.warning("[linear] Rejected validly signed event for a non-installed organization")
            return web.json_response({"status": "forbidden"}, status=403)
        webhook_id = str(payload.get("webhookId") or "")
        if not webhook_id and payload.get("type") == "AppUserNotification":
            webhook_id = "notification-" + hashlib.sha256(raw).hexdigest()[:32]
        if not _WEBHOOK_ID_RE.fullmatch(webhook_id):
            return web.json_response({"status": "bad_webhook_id"}, status=400)
        event_type = str(payload.get("type") or "")
        if event_type != "AgentSessionEvent":
            if self._data_change_events_enabled and event_type in _DATA_EVENT_TYPES:
                return await self._handle_data_event(payload, raw, webhook_id)
            return web.json_response({"status": "ignored"}, status=200)
        action = str(payload.get("action") or "")
        if action not in _ALLOWED_ACTIONS:
            return web.json_response({"status": "ignored"}, status=200)
        agent_session = payload.get("agentSession")
        if not isinstance(agent_session, dict) or not agent_session.get("id"):
            return web.json_response({"status": "missing_agent_session"}, status=400)
        agent_session_id = str(agent_session["id"])
        agent_session_status = str(agent_session.get("status") or "")
        issue = agent_session.get("issue")
        issue_id = str(issue.get("id") or "") if isinstance(issue, dict) else ""
        signal = _activity_signal(payload)
        is_stop = action == "prompted" and signal == "stop"
        delivery_key = _delivery_key(payload, raw)
        if is_stop:
            # Only signed, replay-fresh, organization-matched events reach here.
            now = time.monotonic()
            for key, (seen_at, _) in list(self._stop_delivery_owners.items()):
                if now - seen_at > self.replay_window_seconds:
                    self._stop_delivery_owners.pop(key, None)
            self._stop_delivery_owners.setdefault(
                delivery_key, (now, self._linear_processing_owner(agent_session_id)[2:]),
            )
        claimed = False
        direct_activation_created = False
        inflight: set[str] | None = None
        direct_dispatch_attempted = False
        direct_issue_lock: asyncio.Lock | None = None
        direct_issue_lock_held = False
        dispatch_lock: asyncio.Lock | None = None
        dispatch_lock_held = False
        try:
            if not self._ledger.claim(delivery_key):
                logger.info(
                    "[linear] duplicate subscription_id=%s delivery_key=%s",
                    webhook_id,
                    delivery_key,
                )
                return self._duplicate_delivery_response(delivery_key)
            claimed = True
            inflight = self._inflight_session_deliveries.setdefault(agent_session_id, set())
            inflight.add(delivery_key)
            event_actor_id, _ = _actor(payload)
            manager_activation = (
                self._ledger.get_manager_activation(issue_id)
                if action == "created" and issue_id
                else None
            )
            planned_intake_created = bool(
                manager_activation
                and manager_activation.get("state") == "delegated"
            )
            if action == "created" and issue_id:
                direct_grant = self._ledger.get_direct_activation_grant(issue_id)
                if direct_grant is not None:
                    if direct_grant.get("state") == "dispatched":
                        if hmac.compare_digest(
                            str(direct_grant.get("session_id") or ""), agent_session_id
                        ):
                            self._ledger.mark_done(delivery_key)
                            return web.json_response(
                                {"status": "direct_activation_duplicate"}, status=200
                            )
                        direct_grant = None
                # Self-delegation (agentSessionCreateOnIssue/assignment by the app) arrives
                # with no actor; the signed webhook plus creator/delegate checks carry identity.
                self_or_unset_actor = bool(
                    self._linear.actor_id
                    and (
                        not event_actor_id
                        or hmac.compare_digest(event_actor_id, self._linear.actor_id)
                    )
                )
                if direct_grant is not None:
                    if not self_or_unset_actor:
                        logger.info("[linear] direct created denied: foreign actor issue=%s", issue_id)
                        # A foreign event must neither claim the Direct grant nor
                        # poison semantic dedup for the self-authored delivery of
                        # this same native session that may arrive afterward.
                        self._ledger.release(delivery_key)
                        claimed = False
                        return web.json_response(
                            {"status": "direct_activation_policy_denied"}, status=200
                        )
                    async with asyncio.timeout_at(read_deadline):
                        context = await self._linear.get_issue_closure_context(issue_id)
                    _check_admission_deadline(read_deadline)
                    team_id = str((context.get("team") or {}).get("id") or "")
                    direct_authoritative = bool(
                        self._direct_activation_policy_allows(context, direct_grant)
                        and self_or_unset_actor
                    )
                    if not direct_authoritative or not self._ledger.claim_direct_activation(
                        issue_id,
                        agent_session_id,
                        actor_id=str(self._linear.actor_id or ""),
                        team_id=team_id,
                    ):
                        logger.info(
                            "[linear] direct created denied: policy_or_claim issue=%s state=%s",
                            issue_id, direct_grant.get("state"),
                        )
                        self._ledger.mark_done(delivery_key)
                        return web.json_response(
                            {"status": "direct_activation_policy_denied"}, status=200
                        )
                    direct_activation_created = True
            if (
                action == "created"
                and issue_id
                and not direct_activation_created
                and manager_activation is None
                and self_or_unset_actor
            ):
                async with asyncio.timeout_at(read_deadline):
                    context = await self._linear.get_issue_closure_context(issue_id)
                _check_admission_deadline(read_deadline)
                team_id = str((context.get("team") or {}).get("id") or "")
                owner_id = str((context.get("assignee") or {}).get("id") or "")
                creator_id = str((context.get("creator") or {}).get("id") or "")
                delegate_id = str((context.get("delegate") or {}).get("id") or "")
                parent_id = str((context.get("parent") or {}).get("id") or "")
                if (
                    not parent_id
                    and team_id in self._activation_allowed_team_ids
                    and owner_id in self._planned_owner_ids
                    and hmac.compare_digest(creator_id, self._linear.actor_id)
                    and hmac.compare_digest(delegate_id, self._linear.actor_id)
                    and (
                        self._ledger.has_unbound_direct_reservation(
                            actor_id=self._linear.actor_id,
                            team_id=team_id,
                            issue_fingerprint=self._ledger.direct_issue_fingerprint(
                                team_id, str(context.get("title") or "")
                            ),
                        )
                        # The create tool may bind the grant while the context
                        # read above is in flight; its wake-up then found no event.
                        or (self._ledger.get_direct_activation_grant(issue_id) or {}).get("state")
                        == "granted"
                    )
                ):
                    async with _admission_lock(self._issue_lock(issue_id), read_deadline):
                        if not await self._bind_issue_session(issue_id, agent_session_id, read_deadline=read_deadline):
                            self._ledger.mark_done(delivery_key)
                            return web.json_response({"status": "issue_session_active"}, status=200)
                        self._ledger.put_direct_activation_event(
                            issue_id, agent_session_id, delivery_key, payload
                        )
                    self._ledger.mark_done(delivery_key)
                    self.schedule_direct_activation_reconcile(issue_id)  # no-op until bound
                    return web.json_response(
                        {"status": "direct_activation_waiting_for_grant"}, status=200
                    )
            manager_activation_state = str(
                (manager_activation or {}).get("state") or ""
            )
            manager_activation_session_id = str(
                (manager_activation or {}).get("session_id") or ""
            )
            if (
                action == "created"
                and issue_id
                and manager_activation is not None
                and manager_activation_state != "canceled"
                and (
                    manager_activation_state != "session_started"
                    or hmac.compare_digest(
                        manager_activation_session_id, agent_session_id
                    )
                )
            ):
                return await self._handle_manager_session_created(
                    payload,
                    delivery_key,
                    webhook_id,
                    issue_id,
                    agent_session_id,
                    read_deadline=read_deadline,
                )
            if (
                event_actor_id
                and self._linear.actor_id
                and hmac.compare_digest(event_actor_id, self._linear.actor_id)
                and not (planned_intake_created or direct_activation_created)
            ):
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "ignored_self"}, status=200)
            if is_stop:
                original = self._stop_delivery_owners[delivery_key][1]
                current = self._linear_processing_owner(agent_session_id)[2:]
                same_owner = (
                    (original[1] is not None or original[2] is not None)
                    and original[1] is current[1] and original[2] is current[2]
                )
                new_owner = (
                    current[1] is not None or current[2] is not None
                    or (current[0] is not None and current[0] is not original[0])
                )
                replaced = (
                    (original[0] is not None and current[0] is not original[0])
                    or (original[3] is not None and current[3] != original[3])
                ) if same_owner else new_owner and any(
                    left is not right for left, right in zip(original, current)
                )
                if replaced:
                    # A refused delivery must never fence or cancel its successor.
                    self._ledger.mark_done(delivery_key)
                    return web.json_response({"status": "stale_stop"}, status=200)
            if is_stop and (
                (issue_id and self._issue_lock(issue_id).locked())
                or self._session_lock(agent_session_id).locked()
                or self._outbox_drain_lock.locked()
            ):
                # Interrupt current work before slow reads release their locks.
                # Keep the locked durable fence AND second cancellation below:
                # another admission may finish while Stop waits for those locks.
                await self._cancel_linear_session_processing(agent_session_id)
            if action == "created" and issue_id:
                async with _admission_lock(self._issue_lock(issue_id), read_deadline):
                    pending_closure = self._ledger.get_pending_closure_event(issue_id)
                    if pending_closure is not None:
                        closure_status = await self._reconcile_human_completion(
                            pending_closure["event"],
                            issue_id,
                            _issue_locked=True,
                            _proposed_session_id=agent_session_id,
                        )
                        if closure_status in {"closure_queued", "closure_duplicate"}:
                            self._ledger.mark_done(delivery_key)
                            return web.json_response({"status": closure_status}, status=200)
                        if closure_status == "closure_obsolete":
                            self._ledger.clear_pending_closure_event(
                                issue_id, pending_closure["event_revision"]
                            )
                        else:
                            await self._bind_issue_session(issue_id, agent_session_id, read_deadline=read_deadline)
                            self._ledger.release(delivery_key)
                            claimed = False
                            return web.json_response(
                                {"status": "closure_deferred"}, status=503
                            )
                    if not await self._bind_issue_session(issue_id, agent_session_id, read_deadline=read_deadline):
                        if direct_activation_created:
                            self._ledger.reset_direct_activation_claim(
                                issue_id, agent_session_id, "issue_session_active"
                            )
                        self._ledger.mark_done(delivery_key)
                        return web.json_response({"status": "issue_session_active"}, status=200)
            if action == "prompted" and issue_id:
                async with self._issue_lock(issue_id):
                    if (not is_stop and self._ledger.get_issue_session(issue_id)
                            not in (None, agent_session_id)):
                        self._ledger.mark_done(delivery_key)
                        return web.json_response({"status": "issue_session_active"}, status=200)
                    pending_closure = self._ledger.get_pending_closure_event(issue_id)
                    if pending_closure is not None:
                        closure_status = await self._reconcile_human_completion(
                            pending_closure["event"], issue_id, _issue_locked=True
                        )
                        if closure_status in {
                            "terminal_fenced",
                            "closure_queued",
                            "closure_duplicate",
                        }:
                            self._ledger.mark_done(delivery_key)
                            return web.json_response({"status": closure_status}, status=200)
                        if closure_status == "closure_obsolete":
                            self._ledger.clear_pending_closure_event(
                                issue_id, pending_closure["event_revision"]
                            )
                        else:
                            self._ledger.release(delivery_key)
                            claimed = False
                            return web.json_response(
                                {"status": "closure_deferred"}, status=503
                            )
                    if is_stop:
                        self._ledger.cancel_wait(agent_session_id)
                        self._ledger.cancel_activation_for_session(agent_session_id)
                        self._ledger.cancel_direct_activation_for_session(agent_session_id)
                        if self._ledger.get_manager_activation(issue_id):
                            self._ledger.mark_manager_activation(issue_id, "canceled")
            if is_stop:
                if not issue_id:
                    self._ledger.cancel_wait(agent_session_id)
                    self._ledger.cancel_activation_for_session(agent_session_id)
                    self._ledger.cancel_direct_activation_for_session(agent_session_id)
            if (
                action == "created"
                and self._planned_activation_enabled
                and issue_id
                and not direct_activation_created
            ):
                async with _admission_lock(self._issue_lock(issue_id), read_deadline):
                    async with asyncio.timeout_at(read_deadline):
                        context = await self._linear.get_issue_closure_context(issue_id)
                    _check_admission_deadline(read_deadline)
                    state_type = str((context.get("state") or {}).get("type") or "").casefold()
                    if state_type == "backlog":
                        team_id = str((context.get("team") or {}).get("id") or "")
                        owner_id = str((context.get("assignee") or {}).get("id") or "")
                        delegate_id = str((context.get("delegate") or {}).get("id") or "")
                        creator_id = str((context.get("creator") or {}).get("id") or "")
                        parent_id = str((context.get("parent") or {}).get("id") or "")
                        agent_created_child = bool(
                            self._linear.actor_id
                            and creator_id
                            and parent_id
                            and hmac.compare_digest(creator_id, self._linear.actor_id)
                        )
                        if agent_created_child:
                            # Agent-created coordinator child: not planned human
                            # work. Close the native session immediately so a
                            # stale open session cannot block the MCP lifecycle
                            # (start/complete_child) transitions.
                            self._enqueue_activity(
                                agent_session_id,
                                "response",
                                "Creator-agent manages this child through the MCP lifecycle; native session closed.",
                                item_key=f"creator-owned:{delivery_key}",
                            )
                            self._ledger.mark_done(delivery_key)
                            return web.json_response(
                                {"status": "accepted"}, status=200
                            )
                        if not (
                            team_id in self._activation_allowed_team_ids
                            and owner_id in self._planned_owner_ids
                            and self._linear.actor_id
                            and hmac.compare_digest(delegate_id, self._linear.actor_id)
                        ):
                            self._ledger.mark_done(delivery_key)
                            return web.json_response(
                                {"status": "activation_policy_denied"}, status=200
                            )
                        self._ledger.put_activation_wait(
                            agent_session_id,
                            issue_id,
                            delivery_key,
                            payload,
                        )
                        self._enqueue_activity(
                            agent_session_id,
                            "thought",
                            "Planned work is parked in Backlog. Hermes will begin automatically after the human owner moves it to Todo.",
                            item_key=f"activation-wait:{delivery_key}",
                        )
                        self._ledger.mark_done(delivery_key)
                        return web.json_response(
                            {"status": "waiting_for_activation"}, status=200
                        )
            if direct_activation_created and issue_id and not direct_issue_lock_held:
                direct_issue_lock = self._issue_lock(issue_id)
                async with asyncio.timeout_at(read_deadline):
                    await direct_issue_lock.acquire()
                direct_issue_lock_held = True
                _check_admission_deadline(read_deadline)
                if not self._direct_activation_claim_is_current(
                    issue_id, agent_session_id
                ):
                    self._ledger.mark_done(delivery_key)
                    return web.json_response(
                        {"status": "direct_activation_canceled"}, status=200
                    )
            dispatch_lock = self._session_lock(agent_session_id)
            async with asyncio.timeout_at(read_deadline if action == "created" else None):
                await dispatch_lock.acquire()
            dispatch_lock_held = True
            if action == "created":
                # Readiness only: keep fresh outbox validation after admission.
                async with asyncio.timeout_at(read_deadline):
                    await self._validate_activity_target(agent_session_id)
                _check_admission_deadline(read_deadline)
            native_command = (
                action == "prompted"
                and _activity_body(payload).lstrip().startswith("/")
            )
            human_preemption = (
                action == "prompted"
                and not is_stop
                and not native_command
            )
            if (
                is_stop
                or human_preemption
                or (
                    not native_command
                    and (
                        agent_session_status == "awaitingInput"
                        or signal in {"awaitinginput", "awaiting_input", "approval", "blocked"}
                    )
                )
            ):
                if is_stop or human_preemption:
                    await self._cancel_linear_session_processing(agent_session_id)
            if not is_stop and self._ledger.has_session_closure(agent_session_id):
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "closure_reconciled"}, status=200)
            if (
                direct_activation_created
                and issue_id
                and not self._direct_activation_claim_is_current(
                    issue_id, agent_session_id
                )
            ):
                self._ledger.mark_done(delivery_key)
                return web.json_response(
                    {"status": "direct_activation_canceled"}, status=200
                )
            event = self._message_event(
                payload,
                delivery_key,
                webhook_id,
                activation_resume=planned_intake_created or direct_activation_created,
                direct_activation=direct_activation_created,
            )
            # Private provenance, never a field copied from webhook metadata.
            event._linear_verified_normal_prompt = human_preemption
            if native_command and not self._trusted_native_command_requester(event):
                # Never let the webhook adapter's synthetic source (or the core
                # webhook platform exemption) authorize a control command.
                # Without an exact active human binding, the safe capability is
                # no command dispatch.
                self._ledger.mark_done(delivery_key)
                return web.json_response(
                    {"status": "native_command_requester_unavailable"}, status=200
                )
            if action == "created" and not direct_activation_created:
                # Bound only a read before admission, never core dispatch or a
                # manager/Direct ambiguity-fenced claim. Consume it once so a
                # queued event's later pre-handler check gets a fresh read.
                event._linear_created_read_deadline = read_deadline
            if action == "created":
                _check_admission_deadline(read_deadline)
            if direct_activation_created:
                direct_dispatch_attempted = True
            await self.handle_message(event)
            if direct_activation_created and issue_id:
                if not self._ledger.mark_direct_activation_dispatched(
                    issue_id, agent_session_id
                ):
                    raise RuntimeError("direct activation dispatch state changed")
            elif planned_intake_created and issue_id:
                self._ledger.mark_manager_activation(
                    issue_id, "session_started", session_id=agent_session_id
                )
            acceptance_thought = None
            if not is_stop and getattr(event, "_gateway_accepted", False):
                acceptance_thought = {
                    "agent_session_id": agent_session_id,
                    "issue_id": issue_id,
                    "include_queued": action == "created",
                }
            self._ledger.mark_done(delivery_key, acceptance_thought=acceptance_thought)
            self._repair_acceptance_thoughts(delivery_key)
            logger.info(
                "[linear] accepted subscription_id=%s delivery_key=%s action=%s signal=%s agent_session_id=%s",
                webhook_id,
                delivery_key,
                action,
                signal or "none",
                agent_session_id,
            )
            return web.json_response({"status": "accepted"}, status=200)
        except Exception as exc:
            if claimed and direct_activation_created and issue_id:
                if direct_dispatch_attempted:
                    self._ledger.mark_direct_activation_unknown(
                        issue_id, agent_session_id, str(exc)
                    )
                else:
                    self._ledger.reset_direct_activation_claim(
                        issue_id, agent_session_id, str(exc)
                    )
            if claimed:
                try:
                    self._ledger.release(delivery_key)
                except Exception as release_exc:
                    logger.error(
                        "[linear] Failed to release delivery_key=%s after enqueue error: %s",
                        delivery_key,
                        release_exc,
                        exc_info=True,
                    )
            logger.error(
                "[linear] Failed to enqueue subscription_id=%s delivery_key=%s: %s",
                webhook_id,
                delivery_key,
                exc,
                exc_info=True,
            )
            return web.json_response({"status": "unavailable"}, status=503)
        finally:
            if inflight is not None:
                inflight.discard(delivery_key)
                if not inflight:
                    self._inflight_session_deliveries.pop(agent_session_id, None)
            if dispatch_lock_held and dispatch_lock is not None:
                dispatch_lock.release()
            if direct_issue_lock_held and direct_issue_lock is not None:
                direct_issue_lock.release()

    def _direct_activation_policy_allows(
        self, context: dict[str, Any], grant: dict[str, Any]
    ) -> bool:
        team_id = str((context.get("team") or {}).get("id") or "")
        owner_id = str((context.get("assignee") or {}).get("id") or "")
        creator_id = str((context.get("creator") or {}).get("id") or "")
        delegate_id = str((context.get("delegate") or {}).get("id") or "")
        parent_id = str((context.get("parent") or {}).get("id") or "")
        return bool(
            grant.get("source_platform") in DeliveryLedger.DIRECT_POLICY_RESULTS
            and grant.get("source_user_id")
            and grant.get("source_message_id")
            and grant.get("source_session_id")
            and grant.get("source_profile")
            and grant.get("policy_result")
            == DeliveryLedger.DIRECT_POLICY_RESULTS[grant["source_platform"]]
            and not parent_id
            and team_id in self._activation_allowed_team_ids
            and owner_id in self._planned_owner_ids
            and self._linear is not None
            and self._linear.actor_id
            and hmac.compare_digest(creator_id, self._linear.actor_id)
            and hmac.compare_digest(delegate_id, self._linear.actor_id)
            and hmac.compare_digest(str(grant.get("actor_id") or ""), self._linear.actor_id)
            and hmac.compare_digest(str(grant.get("team_id") or ""), team_id)
            and self._ledger is not None
            and hmac.compare_digest(
                str(grant.get("issue_fingerprint") or ""),
                self._ledger.direct_issue_fingerprint(
                    team_id, str(context.get("title") or "")
                ),
            )
        )

    def _direct_activation_claim_is_current(
        self, issue_id: str, session_id: str
    ) -> bool:
        if self._ledger is None:
            return False
        grant = self._ledger.get_direct_activation_grant(issue_id)
        return bool(
            grant
            and grant.get("state") == "claimed"
            and hmac.compare_digest(str(grant.get("session_id") or ""), session_id)
        )

    def _live_activation_policy_allows(
        self, context: dict[str, Any], *, allow_started: bool = False
    ) -> bool:
        state_type = str((context.get("state") or {}).get("type") or "").casefold()
        team_id = str((context.get("team") or {}).get("id") or "")
        owner_id = str((context.get("assignee") or {}).get("id") or "")
        delegate_id = str((context.get("delegate") or {}).get("id") or "")
        return bool(
            state_type in ({"unstarted", "started"} if allow_started else {"unstarted"})
            and team_id in self._activation_allowed_team_ids
            and owner_id in self._planned_owner_ids
            and self._linear is not None
            and self._linear.actor_id
            and delegate_id
            and hmac.compare_digest(delegate_id, self._linear.actor_id)
        )

    async def _handle_manager_session_created(
        self,
        payload: dict[str, Any],
        delivery_key: str,
        webhook_id: str,
        issue_id: str,
        session_id: str,
        *,
        read_deadline: float | None = None,
    ) -> web.Response:
        """CAS and dispatch one native manager session while closure controls are excluded."""
        assert self._ledger is not None and self._linear is not None
        async with _admission_lock(self._issue_lock(issue_id), read_deadline):
            activation = self._ledger.get_manager_activation(issue_id)
            state = str((activation or {}).get("state") or "")
            evidence = (activation or {}).get("evidence") or {}
            if state == "dispatch_unknown":
                event_actor_id, _ = _actor(payload)
                async with asyncio.timeout_at(read_deadline):
                    recovery_context = await self._linear.get_issue_closure_context(issue_id)
                _check_admission_deadline(read_deadline)
                recovery_state = recovery_context.get("state") or {}
                evidence_state_id = str(evidence.get("current_state_id") or "")
                evidence_revision = str(evidence.get("event_updated_at") or "")
                evidence_team_id = str(evidence.get("team_id") or "")
                evidence_assignee_id = str(evidence.get("assignee_id") or "")
                evidence_delegate_id = str(evidence.get("delegate_id") or "")
                live_revision = str(recovery_context.get("updated_at") or "")
                session_created_at = str(
                    ((payload.get("agentSession") or {}).get("createdAt") or "")
                )
                signed_session_issue_revision = str(
                    (
                        ((payload.get("agentSession") or {}).get("issue") or {}).get(
                            "updatedAt"
                        )
                        or ""
                    )
                )
                exact_reopen_revision = bool(
                    evidence_revision
                    and hmac.compare_digest(live_revision, evidence_revision)
                )
                native_session_revision = False
                if (
                    evidence_revision
                    and live_revision
                    and session_created_at
                    and signed_session_issue_revision
                    and hmac.compare_digest(
                        live_revision, signed_session_issue_revision
                    )
                ):
                    try:
                        evidence_ts = _iso_timestamp(evidence_revision)
                        session_created_ts = _iso_timestamp(session_created_at)
                        signed_issue_ts = _iso_timestamp(signed_session_issue_revision)
                        native_session_revision = bool(
                            evidence_ts < session_created_ts <= signed_issue_ts
                            and signed_issue_ts - session_created_ts <= 5.0
                        )
                    except ValueError:
                        native_session_revision = False
                async with asyncio.timeout_at(read_deadline):
                    sessions = await self._linear.get_issue_agent_sessions(issue_id)
                _check_admission_deadline(read_deadline)
                open_actor_sessions = [
                    session for session in sessions
                    if self._is_execution_capable_open_session(session)
                ]
                recovered_reopen = bool(
                    evidence.get("verification_source")
                    == "signed_human_reopen_plus_live_readback"
                    and event_actor_id
                    and self._linear.actor_id
                    and hmac.compare_digest(event_actor_id, self._linear.actor_id)
                    and evidence_state_id
                    and hmac.compare_digest(
                        str(recovery_state.get("id") or ""), evidence_state_id
                    )
                    and str(recovery_state.get("type") or "").casefold()
                    == "started"
                    and (exact_reopen_revision or native_session_revision)
                    and evidence_team_id
                    and hmac.compare_digest(
                        str((recovery_context.get("team") or {}).get("id") or ""),
                        evidence_team_id,
                    )
                    and evidence_assignee_id
                    and hmac.compare_digest(
                        str((recovery_context.get("assignee") or {}).get("id") or ""),
                        evidence_assignee_id,
                    )
                    and evidence_delegate_id
                    and hmac.compare_digest(
                        str((recovery_context.get("delegate") or {}).get("id") or ""),
                        evidence_delegate_id,
                    )
                    and len(open_actor_sessions) == 1
                    and hmac.compare_digest(
                        str(open_actor_sessions[0].get("id") or ""), session_id
                    )
                )
                if not recovered_reopen:
                    self._ledger.mark_done(delivery_key)
                    return web.json_response(
                        {"status": "dispatch_ambiguous"}, status=200
                    )
                self._ledger.mark_manager_activation(
                    issue_id, "delegated", session_id=session_id
                )
                activation = self._ledger.get_manager_activation(issue_id)
                state = "delegated"
            if state == "session_started":
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "manager_session_duplicate"}, status=200)
            if state == "canceled":
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "activation_policy_denied"}, status=200)
            async with asyncio.timeout_at(read_deadline):
                context = await self._linear.get_issue_closure_context(issue_id)
            _check_admission_deadline(read_deadline)
            expected_session_id = str((activation or {}).get("session_id") or "")
            evidence = (activation or {}).get("evidence") or {}
            reopen_activation = bool(
                evidence.get("verification_source")
                == "signed_human_reopen_plus_live_readback"
            )
            expected_session_matches = bool(
                not expected_session_id
                or hmac.compare_digest(expected_session_id, session_id)
            )
            if not expected_session_matches:
                self._ledger.mark_done(delivery_key)
                return web.json_response(
                    {"status": "manager_session_mismatch"}, status=200
                )
            if not self._live_activation_policy_allows(
                context, allow_started=reopen_activation
            ):
                if state in {"claimed", "failed", "delegation_unknown"}:
                    self._ledger.release(delivery_key)
                    return web.json_response(
                        {"status": "delegation_ambiguous"}, status=503
                    )
                self._ledger.mark_manager_activation(issue_id, "canceled")
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "activation_policy_denied"}, status=200)
            if not await self._bind_issue_session(issue_id, session_id, read_deadline=read_deadline):
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "issue_session_active"}, status=200)
            if state in {"claimed", "failed", "delegation_unknown"}:
                self._ledger.mark_manager_activation(issue_id, "delegated")
            async with _admission_lock(self._session_lock(session_id), read_deadline):
                async with asyncio.timeout_at(read_deadline):
                    await self._validate_activity_target(session_id)
                # After this CAS dispatch may be ambiguous: never time out or
                # reset its owner, even if handle_message itself is slow.
                _check_admission_deadline(read_deadline)
                if not self._ledger.claim_manager_session(issue_id, session_id):
                    current = self._ledger.get_manager_activation(issue_id) or {}
                    status = (
                        "dispatch_ambiguous"
                        if current.get("state") == "dispatch_unknown"
                        else "manager_session_duplicate"
                    )
                    self._ledger.mark_done(delivery_key)
                    return web.json_response({"status": status}, status=200)
                event = self._message_event(
                    payload, delivery_key, webhook_id, activation_resume=True
                )
                self._schedule_thought(
                    session_id, issue_id, delivery_key, include_queued=True
                )
                await self.handle_message(event)
                self._ledger.mark_manager_activation(
                    issue_id, "session_started", session_id=session_id
                )
                self._ledger.mark_done(delivery_key)
            return web.json_response({"status": "accepted"}, status=200)

    def _message_event(
        self,
        payload: dict[str, Any],
        delivery_key: str,
        webhook_id: str,
        *,
        dependency_resume: bool = False,
        activation_resume: bool = False,
        direct_activation: bool = False,
    ) -> MessageEvent:
        action = str(payload.get("action") or "")
        agent_session = payload.get("agentSession")
        if not isinstance(agent_session, dict):
            raise ValueError("Agent Session payload is missing")
        agent_session_id = str(agent_session.get("id") or "")
        issue = agent_session.get("issue")
        issue_id = str(issue.get("id") or "") if isinstance(issue, dict) else ""
        signal = _activity_signal(payload)
        is_stop = action == "prompted" and signal == "stop"
        body = _activity_body(payload)
        is_command = action == "prompted" and body.lstrip().startswith("/")
        actor_id, actor_name = _actor(payload)
        identifier, title, _ = _issue_label(agent_session)
        return MessageEvent(
            text=(
                "/stop"
                if is_stop
                else body
                if is_command
                else build_agent_prompt(
                    payload,
                    dependency_resume=dependency_resume,
                    activation_resume=activation_resume,
                    direct_activation=direct_activation,
                )
            ),
            message_type=MessageType.COMMAND if (is_stop or is_command) else MessageType.TEXT,
            source=self.build_source(
                chat_id=agent_session_id,
                chat_name=f"{identifier} — {title}",
                chat_type="dm",
                user_id=actor_id,
                user_name=actor_name,
                message_id=delivery_key,
                # Preserve baseline authorization for ordinary webhook input.
                # Native control commands remain fail-closed and are gated
                # against the active human turn immediately before dispatch.
                role_authorized=not is_command and not is_stop,
            ),
            raw_message=payload,
            message_id=delivery_key,
            metadata={
                "linear_action": action,
                "linear_webhook_id": webhook_id,
                "linear_delivery_key": delivery_key,
                "linear_agent_session_id": agent_session_id,
                "linear_issue_id": issue_id,
                "linear_signal": signal,
                "linear_dependency_resume": dependency_resume,
                "linear_activation_resume": activation_resume,
                "linear_direct_activation": direct_activation,
            },
        )

    async def _reconcile_planned_activation(
        self,
        payload: dict[str, Any],
        issue_id: str,
        *,
        _issue_locked: bool = False,
    ) -> str | None:
        """Resume one parked Planned session after verified Todo activation."""
        if (
            not self._planned_activation_enabled
            or not self._activation_allowed_team_ids
            or self._linear is None
            or self._ledger is None
        ):
            return None
        wait = self._ledger.get_activation_wait(issue_id)
        if wait is not None and wait.get("state") == "dispatch_unknown":
            return "activation_ambiguous"
        if not _issue_locked:
            async with self._issue_lock(issue_id):
                return await self._reconcile_planned_activation(
                    payload, issue_id, _issue_locked=True
                )
        previous = payload.get("updatedFrom")
        data = payload.get("data")
        if not isinstance(previous, dict) or not isinstance(data, dict):
            return None
        previous_state_id = str(previous.get("stateId") or "")
        event_state = data.get("state")
        event_state_id = str(
            data.get("stateId")
            or (event_state.get("id") if isinstance(event_state, dict) else "")
            or ""
        )
        event_state_type = str(
            event_state.get("type") if isinstance(event_state, dict) else ""
        ).casefold()
        event_updated_at = str(data.get("updatedAt") or "")
        actor_id, _ = _actor(payload)
        if not (
            previous_state_id
            and event_state_id
            and event_state_type in {"unstarted", "started"}
            and event_updated_at
            and actor_id
        ):
            return None
        context = await self._linear.get_issue_closure_context(issue_id)
        state = context.get("state") or {}
        team_id = str((context.get("team") or {}).get("id") or "")
        assignee_id = str((context.get("assignee") or {}).get("id") or "")
        delegate_id = str((context.get("delegate") or {}).get("id") or "")
        live_updated_at = str(context.get("updated_at") or "")
        previous_live = next(
            (
                item for item in context.get("team_states") or []
                if hmac.compare_digest(str(item.get("id") or ""), previous_state_id)
            ),
            None,
        )
        previous_state_type = str(
            previous_live.get("type") if isinstance(previous_live, dict) else ""
        ).casefold()
        standard_activation = previous_state_type == "backlog"
        parked_recovery = bool(
            wait is not None
            and wait.get("state") == "waiting"
            and previous_state_type == "started"
            and event_state_type == "unstarted"
        )
        # Recovery is narrower than ordinary activation: the existing durable
        # wait is the fence and only a human started→Todo transition may use it.
        # Manager activations without a wait must still originate from Backlog.
        authoritative = bool(
            team_id in self._activation_allowed_team_ids
            and assignee_id in self._planned_owner_ids
            and hmac.compare_digest(actor_id, assignee_id)
            and self._linear.actor_id
            and str(state.get("type") or "").casefold() == "unstarted"
            and event_state_type == "unstarted"
            and hmac.compare_digest(str(state.get("id") or ""), event_state_id)
            and live_updated_at
            and hmac.compare_digest(live_updated_at, event_updated_at)
            and previous_live
            and (standard_activation or parked_recovery)
        )
        if not authoritative:
            return "activation_rejected"
        target_delegate_id = str(self._linear.actor_id or "")
        material = "\0".join(
            (issue_id, live_updated_at, event_state_id, actor_id, target_delegate_id, team_id)
        )
        activation_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if wait is None:
            if delegate_id and not hmac.compare_digest(delegate_id, target_delegate_id):
                return "activation_rejected"
            evidence = {
                "issue_id": issue_id,
                "event_updated_at": event_updated_at,
                "previous_state_id": previous_state_id,
                "current_state_id": event_state_id,
                "actor_id": actor_id,
                "assignee_id": assignee_id,
                "delegate_id": target_delegate_id,
                "team_id": team_id,
                "verification_source": "signed_webhook_plus_live_readback",
            }
            current = self._ledger.get_manager_activation(issue_id)
            if (
                current
                and current.get("activation_key") == activation_key
                and delegate_id
                and hmac.compare_digest(delegate_id, target_delegate_id)
                and current.get("state") in {
                    "claimed",
                    "failed",
                    "delegation_unknown",
                }
            ):
                self._ledger.mark_manager_activation(issue_id, "delegated")
                return "manager_delegated"
            if not self._ledger.claim_manager_activation(
                issue_id, activation_key, evidence
            ):
                current = self._ledger.get_manager_activation(issue_id)
                if current and current.get("activation_key") == activation_key:
                    if current.get("state") == "delegation_unknown":
                        return "delegation_ambiguous"
                    return "activation_duplicate"
                return "activation_rejected"
            if delegate_id and hmac.compare_digest(delegate_id, target_delegate_id):
                self._ledger.mark_manager_activation(issue_id, "delegated")
                return "manager_delegated"
            try:
                self._ledger.mark_manager_activation(issue_id, "delegation_unknown")
                await self._linear.assign_issue_delegate(issue_id, target_delegate_id)
                self._ledger.mark_manager_activation(issue_id, "delegated")
                return "manager_delegated"
            except Exception as exc:
                readback = await self._linear.get_issue_closure_context(issue_id)
                actual_delegate = str(
                    (readback.get("delegate") or {}).get("id") or ""
                )
                if actual_delegate and hmac.compare_digest(
                    actual_delegate, target_delegate_id
                ):
                    self._ledger.mark_manager_activation(issue_id, "delegated")
                    return "manager_delegated"
                self._ledger.mark_manager_activation(
                    issue_id, "delegation_unknown", error=str(exc)
                )
                raise
        if not (
            delegate_id and hmac.compare_digest(delegate_id, target_delegate_id)
        ):
            return "activation_rejected"
        if not self._ledger.claim_activation(issue_id, activation_key):
            current = self._ledger.get_activation_wait(issue_id)
            if current and current.get("activation_key") == activation_key:
                return "activation_duplicate"
            return "activation_rejected"
        try:
            session_id = str(wait["session_id"])
            async with self._session_lock(session_id):
                if self._ledger.has_session_closure(session_id):
                    self._ledger.cancel_activation_for_session(session_id)
                    return "activation_rejected"
                event = self._message_event(
                    wait["prompt"],
                    wait["delivery_key"],
                    "planned-activation",
                    activation_resume=True,
                )
                self._schedule_thought(
                    session_id,
                    issue_id,
                    wait["delivery_key"],
                    include_queued=False,
                    body="Todo activation verified; Hermes claimed one-shot manager planning dispatch.",
                )
                await self.handle_message(event)
                self._ledger.mark_activation_resumed(issue_id)
            return "activation_resumed"
        except Exception as exc:
            self._ledger.fail_activation(issue_id, str(exc))
            raise

    async def _reconcile_human_reopen(
        self,
        payload: dict[str, Any],
        issue_id: str,
        *,
        _issue_locked: bool = False,
    ) -> str | None:
        """Create one native session for a signed human completed/canceled→started edge."""
        if (
            not self._planned_activation_enabled
            or self._linear is None
            or self._ledger is None
        ):
            return None
        if not _issue_locked:
            async with self._issue_lock(issue_id):
                return await self._reconcile_human_reopen(
                    payload, issue_id, _issue_locked=True
                )
        previous = payload.get("updatedFrom")
        data = payload.get("data")
        if not isinstance(previous, dict) or not isinstance(data, dict):
            return None
        previous_state_id = str(previous.get("stateId") or "")
        event_state = data.get("state")
        event_state_id = str(
            data.get("stateId")
            or (event_state.get("id") if isinstance(event_state, dict) else "")
            or ""
        )
        event_state_type = str(
            event_state.get("type") if isinstance(event_state, dict) else ""
        ).casefold()
        event_updated_at = str(data.get("updatedAt") or "")
        actor_id, _ = _actor(payload)
        if not (
            previous_state_id and event_state_id and event_state_type == "started"
            and event_updated_at and actor_id
        ):
            return None
        context = await self._linear.get_issue_closure_context(issue_id)
        previous_live = next(
            (
                item for item in context.get("team_states") or []
                if hmac.compare_digest(str(item.get("id") or ""), previous_state_id)
            ),
            None,
        )
        previous_type = str(
            previous_live.get("type") if isinstance(previous_live, dict) else ""
        ).casefold()
        state = context.get("state") or {}
        team_id = str((context.get("team") or {}).get("id") or "")
        assignee_id = str((context.get("assignee") or {}).get("id") or "")
        delegate_id = str((context.get("delegate") or {}).get("id") or "")
        live_updated_at = str(context.get("updated_at") or "")
        authoritative = bool(
            previous_type in {"completed", "canceled"}
            and team_id in self._activation_allowed_team_ids
            and assignee_id in self._planned_owner_ids
            and hmac.compare_digest(actor_id, assignee_id)
            and self._linear.actor_id
            and hmac.compare_digest(delegate_id, self._linear.actor_id)
            and str(state.get("type") or "").casefold() == "started"
            and hmac.compare_digest(str(state.get("id") or ""), event_state_id)
            and live_updated_at
            and hmac.compare_digest(live_updated_at, event_updated_at)
        )
        if not authoritative:
            return None
        sessions = await self._linear.get_issue_agent_sessions(issue_id)
        open_for_actor = any(
            self._is_execution_capable_open_session(session) for session in sessions
        )
        if open_for_actor:
            return "reopen_open_session"
        material = "\0".join(
            (issue_id, event_updated_at, previous_state_id, event_state_id, actor_id, delegate_id)
        )
        activation_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        evidence = {
            "issue_id": issue_id,
            "event_updated_at": event_updated_at,
            "previous_state_id": previous_state_id,
            "current_state_id": event_state_id,
            "actor_id": actor_id,
            "assignee_id": assignee_id,
            "delegate_id": delegate_id,
            "team_id": team_id,
            "verification_source": "signed_human_reopen_plus_live_readback",
        }
        if not self._ledger.claim_manager_reactivation(
            issue_id, activation_key, evidence
        ):
            current = self._ledger.get_manager_activation(issue_id) or {}
            if current.get("activation_key") == activation_key:
                if current.get("state") in {"claimed", "dispatch_unknown"}:
                    return "reopen_ambiguous"
                return "reopen_duplicate"
            return "reopen_rejected"
        self._ledger.mark_manager_activation(issue_id, "dispatch_unknown")
        confirmed = await self._linear.get_issue_closure_context(issue_id)
        confirmed_state = confirmed.get("state") or {}
        confirmed_sessions = await self._linear.get_issue_agent_sessions(issue_id)
        confirmed_open = any(
            self._is_execution_capable_open_session(session)
            for session in confirmed_sessions
        )
        confirmation_matches = bool(
            str((confirmed.get("team") or {}).get("id") or "") == team_id
            and str((confirmed.get("assignee") or {}).get("id") or "") == assignee_id
            and str((confirmed.get("delegate") or {}).get("id") or "") == delegate_id
            and str(confirmed_state.get("id") or "") == event_state_id
            and str(confirmed_state.get("type") or "").casefold() == "started"
            and str(confirmed.get("updated_at") or "") == event_updated_at
            and not confirmed_open
        )
        if not confirmation_matches:
            self._ledger.mark_manager_activation(
                issue_id, "failed", error="reopen_pre_dispatch_changed"
            )
            return "reopen_rejected"
        session_id = await self._linear.create_agent_session_on_issue(issue_id)
        self._ledger.mark_manager_activation(
            issue_id, "delegated", session_id=session_id
        )
        return "reopen_session_created"

    def _is_execution_capable_open_session(self, session: dict[str, Any]) -> bool:
        """Treat vendor-open status as actionable only without a durable closure fence."""
        assert self._linear is not None
        assert self._ledger is not None
        if (
            str(session.get("app_user_id") or "") != self._linear.actor_id
            or str(session.get("status") or "")
            not in {"pending", "active", "awaitingInput"}
        ):
            return False
        session_id = str(session.get("id") or "")
        return not session_id or not self._ledger.has_session_closure(session_id)

    async def _reconcile_human_completion(
        self,
        payload: dict[str, Any],
        issue_id: str,
        *,
        _issue_locked: bool = False,
        _session_locked: bool = False,
        _proposed_session_id: str | None = None,
    ) -> str | None:
        """Normalize one verified human started→completed transition into a durable response."""
        if (
            not self._closure_reconciliation_enabled
            or not self._closure_allowed_team_ids
            or self._linear is None
            or self._ledger is None
        ):
            return None
        previous = payload.get("updatedFrom")
        if not isinstance(previous, dict):
            return None
        previous_state = previous.get("state")
        previous_state_id = str(
            previous.get("stateId")
            or (previous_state.get("id") if isinstance(previous_state, dict) else "")
            or ""
        )
        if not previous_state_id:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        event_state = data.get("state")
        event_state_id = str(
            data.get("stateId")
            or (event_state.get("id") if isinstance(event_state, dict) else "")
            or ""
        )
        event_state_type = str(
            event_state.get("type") if isinstance(event_state, dict) else ""
        ).casefold()
        if not event_state_id or event_state_type != "completed":
            logger.warning(
                "[linear] closure rejected issue=%s reason=invalid_terminal_evidence",
                issue_id,
            )
            return "closure_rejected"
        actor = payload.get("actor")
        if not isinstance(actor, dict):
            actor = payload.get("user")
        actor_id = str(actor.get("id") or "") if isinstance(actor, dict) else ""
        if not actor_id:
            logger.warning("[linear] closure rejected issue=%s reason=missing_actor", issue_id)
            return "closure_rejected"
        event_updated_at = str(data.get("updatedAt") or "")
        try:
            event_updated_ts = _iso_timestamp(event_updated_at)
        except ValueError:
            logger.warning("[linear] closure rejected issue=%s reason=invalid_revision", issue_id)
            return "closure_rejected"
        if not _issue_locked:
            async with self._issue_lock(issue_id):
                return await self._reconcile_human_completion(
                    payload,
                    issue_id,
                    _issue_locked=True,
                    _session_locked=_session_locked,
                    _proposed_session_id=_proposed_session_id,
                )
        persisted_session_id = self._ledger.get_issue_session(issue_id) or ""
        session_id = persisted_session_id or str(_proposed_session_id or "")
        if session_id and not _session_locked:
            async with self._session_lock(session_id):
                return await self._reconcile_human_completion(
                    payload,
                    issue_id,
                    _issue_locked=True,
                    _session_locked=True,
                    _proposed_session_id=_proposed_session_id,
                )
        context = await self._linear.get_issue_closure_context(issue_id)
        state = context.get("state") or {}
        if str(state.get("type") or "").casefold() != "completed":
            return "closure_obsolete"
        current_state_id = str(state.get("id") or "")
        if event_state_id and not hmac.compare_digest(event_state_id, current_state_id):
            logger.warning("[linear] closure rejected issue=%s reason=state_mismatch", issue_id)
            return "closure_rejected"
        team_id = str((context.get("team") or {}).get("id") or "")
        assignee = context.get("assignee") or {}
        delegate = context.get("delegate") or {}
        assignee_id = str(assignee.get("id") or "")
        delegate_id = str(delegate.get("id") or "")
        live_updated_at = str(context.get("updated_at") or "")
        completed_at = str(context.get("completed_at") or "")
        previous_live = next(
            (
                item for item in context.get("team_states") or []
                if hmac.compare_digest(str(item.get("id") or ""), previous_state_id)
            ),
            None,
        )
        current_state_id = str(state.get("id") or "")
        previous_state_type = (
            str(previous_live.get("type") or "").casefold()
            if previous_live
            else ""
        )
        authoritative = bool(
            team_id in self._closure_allowed_team_ids
            and assignee_id
            and hmac.compare_digest(actor_id, assignee_id)
            and self._linear.actor_id
            and delegate_id
            and hmac.compare_digest(delegate_id, self._linear.actor_id)
            and live_updated_at
            and hmac.compare_digest(event_updated_at, live_updated_at)
            and previous_live
            and previous_state_type in {"started", "unstarted"}
            and current_state_id
        )
        if not authoritative:
            logger.warning("[linear] closure rejected issue=%s reason=authoritative_policy", issue_id)
            return "closure_rejected"
        assert previous_live is not None
        if not session_id:
            try:
                sessions = await self._linear.get_issue_agent_sessions(issue_id)
            except LinearAPIError as exc:
                logger.warning(
                    "[linear] closure session recovery deferred issue=%s reason=%s",
                    issue_id,
                    type(exc).__name__,
                )
                if exc.retryable:
                    raise
                sessions = []
            owned_sessions = [
                item
                for item in sessions
                if self._linear.actor_id
                and hmac.compare_digest(
                    str(item.get("app_user_id") or ""), self._linear.actor_id
                )
            ]
            open_sessions = [
                item
                for item in owned_sessions
                if str(item.get("status") or "")
                in {"pending", "active", "awaitingInput"}
            ]
            completed_sessions = [
                item
                for item in owned_sessions
                if str(item.get("status") or "") == "complete"
            ]
            candidates = open_sessions or completed_sessions
            if len(candidates) == 1:
                session_id = str(candidates[0].get("id") or "")
            elif candidates:
                logger.warning(
                    "[linear] closure session recovery ambiguous issue=%s candidates=%d",
                    issue_id,
                    len(candidates),
                )
        if session_id and not _session_locked:
            async with self._session_lock(session_id):
                return await self._reconcile_human_completion(
                    payload,
                    issue_id,
                    _issue_locked=True,
                    _session_locked=True,
                    _proposed_session_id=session_id,
                )
        if not session_id:
            self._ledger.stage_pending_closure_event(
                issue_id,
                event_updated_ts,
                {
                    "actor": {"id": actor_id},
                    "data": {
                        "id": issue_id,
                        "updatedAt": event_updated_at,
                        "state": {"id": current_state_id, "type": "completed"},
                    },
                    "updatedFrom": {"stateId": previous_state_id},
                },
            )
            logger.info("[linear] terminal fenced issue=%s reason=session_unbound", issue_id)
            return "terminal_fenced"
        if not persisted_session_id and not await self._bind_issue_session(issue_id, session_id):
            return "closure_deferred"
        material = "\0".join(
            (
                issue_id,
                live_updated_at,
                current_state_id,
                actor_id,
                delegate_id,
                team_id,
            )
        )
        closure_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        actor_name = str(assignee.get("name") or actor.get("name") or "Human assignee")
        delegate_name = str(delegate.get("name") or self._linear.actor_name or "Hermes")
        previous_name = str(previous_live.get("name") or previous_state_type.title())
        current_name = str(state.get("name") or "Completed")
        body = "\n".join(
            (
                "Closure reconciliation complete.",
                "",
                f"- Human assignee: {actor_name}",
                f"- Verified transition: {previous_name} (`{previous_state_type}`) → {current_name} (`completed`)",
                f"- Delegate: {delegate_name}",
                "- Main deliverable: not rerun",
                "- Terminal issue state: preserved",
            )
        )
        evidence = {
            "issue_id": issue_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "assignee_id": assignee_id,
            "delegate_id": delegate_id,
            "team_id": team_id,
            "previous_state_id": previous_state_id,
            "current_state_id": current_state_id,
            "event_updated_at": event_updated_at,
            "live_updated_at": live_updated_at,
            "verification_source": "signed_webhook_plus_live_readback",
            "completed_at": completed_at,
        }
        async with self._outbox_drain_lock:
            inserted = self._ledger.enqueue_closure_activity(
                closure_key,
                issue_id,
                session_id,
                self._activity_uuid(f"closure:{closure_key}"),
                body,
                evidence,
                indicator_activity_id=self._activity_uuid(
                    f"closure-indicator:{closure_key}"
                ),
                indicator_body=(
                    "⏳ Done received — human acceptance and closure evidence "
                    "are being verified…"
                ),
            )
        self._outbox_wakeup.set()
        if not inserted:
            logger.info("[linear] closure duplicate issue=%s key=%s", issue_id, closure_key)
            return "closure_duplicate"
        status = "closure_queued"
        logger.info(
            "[linear] closure %s issue=%s session=%s key=%s",
            status,
            issue_id,
            session_id,
            closure_key,
        )
        return status

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def reserve_channel_route(self, **route: str) -> bool:
        """Synchronously persist a hook command, then wake the bounded worker."""
        if self._ledger is None:
            raise RuntimeError("Linear routing ledger is unavailable")
        created = self._ledger.claim_channel_route(**route)
        self._channel_route_wakeup.set()
        if not created:
            existing = self._ledger.get_channel_route(str(route.get("operation_key") or ""))
            if existing is not None:
                task = asyncio.create_task(self._notify_channel_route_status(existing))
                self._channel_route_notice_tasks.add(task)
                task.add_done_callback(self._channel_route_notice_tasks.discard)
        return created

    def _channel_source_adapter(self, route: dict[str, Any]) -> Any | None:
        runner = getattr(self, "gateway_runner", None)
        profile = str(route.get("source_profile") or "")
        resolver = getattr(runner, "_authorization_adapter", None)
        if callable(resolver) and not type(resolver).__module__.startswith("unittest.mock"):
            try:
                if route.get("source_via_relay") is True:
                    return resolver(Platform.RELAY, None)
                return resolver(
                    Platform(str(route.get("source_platform") or "")),
                    profile or None,
                )
            except Exception:
                return None
        if route.get("source_via_relay") is True:
            adapters = getattr(runner, "adapters", None) or {}
            desired = "relay"
        elif profile:
            profile_maps = getattr(runner, "_profile_adapters", None) or {}
            adapters = profile_maps.get(profile)
            if not isinstance(adapters, dict):
                return None
            desired = str(route.get("source_platform") or "").casefold()
        else:
            adapters = getattr(runner, "adapters", None) or {}
            desired = str(route.get("source_platform") or "").casefold()
        for key, adapter in adapters.items():
            name = str(getattr(key, "value", key) or "").casefold()
            if name == desired:
                return adapter
        return None

    async def _notify_channel_route(self, route: dict[str, Any], content: str) -> bool:
        adapter = self._channel_source_adapter(route)
        if adapter is None:
            return False
        try:
            reply_to = str(route.get("source_message_id") or "") or None
            metadata: dict[str, Any] = {}
            thread_id = str(route.get("source_thread_id") or "")
            if thread_id:
                metadata["thread_id"] = thread_id
            platform_name = str(route.get("source_platform") or "").casefold()
            scope_id = str(route.get("source_scope_id") or "")
            if platform_name == "slack" and scope_id:
                metadata["slack_team_id"] = scope_id
            if platform_name == "telegram" and route.get("source_chat_type") == "dm":
                metadata["telegram_dm_topic_reply_fallback"] = True
                if thread_id not in {"", "1"}:
                    metadata["direct_messages_topic_id"] = thread_id
                if reply_to:
                    metadata["telegram_reply_to_message_id"] = reply_to
            if route.get("source_via_relay") is True:
                metadata["_relay_logical_platform"] = platform_name
                user_id = str(route.get("source_user_id") or "")
                if scope_id:
                    metadata["scope_id"] = scope_id
                if user_id:
                    metadata["user_id"] = user_id
            send_kwargs: dict[str, Any] = {
                "chat_id": str(route.get("source_chat_id") or ""),
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata or None,
            }
            result = await adapter.send(**send_kwargs)
            return not (result is not None and getattr(result, "success", True) is False)
        except Exception:
            logger.warning("[linear] Cross-channel source status delivery failed")
            return False

    async def _notify_channel_route_status(self, route: dict[str, Any]) -> None:
        issue_ref = str(route.get("issue_ref") or "Linear issue")
        state = str(route.get("state") or "unknown")
        statuses = {
            "claimed": "durably reserved and awaiting validation; it has not been dispatched yet",
            "dispatching": "crossed the dispatch boundary; the final dispatch result is not known yet",
            "dispatched": "was already dispatched to its canonical native Linear AgentSession",
            "blocked": "was blocked before dispatch by the current Linear lifecycle",
            "failed": "failed before dispatch after bounded recovery attempts",
            "ambiguous": "has an ambiguous dispatch result and will not be replayed automatically",
        }
        await self._notify_channel_route(
            route,
            f"{issue_ref}: this source command {statuses.get(state, 'has recorded routing state')}. "
            f"https://linear.app/issue/{issue_ref}",
        )

    async def _channel_route_loop(self) -> None:
        """Recover and process only pre-dispatch work in bounded batches."""
        while self._running:
            assert self._ledger is not None
            routes = self._ledger.claim_due_channel_routes(limit=_CHANNEL_ROUTE_BATCH_SIZE)
            for route in routes:
                if not self._running:
                    return
                await self._process_channel_route(route)
            if len(routes) >= _CHANNEL_ROUTE_BATCH_SIZE:
                await asyncio.sleep(0)
                continue
            self._channel_route_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._channel_route_wakeup.wait(), timeout=_CHANNEL_ROUTE_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    def _channel_route_source_authorized(self, route: dict[str, Any]) -> bool:
        """Recheck current gateway authorization before every replay attempt."""
        runner = getattr(self, "gateway_runner", None)
        checker = getattr(runner, "_is_user_authorized", None)
        if not callable(checker):
            return False
        try:
            source = SessionSource(
                platform=Platform(str(route.get("source_platform") or "")),
                chat_id=str(route.get("source_chat_id") or ""),
                chat_type=str(route.get("source_chat_type") or "dm"),
                user_id=str(route.get("source_user_id") or "") or None,
                user_name=str(route.get("source_user_name") or "") or None,
                thread_id=str(route.get("source_thread_id") or "") or None,
                profile=str(route.get("source_profile") or "") or None,
                scope_id=str(route.get("source_scope_id") or "") or None,
                delivered_via_upstream_relay=route.get("source_via_relay") is True,
            )
            return checker(source) is True
        except Exception:
            logger.warning("[linear] Cross-channel source authorization recheck failed")
            return False

    async def _process_channel_route(self, route: dict[str, Any]) -> None:
        assert self._ledger is not None
        operation_key = str(route["operation_key"])
        issue_ref = str(route["issue_ref"])
        try:
            if not self._channel_route_source_authorized(route):
                self._ledger.mark_channel_route(
                    operation_key, "blocked", error="source_authorization_revoked"
                )
                await self._notify_channel_route(
                    route,
                    f"{issue_ref}: source authorization is no longer valid; "
                    "nothing was dispatched. Re-authorize and send a new source command.",
                )
                return
            await self._notify_channel_route(
                route,
                f"{issue_ref}: source command is durably reserved and being validated; "
                "it has not been dispatched yet.",
            )
            target = await self.get_channel_route_target(issue_ref)
            issue_id = str(target.get("id") or "")
            identifier = str(target.get("identifier") or issue_ref)
            title = str(target.get("title") or "")
            session_id = str(target.get("session_id") or "")
            if not self._ledger.set_channel_route_target(operation_key, issue_id, session_id):
                return
            route = self._ledger.get_channel_route(operation_key) or route
            if target.get("routable") is not True:
                self._ledger.mark_channel_route(operation_key, "blocked")
                actor_name = str(getattr(self._linear, "actor_name", None) or "Linear agent")
                actor_mention = actor_name if actor_name.startswith("@") else f"@{actor_name}"
                await self._notify_channel_route(
                    route,
                    f"{identifier}: no single active native {actor_name} AgentSession is routable. "
                    f"Mention {actor_mention} on the Linear issue to create a fresh native session; "
                    "nothing was dispatched from the source channel. "
                    f"https://linear.app/issue/{identifier}",
                )
                return
            routed = MessageEvent(
                text=(
                    "Adapter-verified cross-channel follow-up. Treat the command below as "
                    "user-provided input for this canonical Linear issue. Do not create a "
                    "parallel source-channel execution.\n\n"
                    f"Source platform: {route['source_platform']}\n"
                    f"Issue: {identifier}\n"
                    f"Command: {route['command_text']}"
                ),
                message_type=MessageType.TEXT,
                source=self.build_source(
                    chat_id=session_id,
                    chat_name=f"{identifier} — {title}",
                    chat_type="dm",
                    user_id=str(route["source_user_id"]),
                    user_name=str(route["source_user_name"]),
                    message_id=operation_key,
                    role_authorized=True,
                ),
                raw_message={
                    "source_platform": route["source_platform"],
                    "source_chat_id": route["source_chat_id"],
                    "source_thread_id": route["source_thread_id"],
                    "source_message_id": route["source_message_id"],
                    "linear_issue_id": issue_id,
                    "linear_agent_session_id": session_id,
                },
                message_id=operation_key,
                metadata={
                    "linear_action": "cross_channel_prompted",
                    "linear_delivery_key": operation_key,
                    "linear_agent_session_id": session_id,
                    "linear_issue_id": issue_id,
                    "linear_source_platform": route["source_platform"],
                    "linear_source_chat_id": route["source_chat_id"],
                    "linear_source_thread_id": route["source_thread_id"],
                    "linear_source_message_id": route["source_message_id"],
                },
            )

            def begin_dispatch() -> bool:
                if not self._ledger:
                    return False
                if not self._ledger.mark_channel_route(operation_key, "dispatching"):
                    return False
                return True

            accepted = await self.dispatch_channel_route(
                issue_ref,
                issue_id,
                session_id,
                routed,
                before_dispatch=begin_dispatch,
                authorize_dispatch=lambda: self._channel_route_source_authorized(route),
            )
            if not accepted:
                self._ledger.mark_channel_route(operation_key, "blocked")
                await self._notify_channel_route(
                    route,
                    f"{identifier}: native lifecycle changed during locked revalidation; "
                    "nothing was dispatched. "
                    f"https://linear.app/issue/{identifier}",
                )
                return
            if not self._ledger.mark_channel_route(operation_key, "dispatched"):
                self._ledger.mark_channel_route(
                    operation_key, "ambiguous", error="dispatch_state_commit_failed"
                )
                await self._notify_channel_route_status(
                    self._ledger.get_channel_route(operation_key) or route
                )
                return
            await self._notify_channel_route(
                route,
                f"{identifier}: source command was dispatched to the canonical native Linear "
                f"AgentSession. https://linear.app/issue/{identifier}",
            )
        except asyncio.CancelledError:
            current = self._ledger.get_channel_route(operation_key)
            if current is not None and current.get("state") == "dispatching":
                self._ledger.mark_channel_route(
                    operation_key, "ambiguous", error="dispatch_cancelled"
                )
            raise
        except Exception as exc:
            logger.exception("[linear] Cross-channel canonical routing failed")
            current = self._ledger.get_channel_route(operation_key)
            if current is None:
                return
            if current.get("state") == "dispatching":
                self._ledger.mark_channel_route(
                    operation_key, "ambiguous", error=type(exc).__name__
                )
                await self._notify_channel_route_status(
                    self._ledger.get_channel_route(operation_key) or current
                )
                return
            if current.get("state") != "claimed":
                return
            delay = min(60.0, float(2 ** max(0, int(current["attempt_count"]) - 1)))
            self._ledger.retry_channel_route(
                operation_key,
                error=type(exc).__name__,
                next_attempt_at=time.time() + delay,
                max_attempts=_CHANNEL_ROUTE_MAX_ATTEMPTS,
            )
            updated = self._ledger.get_channel_route(operation_key)
            if updated is not None and updated.get("state") == "failed":
                await self._notify_channel_route_status(updated)

    async def get_channel_route_target(self, issue_ref: str) -> dict[str, Any]:
        """Resolve and authorize the single live native session for an issue."""
        if self._linear is None:
            raise LinearAPIError("Linear client is unavailable for channel routing")
        context = await self._linear.get_channel_routing_context(issue_ref)
        actor_id = str(self._linear.actor_id or "")
        delegate_id = str((context.get("delegate") or {}).get("id") or "")
        state_type = str((context.get("state") or {}).get("type") or "").casefold()
        open_sessions = [
            session
            for session in context.get("sessions") or []
            if str(session.get("status") or "") in _OPEN_AGENT_SESSION_STATUSES
            and not str(session.get("ended_at") or "")
            and actor_id
            and str(session.get("app_user_id") or "") == actor_id
        ]
        session_id = str(open_sessions[0].get("id") or "") if len(open_sessions) == 1 else ""
        routable = bool(
            state_type not in {"completed", "canceled"}
            and actor_id
            and delegate_id == actor_id
            and session_id
        )
        return {**context, "session_id": session_id, "routable": routable}

    async def dispatch_channel_route(
        self,
        issue_ref: str,
        expected_issue_id: str,
        expected_session_id: str,
        event: MessageEvent,
        *,
        before_dispatch: Callable[[], bool] | None = None,
        authorize_dispatch: Callable[[], bool] | None = None,
    ) -> bool:
        """Revalidate and serialize cross-channel dispatch with native intake."""
        async with self._issue_lock(expected_issue_id), self._session_lock(expected_session_id):
            target = await self.get_channel_route_target(issue_ref)
            if (
                target.get("routable") is not True
                or str(target.get("id") or "") != expected_issue_id
                or str(target.get("session_id") or "") != expected_session_id
            ):
                return False
            if self._ledger is None or self._ledger.has_session_closure(expected_session_id):
                return False
            if authorize_dispatch is not None and not authorize_dispatch():
                return False
            if not await self._bind_issue_session(expected_issue_id, expected_session_id):
                return False
            if before_dispatch is not None and not before_dispatch():
                raise LinearAPIError("Cross-channel durable dispatch boundary was not acquired")
            await self.handle_message(event)
            return True

    async def _bind_issue_session(
        self, issue_id: str, session_id: str, *, read_deadline: float | None = None,
    ) -> bool:
        """One issue execution per profile/app; callers hold the issue lock.

        Vendor terminal status must belong to the exact old binding. The SQLite
        CAS also protects against another ledger connection replacing it mid-read.
        """
        assert self._ledger is not None and self._linear is not None
        previous = self._ledger.get_issue_session(issue_id)
        if previous and previous != session_id:
            source = self.build_source(chat_id=previous, chat_type="dm")
            extra = getattr(self.config, "extra", None) or {}
            key = build_session_key(
                source,
                group_sessions_per_user=extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
                profile=self._session_key_profile(source),
            )
            def locally_active() -> bool:
                return bool(
                    self._inflight_session_deliveries.get(previous)
                    or self._session_lock(previous).locked()
                    or key in self._active_sessions or key in self._pending_messages
                    or previous in self._active_turn_events
                )

            # Reject without vendor I/O under the issue lock: Stop needs it too.
            if locally_active():
                return False
            async with asyncio.timeout_at(read_deadline):
                context = await self._linear.get_agent_session_delivery_context(previous)
            _check_admission_deadline(read_deadline)
            if not (
                self._linear.actor_id
                and context.get("id") == previous
                and context.get("issue_id") == issue_id
                and context.get("app_user_id") == self._linear.actor_id
                and context.get("status") in {"complete", "error"}
            ) or locally_active():  # Local ownership can change across the read.
                return False
            # Terminal upstream must not orphan a locally resumable execution.
            for pending in (
                self._ledger.get_wait(previous),
                self._ledger.get_activation_wait(issue_id),
                self._ledger.get_direct_activation_event(issue_id),
            ):
                if (pending and pending.get("session_id") == previous
                        and pending.get("state") in {"waiting", "resuming", "claimed", "dispatch_unknown"}):
                    return False
        _check_admission_deadline(read_deadline)
        return self._ledger.bind_issue_session(
            issue_id, session_id, expected_session_id=previous,
        )

    def _issue_lock(self, issue_id: str) -> asyncio.Lock:
        lock = self._issue_locks.get(issue_id)
        if lock is None:
            lock = asyncio.Lock()
            self._issue_locks[issue_id] = lock
        return lock

    async def _handle_data_event(
        self,
        payload: dict[str, Any],
        raw: bytes,
        webhook_id: str,
    ) -> web.Response:
        """Observe control/context changes without turning them into LLM runs."""
        if self._linear is None or self._ledger is None:
            return web.json_response({"status": "unavailable"}, status=503)
        delivery_key = _delivery_key(payload, raw)
        claimed = False
        try:
            if not self._ledger.claim(delivery_key):
                return self._duplicate_delivery_response(delivery_key)
            claimed = True
            actor_id, _ = _actor(payload)
            event_type = str(payload.get("type") or "")
            action = str(payload.get("action") or "")
            data = payload.get("data")
            if not isinstance(data, dict):
                data = {}
            entity_id = str(data.get("id") or "")
            if self._linear.actor_id and hmac.compare_digest(actor_id, self._linear.actor_id):
                event_state = data.get("state")
                event_state_type = str(
                    event_state.get("type") if isinstance(event_state, dict) else ""
                ).casefold()
                if (
                    event_type == "Issue"
                    and action == "update"
                    and entity_id
                    and event_state_type in {"completed", "canceled"}
                ):
                    async with self._issue_lock(entity_id):
                        self._ledger.cancel_activation_for_issue(entity_id)
                        if self._ledger.get_manager_activation(entity_id):
                            self._ledger.mark_manager_activation(entity_id, "canceled")
                        bound_session = self._ledger.get_issue_session(entity_id)
                        # Our own close during a live turn came from that turn's tool call;
                        # cancelling it here drops the final reply and strands the session.
                        if bound_session and any(
                            self._linear_processing_owner(str(bound_session))[3:5]
                        ):
                            logger.info(
                                "[linear] self %s during live turn; keeping session=%s",
                                event_state_type, bound_session,
                            )
                        elif bound_session:
                            await self._cancel_linear_session_processing(bound_session)
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "ignored_self"}, status=200)
            notification = payload.get("notification")
            if not isinstance(notification, dict):
                notification = {}
            entity_id = str(data.get("id") or "")
            activation_status: str | None = None
            reopen_status: str | None = None
            closure_status: str | None = None
            if event_type == "Issue" and action == "update" and entity_id:
                async with self._issue_lock(entity_id):
                    event_state = data.get("state")
                    event_state_type = str(
                        event_state.get("type") if isinstance(event_state, dict) else ""
                    ).casefold()
                    if event_state_type in {"completed", "canceled"}:
                        self._ledger.cancel_activation_for_issue(entity_id)
                        if self._ledger.get_manager_activation(entity_id):
                            self._ledger.mark_manager_activation(entity_id, "canceled")
                        bound_session = self._ledger.get_issue_session(entity_id)
                        if bound_session:
                            await self._cancel_linear_session_processing(bound_session)
                    reopen_status = await self._reconcile_human_reopen(
                        payload, entity_id, _issue_locked=True
                    )
                    if reopen_status is None:
                        activation_status = await self._reconcile_planned_activation(
                            payload, entity_id, _issue_locked=True
                        )
                    closure_status = await self._reconcile_human_completion(
                        payload, entity_id, _issue_locked=True
                    )
                    previous = payload.get("updatedFrom")
                    if not isinstance(previous, dict):
                        previous = {}
                    delegate_changed = any(
                        key in previous
                        for key in ("delegate", "delegateId", "delegateMetadata")
                    )
                    if delegate_changed and not (
                        data.get("delegate") or data.get("delegateId")
                    ):
                        self._ledger.cancel_waits_for_issue(entity_id)
                        self._ledger.cancel_activation_for_issue(entity_id)
                        await self._stop_bound_turns(
                            entity_id, "linear_delegate_removed"
                        )
            if event_type == "AppUserNotification" and action == "issueUnassignedFromYou":
                notification_issue = notification.get("issue")
                if not isinstance(notification_issue, dict):
                    notification_issue = {}
                notified_issue_id = str(notification.get("issueId") or notification_issue.get("id") or "")
                if notified_issue_id:
                    self._ledger.cancel_waits_for_issue(notified_issue_id)
                    self._ledger.cancel_activation_for_issue(notified_issue_id)
                    await self._stop_bound_turns(
                        notified_issue_id, "linear_delegate_removed"
                    )
            if event_type == "OAuthApp" and action == "revoked":
                self._oauth_revoked = True
                logger.error("[linear] OAuth application access was revoked")
            target_ids = {entity_id} if entity_id else set()
            if event_type == "IssueRelation":
                for key in ("issueId", "relatedIssueId"):
                    if data.get(key):
                        target_ids.add(str(data[key]))
            if event_type in {"Issue", "IssueRelation"}:
                for target_id in target_ids:
                    await self._stop_bound_turns_if_blocked(target_id)
            resumed = 0
            if activation_status in {"activation_ambiguous", "delegation_ambiguous"} or reopen_status == "reopen_ambiguous":
                self._ledger.release(delivery_key)
                claimed = False
                return web.json_response(
                    {"status": reopen_status or activation_status}, status=503
                )
            self._ledger.mark_done(delivery_key)
            logger.info(
                "[linear] observed data event type=%s action=%s entity=%s resumed=%d activation=%s reopen=%s closure=%s subscription=%s",
                event_type,
                action,
                entity_id or "none",
                resumed,
                activation_status or "none",
                reopen_status or "none",
                closure_status or "none",
                webhook_id,
            )
            return web.json_response(
                {
                    "status": reopen_status or activation_status or closure_status or "observed",
                    "resumed": resumed,
                },
                status=200,
            )
        except Exception as exc:
            if claimed:
                try:
                    self._ledger.release(delivery_key)
                except Exception:
                    logger.exception("[linear] Failed to release data delivery %s", delivery_key)
            logger.exception("[linear] Data event reconciliation failed: %s", exc)
            return web.json_response({"status": "unavailable"}, status=503)

    def schedule_direct_activation_reconcile(self, issue_id: str) -> None:
        """Wake durable Direct reconciliation from an outbound tool thread."""
        loop = self._event_loop
        if not issue_id or loop is None or loop.is_closed() or not self._running:
            return

        def start() -> None:
            if not self._running:
                return
            task = loop.create_task(self._reconcile_direct_activation_event(issue_id))
            self._tool_progress_tasks.add(task)
            task.add_done_callback(self._tool_progress_tasks.discard)

        try:
            if asyncio.get_running_loop() is loop:
                start()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(start)

    async def _reconcile_direct_activation_event(self, issue_id: str) -> bool:
        if self._ledger is None or self._linear is None:
            return False
        async with self._issue_lock(issue_id):
            pending = self._ledger.get_direct_activation_event(issue_id)
            grant = self._ledger.get_direct_activation_grant(issue_id)
            if pending is None or pending.get("state") != "waiting" or grant is None:
                return False
            context = await self._linear.get_issue_closure_context(issue_id)
            if not self._direct_activation_policy_allows(context, grant):
                self._ledger.mark_direct_activation_event(
                    issue_id, "failed", error="direct_activation_policy_denied"
                )
                self._ledger.fail_direct_activation_grant(
                    str(grant.get("operation_key") or ""),
                    "direct_activation_policy_denied",
                )
                return False
            team_id = str((context.get("team") or {}).get("id") or "")
            session_id = str(pending.get("session_id") or "")
            if not self._ledger.claim_direct_activation(
                issue_id,
                session_id,
                actor_id=str(self._linear.actor_id or ""),
                team_id=team_id,
            ):
                return False
            if not self._ledger.mark_direct_activation_event(issue_id, "claimed"):
                return False
            dispatch_attempted = False
            try:
                async with self._session_lock(session_id):
                    if self._ledger.has_session_closure(session_id):
                        self._ledger.cancel_direct_activation_for_session(session_id)
                        return False
                    event = self._message_event(
                        pending["prompt"],
                        str(pending["delivery_key"]),
                        "direct-activation-recovery",
                        activation_resume=True,
                        direct_activation=True,
                    )
                    self._schedule_thought(
                        session_id,
                        issue_id,
                        str(pending["delivery_key"]),
                        include_queued=True,
                        body="Verified Direct instruction grant bound; Hermes resumed the native session.",
                    )
                    dispatch_attempted = True
                    await self.handle_message(event)
                    if not self._ledger.mark_direct_activation_dispatched(issue_id, session_id):
                        raise RuntimeError("direct activation dispatch state changed")
                    if not self._ledger.mark_direct_activation_event(issue_id, "dispatched"):
                        raise RuntimeError("direct activation event state changed")
                return True
            except Exception as exc:
                if dispatch_attempted:
                    self._ledger.mark_direct_activation_unknown(
                        issue_id, session_id, str(exc)
                    )
                else:
                    self._ledger.reset_direct_activation_claim(
                        issue_id, session_id, str(exc)
                    )
                logger.exception(
                    "[linear] Direct activation recovery failed issue=%s: %s",
                    issue_id,
                    exc,
                )
                return False

    async def _dependency_loop(self) -> None:
        """Low-frequency recovery path; webhook events remain the primary wake-up."""
        while self._running:
            try:
                if self._ledger is not None:
                    self._ledger.expire_orphan_direct_grants()
                    for pending in self._ledger.list_direct_activation_events():
                        try:
                            await self._reconcile_direct_activation_event(pending["issue_id"])
                        except Exception:
                            logger.exception("[linear] Direct recovery failed issue=%s", pending["issue_id"])
                await asyncio.sleep(self._dependency_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[linear] Dependency recovery loop failed: %s", exc)
                await asyncio.sleep(self._dependency_poll_seconds)

    def _repair_acceptance_thoughts(self, delivery_key: str | None = None) -> None:
        if self._ledger is None:
            return
        for receipt in self._ledger.pending_acceptance_thoughts(delivery_key):
            key = receipt["delivery_key"]
            try:
                self._schedule_thought(
                    receipt["agent_session_id"], receipt["issue_id"], key,
                    include_queued=receipt["include_queued"], acceptance_delivery_key=key,
                )
                self._ledger.mark_acceptance_thought_scheduled(key)
            except Exception:
                if delivery_key is not None:
                    raise  # HTTP replay stays 503; unrelated outbox work must not stall.
                logger.warning("[linear] Acceptance thought repair failed key=%s", key, exc_info=True)

    def _schedule_thought(
        self,
        agent_session_id: str,
        issue_id: str,
        delivery_key: str,
        *,
        include_queued: bool,
        body: str | None = None,
        acceptance_delivery_key: str = "",
    ) -> None:
        if body is None:
            actor_name = getattr(self._linear, "actor_name", None) or "Hermes"
            body = f"{actor_name} accepted the task; Hermes is processing it."
        self._enqueue_activity(
            agent_session_id,
            "thought",
            body,
            item_key=f"thought:{delivery_key}",
            metadata={"acceptance_delivery_key": acceptance_delivery_key} if acceptance_delivery_key else None,
        )
        if include_queued:
            self._enqueue_status(
                agent_session_id, issue_id, "queued", delivery_key,
                acceptance_delivery_key=acceptance_delivery_key,
            )
        self._enqueue_status(
            agent_session_id, issue_id, "running", delivery_key,
            acceptance_delivery_key=acceptance_delivery_key,
        )
        task = asyncio.create_task(self._post_thought(agent_session_id))
        bucket = self._ack_tasks.setdefault(agent_session_id, set())
        bucket.add(task)

        def _done(completed: asyncio.Task) -> None:
            current = self._ack_tasks.get(agent_session_id)
            if current is not None:
                current.discard(completed)
                if not current:
                    self._ack_tasks.pop(agent_session_id, None)

        task.add_done_callback(_done)

    @staticmethod
    def _activity_uuid(item_key: str) -> str:
        # Linear accepts client-generated activity IDs but its live validator
        # requires UUIDv4-shaped values. Derive the bytes from the stable item
        # key, then set RFC 4122 version/variant bits to v4. This preserves
        # deterministic replay without sending a UUIDv5 value.
        digest = hashlib.sha256(f"linear-hermes:{item_key}".encode()).digest()[:16]
        return str(uuid.UUID(bytes=digest, version=4))

    def _enqueue_activity(
        self,
        agent_session_id: str,
        activity_type: str,
        body: str,
        *,
        item_key: str | None = None,
        ephemeral: bool = False,
        metadata: Mapping[str, Any] | None = None,
        acceptance_snapshot: dict[str, Any] | None = None,
        atomic_final_key: bool = False,
    ) -> str:
        if self._ledger is None:
            raise RuntimeError("Linear outbox is unavailable")
        if self._ledger.has_session_closure(agent_session_id):
            suppressed_key = item_key or f"response:{uuid.uuid4()}"
            logger.info(
                "[linear] suppressed post-closure activity session=%s key=%s",
                agent_session_id,
                suppressed_key,
            )
            return self._activity_uuid(suppressed_key)
        terminal_activity = activity_type in {"response", "error", "elicitation"}
        transition_locked = False
        if terminal_activity:
            self._progress_transition_lock.acquire()
            transition_locked = True
        turn_key = ""
        try:
            if terminal_activity:
                turn_key = self._current_progress_turn_key(agent_session_id)
                if not turn_key and self._ledger is not None:
                    turn_key = self._ledger.ensure_progress_turn(
                        agent_session_id,
                        f"terminal:{self._activity_uuid(item_key or agent_session_id)}",
                    )
            if atomic_final_key:
                if activity_type != "response" or not turn_key or item_key is not None:
                    raise RuntimeError("Atomic final activity metadata is invalid")
                item_key = f"final:{agent_session_id}:{turn_key}"
            item_key = item_key or f"response:{uuid.uuid4()}"
            activity_id = self._activity_uuid(item_key)
            payload: dict[str, Any] = {
                "activity_id": activity_id,
                "agent_session_id": agent_session_id,
                "activity_type": activity_type,
                "body": body,
            }
            if turn_key:
                payload["terminal_progress_key"] = turn_key
            if ephemeral:
                payload["ephemeral"] = True
            if metadata:
                payload.update(dict(metadata))
            if acceptance_snapshot is not None:
                payload["acceptance_snapshot"] = acceptance_snapshot
            self._ledger.enqueue_outbox(
                f"activity:{item_key}",
                agent_session_id,
                "activity.transient.create" if ephemeral else "activity.create",
                payload,
            )
            self._outbox_wakeup.set()
            if terminal_activity:
                self._ledger.cancel_acceptance_thoughts(agent_session_id)
                self._notify_terminal_progress_fence(
                    agent_session_id, expected_turn_key=turn_key
                )
        finally:
            if transition_locked:
                self._progress_transition_lock.release()
        return activity_id

    def _enqueue_status(
        self,
        agent_session_id: str,
        issue_id: str,
        execution_state: str,
        delivery_key: str,
        *,
        acceptance_delivery_key: str = "",
    ) -> None:
        if (
            not self._status_writeback_enabled
            or not issue_id
            or self._ledger is None
            or self._ledger.has_session_closure(agent_session_id)
        ):
            return
        self._ledger.enqueue_outbox(
            f"status:{delivery_key}:{execution_state}",
            agent_session_id,
            "issue.state.update",
            {
                "issue_id": issue_id,
                "execution_state": execution_state,
                "acceptance_delivery_key": acceptance_delivery_key,
                "state_name": self._status_mapping[execution_state],
                "state_rank": self._status_ranks[execution_state],
                "state_ranks": {
                    self._status_mapping[state]: rank for state, rank in self._status_ranks.items()
                },
            },
        )
        self._outbox_wakeup.set()

    async def _post_thought(self, agent_session_id: str) -> None:
        try:
            # Drain the currently due sequence immediately; the background loop
            # owns any delayed retry after this fast acknowledgement attempt.
            while await self._drain_outbox_once():
                pass
        except Exception as exc:
            logger.warning("[linear] Initial thought enqueue failed for %s: %s", agent_session_id, exc)

    async def _wait_for_thought(self, agent_session_id: str) -> None:
        tasks = list(self._ack_tasks.get(agent_session_id, set()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _outbox_loop(self) -> None:
        while self._running:
            try:
                _dump_asyncio_tasks_on_request(self.database_path)
                self._repair_acceptance_thoughts()
                delivered = await self._drain_outbox_once()
                if delivered:
                    continue
                self._outbox_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._outbox_wakeup.wait(), timeout=self._outbox_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[linear] Outbox worker failed: %s", exc)
                await asyncio.sleep(self._outbox_poll_seconds)

    async def _revalidate_turn_success_item(self, item: Any) -> bool:
        """Recheck every authoritative gate at the last pre-dispatch boundary."""
        if self._ledger is None or self._linear is None:
            return False
        decision_id = str(item.payload.get("linear_turn_decision_id") or "")
        issue_id = str(item.payload.get("linear_issue_id") or "")
        turn_result = item.payload.get("linear_turn_result")
        decision = self._ledger.get_turn_decision(decision_id) if decision_id else None
        outcome = "blocked"
        error = "Delayed Linear success lost its authoritative acceptance evidence"
        if decision is not None and issue_id and isinstance(turn_result, Mapping):
            probe = MessageEvent(
                text="",
                source=SessionSource(
                    platform=self.platform,
                    chat_id=str(decision["agent_session_id"]),
                    chat_type="dm",
                ),
                internal=True,
                metadata={
                    "linear_agent_session_id": decision["agent_session_id"],
                    "linear_issue_id": issue_id,
                },
            )
            try:
                context = await self._linear.get_agent_turn_context(
                    str(decision["agent_session_id"])
                )
                live_outcome, live_reason = self._classify_turn_outcome_with_reason(
                    probe, turn_result, context
                )
                if live_outcome == "success":
                    live_context = await self._validate_activity_target(
                        str(decision["agent_session_id"])
                    )
                    self._validate_final_acceptance_snapshot(item.payload, live_context)
                    if await self._turn_success_session_matches(
                        decision, turn_result
                    ):
                        return True
                    live_outcome, live_reason = "stopped", "session_rotation"
                if live_outcome == "stopped" or (
                    live_outcome == "awaiting_input" and context.get("status") == "awaitingInput"
                ) or (
                    live_outcome == "blocked" and live_reason in _SILENT_CONTROL_REASONS
                ):
                    error = f"Delayed Linear success was fenced by live gate: {live_reason}"
                    changed = self._ledger.fence_turn_success_without_activity(
                        decision_id,
                        error,
                        response_item_id=item.id,
                        aggregate_key=item.aggregate_key,
                        progress_key=str(item.payload.get("terminal_progress_key") or ""),
                    )
                    if not changed:
                        raise LinearAPIError(
                            "Delayed Linear success could not be fenced atomically",
                            retryable=False,
                        )
                    self._notify_terminal_progress_fence(
                        item.aggregate_key,
                        expected_turn_key=str(item.payload.get("terminal_progress_key") or ""),
                    )
                    return False
                outcome = live_outcome if live_outcome in {"blocked", "stopped"} else "blocked"
                error = f"Delayed Linear success was fenced by live gate: {live_reason}"
            except LinearAPIError as exc:
                if exc.retryable:
                    # The response has already been claimed by the outbox. A
                    # transient authoritative read must leave that claim
                    # retryable; fencing it would lose an unsent success.
                    raise
                error = f"Delayed Linear success revalidation failed: {exc}"
            except Exception as exc:
                error = f"Delayed Linear success revalidation failed: {exc}"
        error_key = f"turn-success-fenced:{decision_id or item.id}"
        error_payload = {
            "activity_id": self._activity_uuid(error_key),
            "agent_session_id": item.aggregate_key,
            "activity_type": "error",
            "body": error[:4000],
        }
        changed = self._ledger.fence_claimed_turn_success(
            decision_id,
            item.id,
            f"activity:{error_key}",
            item.aggregate_key,
            error_payload,
            outcome,
            error,
        )
        if not changed:
            raise LinearAPIError(
                "Delayed Linear success could not be fenced atomically",
                retryable=False,
            )
        self._outbox_wakeup.set()
        return False

    async def _turn_success_session_matches(
        self, decision: Mapping[str, Any], turn_result: Mapping[str, Any]
    ) -> bool:
        """Resolve the stored public source and verify its live Hermes binding."""
        try:
            decision_session_id = str(decision.get("hermes_session_id") or "")
            result_session_id = str(turn_result.get("session_id") or "")
            source_snapshot = decision.get("source")
            if (
                not decision_session_id
                or not result_session_id
                or decision_session_id != result_session_id
                or not isinstance(source_snapshot, Mapping)
                or not source_snapshot
            ):
                return False
            source = self._source_from_snapshot(
                source_snapshot, str(decision.get("agent_session_id") or "")
            )
            if str(source.chat_id or "") != str(
                decision.get("agent_session_id") or ""
            ):
                return False
            extra = getattr(self.config, "extra", None) or {}
            session_key = build_session_key(
                source,
                group_sessions_per_user=extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
                profile=self._session_key_profile(source),
            )
            store = getattr(
                getattr(self, "gateway_runner", None), "async_session_store", None
            )
            lookup = getattr(store, "lookup_by_session_key", None)
            if not callable(lookup):
                return False
            entry = await lookup(session_key)
            current_session_id = str(getattr(entry, "session_id", "") or "")
            return bool(
                entry is not None
                and str(getattr(entry, "session_key", "") or "") == session_key
                and current_session_id == decision_session_id
                and current_session_id == result_session_id
            )
        except Exception as exc:
            logger.warning(
                "[linear] authoritative Hermes success binding failed closed: %s", exc
            )
            return False

    async def _drain_outbox_once(self) -> bool:
        if self._ledger is None or self._linear is None:
            return False
        async with self._outbox_drain_lock:
            item = self._ledger.claim_due_outbox(
                include_continuations=False
            )
            if item is None:
                return False
            acceptance_key = item.payload.get("acceptance_delivery_key")
            if acceptance_key and not self._ledger.acceptance_thought_is_current(acceptance_key):
                self._ledger.mark_outbox_delivered(item.id)
                return True
            turn_success_item = item.id.startswith("activity:turn-success:")
            if item.payload.get("activity_type") == "response" and item.attempts > 1:
                # A prior create may already have completed the vendor session.
                # Reconcile only that immutable response; never replay the work
                # or require its now-terminal session to admit new execution.
                try:
                    verified = await self._linear.verify_response_receipt(
                        item.payload["activity_id"], item.aggregate_key, item.payload["body"]
                    )
                    if not verified and item.payload.get("response_create_acknowledged") is True:
                        raise LinearAPIError("Acknowledged response receipt not visible yet", retryable=True)
                except LinearAPIError as exc:
                    if exc.retryable and item.attempts < 8:
                        delay = min(self._outbox_max_delay,
                                    self._outbox_base_delay * 2 ** min(item.attempts - 1, 16))
                        self._ledger.reschedule_outbox(item.id, str(exc), delay)
                    else:
                        self._ledger.dead_letter_outbox(item.id, str(exc))
                    return True
                if verified:
                    self._ledger.mark_outbox_delivered(item.id)
                    return True
            if turn_success_item:
                try:
                    valid = await self._revalidate_turn_success_item(item)
                except LinearAPIError as exc:
                    if not exc.retryable:
                        raise
                    exponent = min(max(item.attempts - 1, 0), 16)
                    delay = min(
                        self._outbox_max_delay,
                        self._outbox_base_delay * (2**exponent),
                    )
                    self._ledger.reschedule_outbox(item.id, str(exc), delay)
                    logger.warning(
                        "[linear] Success revalidation retry id=%s attempts=%d delay=%.1fs: %s",
                        item.id,
                        item.attempts,
                        delay,
                        exc,
                    )
                    return True
                if not valid:
                    return True
            if self._closure_reconciliation_enabled and item.operation == "issue.state.update":
                self._ledger.mark_outbox_delivered(item.id)
                logger.info(
                    "[linear] suppressed state writeback in closure-safe mode item=%s",
                    item.id,
                )
                return True
            if (
                self._ledger.has_session_closure(item.aggregate_key)
                and not item.id.startswith("activity:closure:")
            ):
                self._ledger.mark_outbox_delivered(item.id)
                logger.info(
                    "[linear] suppressed claimed post-closure outbox item=%s session=%s",
                    item.id,
                    item.aggregate_key,
                )
                return True
            try:
                if item.operation in {"activity.create", "activity.transient.create"}:
                    if item.payload.get("visibility_decision_id"):
                        # The outbox lock also serializes native Stop fences.
                        # Never acquire the session lock here: inline commands
                        # can await this drain while already holding that lock.
                        if not await self._visible_turn_activity_is_live(item):
                            if not self._ledger.update_outbox_payload_metadata(
                                item.id, {"visibility_suppressed": True}
                            ):
                                raise LinearAPIError("Visibility suppression was not recorded", retryable=True)
                            self._ledger.mark_outbox_delivered(item.id)
                            return True
                        await self._linear.create_activity(
                            item.payload["agent_session_id"],
                            item.payload["activity_type"],
                            item.payload["body"],
                            activity_id=item.payload["activity_id"],
                            ephemeral=False,
                        )
                        self._ledger.mark_outbox_delivered(item.id)
                        return True
                    orphan_id = item.payload.get("orphan_success_decision_id")
                    if orphan_id:
                        decision = self._ledger.get_turn_decision(str(orphan_id))
                        try:
                            context = await self._linear.get_agent_turn_context(item.aggregate_key)
                            allowed = decision is not None and self._orphan_success_activity_allowed(decision, context)
                        except LinearAPIError:
                            raise
                        except Exception:
                            allowed = False
                        if not allowed:
                            self._ledger.mark_outbox_delivered(item.id)
                            return True
                    if (
                        not item.id.startswith("activity:closure:")
                        and not turn_success_item
                    ):
                        live_context = await self._validate_activity_target(
                            item.payload["agent_session_id"]
                        )
                        if (
                            live_context.get("status") == "complete"
                            and not item.id.startswith("activity:closure-error:")
                        ):
                            if item.payload.get("activity_type") == "response":
                                raise LinearAPIError(
                                    "Completed session has no verified response receipt",
                                    retryable=False,
                                )
                            suppression = {"terminal_suppressed": True, "terminal_session_status": "complete"}
                            if not self._ledger.update_outbox_payload_metadata(item.id, suppression):
                                raise LinearAPIError("Terminal suppression was not recorded", retryable=True)
                            self._ledger.mark_outbox_delivered(item.id)
                            logger.info(
                                "[linear] suppressed stale post-terminal activity item=%s session=%s",
                                item.id, item.aggregate_key,
                            )
                            return True
                        if item.payload.get("activity_type") == "response":
                            if (
                                "acceptance_snapshot" in item.payload
                                or item.id.startswith("activity:final:")
                            ):
                                self._validate_final_acceptance_snapshot(
                                    item.payload, live_context
                                )
                            elif item.id.startswith("activity:creator-owned:"):
                                issue_id = live_context.get("issue_id")
                                issue = (
                                    await self._linear.get_issue_closure_context(issue_id)
                                    if isinstance(issue_id, str) and issue_id else {}
                                )
                                if not (
                                    item.operation == "activity.create"
                                    and item.aggregate_key == item.payload.get("agent_session_id")
                                    and item.payload.get("activity_id") == self._activity_uuid(
                                        item.id.removeprefix("activity:")
                                    )
                                    and item.payload.get("body") == (
                                        "Creator-agent manages this child through the MCP lifecycle; "
                                        "native session closed."
                                    )
                                    and issue.get("id") == issue_id
                                    and (issue.get("parent") or {}).get("id")
                                    and self._linear.actor_id
                                    and hmac.compare_digest(
                                        str((issue.get("creator") or {}).get("id") or ""),
                                        self._linear.actor_id,
                                    )
                                ):
                                    raise LinearAPIError(
                                        "Creator-owned control response is no longer authorized",
                                        retryable=False,
                                    )
                            elif (
                                not isinstance(live_context.get("description"), str)
                                or not all(
                                    isinstance(live_context.get(key), str) and live_context[key]
                                    for key in ("issue_id", "updated_at", "delegate_id")
                                )
                                or not acceptance_gate(
                                    live_context["description"], set()
                                ).allowed
                            ):
                                raise LinearAPIError("Final acceptance snapshot is missing", retryable=False)
                    # Recheck after the final awaited target validation and
                    # immediately before vendor create: the waiter or turn
                    # may disappear while that validation is in flight.
                    acceptance_key = item.payload.get("acceptance_delivery_key")
                    if acceptance_key and not self._ledger.acceptance_thought_is_current(acceptance_key):
                        self._ledger.mark_outbox_delivered(item.id)
                        return True
                    await self._linear.create_activity(
                        item.payload["agent_session_id"],
                        item.payload["activity_type"],
                        item.payload["body"],
                        activity_id=item.payload["activity_id"],
                        ephemeral=bool(item.payload.get("ephemeral", False)),
                    )
                    if item.payload["activity_type"] == "response":
                        if not self._ledger.update_outbox_payload_metadata(
                            item.id, {"response_create_acknowledged": True}
                        ):
                            raise LinearAPIError("Response acknowledgment persistence failed", retryable=True)
                        verified = await self._linear.verify_response_receipt(
                            item.payload["activity_id"],
                            item.payload["agent_session_id"],
                            item.payload["body"],
                        )
                        if not verified:
                            raise LinearAPIError("Response receipt not visible yet", retryable=True)
                elif item.operation == "issue.state.update":
                    await self._linear.update_issue_state(
                        item.payload["issue_id"],
                        item.payload["state_name"],
                        int(item.payload["state_rank"]),
                        item.payload["state_ranks"],
                    )
                else:
                    raise LinearAPIError(f"Unknown outbox operation: {item.operation}")
            except LinearAPIError as exc:
                if exc.retryable:
                    delay = exc.retry_after
                    if delay is None:
                        exponent = min(max(item.attempts - 1, 0), 16)
                        delay = min(self._outbox_max_delay, self._outbox_base_delay * (2**exponent))
                    self._ledger.reschedule_outbox(item.id, str(exc), float(delay))
                    logger.warning(
                        "[linear] Outbox retry id=%s attempts=%d delay=%.1fs: %s",
                        item.id,
                        item.attempts,
                        delay,
                        exc,
                    )
                else:
                    cleanup_inserted = self._ledger.dead_letter_outbox(
                        item.id,
                        str(exc),
                        closure_cleanup_activity_id=self._activity_uuid(
                            f"closure-error:{item.id}"
                        ),
                        closure_cleanup_body=(
                            "⚠️ The closure response could not be published; "
                            "operational intervention is required."
                        ),
                    )
                    if cleanup_inserted:
                        self._outbox_wakeup.set()
                    logger.error("[linear] Outbox dead letter id=%s: %s", item.id, exc)
            except Exception as exc:
                if _ledger_busy(exc):
                    exponent = min(max(item.attempts - 1, 0), 16)
                    delay = min(self._outbox_max_delay, self._outbox_base_delay * (2**exponent))
                    self._ledger.reschedule_outbox(item.id, str(exc), delay)
                    logger.warning("[linear] Outbox ledger retry id=%s delay=%.1fs: %s", item.id, delay, exc)
                    return True
                cleanup_inserted = self._ledger.dead_letter_outbox(
                    item.id,
                    str(exc),
                    closure_cleanup_activity_id=self._activity_uuid(
                        f"closure-error:{item.id}"
                    ),
                    closure_cleanup_body=(
                        "⚠️ The closure response could not be published; "
                        "operational intervention is required."
                    ),
                )
                if cleanup_inserted:
                    self._outbox_wakeup.set()
                logger.exception("[linear] Outbox dead letter id=%s: %s", item.id, exc)
            else:
                self._ledger.mark_outbox_delivered(item.id)
            return True

    async def _visible_turn_activity_is_live(self, item: Any) -> bool:
        if self._ledger is None or self._linear is None:
            return False
        decision = self._ledger.get_turn_decision(
            str(item.payload.get("visibility_decision_id") or "")
        )
        if decision is None or decision["agent_session_id"] != item.aggregate_key:
            return False
        expected = "blocked" if item.payload.get("activity_type") == "error" else "continue"
        if decision["outcome"] != expected or decision["dispatch_state"] not in (
            {"fenced"} if expected == "blocked" else {"completed", "enqueued", "running"}
        ):
            return False
        if expected == "blocked" and decision["error"] != "native_goal_paused":
            return False
        if self._ledger.has_session_closure(item.aggregate_key):
            return False
        progress_key = str(item.payload.get("terminal_progress_key") or "")
        if progress_key and progress_key != self._current_progress_turn_key(item.aggregate_key):
            return False
        context = await self._linear.get_agent_turn_context(item.aggregate_key)
        issue = context.get("issue") or {}
        if (
            context.get("id") != item.aggregate_key
            or context.get("status") != "active"
            or not self._linear.actor_id
            or context.get("app_user_id") != self._linear.actor_id
            or issue.get("id") != decision["issue_id"]
            or (issue.get("delegate") or {}).get("id") != self._linear.actor_id
            or (issue.get("state") or {}).get("type") not in {"started", "unstarted"}
            or context.get("open_blockers")
        ):
            return False
        if not await self._turn_success_session_matches(
            decision, {"session_id": decision["hermes_session_id"]}
        ):
            return False
        if expected == "blocked":
            source = self._source_from_snapshot(decision["source"], item.aggregate_key)
            state = await self._goal_state_for_source(source, decision["hermes_session_id"])
            return bool(
                state is not None
                and getattr(state, "status", "") == "paused"
                and getattr(state, "last_verdict", "") == "blocked"
                and str(getattr(state, "paused_reason", "") or "").startswith("judged unachievable:")
            )
        return True

    async def _validate_activity_target(self, agent_session_id: str) -> dict[str, Any]:
        """Fail closed when a normal activity target changed app-user owner."""
        if self._linear is None:
            raise LinearAPIError("Linear client is unavailable", retryable=True)
        try:
            async with asyncio.timeout(4.0):
                context = await self._linear.get_agent_session_delivery_context(agent_session_id)
        except TimeoutError as exc:
            raise LinearAPIError("Linear activity owner read timed out", retryable=True) from exc
        if not self._linear.actor_id or not hmac.compare_digest(
            str(context.get("app_user_id") or ""), self._linear.actor_id
        ):
            raise LinearAPIError(
                "Linear Agent Session delivery target is owned by another app user",
                retryable=False,
            )
        return context

    def _validate_final_acceptance_snapshot(
        self,
        payload: dict[str, Any],
        live_context: dict[str, Any],
    ) -> None:
        """Revalidate one durable final against live issue acceptance state."""
        snapshot = payload.get("acceptance_snapshot")
        required = {
            "issue_id", "updated_at", "delegate_id", "criterion_hashes", "evidence_hashes"
        }
        if not isinstance(snapshot, dict) or set(snapshot) != required:
            raise LinearAPIError("Final acceptance snapshot is missing", retryable=False)
        criterion_hashes = snapshot.get("criterion_hashes")
        evidence_hashes = snapshot.get("evidence_hashes")
        if (
            not isinstance(criterion_hashes, list)
            or not isinstance(evidence_hashes, list)
            or any(not isinstance(value, str) or not value for value in criterion_hashes)
            or any(not isinstance(value, str) or not value for value in evidence_hashes)
            or criterion_hashes != sorted(set(criterion_hashes))
            or evidence_hashes != sorted(set(evidence_hashes))
        ):
            raise LinearAPIError("Final acceptance snapshot is malformed", retryable=False)
        live_issue_id = str(live_context.get("issue_id") or "")
        live_revision = str(live_context.get("updated_at") or "")
        live_delegate = str(live_context.get("delegate_id") or "")
        live_criteria = acceptance_criteria(str(live_context.get("description") or ""))
        live_criterion_hashes = sorted(item.criterion_hash for item in live_criteria)
        actor_id = str(self._linear.actor_id or "") if self._linear is not None else ""
        live_evidence_hashes = sorted(
            self._ledger.acceptance_evidence_hashes(
                live_issue_id,
                actor_id,
                accepted_revision=live_revision,
            )
        ) if self._ledger is not None else []
        gate = acceptance_gate(
            str(live_context.get("description") or ""),
            set(live_evidence_hashes),
        )
        exact = (
            hmac.compare_digest(str(snapshot.get("issue_id") or ""), live_issue_id)
            and hmac.compare_digest(str(snapshot.get("updated_at") or ""), live_revision)
            and hmac.compare_digest(str(snapshot.get("delegate_id") or ""), live_delegate)
            and criterion_hashes == live_criterion_hashes
            and evidence_hashes == live_evidence_hashes
            and (not live_criteria or hmac.compare_digest(live_delegate, actor_id))
            and gate.allowed
        )
        if not exact:
            raise LinearAPIError("Final acceptance snapshot is stale", retryable=False)

    def _core_goals_available(self) -> bool:
        """Only the retired fork core exposed gateway goal operations.

        Upstream Hermes runs one model turn per admitted Linear prompt: no
        native goal is created, judged, resumed or continued.
        """
        runner = getattr(self, "gateway_runner", None)
        return callable(getattr(runner, "goal_state_for_source", None))

    def _goal_operation(self, name: str) -> Callable[..., Any]:
        operation = getattr(getattr(self, "gateway_runner", None), name, None)
        if not callable(operation):
            raise RuntimeError(f"Hermes gateway goal operation is unavailable: {name}")
        return operation

    async def _goal_state_for_source(
        self, source: SessionSource, hermes_session_id: str
    ) -> Any:
        if not self._core_goals_available():
            return None
        return await self._goal_operation("goal_state_for_source")(
            source, session_id=hermes_session_id
        )

    def _classify_turn_outcome_with_reason(
        self,
        event: MessageEvent,
        turn_result: Any,
        context: dict[str, Any],
    ) -> tuple[str, str]:
        """Classify live and structured evidence; every ambiguity is blocked."""
        def blocked(reason: str) -> tuple[str, str]:
            return "blocked", reason

        if self._ledger is None or self._linear is None:
            return blocked("unverified")
        session_id = str(event.metadata.get("linear_agent_session_id") or "")
        issue_id = str(event.metadata.get("linear_issue_id") or "")
        if not session_id or not issue_id or not isinstance(turn_result, Mapping):
            return blocked("turn_result_invalid")
        required = {"completed", "failed", "interrupted", "turn_exit_reason", "session_id"}
        if not required.issubset(turn_result):
            return blocked("turn_result_invalid")
        if any(type(turn_result[key]) is not bool for key in ("completed", "failed", "interrupted")):
            return blocked("turn_result_invalid")
        if self._ledger.has_session_closure(session_id):
            return "stopped", "stopped"
        if str(event.metadata.get("linear_signal") or "").casefold() == "stop":
            return "stopped", "stopped"
        if str(context.get("id") or "") != session_id:
            return blocked("session_context_mismatch")
        if not self._linear.actor_id or not hmac.compare_digest(
            str(context.get("app_user_id") or ""), self._linear.actor_id
        ):
            return "stopped", "actor_mismatch"
        issue = context.get("issue")
        if not isinstance(issue, dict) or str(issue.get("id") or "") != issue_id:
            return blocked("issue_mismatch")
        delegate = issue.get("delegate")
        if not isinstance(delegate, dict) or not self._linear.actor_id or not hmac.compare_digest(
            str(delegate.get("id") or ""), self._linear.actor_id
        ):
            return "stopped", "actor_mismatch"
        state = issue.get("state")
        if not isinstance(state, dict):
            return blocked("issue_state_invalid")
        state_type = str(state.get("type") or "").casefold()
        status = str(context.get("status") or "")
        reason = str(turn_result.get("turn_exit_reason") or "").casefold()
        if state_type == "canceled":
            return "stopped", "stopped"
        if state_type == "completed":
            return "stopped", "stopped"
        if state_type not in {"unstarted", "started"}:
            return blocked("issue_state_invalid")
        if turn_result["interrupted"] or reason in {"stopped", "stop", "cancelled", "canceled"}:
            return "stopped", "stopped"
        if turn_result["failed"]:
            return blocked("turn_failed")
        if status == "error":
            return blocked("session_error")
        if reason in {"failed", "error", "blocked"}:
            return blocked("error_exit_reason")
        # A typed human wait is an elicitation even when another live field is
        # also conservative (for example, an open dependency is present).
        # Do not turn the question into a generic safety error at ingress.
        if reason in {"approval", "awaiting_approval", "requires_approval"}:
            return "approval", "approval"
        if status == "awaitingInput" or reason in {"awaiting_input", "awaitinginput", "elicitation"}:
            return "awaiting_input", "awaiting_input"
        if context.get("open_blockers"):
            return blocked("open_blockers")
        if status == "stale":
            if event.metadata.get("linear_dependency_resume"):
                # A dependency wake must prove its untouched native wait and
                # deliver progress before stale can become executable again.
                return "awaiting_input", "dependency_stale"
            return "stopped", "stopped"
        if status == "complete":
            return blocked("status_invalid")
        if status not in {"pending", "active"}:
            return blocked("status_invalid")
        if turn_result["completed"] and self._acceptance_is_fully_checked(issue):
            evidence = self._ledger.acceptance_evidence_hashes(
                issue_id, self._linear.actor_id,
                accepted_revision=str(issue.get("updatedAt") or ""),
            )
            gate = acceptance_gate(str(issue.get("description") or ""), evidence)
            if gate.criteria and gate.allowed:
                return "success", "success"
        if not turn_result["completed"] and not reason.startswith(
            "max_iterations_reached("
        ):
            return blocked("incomplete_exit_reason")
        return "continue", "continue"

    def _classify_turn_outcome(
        self, event: MessageEvent, turn_result: Any, context: dict[str, Any]
    ) -> str:
        """Compatibility wrapper retaining the historical outcome-only API."""
        return self._classify_turn_outcome_with_reason(event, turn_result, context)[0]

    @staticmethod
    def _acceptance_checkbox_matches(issue: dict[str, Any]) -> list[re.Match[str]]:
        description = str(issue.get("description") or "")
        lines = description.splitlines()
        tokens = _COMMONMARK.parse(description)
        acceptance_positions: list[int] = []
        h2_positions: list[int] = []
        for index, token in enumerate(tokens):
            if (
                token.type != "heading_open"
                or token.tag != "h2"
                or token.level != 0
                or not token.map
            ):
                continue
            h2_positions.append(int(token.map[0]))
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            heading = str(getattr(inline, "content", "") or "")
            name = " ".join(heading.casefold().split())
            if name in _ACCEPTANCE_H2_NAMES:
                acceptance_positions.append(int(token.map[0]))
        if len(acceptance_positions) != 1:
            return []
        section_start = acceptance_positions[0]
        section_end = next(
            (position for position in h2_positions if position > section_start),
            len(lines),
        )
        matches: list[re.Match[str]] = []
        seen_lines: set[int] = set()
        for token in tokens:
            if token.type != "list_item_open" or not token.map:
                continue
            line_number = int(token.map[0])
            if not section_start < line_number < section_end or line_number in seen_lines:
                continue
            seen_lines.add(line_number)
            match = _ACCEPTANCE_CHECKBOX_RE.fullmatch(lines[line_number])
            if match is not None:
                matches.append(match)
        return matches

    @classmethod
    def _acceptance_is_fully_checked(cls, issue: dict[str, Any]) -> bool:
        matches = cls._acceptance_checkbox_matches(issue)
        return bool(matches) and all(match.group(1).casefold() == "x" for match in matches)

    def _session_key_profile(self, source: SessionSource) -> str | None:
        stamped = str(getattr(source, "profile", "") or "").strip()
        return stamped or None

    def _source_from_snapshot(
        self, snapshot: Mapping[str, Any], agent_session_id: str
    ) -> SessionSource:
        values = {
            field.name: snapshot[field.name]
            for field in fields(SessionSource)
            if field.name in snapshot and field.name != "platform"
        }
        values["platform"] = Platform(
            str(snapshot.get("platform") or self.platform.value)
        )
        values["chat_id"] = str(snapshot.get("chat_id") or agent_session_id)
        return SessionSource(**values)

    def _linear_processing_owner(self, session_id: str) -> tuple[Any, ...]:
        source = self.build_source(
            chat_id=session_id,
            chat_name="Linear",
            chat_type="dm",
            user_id="linear-control-plane",
            user_name="Linear control plane",
            message_id=f"terminal:{session_id}",
            role_authorized=True,
        )
        extra = getattr(self.config, "extra", None) or {}
        session_key = build_session_key(
            source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
            profile=self._session_key_profile(source),
        )
        active_event = self._active_turn_events.get(str(session_id))
        owner_task = self._session_tasks.get(session_key)
        owner_guard = self._active_sessions.get(session_key)
        owner_generation = getattr(owner_guard, "_hermes_run_generation", None)
        return source, session_key, active_event, owner_task, owner_guard, owner_generation

    async def _cancel_linear_session_processing(self, session_id: str) -> None:
        (source, session_key, active_event, owner_task,
         owner_guard, owner_generation) = self._linear_processing_owner(session_id)
        owner_session_id = (
            str(active_event.metadata.get("gateway_session_id") or "") if active_event else ""
        ) or None
        try:
            if self._ledger is not None:
                self._ledger.cancel_acceptance_thoughts(session_id)
        finally:
            # Failed persistence keeps HTTP retryable, never execution alive.
            interrupt = getattr(self.gateway_runner, "interrupt_session_processing", None)
            try:
                owner_changed = (
                    (owner_task is not None and self._session_tasks.get(session_key) is not owner_task)
                    or (owner_guard is not None and self._active_sessions.get(session_key) is not owner_guard)
                )
                if callable(interrupt) and not owner_changed:
                    # Use the runner's source-scoped async seam and pin the Hermes
                    # identity when this turn has one.
                    interrupt_source = active_event.source if active_event is not None else source
                    await interrupt(
                        interrupt_source,
                        reason="linear_authoritative_stop",
                        expected_session_id=owner_session_id,
                        expected_run_generation=owner_generation,
                    )
            finally:
                if owner_task is None and owner_guard is None:
                    if (self._session_tasks.get(session_key) is None
                            and self._active_sessions.get(session_key) is None):
                        await self.cancel_session_processing(session_key)
                else:
                    await self._cancel_owned_session_processing(
                        session_key, owner_task, owner_guard
                    )

    async def _cancel_owned_session_processing(
        self, session_key: str, owner_task: Any, owner_guard: Any
    ) -> None:
        if self._accepts_kwarg(
            self.cancel_session_processing, "expected_task", var_kw=False, unknown=False
        ):
            await self.cancel_session_processing(
                session_key, expected_task=owner_task, expected_guard=owner_guard,
            )
        elif (self._session_tasks.get(session_key) in (None, owner_task)
                and self._active_sessions.get(session_key) in (None, owner_guard)):
            # Upstream cancel has no owner fence; never cancel a newer turn.
            await self.cancel_session_processing(session_key)

    async def _stop_bound_turns(self, issue_id: str, reason: str) -> bool:
        if self._ledger is None:
            return False
        session_id = self._ledger.get_issue_session(issue_id)
        if not session_id:
            return False
        await self._cancel_linear_session_processing(session_id)
        return True

    async def _stop_bound_turns_if_blocked(self, issue_id: str) -> bool:
        if self._ledger is None or self._linear is None:
            return False
        session_id = self._ledger.get_issue_session(issue_id)
        if not session_id:
            return False
        if not await self._linear.get_open_blockers(issue_id):
            return False
        return await self._stop_bound_turns(issue_id, "linear_issue_blocked")

    def _orphan_success_activity_allowed(self, row: dict[str, Any], context: dict[str, Any]) -> bool:
        probe = MessageEvent(
            text="", internal=True,
            source=self._source_from_snapshot(row.get("source") or {}, row["agent_session_id"]),
            metadata={
                "linear_agent_session_id": row["agent_session_id"],
                "linear_issue_id": row["issue_id"],
            },
        )
        gate = self._classify_turn_outcome(probe, {
            "completed": False, "failed": False, "interrupted": False,
            "turn_exit_reason": "max_iterations_reached(recovery)",
            "session_id": row["hermes_session_id"],
        }, context)
        issue = context.get("issue") or {}
        owned_open = (
            context.get("id") == row["agent_session_id"]
            and context.get("status") in _OPEN_AGENT_SESSION_STATUSES
            and bool(self._linear.actor_id)
            and context.get("app_user_id") == self._linear.actor_id
            and issue.get("id") == row["issue_id"]
            and (issue.get("delegate") or {}).get("id") == self._linear.actor_id
            and gate != "stopped"
            and not self._ledger.has_session_closure(row["agent_session_id"])
        )
        return owned_open

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
    ) -> SendResult:
        del reply_to
        if self._linear is None or self._ledger is None:
            return SendResult(success=False, error="Linear outbox is unavailable", retryable=True)
        await self._wait_for_thought(chat_id)
        if self._ledger.has_session_closure(chat_id):
            return SendResult(
                success=True,
                message_id=self._activity_uuid(f"suppressed:closure:{chat_id}"),
            )
        transient_progress = bool(
            isinstance(metadata, dict) and metadata.get("transient_progress") is True
        )
        # The native gateway marks interim sends structurally. Keep the exact
        # legacy heartbeat matcher only for older senders lacking that marker.
        long_running_heartbeat = bool(
            (isinstance(metadata, dict) and metadata.get("_interim_send") is True)
            or _LINEAR_LONG_RUNNING_HEARTBEAT_RE.fullmatch(content)
            or _LINEAR_CORE_BUDGET_NOTICE_RE.fullmatch(content)
        )
        trusted_ephemeral_notice = content in _LINEAR_EPHEMERAL_NOTICES
        try:
            delivery_context = await self._validate_activity_target(chat_id)
            if (
                (long_running_heartbeat or trusted_ephemeral_notice)
                and not self._progress_chat_is_allowed(chat_id)
            ):
                digest = hashlib.sha256(content.encode()).hexdigest()[:24]
                return SendResult(
                    success=True,
                    message_id=self._activity_uuid(
                        f"suppressed:terminal-heartbeat:{chat_id}:{digest}"
                    ),
                )
            nonterminal_progress = (
                transient_progress or long_running_heartbeat or trusted_ephemeral_notice
            )
            transient_progress_key = ""
            if transient_progress:
                transient_progress_key = str(
                    metadata.get("transient_progress_key") or ""
                ).strip()
                if not transient_progress_key:
                    raise LinearAPIError(
                        "Transient Linear progress requires a trusted turn key",
                        retryable=False,
                    )
                if not self._progress_is_allowed(chat_id, transient_progress_key):
                    digest = hashlib.sha256(content.encode()).hexdigest()[:24]
                    return SendResult(
                        success=True,
                        message_id=self._activity_uuid(
                            f"suppressed:terminal:{chat_id}:{transient_progress_key}:{digest}"
                        ),
                    )
            activity_type = "thought" if (
                nonterminal_progress
                or content.startswith(_LINEAR_HOME_CHANNEL_NOTICE_PREFIX)
            ) else "response"
            item_key = None
            acceptance_snapshot: dict[str, Any] | None = None
            atomic_final_key = False
            if activity_type == "response":
                issue_id = str(delivery_context.get("issue_id") or "")
                delegate_id = str(delivery_context.get("delegate_id") or "")
                actor_id = str(self._linear.actor_id or "")
                description = str(delivery_context.get("description") or "")
                evidence_hashes = self._ledger.acceptance_evidence_hashes(
                    issue_id,
                    actor_id,
                    accepted_revision=str(delivery_context.get("updated_at") or ""),
                )
                criteria = acceptance_criteria(description)
                current_revision = str(delivery_context.get("updated_at") or "")
                required_hashes = {item.criterion_hash for item in criteria}
                gate = acceptance_gate(description, evidence_hashes)
                gate_reason = gate.reason
                if gate.criteria and (
                    not delegate_id or not hmac.compare_digest(delegate_id, actor_id)
                ):
                    gate_reason = "acceptance_delegate_mismatch"
                if gate_reason not in {"no_acceptance_criteria", "acceptance_complete"}:
                    activity_type = "error"
                    content = (
                        "Acceptance final gate blocked the success response: "
                        f"{gate_reason}. Unverified criteria remain open; no completion was delivered."
                    )
                    item_key = (
                        f"acceptance-gate:{chat_id}:"
                        f"{delivery_context.get('updated_at') or 'unknown'}:{gate_reason}"
                    )
                else:
                    acceptance_snapshot = {
                        "issue_id": issue_id,
                        "updated_at": current_revision,
                        "delegate_id": delegate_id,
                        "criterion_hashes": sorted(required_hashes),
                        "evidence_hashes": sorted(evidence_hashes),
                    }
                    atomic_final_key = True
            if transient_progress:
                digest = hashlib.sha256(content.encode()).hexdigest()[:24]
                item_key = f"progress:{chat_id}:{transient_progress_key}:{digest}"
            elif long_running_heartbeat:
                digest = hashlib.sha256(content.encode()).hexdigest()[:24]
                item_key = f"heartbeat:{chat_id}:{digest}"
            activity_id = self._enqueue_activity(
                chat_id,
                activity_type,
                content,
                item_key=item_key,
                ephemeral=nonterminal_progress,
                acceptance_snapshot=acceptance_snapshot,
                atomic_final_key=atomic_final_key,
            )

            if nonterminal_progress and item_key is not None:
                item = self._ledger.get_outbox_item(f"activity:{item_key}")
                if item is None:
                    raise LinearAPIError(
                        "Transient Linear progress was not durably accepted",
                        retryable=True,
                    )
                if item["state"] == "dead":
                    raise LinearAPIError(
                        "Transient Linear progress is dead-lettered",
                        retryable=False,
                    )
            await self._drain_outbox_once()
            if nonterminal_progress and item_key is not None:
                item = self._ledger.get_outbox_item(f"activity:{item_key}")
                if item is not None and item["state"] == "dead":
                    raise LinearAPIError(
                        "Transient Linear progress is dead-lettered",
                        retryable=False,
                    )
            if activity_type == "error" and item_key and item_key.startswith("acceptance-gate:"):
                return SendResult(
                    success=False,
                    message_id=activity_id,
                    error="Acceptance final gate rejected the success response",
                    retryable=False,
                )
            # Success means durably accepted. The outbox owns transport retries.
            return SendResult(success=True, message_id=activity_id)
        except LinearAPIError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

    def _trusted_native_command_requester(self, event: MessageEvent) -> bool:
        """Require an exact active human source before any native slash dispatch."""
        source = event.source
        if source is None or bool(event.metadata.get("linear_direct_activation")):
            return False
        session_id = str(event.metadata.get("linear_agent_session_id") or source.chat_id or "")
        active = self._active_turn_events.get(session_id)
        if (
            active is None
            or active.source is None
            or bool(active.metadata.get("linear_direct_activation"))
        ):
            return False
        active_id = str(active.source.user_id or "")
        incoming_id = str(source.user_id or "")
        if not active_id or not incoming_id:
            return False
        return (
            hmac.compare_digest(active_id, incoming_id)
            and str(active.metadata.get("linear_agent_session_id") or "") == session_id
            and str(active.metadata.get("linear_issue_id") or "")
            == str(event.metadata.get("linear_issue_id") or "")
        )

    def _notify_terminal_progress_fence(
        self, chat_id: str, *, expected_turn_key: str
    ) -> None:
        ledger = self._ledger
        if ledger is not None:
            ledger.fence_progress_turn(chat_id, expected_turn_key)
        with self._progress_state_lock:
            current = self._progress_turns.get(chat_id)
            if current is None and expected_turn_key:
                self._progress_turns[chat_id] = (expected_turn_key, True)
            elif current is not None and current[0] == expected_turn_key:
                self._progress_turns[chat_id] = (expected_turn_key, True)
            while len(self._progress_turns) > _PROGRESS_TURN_STATE_LIMIT:
                self._progress_turns.pop(next(iter(self._progress_turns)))
        callback = self._terminal_progress_callback
        if callback is None:
            return
        try:
            callback(chat_id, turn_key=expected_turn_key, adapter=self)
        except Exception:
            logger.warning("[linear] Terminal progress fence callback failed", exc_info=True)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if self._ledger is None or event.source is None:
            return
        # Never clear a newer event's turn state.
        if (outcome != ProcessingOutcome.SUCCESS
                and self._active_turn_events.get(event.source.chat_id) is event):
            self._active_turn_events.pop(event.source.chat_id, None)
        await self._wait_for_thought(event.source.chat_id)
        if self._ledger.has_session_closure(event.source.chat_id):
            logger.info(
                "[linear] suppressed processing completion after human closure session=%s",
                event.source.chat_id,
            )
            return
        delivery_key = str(event.metadata.get("linear_delivery_key") or event.message_id or uuid.uuid4())
        if outcome == ProcessingOutcome.FAILURE:
            self._enqueue_activity(
                event.source.chat_id,
                "error",
                "Hermes encountered an error while processing the task. The issue state was preserved for retry or human triage.",
                item_key=f"error:{delivery_key}",
            )

        # SUCCESS preserves the issue state for the human final-acceptance gate.
        # The durable response activity carries the evidence Mutlu reviews before
        # moving the issue to Done/Completed. FAILURE and CANCELLED also preserve
        # the current state; neither is evidence for a terminal transition.
        await self._drain_outbox_once()
        if event.source is not None and outcome == ProcessingOutcome.SUCCESS:
            if self._active_turn_events.get(event.source.chat_id) is event:
                self._active_turn_events.pop(event.source.chat_id, None)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": str(chat_id), "name": "Linear Agent Session", "type": "dm"}

    @staticmethod
    def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
        """Keep Markdown image URLs inside Linear's response body."""
        return [], content
