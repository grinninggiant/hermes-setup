"""Exact, read-only vendor receipt validation (isolated transport fixtures)."""
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from linear_client import LinearClient
from oauth_store import LinearAPIError


class ResponseReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_requires_exact_response_identity_body_owner_and_terminal_session(self):
        client = LinearClient(oauth_file="unused")
        client.actor_id = "actor"
        receipt = {"id": "activity", "user": {"id": "actor"},
            "agentSession": {"id": "session", "status": "complete", "appUser": {"id": "actor"}},
            "content": {"__typename": "AgentActivityResponseContent", "body": "result"}}
        client.graphql = mock.AsyncMock(return_value={"agentActivity": receipt})
        self.assertTrue(await client.verify_response_receipt("activity", "session", "result"))
        mutations = [(("id",), "wrong"), (("user", "id"), "other"),
            (("agentSession", "id"), "other"), (("agentSession", "appUser", "id"), "other"),
            (("content", "body"), "wrong"), (("content", "__typename"), "AgentActivityThoughtContent")]
        for path, value in mutations:
            wrong = deepcopy(receipt)
            parent = wrong
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = value
            client.graphql.return_value = {"agentActivity": wrong}
            with self.subTest(path=path), self.assertRaises(LinearAPIError) as caught:
                await client.verify_response_receipt("activity", "session", "result")
            self.assertFalse(caught.exception.retryable)
        client.graphql.return_value = {"agentActivity": None}
        self.assertFalse(await client.verify_response_receipt("activity", "session", "result"))
        pending = deepcopy(receipt)
        pending["agentSession"]["status"] = "active"
        client.graphql.return_value = {"agentActivity": pending}
        with self.assertRaises(LinearAPIError) as caught:
            await client.verify_response_receipt("activity", "session", "result")
        self.assertTrue(caught.exception.retryable)
