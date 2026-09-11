"""Missing requester regression: human owner binding is clarify-only."""
import json
import unittest
from unittest import mock
from test_native_clarify import NativeClarifyTests
from tools import clarify_gateway


class OwnerBindingTests(NativeClarifyTests):
    async def test_missing_requester_owner_reply_resolves_without_granting_commands(self):
        active = self.adapter._active_turn_events['linear-session-221']
        active.source.user_id = None
        context = await self.adapter._linear.get_agent_turn_context('linear-session-221')
        context['issue']['assignee'] = {'id': 'user-221', 'app': False}
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
        clarify_gateway.register('owner-gap', self.key, 'Target?', ['yes', 'no'])
        response = await self.webhook(self.payload(body='1'))
        self.assertEqual(json.loads(response.text)['status'], 'clarify_resolved')
        self.assertEqual(clarify_gateway.wait_for_response('owner-gap', .01), 'yes')
        self.assertIsNone(active.source.user_id)
        self.adapter.handle_message = mock.AsyncMock()
        command = await self.webhook(self.payload(body='/approve', webhook='command-after'))
        self.assertEqual(json.loads(command.text)['status'], 'native_command_requester_unavailable')
        self.adapter.handle_message.assert_not_awaited()


    async def test_missing_requester_binding_is_fail_closed_and_direct_safe(self):
        import copy
        active = self.adapter._active_turn_events['linear-session-221']
        active.source.user_id = None
        base = await self.adapter._linear.get_agent_turn_context('linear-session-221')
        base['issue']['assignee'] = {'id': 'user-221', 'app': False}
        cases = [
            ('owner-null', 'owner', None),
            ('owner-app', 'owner', {'id': 'user-221', 'app': True}),
            ('owner-unknown', 'owner', {'id': 'user-221'}),
            ('owner-other', 'owner', {'id': 'other-user', 'app': False}),
            ('session-other', 'id', 'other-session'),
            ('app-other', 'app_user_id', 'other-app'),
            ('session-closed', 'status', 'complete'),
            ('issue-other', 'issue-id', 'other-issue'),
            ('delegate-other', 'delegate', {'id': 'other-app'}),
            ('terminal', 'state', {'type': 'completed'}),
        ]
        for name, field, value in cases:
            with self.subTest(name=name):
                context = copy.deepcopy(base)
                if field == 'owner':
                    context['issue']['assignee'] = value
                elif field == 'issue-id':
                    context['issue']['id'] = value
                elif field in {'delegate', 'state'}:
                    context['issue'][field] = value
                else:
                    context[field] = value
                self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=context)
                clarify_gateway.register(name, self.key, 'Target?', ['yes'])
                response = await self.webhook(self.payload(body='1', webhook=name))
                self.assertNotEqual(json.loads(response.text)['status'], 'clarify_resolved')
                self.assertFalse(clarify_gateway.get_pending_for_session(self.key, include_choice_prompts=True).event.is_set())
                clarify_gateway.clear_session(self.key)
        active.metadata['linear_direct_activation'] = True
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(return_value=base)
        clarify_gateway.register('direct-owner', self.key, 'Target?', ['yes'])
        response = await self.webhook(self.payload(body='1', webhook='direct-owner'))
        self.assertEqual(json.loads(response.text)['status'], 'clarify_resolved')
        self.assertIsNone(active.source.user_id)


if __name__ == '__main__':
    unittest.main()
