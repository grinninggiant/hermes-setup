from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from linear_tools import (  # noqa: E402
    ADMIN_TRASH_SCHEMA,
    ADMIN_TRASH_PREVIEW_SCHEMA,
    _ADMIN_TRASH_APPROVALS,
    _ADMIN_TRASH_PENDING,
    _ADMIN_TRASH_PREVIEW_LOCK,
    _ADMIN_TRASH_PREVIEWS,
    OutboundLedger,
    _admin_trash_approval_hook,
    _make_admin_trash_handlers,
    register_outbound_tools,
)

TEAM_ID = "772a55a0-9914-4a36-a0c2-d026ef421324"
ISSUE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ISSUE = {
    "id": ISSUE_ID,
    "identifier": "OPS-1",
    "title": "Old housekeeping record",
    "updatedAt": "2026-08-18T12:00:00Z",
    "archivedAt": None,
    "trashed": False,
    "team": {"id": TEAM_ID, "key": "OPS"},
    "state": {"type": "completed", "name": "Done"},
}


class PlatformConfigResolutionTests(unittest.TestCase):
    def test_native_top_level_precedence_and_legacy_fallback(self):
        from linear_tools import _load_linear_extra

        legacy = {"outbound_mcp": {"enabled": True, "admin_trash_enabled": False}}
        current = {"outbound_mcp": {"enabled": True, "admin_trash_enabled": True}}
        config = {"gateway": {"platforms": {"linear": {"extra": legacy}}}}
        with mock.patch("hermes_cli.config.load_config", return_value=config):
            self.assertEqual(_load_linear_extra(), legacy)
            config["platforms"] = {"linear": {"extra": current}}
            self.assertEqual(_load_linear_extra(), current)
            self.assertFalse(legacy["outbound_mcp"]["admin_trash_enabled"])


class FakeContext:
    profile_name = "general"

    def __init__(self) -> None:
        self.tools = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)


def extra(root: Path, *, admin=False, mutations=True):
    return {
        "oauth_file": str(root / "credential"),
        "database_path": str(root / "inbound.sqlite3"),
        "outbound_mcp": {
            "enabled": True,
            "mutations_enabled": mutations,
            "allowed_mutation_tools": ["linear_save_issue", "linear_save_comment"],
            "ledger_path": str(root / "outbound.sqlite3"),
            "quota_admission_lock_path": str(
                Path.home() / ".hermes" / "state" / "locks" / "linear-quota-admission.lock"
            ),
            "quota_team_ids": [TEAM_ID],
            "endpoint": "https://mcp.linear.app/mcp",
            "expected_actor_id": "actor-1",
            "expected_organization_id": "org-1",
            "allowed_team_ids": [TEAM_ID],
            "sensitive_mode": "standard",
            "admin_trash_enabled": admin,
            "admin_trash_team_ids": [TEAM_ID] if admin else [],
        },
    }


def client_with(*responses):
    client = SimpleNamespace(
        actor_id="actor-1",
        organization_id="org-1",
        connect=mock.AsyncMock(),
        close=mock.AsyncMock(),
        graphql=mock.AsyncMock(side_effect=responses),
    )
    return client


class AdminTrashRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.root.chmod(0o700)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_admin_tools_are_separate_opt_in_and_do_not_change_mcp_allowlist(self):
        ctx = FakeContext()
        config = extra(self.root, admin=True)
        with mock.patch("linear_tools._tool_names_available", return_value=True):
            register_outbound_tools(ctx, extra=config)

        self.assertEqual(
            set(ctx.tools),
            {
                "linear_get_issue",
                "linear_list_issues",
                "linear_save_issue",
                "linear_save_comment",
                "linear_verify_ops200_soul",
                "linear_verify_ops200_human_state",
                "linear_admin_trash_preview",
                "linear_admin_trash",
            },
        )
        self.assertEqual(
            config["outbound_mcp"]["allowed_mutation_tools"],
            ["linear_save_issue", "linear_save_comment"],
        )
        self.assertIn("pre_tool_call", ctx.hooks)
        self.assertIn("post_approval_response", ctx.hooks)
        self.assertNotIn("approved", ADMIN_TRASH_SCHEMA["parameters"]["properties"])
        self.assertNotIn("permanentlyDelete", str(ADMIN_TRASH_SCHEMA))
        self.assertEqual(
            set(ADMIN_TRASH_PREVIEW_SCHEMA["parameters"]["properties"]),
            {"issue_refs", "target_team_id"},
        )

        specialist = FakeContext()
        specialist.profile_name = "specialist"
        with mock.patch("linear_tools._tool_names_available", return_value=True):
            register_outbound_tools(specialist, extra=config)
        self.assertNotIn("linear_admin_trash", specialist.tools)
        self.assertNotIn("linear_admin_trash_preview", specialist.tools)

    def test_admin_tools_remain_absent_without_each_explicit_gate(self):
        for config in (
            extra(self.root, admin=False),
            extra(self.root, admin=True, mutations=False),
        ):
            with self.subTest(config=config["outbound_mcp"]):
                ctx = FakeContext()
                with mock.patch("linear_tools._tool_names_available", return_value=True):
                    register_outbound_tools(ctx, extra=config)
                self.assertNotIn("linear_admin_trash", ctx.tools)
                self.assertNotIn("linear_admin_trash_preview", ctx.tools)


class AdminTrashFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with _ADMIN_TRASH_PREVIEW_LOCK:
            _ADMIN_TRASH_PREVIEWS.clear()
            _ADMIN_TRASH_PENDING.clear()
            _ADMIN_TRASH_APPROVALS.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.root.chmod(0o700)

    def tearDown(self):
        with _ADMIN_TRASH_PREVIEW_LOCK:
            _ADMIN_TRASH_PREVIEWS.clear()
            _ADMIN_TRASH_PENDING.clear()
            _ADMIN_TRASH_APPROVALS.clear()
        self.tempdir.cleanup()

    def handlers(self, clients):
        return _make_admin_trash_handlers(
            profile_id="general",
            oauth_file="unused-oauth.json",
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            admin_team_ids={TEAM_ID},
            ledger_path=str(self.root / "outbound.sqlite3"),
            client_factory=lambda **_kwargs: clients.pop(0),
        )

    def approval_context(self):
        ctx = FakeContext()
        with mock.patch("linear_tools._tool_names_available", return_value=True):
            register_outbound_tools(ctx, extra=extra(self.root, admin=True))
        return ctx

    def native_context(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli import plugins
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        home = self.root / "hermes-home"
        home.mkdir(mode=0o700)
        home_token = set_hermes_home_override(home)
        self.addCleanup(reset_hermes_home_override, home_token)
        manager = PluginManager(scope_key=str(home.resolve()))
        manager._discovered = True
        manager_patch = mock.patch("hermes_cli.plugins.get_plugin_manager", return_value=manager)
        profile_patch = mock.patch("hermes_cli.profiles.get_active_profile_name", return_value="general")
        manager_patch.start()
        profile_patch.start()
        self.addCleanup(manager_patch.stop)
        self.addCleanup(profile_patch.stop)
        self.addCleanup(manager.unload)
        ctx = PluginContext(PluginManifest(name="linear-agent-platform"), manager)
        register_outbound_tools(ctx, extra=extra(self.root, admin=True))
        return ctx

    def native_tool_call(self, name, args, *, tool_call_id, session_id):
        from model_tools import handle_function_call

        result = handle_function_call(
            name,
            args,
            tool_call_id=tool_call_id,
            session_id=session_id,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
        return json.loads(result)

    def native_approval(self, ctx, preview, *, tool_call_id, session_id):
        from tools import approval_context

        events = {"pre": [], "post": []}
        ctx.register_hook("pre_tool_call", lambda **payload: events["pre"].append(dict(payload)))
        ctx.register_hook("post_approval_response", lambda **payload: events["post"].append(dict(payload)))
        args = {
            "issue_refs": [issue["identifier"] for issue in preview["issues"]],
            "target_team_id": TEAM_ID,
            "preview_id": preview["preview_id"],
            "preview_digest": preview["preview_digest"],
        }
        interactive = approval_context.set_hermes_interactive_context(True)
        self.addCleanup(approval_context.reset_hermes_interactive_context, interactive)
        with (
            mock.patch("tools.approval._yolo_active", return_value=False),
            mock.patch("tools.approval._presence", return_value=(None, True, False, False)),
            mock.patch("tools.approval_context._get_approval_mode", return_value="manual"),
            mock.patch("tools.approval.prompt_dangerous_approval", return_value="once") as prompt,
        ):
            result = self.native_tool_call(
                "linear_admin_trash",
                args,
                tool_call_id=tool_call_id,
                session_id=session_id,
            )
        prompt.assert_called_once()
        pre = next(event for event in events["pre"] if event.get("tool_call_id") == tool_call_id)
        post = next(event for event in events["post"] if event.get("tool_call_id") == tool_call_id)
        self.assertEqual(pre["session_id"], session_id)
        self.assertEqual(post["session_id"], session_id)
        self.assertEqual(post["choice"], "once")
        self.assertEqual(post["pattern_key"], f"plugin_rule:linear_admin_trash:{tool_call_id}")
        return result

    def approve_preview(self, result, *, tool_call_id="tool-call-1", session_id="session-1"):
        args = {
            "issue_refs": ["OPS-1"],
            "target_team_id": TEAM_ID,
            "preview_id": result["preview_id"],
            "preview_digest": result["preview_digest"],
        }
        ctx = self.approval_context()
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

        def dispatch_hooks(hook_name, **kwargs):
            return [callback(**kwargs) for callback in ctx.hooks.get(hook_name, [])]

        with (
            mock.patch("tools.approval._yolo_active", return_value=False),
            mock.patch("tools.approval._presence", return_value=(None, True, False, False)),
            mock.patch("tools.approval_context._get_approval_mode", return_value="manual"),
            mock.patch("tools.approval_context._is_cron_approval_context", return_value=False),
            mock.patch("tools.approval_context._is_single_query_approval_context", return_value=False),
            mock.patch("tools.approval.prompt_dangerous_approval", return_value="once") as prompt,
            mock.patch("hermes_cli.lifecycle.invoke_hook", side_effect=dispatch_hooks),
        ):
            blocked, modified = _dispatch_pre_tool_call_hooks(
                "linear_admin_trash", args, tool_call_id=tool_call_id, session_id=session_id
            )
        self.assertIsNone(blocked)
        self.assertIsNone(modified)
        prompt.assert_called_once()
        return args, {
            "message": prompt.call_args.args[1],
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "ctx": ctx,
        }

    async def run_native_trash(self, trash, args, *, tool_call_id, session_id="session-1"):
        from model_tools import _CallIds, _execute_tool, _run_async, registry

        def dispatch(_name, tool_args, **kwargs):
            return _run_async(trash(tool_args, **kwargs))

        with mock.patch.object(registry, "dispatch", side_effect=dispatch):
            return _execute_tool(
                "linear_admin_trash",
                args,
                args,
                _CallIds(session_id=session_id, tool_call_id=tool_call_id),
                user_task=None,
                enabled_tools=None,
                skip_tool_execution_middleware=True,
            )

    async def test_preview_is_exact_read_only_and_native_approval_binds_issue_set(self):
        client = client_with({"issue": ISSUE})
        preview, trash = self.handlers([client])

        result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )

        self.assertEqual(result["mode"], "preview-only")
        self.assertEqual(result["issues"][0]["identifier"], "OPS-1")
        self.assertFalse(result["issues"][0]["trashed"])
        self.assertEqual(len(result["preview_digest"]), 64)
        self.assertTrue(all("mutation" not in call.args[0].casefold() for call in client.graphql.await_args_list))
        args, approval = self.approve_preview(result)
        self.assertIn("OPS-1", approval["message"])
        self.assertIn(result["preview_digest"], approval["message"])
        self.assertIn("trash", approval["message"].casefold())
        self.assertIn("not permanent", approval["message"].casefold())
        self.assertNotIn("approved", args)

        tampered = {**args, "issue_refs": ["OPS-2"]}
        rejected = await self.run_native_trash(
            trash, tampered, tool_call_id=approval["tool_call_id"]
        )
        self.assertEqual(rejected["error"], "linear_admin_trash_denied")
        self.assertEqual(client.graphql.await_count, 1)

    async def test_unapproved_or_fabricated_approval_argument_never_reaches_graphql(self):
        client = client_with({"issue": ISSUE})
        preview, trash = self.handlers([client])
        result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )
        args = {
            "issue_refs": ["OPS-1"],
            "target_team_id": TEAM_ID,
            "preview_id": result["preview_id"],
            "preview_digest": result["preview_digest"],
        }

        blocked = await trash({**args, "approved": True})
        self.assertEqual(blocked["error"], "linear_admin_trash_denied")
        self.assertEqual(client.graphql.await_count, 1)
        direct = await trash(args)
        self.assertEqual(direct["error"], "linear_admin_trash_denied")
        self.assertEqual(client.graphql.await_count, 1)

        with (
            mock.patch("tools.approval._yolo_active", return_value=False),
            mock.patch("tools.approval._presence", return_value=(None, True, False, False)),
            mock.patch("tools.approval_context._get_approval_mode", return_value="manual"),
            mock.patch("tools.approval_context._is_cron_approval_context", return_value=False),
            mock.patch("tools.approval_context._is_single_query_approval_context", return_value=False),
        ):
            requested = _admin_trash_approval_hook(
                tool_name="linear_admin_trash", args=args,
                tool_call_id="request-only", session_id="session-1",
            )
        self.assertEqual(requested["action"], "approve")
        still_blocked = await self.run_native_trash(
            trash, args, tool_call_id="request-only"
        )
        self.assertEqual(still_blocked["error"], "linear_admin_trash_denied")
        self.assertEqual(client.graphql.await_count, 1)

    async def test_native_approval_cannot_be_bypassed_by_off_mode_or_replayed(self):
        client = client_with({"issue": ISSUE})
        preview, _trash = self.handlers([client])
        result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )
        args = {
            "issue_refs": ["OPS-1"],
            "target_team_id": TEAM_ID,
            "preview_id": result["preview_id"],
            "preview_digest": result["preview_digest"],
        }
        ctx = self.approval_context()
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

        def dispatch_hooks(hook_name, **kwargs):
            return [callback(**kwargs) for callback in ctx.hooks.get(hook_name, [])]

        with (
            mock.patch("tools.approval._yolo_active", return_value=False),
            mock.patch("tools.approval._presence", return_value=(None, True, False, False)),
            mock.patch("tools.approval_context._get_approval_mode", return_value="off"),
            mock.patch("hermes_cli.lifecycle.invoke_hook", side_effect=dispatch_hooks),
        ):
            blocked, _modified = _dispatch_pre_tool_call_hooks(
                "linear_admin_trash", args, tool_call_id="off-call", session_id="session-1"
            )
        self.assertIsNotNone(blocked)

        args, decision = self.approve_preview(result)
        with (
            mock.patch("tools.approval._yolo_active", return_value=False),
            mock.patch("tools.approval._presence", return_value=(None, True, False, False)),
            mock.patch("tools.approval_context._get_approval_mode", return_value="manual"),
            mock.patch("tools.approval_context._is_cron_approval_context", return_value=False),
            mock.patch("tools.approval_context._is_single_query_approval_context", return_value=False),
        ):
            replay = _admin_trash_approval_hook(
                tool_name="linear_admin_trash", args=args,
                tool_call_id=decision["tool_call_id"], session_id="session-1",
            )
        self.assertEqual(replay["action"], "block")

    async def test_preview_nullable_vendor_trash_state_and_missing_field(self):
        for value in (None, False, True, "false", "missing"):
            issue = {**ISSUE, "trashed": value}
            if value == "missing":
                del issue["trashed"]
            preview, _ = self.handlers([client_with({"issue": issue})])
            result = await preview(
                {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
            )
            if value is None or value is False:
                self.assertEqual(result["mode"], "preview-only")
            else:
                self.assertEqual(result["error"], "linear_admin_trash_denied")

    async def test_trash_uses_only_issue_archive_trash_true_and_reads_back(self):
        preview_client = client_with({"issue": ISSUE})
        trashed_issue = {**ISSUE, "trashed": True}
        trash_client = client_with(
            {"issue": ISSUE},
            {"issueArchive": {"success": True, "entity": trashed_issue}},
            {"issue": trashed_issue},
        )
        preview, trash = self.handlers([preview_client, trash_client])
        preview_result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )
        args, decision = self.approve_preview(preview_result)

        result = await self.run_native_trash(
            trash, args, tool_call_id=decision["tool_call_id"]
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["issues"][0]["trashed"])
        mutation_calls = [
            call for call in trash_client.graphql.await_args_list
            if "mutation" in call.args[0].casefold()
        ]
        self.assertEqual(len(mutation_calls), 1)
        mutation, variables = mutation_calls[0].args
        self.assertIn("issueArchive", mutation)
        self.assertIn("trash: true", mutation)
        self.assertNotIn("issueDelete", mutation)
        self.assertNotIn("permanentlyDelete", mutation)
        self.assertEqual(variables, {"id": ISSUE_ID})
        self.assertEqual(trash_client.graphql.await_args_list[-1].args[1], {"id": ISSUE_ID})

    async def test_changed_snapshot_aborts_before_mutation(self):
        preview_client = client_with({"issue": ISSUE})
        changed = {**ISSUE, "updatedAt": "2026-08-18T12:01:00Z"}
        trash_client = client_with({"issue": changed})
        preview, trash = self.handlers([preview_client, trash_client])
        preview_result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )
        args, decision = self.approve_preview(preview_result)

        result = await self.run_native_trash(
            trash, args, tool_call_id=decision["tool_call_id"]
        )

        self.assertEqual(result["error"], "linear_admin_trash_denied")
        self.assertEqual(trash_client.graphql.await_count, 1)
        self.assertFalse(any("mutation" in call.args[0].casefold() for call in trash_client.graphql.await_args_list))

    async def test_registered_tools_use_native_hooks_and_dispatch_context(self):
        trashed_issue = {**ISSUE, "trashed": True}
        preview_client = client_with({"issue": ISSUE})
        trash_client = client_with(
            {"issue": ISSUE},
            {"issueArchive": {"success": True, "entity": {"id": ISSUE_ID}}},
            {"issue": trashed_issue},
        )
        ctx = self.native_context()
        with mock.patch("linear_tools.LinearClient", side_effect=[preview_client, trash_client]):
            preview = self.native_tool_call(
                "linear_admin_trash_preview",
                {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID},
                tool_call_id="preview-native-call",
                session_id="native-session",
            )
            self.assertEqual(preview["mode"], "preview-only")
            result = self.native_approval(
                ctx,
                preview,
                tool_call_id="trash-native-call",
                session_id="native-session",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["issues"][0]["id"], ISSUE_ID)
        self.assertEqual(
            len([call for call in trash_client.graphql.await_args_list if "mutation" in call.args[0].casefold()]),
            1,
        )

    async def test_native_telegram_gateway_approval_consumes_once_via_real_callback(self):
        from gateway.config import PlatformConfig
        from gateway.run_turn_runner import TurnRunner
        from gateway.session_context import clear_session_vars, set_session_vars
        from plugins.platforms.telegram.adapter import TelegramAdapter
        from tools import approval

        preview_client = client_with({"issue": ISSUE})
        trashed_issue = {**ISSUE, "trashed": True}
        trash_client = client_with(
            {"issue": ISSUE},
            {"issueArchive": {"success": True, "entity": {"id": ISSUE_ID}}},
            {"issue": trashed_issue},
        )
        ctx = self.native_context()
        approval_events = []
        ctx.register_hook("post_approval_response", lambda **payload: approval_events.append(dict(payload)))

        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
        adapter._bot = mock.AsyncMock()
        adapter._app = mock.MagicMock()
        adapter.set_authorization_check(lambda user_id, *_args, **_kwargs: str(user_id) == "12345")
        adapter._bot.send_message = mock.AsyncMock(return_value=SimpleNamespace(message_id=55))
        session_key = "telegram-session-native-trash"
        session_id = "telegram-native-session"
        tool_call_id = "telegram-native-trash-call"
        loop = asyncio.get_running_loop()
        prompt_ready = asyncio.Event()
        runner_ctx = SimpleNamespace(
            session_key=session_key,
            _status_adapter=adapter,
            _status_chat_id="12345",
            _status_thread_metadata={},
            _loop_for_step=loop,
            stream_consumer_holder=[None],
            _run_still_current=lambda: True,
        )
        runner = TurnRunner(None, runner_ctx)
        requests = []

        def notify_gateway(approval_data):
            requests.append(dict(approval_data))
            runner._approval_notify_sync(approval_data)
            loop.call_soon_threadsafe(prompt_ready.set)

        approval.unregister_gateway_notify(session_key)
        approval.register_gateway_notify(session_key, notify_gateway)
        session_tokens = set_session_vars(
            platform="telegram", session_key=session_key, session_id=session_id, cron_session=""
        )
        worker = None
        try:
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "HERMES_GATEWAY_SESSION": "",
                        "HERMES_EXEC_ASK": "",
                        "HERMES_INTERACTIVE": "",
                        "HERMES_SINGLE_QUERY_SESSION": "",
                    },
                ),
                mock.patch("tools.approval._yolo_active", return_value=False),
                mock.patch("tools.approval_context._get_approval_mode", return_value="manual"),
                mock.patch("tools.approval_context._get_approval_timeout", return_value=2),
                mock.patch("linear_tools.LinearClient", side_effect=[preview_client, trash_client]),
            ):
                self.assertEqual(approval._presence()[1:], (False, True, False))
                preview = self.native_tool_call(
                    "linear_admin_trash_preview",
                    {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID},
                    tool_call_id="telegram-preview-call",
                    session_id=session_id,
                )
                args = {
                    "issue_refs": ["OPS-1"],
                    "target_team_id": TEAM_ID,
                    "preview_id": preview["preview_id"],
                    "preview_digest": preview["preview_digest"],
                }
                def trash_call():
                    try:
                        return self.native_tool_call(
                            "linear_admin_trash", args, tool_call_id=tool_call_id, session_id=session_id
                        )
                    finally:
                        from model_tools import _worker_thread_local
                        tool_loop = getattr(_worker_thread_local, "loop", None)
                        if tool_loop is not None and not tool_loop.is_closed():
                            tool_loop.close()

                worker = asyncio.create_task(asyncio.to_thread(trash_call))
                try:
                    await asyncio.wait_for(prompt_ready.wait(), timeout=5)
                    self.assertEqual(adapter._bot.send_message.await_count, 1)
                    self.assertEqual(trash_client.graphql.await_count, 0)
                    self.assertEqual(len(requests), 1)
                    request = requests[0]
                    self.assertTrue(request.get("request_id"))
                    self.assertEqual(request["pattern_key"], f"plugin_rule:linear_admin_trash:{tool_call_id}")
                    self.assertIn("OPS-1", request["description"])
                    self.assertIn(preview["preview_digest"], request["description"])

                    markup = adapter._bot.send_message.await_args.kwargs["reply_markup"]
                    buttons = [button for row in markup.inline_keyboard for button in row]
                    self.assertEqual(
                        {button.callback_data.split(":")[1] for button in buttons},
                        {"once", "session", "always", "deny"},
                    )
                    callback_data = next(
                        button.callback_data for button in buttons
                        if button.callback_data.startswith("ea:once:")
                    )
                    self.assertEqual(
                        adapter._approval_state,
                        {int(callback_data.rsplit(":", 1)[1]): session_key},
                    )
                    query = SimpleNamespace(
                        data=callback_data,
                        message=SimpleNamespace(
                            chat_id=12345,
                            chat=SimpleNamespace(type="private"),
                            message_thread_id=None,
                        ),
                        from_user=SimpleNamespace(first_name="Operator", id=12345),
                        answer=mock.AsyncMock(),
                        edit_message_text=mock.AsyncMock(),
                    )
                    await adapter._handle_callback_query(
                        SimpleNamespace(callback_query=query), SimpleNamespace()
                    )
                    result = await asyncio.wait_for(worker, timeout=5)
                finally:
                    if not worker.done():
                        approval.resolve_gateway_approval(session_key, "deny")
                        try:
                            await asyncio.wait_for(worker, timeout=5)
                        except Exception:
                            pass

                self.assertEqual(result.get("status"), "success", result)
                self.assertTrue(result["issues"][0]["trashed"])
                self.assertEqual(query.data, callback_data)
                self.assertEqual(query.answer.await_args.kwargs["text"], "✅ Approved once")
                approvals = [event for event in approval_events if event.get("tool_call_id") == tool_call_id]
                self.assertEqual(len(approvals), 1)
                self.assertEqual(approvals[0]["session_id"], session_id)
                self.assertEqual(approvals[0]["session_key"], session_key)
                self.assertEqual(approvals[0]["surface"], "gateway")
                self.assertEqual(approvals[0]["choice"], "once")
                mutation_calls = [
                    call for call in trash_client.graphql.await_args_list
                    if "mutation" in call.args[0].casefold()
                ]
                self.assertEqual(len(mutation_calls), 1)
                self.assertIn("issueArchive", mutation_calls[0].args[0])
                self.assertIn("trash: true", mutation_calls[0].args[0])

                replay = self.native_tool_call(
                    "linear_admin_trash", args, tool_call_id=tool_call_id, session_id=session_id
                )
                self.assertTrue(replay.get("error"))
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(mutation_calls), 1)
        finally:
            approval.resolve_gateway_approval(session_key, "deny")
            approval.unregister_gateway_notify(session_key)
            clear_session_vars(session_tokens)

    async def test_native_partial_batch_reports_confirmed_ids_in_result_and_ledger(self):
        issues = [
            ISSUE,
            {**ISSUE, "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "identifier": "OPS-2"},
            {**ISSUE, "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "identifier": "OPS-3"},
        ]
        trashed_first = {**issues[0], "trashed": True}
        preview_client = client_with(*({"issue": issue} for issue in issues))
        trash_client = client_with(
            {"issue": issues[0]},
            {"issueArchive": {"success": True, "entity": {"id": issues[0]["id"]}}},
            {"issue": trashed_first},
            {"issue": issues[1]},
            {"issueArchive": {"success": False}},
        )
        ctx = self.native_context()
        ledger_path = str(self.root / "outbound.sqlite3")
        with mock.patch("linear_tools.LinearClient", side_effect=[preview_client, trash_client]):
            preview = self.native_tool_call(
                "linear_admin_trash_preview",
                {"issue_refs": [issue["identifier"] for issue in issues], "target_team_id": TEAM_ID},
                tool_call_id="preview-partial-call",
                session_id="partial-session",
            )
            result = self.native_approval(
                ctx,
                preview,
                tool_call_id="trash-partial-call",
                session_id="partial-session",
            )

        self.assertEqual(result["error"], "linear_admin_trash_partial")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["trashed_issue_ids"], [issues[0]["id"]])
        self.assertEqual(result["failed_issue_id"], issues[1]["id"])
        self.assertEqual(result["not_attempted_issue_ids"], [issues[2]["id"]])
        self.assertEqual(
            len([call for call in trash_client.graphql.await_args_list if "mutation" in call.args[0].casefold()]),
            2,
        )

        ledger = OutboundLedger(ledger_path)
        try:
            entry = ledger.lookup(
                operation_key=f"linear-admin-trash:{preview['preview_id']}",
                tool_name="linear_admin_trash",
                payload={
                    "target_team_id": TEAM_ID,
                    "preview_digest": preview["preview_digest"],
                    "issue_ids": [issue["id"] for issue in issues],
                },
                profile_id="general",
                actor_id="actor-1",
                team_id=TEAM_ID,
            )
        finally:
            ledger.close()
        self.assertEqual(entry.status, "failed")
        self.assertEqual(
            json.loads(entry.error_code),
            {
                "changed_issue_id": None,
                "failed_issue_id": issues[1]["id"],
                "not_attempted_issue_ids": [issues[2]["id"]],
                "reason": "trash_rejected",
                "trashed_issue_ids": [issues[0]["id"]],
                "uncertain_issue_id": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
