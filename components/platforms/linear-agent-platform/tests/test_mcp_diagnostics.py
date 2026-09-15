"""Offline diagnostic contracts; no credentials or vendor traffic."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_client as mcp
import linear_tools
import test_mcp_client as fixtures
import test_linear_tools as tool_fixtures

SECRET = "Bearer secret-token session-id=private body=private"


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.LinearMCPClientTests()
        await self.fixture.asyncSetUp()
        self.client = self.fixture.client()

    async def asyncTearDown(self):
        await self.client.close()
        await self.fixture.asyncTearDown()

    async def test_connect_and_catalog_causes_are_distinct(self):
        self.fixture.protocol_version = "unsupported"
        with self.assertRaises(mcp.LinearMCPError) as caught:
            await self.client.connect()
        self.assertEqual(caught.exception.diagnostic()["code"], "connect_contract")
        self.assertEqual(caught.exception.diagnostic()["stage"], "connect")
        self.fixture.protocol_version = "2025-03-26"
        self.fixture.tools.pop()
        with self.assertRaises(mcp.LinearMCPError) as caught:
            await self.client.connect()
        self.assertEqual(caught.exception.diagnostic()["code"], "catalog_contract")
        self.assertEqual(caught.exception.diagnostic()["stage"], "catalog_validation")

    async def test_jsonrpc_identity_and_vendor_stage(self):
        await self.client.connect()
        self.fixture.tool_rpc_error = True
        for tool, stage in [("get_user", "identity"), ("get_issue", "vendor_operation")]:
            with self.assertRaises(mcp.LinearMCPError) as caught:
                await self.client.call_tool(tool, {"query": SECRET, "id": SECRET})
            self.assertEqual(caught.exception.diagnostic(), {
                "version": 1, "code": "jsonrpc_error", "stage": stage,
                "tool": tool, "http_status": None,
            })

    async def test_http_timeout_and_refresh_are_distinct(self):
        await self.client.connect()
        with mock.patch.object(self.client._session, "post", side_effect=asyncio.TimeoutError(SECRET)):
            with self.assertRaises(mcp.LinearMCPError) as caught:
                await self.client.call_tool("get_issue", {"id": SECRET})
        self.assertEqual(caught.exception.diagnostic()["code"], "http_timeout")
        self.fixture.unauthorized_once = True
        original = self.fixture.store.access_token
        async def fail_refresh(**kwargs):
            if kwargs.get("force_refresh"):
                raise RuntimeError(SECRET)
            return await original(**kwargs)
        with mock.patch.object(self.fixture.store, "access_token", side_effect=fail_refresh):
            with self.assertRaises(mcp.LinearMCPError) as caught:
                await self.client.call_tool("get_issue", {"id": SECRET})
        diagnostic = caught.exception.diagnostic()
        self.assertEqual(diagnostic["code"], "auth_refresh")
        self.assertEqual(diagnostic["http_status"], 401)
        self.assertNotIn(SECRET, json.dumps(diagnostic))

    async def test_http_status_and_vendor_rejection(self):
        await self.client.connect()
        self.fixture.always_404_methods.add("tools/call")
        with self.assertRaises(mcp.LinearMCPError) as caught:
            await self.client.call_tool("get_issue", {"id": SECRET})
        self.assertEqual(caught.exception.diagnostic()["code"], "http_status")
        self.assertEqual(caught.exception.diagnostic()["http_status"], 404)
        self.fixture.always_404_methods.clear()
        self.fixture.tool_error = True
        with self.assertRaises(mcp.LinearMCPError) as caught:
            await self.client.call_tool("get_issue", {"id": SECRET})
        self.assertEqual(caught.exception.diagnostic()["code"], "vendor_error")


    async def test_untrusted_jsonrpc_error_and_transport_metadata_never_render(self):
        await self.client.connect()
        async def rpc_error(payload, **kwargs):
            return {"jsonrpc": "2.0", "id": kwargs["request_id"],
                    "error": {"code": SECRET, "message": SECRET, "data": {"headers": SECRET}}}, SECRET
        with mock.patch.object(self.client, "_post", side_effect=rpc_error):
            with self.assertRaises(mcp.LinearMCPError) as caught:
                await self.client.call_tool("get_issue", {"id": SECRET})
        self.assertEqual(caught.exception.diagnostic()["code"], "jsonrpc_error")
        self.assertNotIn(SECRET, json.dumps(caught.exception.diagnostic()))
        transport_error = mcp.aiohttp.ClientConnectionError(SECRET)
        transport_error.headers = {"Authorization": SECRET}
        with mock.patch.object(self.client._session, "post", side_effect=transport_error):
            with self.assertRaises(mcp.LinearMCPError) as caught:
                await self.client.call_tool("get_issue", {"id": SECRET})
        self.assertEqual(caught.exception.diagnostic()["code"], "http_transport")
        self.assertNotIn(SECRET, json.dumps(caught.exception.diagnostic()))

    async def test_catalog_http_failure_keeps_catalog_stage(self):
        self.fixture.always_404_methods.add("tools/list")
        with self.assertRaises(mcp.LinearMCPError) as caught:
            await self.client.connect()
        self.assertEqual(caught.exception.diagnostic()["stage"], "catalog_validation")
        self.assertEqual(caught.exception.diagnostic()["http_status"], 404)

    async def test_registered_handler_identity_and_vendor_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            registration = tool_fixtures.RegistrationTests()
            registration.root = Path(directory)
            ctx = tool_fixtures.FakeContext()
            linear_tools.register_outbound_tools(ctx, extra=registration.extra())
            graphql = tool_fixtures.FakeGraphQL()
            graphql.connect = mock.AsyncMock()
            graphql.close = mock.AsyncMock()
            self.fixture.tool_rpc_error = True
            with (
                mock.patch("linear_tools.LinearOAuthStore"),
                mock.patch("linear_tools.LinearClient", return_value=graphql),
                mock.patch("linear_tools.LinearMCPClient", return_value=self.client),
            ):
                result = json.loads(await ctx.tools["linear_list_issues"]["handler"]({"team": "ops-1"}))
            self.assertEqual(result["diagnostic"]["stage"], "identity")
            self.assertEqual(result["diagnostic"]["tool"], "get_user")
            self.fixture.tool_rpc_error = False
            original = self.client._send_rpc
            async def fail_vendor(method, params, **kwargs):
                if method == "tools/call" and params["name"] == "list_issues":
                    raise mcp.LinearMCPError(SECRET, code="jsonrpc_error")
                return await original(method, params, **kwargs)
            with (
                mock.patch("linear_tools.LinearOAuthStore"),
                mock.patch("linear_tools.LinearClient", return_value=graphql),
                mock.patch("linear_tools.LinearMCPClient", return_value=self.client),
                mock.patch.object(self.client, "_send_rpc", side_effect=fail_vendor),
            ):
                result = json.loads(await ctx.tools["linear_list_issues"]["handler"]({"team": "ops-1"}))
            self.assertEqual(result["diagnostic"]["stage"], "vendor_operation")
            self.assertEqual(result["diagnostic"]["tool"], "list_issues")
            self.assertNotIn(SECRET, json.dumps(result))

    async def test_mutation_diagnostics_preserve_unknown_fence(self):
        execution = tool_fixtures.ExecutionTests()
        await execution.asyncSetUp()
        try:
            for error_type, reason in [
                (mcp.MCPOutcomeUnknown, "legacy message"),
                (mcp.LinearMCPToolError, "vendor_is_error"),
                (mcp.LinearMCPError, "mcp_protocol_error"),
            ]:
                error = error_type("legacy message", code="http_timeout", stage="vendor_operation", tool="save_issue")
                client = tool_fixtures.FakeMCP()
                original = client.call_tool
                async def fail_mutation(name, arguments, *, mutation=False):
                    if name == "save_issue":
                        raise error
                    return await original(name, arguments, mutation=mutation)
                client.call_tool = fail_mutation
                result, _, _ = await execution.run_create(
                    operation_key=error_type.__name__, current_count=23, mcp=client,
                )
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["diagnostic"]["code"], "http_timeout")
                self.assertEqual(result["diagnostic"]["tool"], "save_issue")
                # Each unknown create fences the shared capacity; reset only the fixture.
                await execution.asyncTearDown()
                await execution.asyncSetUp()
        finally:
            await execution.asyncTearDown()

class SafeOutputTests(unittest.TestCase):
    def test_unknown_exception_is_not_stringified_or_inspected(self):
        class HostileError(RuntimeError):
            def __str__(self):
                raise AssertionError("exception stringification is forbidden")
        error = HostileError(SECRET)
        error.diagnostic = {"code": SECRET, "body": SECRET}
        self.assertEqual(mcp.safe_mcp_diagnostic(error, stage=SECRET), {
            "version": 1, "code": "unknown", "stage": "unknown",
            "tool": None, "http_status": None,
        })

    def test_hostile_metadata_is_allowlisted_at_serialization(self):
        error = mcp.LinearMCPError(SECRET, code=SECRET, stage=SECRET, tool=SECRET, http_status=SECRET)
        error.code = {"Authorization": SECRET}
        error.stage = [SECRET]
        error.tool = SECRET
        error.http_status = True
        error.body = SECRET
        self.assertEqual(error.diagnostic(), {
            "version": 1, "code": "unknown", "stage": "unknown",
            "tool": None, "http_status": None,
        })
        self.assertEqual(str(error), SECRET)  # Legacy exception message remains intact.

    def test_registered_handler_preserves_legacy_fields_and_safe_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = tool_fixtures.RegistrationTests()
            fixture.root = Path(directory)
            ctx = tool_fixtures.FakeContext()
            linear_tools.register_outbound_tools(ctx, extra=fixture.extra(mutations=False))
            graphql = mock.MagicMock(connect=mock.AsyncMock(), close=mock.AsyncMock())
            client = mock.MagicMock(connect=mock.AsyncMock(), close=mock.AsyncMock())
            for error in [RuntimeError(SECRET), mcp.LinearMCPError(SECRET, code="catalog_contract", stage="catalog_validation")]:
                client.connect.side_effect = error
                with (
                    mock.patch("linear_tools.LinearOAuthStore"),
                    mock.patch("linear_tools.LinearClient", return_value=graphql),
                    mock.patch("linear_tools.LinearMCPClient", return_value=client),
                ):
                    result = json.loads(asyncio.run(ctx.tools["linear_get_issue"]["handler"]({"id": SECRET})))
                self.assertEqual(result["error"], "linear_tool_failed")
                self.assertEqual(result["reason"], type(error).__name__)
                self.assertEqual(result["diagnostic"]["code"], "unknown" if type(error) is RuntimeError else "catalog_contract")
                self.assertEqual(result["diagnostic"]["stage"], "connect" if type(error) is RuntimeError else "catalog_validation")
                self.assertNotIn(SECRET, json.dumps(result))
