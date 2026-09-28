from __future__ import annotations

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
    _ADMIN_TRASH_PREVIEW_LOCK,
    _ADMIN_TRASH_PREVIEWS,
    OutboundLedger,
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
                "linear_verify_criterion",
                "linear_admin_trash_preview",
                "linear_admin_trash",
            },
        )
        self.assertEqual(
            config["outbound_mcp"]["allowed_mutation_tools"],
            ["linear_save_issue", "linear_save_comment"],
        )
        self.assertEqual(ctx.hooks, {})
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
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.root.chmod(0o700)

    def tearDown(self):
        with _ADMIN_TRASH_PREVIEW_LOCK:
            _ADMIN_TRASH_PREVIEWS.clear()
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

    def native_context(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
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

    async def test_preview_is_exact_read_only(self):
        client = client_with({"issue": ISSUE})
        preview, _trash = self.handlers([client])

        result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )

        self.assertEqual(result["mode"], "preview-only")
        self.assertEqual(result["issues"][0]["identifier"], "OPS-1")
        self.assertFalse(result["issues"][0]["trashed"])
        self.assertEqual(len(result["preview_digest"]), 64)
        self.assertTrue(all("mutation" not in call.args[0].casefold() for call in client.graphql.await_args_list))

    async def test_fabricated_approved_argument_is_ignored(self):
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

        with mock.patch("tools.approval_prompt.request_elicitation_consent") as request_consent:
            blocked = await trash({**args, "approved": True}, session_id="session-1")

        self.assertEqual(blocked["error"], "linear_admin_trash_denied")
        request_consent.assert_not_called()
        self.assertEqual(client.graphql.await_count, 1)

    async def test_preview_digest_refs_team_and_session_are_bound_before_consent(self):
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
        invalid_calls = (
            ({**args, "issue_refs": ["OPS-2"]}, "session-1"),
            ({**args, "target_team_id": "other-team"}, "session-1"),
            ({**args, "preview_digest": "0" * 64}, "session-1"),
            (args, "other-session"),
        )

        with mock.patch("tools.approval_prompt.request_elicitation_consent") as request_consent:
            for invalid_args, session_id in invalid_calls:
                denied = await trash(invalid_args, session_id=session_id)
                self.assertEqual(denied["error"], "linear_admin_trash_denied")

        request_consent.assert_not_called()
        self.assertEqual(client.graphql.await_count, 1)

    async def test_deny_and_timeout_do_not_mutate(self):
        for consent in ("decline", "cancel"):
            with self.subTest(consent=consent):
                preview_client = client_with({"issue": ISSUE})
                trash_client = client_with({"issue": ISSUE})
                preview, trash = self.handlers([preview_client, trash_client])
                result = await preview(
                    {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
                )
                args = {
                    "issue_refs": ["OPS-1"],
                    "target_team_id": TEAM_ID,
                    "preview_id": result["preview_id"],
                    "preview_digest": result["preview_digest"],
                }
                with mock.patch(
                    "tools.approval_prompt.request_elicitation_consent", return_value=consent
                ) as request_consent:
                    denied = await trash(args, session_id="session-1")

                self.assertEqual(denied["error"], "linear_admin_trash_denied")
                request_consent.assert_called_once()
                self.assertEqual(trash_client.graphql.await_count, 0)

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
        args = {
            "issue_refs": ["OPS-1"],
            "target_team_id": TEAM_ID,
            "preview_id": preview_result["preview_id"],
            "preview_digest": preview_result["preview_digest"],
        }

        with mock.patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            result = await trash(args, session_id="session-1")

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

    async def test_elicitation_consent_accepts_exact_preview_and_mutates(self):
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
        args = {
            "issue_refs": ["OPS-1"],
            "target_team_id": TEAM_ID,
            "preview_id": preview_result["preview_id"],
            "preview_digest": preview_result["preview_digest"],
        }

        with mock.patch(
            "tools.approval_prompt.request_elicitation_consent", return_value="accept"
        ) as request_consent:
            result = await trash(args, session_id="session-1")

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["issues"][0]["trashed"])
        request_consent.assert_called_once()
        message, description = request_consent.call_args.args
        self.assertIn('"OPS-1" — "Old housekeeping record"', message)
        self.assertIn("Count: 1", message)
        self.assertIn("Linear trash (geri alınabilir), kalıcı silme değil", message)
        self.assertTrue(description)
        self.assertEqual(request_consent.call_args.kwargs["surface"], "linear-admin-trash")
        self.assertEqual(request_consent.call_args.kwargs["title"], "Confirm Linear trash")
        self.assertEqual(
            len([call for call in trash_client.graphql.await_args_list if "mutation" in call.args[0].casefold()]),
            1,
        )
        replay = await trash(args, session_id="session-1")
        self.assertEqual(replay["error"], "linear_admin_trash_denied")
        request_consent.assert_called_once()

    async def test_changed_snapshot_after_consent_aborts_before_mutation(self):
        preview_client = client_with({"issue": ISSUE})
        changed = {**ISSUE, "updatedAt": "2026-08-18T12:01:00Z"}
        trash_client = client_with({"issue": changed})
        preview, trash = self.handlers([preview_client, trash_client])
        preview_result = await preview(
            {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id="session-1"
        )
        args = {
            "issue_refs": ["OPS-1"],
            "target_team_id": TEAM_ID,
            "preview_id": preview_result["preview_id"],
            "preview_digest": preview_result["preview_digest"],
        }

        with mock.patch(
            "tools.approval_prompt.request_elicitation_consent", return_value="accept"
        ) as request_consent:
            result = await trash(args, session_id="session-1")

        self.assertEqual(result["error"], "linear_admin_trash_denied")
        request_consent.assert_called_once()
        self.assertEqual(trash_client.graphql.await_count, 1)
        self.assertFalse(any("mutation" in call.args[0].casefold() for call in trash_client.graphql.await_args_list))

    async def test_registered_tools_dispatch_exact_session_bound_consent(self):
        trashed_issue = {**ISSUE, "trashed": True}
        preview_client = client_with({"issue": ISSUE})
        trash_client = client_with(
            {"issue": ISSUE},
            {"issueArchive": {"success": True, "entity": {"id": ISSUE_ID}}},
            {"issue": trashed_issue},
        )
        self.native_context()
        with (
            mock.patch("linear_tools.LinearClient", side_effect=[preview_client, trash_client]),
            mock.patch(
                "tools.approval_prompt.request_elicitation_consent", return_value="accept"
            ) as request_consent,
        ):
            preview = self.native_tool_call(
                "linear_admin_trash_preview",
                {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID},
                tool_call_id="preview-native-call",
                session_id="native-session",
            )
            self.assertEqual(preview["mode"], "preview-only")
            args = {
                "issue_refs": ["OPS-1"],
                "target_team_id": TEAM_ID,
                "preview_id": preview["preview_id"],
                "preview_digest": preview["preview_digest"],
            }
            result = self.native_tool_call(
                "linear_admin_trash",
                args,
                tool_call_id="trash-native-call",
                session_id="native-session",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["issues"][0]["id"], ISSUE_ID)
        self.assertEqual(request_consent.call_count, 1)
        self.assertEqual(
            len([call for call in trash_client.graphql.await_args_list if "mutation" in call.args[0].casefold()]),
            1,
        )

    async def test_real_elicitation_consent_gateway_works_with_approvals_off(self):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import approval
        from tools.approval_context import reset_current_session_key, set_current_session_key

        preview_client = client_with({"issue": ISSUE})
        trashed_issue = {**ISSUE, "trashed": True}
        trash_client = client_with(
            {"issue": ISSUE},
            {"issueArchive": {"success": True, "entity": {"id": ISSUE_ID}}},
            {"issue": trashed_issue},
        )
        preview, trash = self.handlers([preview_client, trash_client])
        session_key = "telegram-session-linear-trash-consent"
        session_id = "telegram-linear-trash-session"
        requests = []

        def notify(approval_data):
            requests.append(dict(approval_data))
            self.assertEqual(
                approval.resolve_gateway_approval(
                    session_key, "once", request_id=approval_data["request_id"]
                ),
                1,
            )

        approval.unregister_gateway_notify(session_key)
        approval.register_gateway_notify(session_key, notify)
        session_tokens = set_session_vars(
            platform="telegram", session_key=session_key, session_id=session_id, cron_session=""
        )
        key_token = set_current_session_key(session_key)
        try:
            with (
                mock.patch("tools.approval_context._get_approval_mode", return_value="off"),
                mock.patch("tools.approval_context._get_approval_timeout", return_value=2),
            ):
                preview_result = await preview(
                    {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id=session_id
                )
                args = {
                    "issue_refs": ["OPS-1"],
                    "target_team_id": TEAM_ID,
                    "preview_id": preview_result["preview_id"],
                    "preview_digest": preview_result["preview_digest"],
                }
                result = await trash(args, session_id=session_id)
        finally:
            approval.unregister_gateway_notify(session_key)
            reset_current_session_key(key_token)
            clear_session_vars(session_tokens)

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["pattern_key"], "mcp_elicitation")
        self.assertTrue(requests[0]["request_id"])
        self.assertIn("OPS-1", requests[0]["command"])
        self.assertIn("Old housekeeping record", requests[0]["command"])
        self.assertIn("Count: 1", requests[0]["command"])
        self.assertIn("Linear trash (geri alınabilir), kalıcı silme değil", requests[0]["command"])
        self.assertIn(preview_result["preview_digest"], requests[0]["description"])
        self.assertEqual(
            len([call for call in trash_client.graphql.await_args_list if "mutation" in call.args[0].casefold()]),
            1,
        )

    async def test_missing_gateway_consent_surface_fails_closed(self):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import approval
        from tools.approval_context import reset_current_session_key, set_current_session_key

        preview_client = client_with({"issue": ISSUE})
        trash_client = client_with({"issue": ISSUE})
        preview, trash = self.handlers([preview_client, trash_client])
        session_key = "telegram-session-linear-trash-no-notify"
        session_id = "telegram-linear-trash-no-notify-session"
        approval.unregister_gateway_notify(session_key)
        session_tokens = set_session_vars(
            platform="telegram", session_key=session_key, session_id=session_id, cron_session=""
        )
        key_token = set_current_session_key(session_key)
        try:
            preview_result = await preview(
                {"issue_refs": ["OPS-1"], "target_team_id": TEAM_ID}, session_id=session_id
            )
            args = {
                "issue_refs": ["OPS-1"],
                "target_team_id": TEAM_ID,
                "preview_id": preview_result["preview_id"],
                "preview_digest": preview_result["preview_digest"],
            }
            result = await trash(args, session_id=session_id)
        finally:
            reset_current_session_key(key_token)
            clear_session_vars(session_tokens)

        self.assertEqual(result["error"], "linear_admin_trash_denied")
        self.assertEqual(trash_client.graphql.await_count, 0)

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
        preview_handler, trash_handler = self.handlers([preview_client, trash_client])
        ledger_path = str(self.root / "outbound.sqlite3")
        preview = await preview_handler(
            {"issue_refs": [issue["identifier"] for issue in issues], "target_team_id": TEAM_ID},
            session_id="partial-session",
        )
        args = {
            "issue_refs": [issue["identifier"] for issue in issues],
            "target_team_id": TEAM_ID,
            "preview_id": preview["preview_id"],
            "preview_digest": preview["preview_digest"],
        }
        with mock.patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            result = await trash_handler(args, session_id="partial-session")

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
