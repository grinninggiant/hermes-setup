"""Evidence-only reconciliation. Never renews an activation grant or calls a mutation."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

QUERY = """query DirectReconciliationIssue($id: String!) {
  issue(id: $id) {
    id identifier title updatedAt creator { id } delegate { id }
    team { id } state { id type }
  }
}"""
ERRORS = frozenset({'immediate_retention_dry_run_unavailable', 'unbound_reservation_expired'})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def database(home, name, *, writable=False):
    home = Path(home)
    path = home / 'state' / name
    if home.name != 'general' or home.resolve() != home.absolute():
        raise ValueError('Only the explicit, non-symlink general profile is supported')
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Symlink database paths are forbidden')
    for item in (home, path.parent, path):
        if item.stat().st_uid != os.getuid():
            raise ValueError('Profile database ownership mismatch')
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('Database directory must be owner-only')
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    return db


def snapshots(home, keys):
    if not keys or len(keys) != len(set(keys)) or any(not re.fullmatch('[0-9a-f]{64}', k) for k in keys):
        raise ValueError('Explicit unique operation SHA256 values are required')
    grants = database(home, 'linear-bridge.sqlite3')
    outbound = database(home, 'linear-outbound-mcp.sqlite3')
    try:
        rows = {}
        for row in grants.execute('SELECT * FROM direct_activation_grants'):
            key = hashlib.sha256(row['operation_key'].encode()).hexdigest()
            if key in keys:
                result = outbound.execute('SELECT * FROM linear_mcp_operations WHERE operation_key=?', (key,)).fetchone()
                if result is None:
                    raise ValueError('No exact outbound operation')
                rows[key] = (dict(row), dict(result))
        if set(rows) != set(keys):
            raise ValueError('An exact Direct operation is missing')
        return rows
    finally:
        grants.close()
        outbound.close()


def issue_reference(value):
    if value.startswith('quota-admission:v2:'):
        encoded = value[len('quota-admission:v2:'):]
        value = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))['result_id']
    if not isinstance(value, str):
        raise ValueError('Invalid outbound result')
    if re.fullmatch('[A-Z][A-Z0-9]*-[1-9][0-9]*', value):
        return value
    if str(uuid.UUID(value)) != value:
        raise ValueError('Noncanonical outbound issue UUID')
    return value


async def build_plan(home, client, keys):
    records = []
    for key, (grant, outbound) in sorted(snapshots(home, keys).items()):
        if grant['state'] != 'failed' or grant['issue_id'] is not None or grant['last_error'] not in ERRORS:
            raise ValueError('Only supported failed, unbound grants can be reconciled')
        if outbound['status'] != 'success' or outbound['tool_name'] != 'save_issue':
            raise ValueError('Outbound create is not durably successful')
        if grant['source_profile'] != 'general' or outbound['profile_id'] != 'general':
            raise ValueError('Profile mismatch')
        if not client.actor_id or grant['actor_id'] != client.actor_id or outbound['actor_id'] != client.actor_id:
            raise ValueError('Actor mismatch')
        if outbound['team_id'] != grant['team_id']:
            raise ValueError('Outbound team mismatch')
        ref = issue_reference(outbound['result_id'])
        issue = (await client.graphql(QUERY, {'id': ref})).get('issue') or {}
        issue_id = issue.get('id', '')
        if str(uuid.UUID(issue_id)) != issue_id or ref not in (issue_id, issue.get('identifier')):
            raise ValueError('Vendor issue reference mismatch')
        if any((issue.get(field) or {}).get('id') != grant['actor_id'] for field in ('creator', 'delegate')):
            raise ValueError('Vendor ownership mismatch')
        team = (issue.get('team') or {}).get('id')
        title = issue.get('title')
        if not isinstance(team, str) or team != grant['team_id'] or not isinstance(title, str) or not title:
            raise ValueError('Vendor team/title missing or mismatched')
        fingerprint = hashlib.sha256((team + '\0' + title).encode()).hexdigest()
        if fingerprint != grant['issue_fingerprint'] or not issue.get('updatedAt'):
            raise ValueError('Vendor fingerprint or revision mismatch')
        records.append({'operation_key_sha256': key, 'grant_sha256': digest(grant),
            'outbound_sha256': digest(outbound), 'issue_id': issue_id,
            'identifier': issue['identifier'], 'vendor_updated_at': issue['updatedAt'],
            'vendor_state': issue.get('state'), 'issue_fingerprint': fingerprint,
            'original_error': grant['last_error']})
    return {'records': records, 'sha256': digest(records), 'mode': 'no_activation'}


async def reconcile(home, client, keys, *, expected_sha256):
    plan = await build_plan(home, client, keys)
    if plan['sha256'] != expected_sha256:
        raise ValueError('Reconciliation evidence changed; obtain a fresh dry-run')
    db = database(home, 'linear-bridge.sqlite3', writable=True)
    try:
        db.execute('BEGIN IMMEDIATE')
        current = snapshots(home, keys)
        for record in plan['records']:
            grant, outbound = current[record['operation_key_sha256']]
            if digest(grant) != record['grant_sha256'] or digest(outbound) != record['outbound_sha256']:
                raise ValueError('Local state changed before reconciliation')
        db.execute('''CREATE TABLE IF NOT EXISTS direct_activation_reconciliations (
            operation_key_sha256 TEXT PRIMARY KEY, issue_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL, reconciled_at INTEGER NOT NULL)''')
        for record in plan['records']:
            existing = db.execute('SELECT issue_id,evidence_json FROM direct_activation_reconciliations WHERE operation_key_sha256=?', (record['operation_key_sha256'],)).fetchone()
            encoded = json.dumps(record, sort_keys=True)
            if existing is not None:
                if existing['issue_id'] != record['issue_id'] or existing['evidence_json'] != encoded:
                    raise ValueError('Existing reconciliation differs; no overwrite permitted')
                continue
            db.execute('INSERT INTO direct_activation_reconciliations VALUES (?,?,?,?)',
                (record['operation_key_sha256'], record['issue_id'], encoded, int(time.time())))
        db.commit()
        return {'status': 'reconciled_no_activation', 'sha256': plan['sha256'], 'records': plan['records']}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation-sha256', action='append', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.expected_sha256:
        parser.error('--apply requires the exact dry-run --expected-sha256')
    home = Path.home() / '.hermes/profiles/general'
    if Path(os.environ.get('HERMES_HOME', '')).resolve() != home.resolve():
        parser.error('This command must run in the existing general profile environment')
    import yaml
    from linear_client import LinearClient
    config = yaml.safe_load((home / 'config.yaml').read_text())
    platform = config['gateway']['platforms']['linear']
    # Resolve credentials through the same configured OAuth store as the native client.
    oauth_file = platform['extra']['oauth_file']
    async def run():
        client = LinearClient(oauth_file=oauth_file)
        try:
            await client.connect()
            if args.apply:
                return await reconcile(home, client, args.operation_sha256, expected_sha256=args.expected_sha256)
            return await build_plan(home, client, args.operation_sha256)
        finally:
            await client.close()
    print(json.dumps(asyncio.run(run()), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
