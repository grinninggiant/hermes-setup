import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retention import RetentionInventoryReader
from oauth_store import LinearAPIError

class RetentionReadResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_recovers_transient_timeout_without_skipping_evidence(self):
        client = AsyncMock()
        page = {'team': {'id': 'team', 'key': 'OPS', 'issues': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}
        client.graphql.side_effect = [LinearAPIError('Linear GraphQL request timed out'), page, page]
        self.assertEqual(await RetentionInventoryReader(client).read_team('team', 'OPS'), [])
        self.assertEqual(client.graphql.await_count, 3)

    async def test_permanent_failure_remains_fail_closed(self):
        client = AsyncMock()
        client.graphql.side_effect = LinearAPIError('permission denied')
        with self.assertRaises(LinearAPIError):
            await RetentionInventoryReader(client).read_team('team', 'OPS')
        self.assertEqual(client.graphql.await_count, 1)
