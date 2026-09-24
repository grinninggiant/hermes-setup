"""Exact, read-only vendor receipt validation (isolated transport fixtures)."""
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from linear_client import LinearClient, _same_response_link_serialization
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

    def test_hidden_markdown_changes_are_not_link_serialization(self):
        link = "[report](<https://example.com/report>)"
        plain = "[report](https://example.com/report)"
        unused = f'[unused]: https://other.example "{link}"'
        reference = (
            r"[\[r\](https://example.com/)]: https://target.example/" + "\n"
            + r"[\[r\](<https://example.com/>)]: https://target.example/" + "\n\n"
        )
        cases = [
            ("[![report](https://example.com/report)](https://example.com/target)",
             "[![report](<https://example.com/report>)](https://example.com/target)"),
            ('`[report](https://example.com/report "source")`',
             '`[report](<https://example.com/report> "source")`'),
            ("> " * 20 + f"`{plain}`", "> " * 20 + f"`{link}`"),
            (f"```{plain}\nresult\n```", f"```{link}\nresult\n```"),
            (unused.replace(link, plain) + "\n\nresult", unused + "\n\nresult"),
            (reference + r"[shown][\[r\](https://example.com/)]",
             reference + r"[shown][\[r\](<https://example.com/>)]"),
            ("a\u0085b\u0085c\n\n" + unused.replace(link, plain) + "\n\n" + link,
             "a\u0085b\u0085c\n\n" + unused + "\n\n" + link),
            ("a\rb\rc\n\n" + link + "\n\n" + unused.replace(link, plain),
             "a\rb\rc\n\n" + link + "\n\n" + unused),
        ]
        for sent, actual in cases:
            with self.subTest(actual=actual):
                self.assertFalse(_same_response_link_serialization(sent, actual))

    async def test_only_angle_wrapped_inline_link_destinations_are_equivalent(self):
        client = LinearClient(oauth_file="unused")
        client.actor_id = "actor"
        receipt = {"id": "activity", "user": {"id": "actor"},
            "agentSession": {"id": "session", "status": "complete", "appUser": {"id": "actor"}},
            "content": {"__typename": "AgentActivityResponseContent", "body": ""}}
        client.graphql = mock.AsyncMock(return_value={"agentActivity": receipt})
        expected = "Evidence [report](https://example.com/report?q=1#part) end"
        receipt["content"]["body"] = "Evidence [report](<https://example.com/report?q=1#part>) end"
        self.assertTrue(await client.verify_response_receipt("activity", "session", expected))
        receipt["content"]["body"] = expected
        self.assertTrue(await client.verify_response_receipt("activity", "session", "Evidence [report](<https://example.com/report?q=1#part>) end"))
        titled = 'Evidence [report](https://example.com/report?q=1#part "source") end'
        receipt["content"]["body"] = 'Evidence [report](<https://example.com/report?q=1#part> "source") end'
        self.assertTrue(await client.verify_response_receipt("activity", "session", titled))
        receipt["content"]["body"] = titled
        self.assertTrue(await client.verify_response_receipt(
            "activity", "session", 'Evidence [report](<https://example.com/report?q=1#part> "source") end'
        ))
        for actual, sent in [
            ("Evidence [other](<https://example.com/report?q=1#part>) end", expected),
            ("Evidence [report](<https://example.com/report?q=2#part>) end", expected),
            ("Evidence [report](<https://example.com/Report?q=1#part>) end", expected),
            ("Evidence [report](<https://example.com/report?q=1#other>) end", expected),
            ("Evidence [report](<https://example.com/report?q=1#part>) extra", expected),
            ("Evidence [report](<https://example.com/report?q=1#part> 'title') end", expected),
            ("Evidence ![report](<https://example.com/report?q=1#part>) end", expected),
            ("`[report](<https://example.com/report>)`", "`[report](https://example.com/report)`"),
            ("```md\n[report](<https://example.com/report>)\n```", "```md\n[report](https://example.com/report)\n```"),
            ("\\[report](<https://example.com/report>)", "\\[report](https://example.com/report)"),
            ("[report](<https://example.com/report>)", "[report](https://example.com/report) extra"),
            ("[report](<https://example.com/report>)", "![report](https://example.com/report)"),
            (None, expected),
            (None, None),
        ]:
            receipt["content"]["body"] = actual
            with self.subTest(actual=actual), self.assertRaises(LinearAPIError) as caught:
                await client.verify_response_receipt("activity", "session", sent)
            self.assertFalse(caught.exception.retryable)
        receipt["content"]["body"] = "Evidence [report](<https://example.com/report?q=1#part>) end"
        for path, value in [(("id",), "other"), (("user", "id"), "other"),
                            (("agentSession", "id"), "other"),
                            (("agentSession", "appUser", "id"), "other"),
                            (("content", "__typename"), "AgentActivityThoughtContent")]:
            wrong = deepcopy(receipt)
            target = wrong
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            client.graphql.return_value = {"agentActivity": wrong}
            with self.subTest(path=path), self.assertRaises(LinearAPIError) as caught:
                await client.verify_response_receipt("activity", "session", expected)
            self.assertFalse(caught.exception.retryable)
        client.graphql.return_value = {"agentActivity": receipt}
        for status in ("pending", "active", "unknown"):
            receipt["agentSession"]["status"] = status
            with self.subTest(status=status), self.assertRaises(LinearAPIError) as caught:
                await client.verify_response_receipt("activity", "session", expected)
            self.assertTrue(caught.exception.retryable)
