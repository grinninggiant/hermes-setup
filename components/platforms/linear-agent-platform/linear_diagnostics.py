#!/usr/bin/env python3
"""Read-only Linear diagnostics for one explicit Hermes profile (OPS-216).

Replaces ad hoc `sqlite3 linear-bridge.sqlite3 ...` and raw GraphQL snippets.
Never mutates vendor or local state; ledgers open with SQLite `mode=ro`.
Exit codes: 0 ok, 2 profile/config error, 3 vendor/read error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import yaml

# (table, state column) pairs summarized by `ledger`; missing tables are reported, not fatal.
BRIDGE_COUNTS = (
    ("outbox", "state"),
    ("direct_activation_grants", "state"),
    ("waiting_executions", "state"),
    ("activation_waits", "state"),
    ("deliveries", "state"),
)
OPEN_OUTBOX = "SELECT id, operation, state, attempts, updated_at FROM outbox WHERE state IN ('pending','in_flight','dead') ORDER BY updated_at DESC LIMIT 20"
UNKNOWN_MCP = "SELECT substr(operation_key,1,12) AS operation_key, tool_name, substr(error_code,1,60) AS error_code, updated_at FROM linear_mcp_operations WHERE status='outcome_unknown' ORDER BY updated_at DESC LIMIT 10"


class ConfigError(Exception):
    pass


def load_platform(home: Path) -> dict:
    """Profile-local Linear config; fail closed when anything is missing."""
    config_path = home / "config.yaml"
    if not config_path.is_file():
        raise ConfigError(f"config_missing:{config_path}")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    extra = (((config.get("gateway") or {}).get("platforms") or {}).get("linear") or {}).get("extra")
    if not isinstance(extra, dict) or not extra.get("database_path") or not extra.get("oauth_file"):
        raise ConfigError("linear_platform_not_configured")
    return extra


def _ro(path: str) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise ConfigError(f"ledger_missing:{path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _counts(con: sqlite3.Connection, table: str, column: str) -> dict | None:
    try:
        return {r[0]: r[1] for r in con.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY 1")}
    except sqlite3.OperationalError:
        return None  # table absent in this schema version


def ledger(extra: dict) -> dict:
    con = _ro(extra["database_path"])
    try:
        result = {
            "bridge": {t: _counts(con, t, c) for t, c in BRIDGE_COUNTS},
            "bindings": con.execute("SELECT COUNT(*) FROM issue_session_bindings").fetchone()[0],
            "open_outbox": [dict(r) for r in con.execute(OPEN_OUTBOX)],
        }
    finally:
        con.close()
    mcp_path = (extra.get("outbound_mcp") or {}).get("ledger_path")
    if mcp_path:
        mcp = _ro(mcp_path)
        try:
            result["outbound_mcp"] = _counts(mcp, "linear_mcp_operations", "status")
            result["outcome_unknown_recent"] = [dict(r) for r in mcp.execute(UNKNOWN_MCP)]
        finally:
            mcp.close()
    return result


def local_binding(extra: dict, issue_id: str) -> str | None:
    con = _ro(extra["database_path"])
    try:
        row = con.execute("SELECT session_id FROM issue_session_bindings WHERE issue_id=?", (issue_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


async def _vendor(extra: dict, command: str, ref: str) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from linear_client import LinearClient

    client = LinearClient(oauth_file=extra["oauth_file"])
    try:
        await client.connect()
        if command == "issue":
            ctx = await client.get_channel_routing_context(ref)
            ctx["local_bound_session"] = local_binding(extra, ctx["id"])
            return ctx
        ctx = await client.get_agent_session_delivery_context(ref)
        ctx["description_chars"] = len(ctx.pop("description"))  # body stays out of diagnostics
        ctx["terminal_responses"] = await client.get_agent_session_terminal_response_count(ref)
        return ctx
    finally:
        await client.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="linear-diagnostics", description="Read-only Linear diagnostics")
    where = root.add_mutually_exclusive_group(required=True)
    where.add_argument("--profile", help="Hermes profile name under ~/.hermes/profiles")
    where.add_argument("--home", type=Path, help="Explicit profile home directory")
    root.add_argument("--json", action="store_true", help="Compact stable JSON")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("ledger", help="Local bridge + outbound MCP ledger summary")
    sub.add_parser("issue", help="Issue state/delegate/sessions + local binding").add_argument("ref", help="OPS-123 or UUID")
    sub.add_parser("session", help="Agent Session owner/state/terminal responses").add_argument("session_id")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = args.home or Path.home() / ".hermes/profiles" / args.profile
    try:
        extra = load_platform(home)
        result = ledger(extra) if args.command == "ledger" else asyncio.run(
            _vendor(extra, args.command, args.ref if args.command == "issue" else args.session_id))
    except ConfigError as exc:
        print(json.dumps({"status": "config_error", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:  # vendor/transport errors: type + message only, never payloads
        print(json.dumps({"status": "read_error", "type": type(exc).__name__, "reason": str(exc)[:200]}), file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")) if args.json
          else json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
