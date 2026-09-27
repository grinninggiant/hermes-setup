# Component taxonomy

Canonical repository components live at:

```text
components/<domain>/<vendor-or-product>-<capability>/
```

A component name must reveal both what product/vendor it belongs to and what capability it provides. Domain directories group ownership and purpose; they are not catch-alls. New top-level `integrations/`, `misc/`, `utils/`, `helpers/`, or `other/` component buckets are forbidden.

## Canonical catalog

| Domain | Component | Purpose |
|---|---|---|
| Platforms | [`platforms/linear-agent-platform`](platforms/linear-agent-platform/README.md) | Linear Agent Sessions, lifecycle, outbound policy, and plugin deployment |
| Secrets | [`secrets/1password-hermes-bootstrap`](secrets/1password-hermes-bootstrap/README.md) | 1Password SDK-backed Hermes credential bootstrap |
| Memory | [`memory/honcho-codex-bridge`](memory/honcho-codex-bridge/README.md) | Honcho inference to Hermes Codex OAuth bridge |
| Operations | [`operations/gateway-restart-coordinator`](operations/gateway-restart-coordinator/README.md) | Ordered and recoverable gateway restart coordination |
| Operations | [`operations/gateway-restart-request`](operations/gateway-restart-request/README.md) | Agent-side validated request tool for the coordinator |
| Security | [`security/github-personal-ssh-guard`](security/github-personal-ssh-guard/README.md) | Guard against agent use of the host's personal GitHub SSH identity |
| Security | [`security/github-app-token-broker`](security/github-app-token-broker/README.md) | Organization-scoped GitHub App tokens for agent `gh` and Git HTTPS |
| Commands | [`commands/codex-usage-command`](commands/codex-usage-command/README.md) | Fleet-wide Telegram `/codex_usage` command |

## Naming contract

1. Add components only under an existing, specific domain or introduce a reviewed domain whose name describes a stable responsibility.
2. Use lowercase kebab-case for domain and component directory names.
3. Component directory names must include the vendor/product and the real capability; avoid opaque names such as `adapter`, `plugin`, `service`, or `tool` by themselves.
4. Executable source tools and installers must also carry vendor/product plus purpose where a generic filename would be ambiguous outside its directory.
5. Keep source paths and deployed runtime paths separate. A source taxonomy rename does not authorize a runtime path, credential, config, LaunchAgent, or service mutation.
6. Update source, tests, installers, deploy helpers, documentation, commands, and relative links in one change. Historical snapshots may retain old paths only when clearly labeled as non-current evidence.

`tests/test_component_taxonomy.py` enforces the canonical set and rejects catch-all or known opaque source names deterministically.

## CLI capability registry (OPS-216)

Ad hoc read-only discovery may be temporary once. A capability needed twice gets a row here; mutations never use ad hoc scripts. Every row: explicit profile/home, fail-closed config, metadata-only output, deterministic exit codes.

| Command | Owner | R/W | Scope | Output | Guard / approval |
|---|---|---|---|---|---|
| `linear_diagnostics.py --profile P ledger` | `platforms/linear-agent-platform` | R (SQLite `mode=ro`) | one profile | JSON, keys truncated | exit 2 on missing config/ledger |
| `linear_diagnostics.py --profile P issue OPS-N` | same | R (vendor) | profile OAuth identity | state, delegate, sessions, local binding | exit 3 on vendor error |
| `linear_diagnostics.py --profile P session ID` | same | R (vendor) | profile OAuth identity | owner/state/terminal count; no body | exit 3 on vendor error |
| `linear_save_issue` / `linear_save_comment` / `mark_acceptance` tools | same (plugin) | W | profile app user, allowlisted team | read-back | operation key, outbound policy, ledger `outcome_unknown` |
| `retention.py`, `quota_watchdog.py`, `direct_reconciliation.py` | same | R, W only with `--apply` + dry-run hash | explicit team UUID | JSON manifest | dry-run hash, explicit approval for trash |
| `restartctl.py status [--task-id T] [--recent N]` | `operations/gateway-restart-coordinator` | R | fleet queue | JSON, no payload/evidence body | exit 2 on missing task |
| `request_gateway_restart` tool / `restartctl.py request` | `operations/gateway-restart-request` | W | general, coder | queue row + ledger | requester identity, artifact hash, one restart |
| `backup_ops.py` | `scripts/` (OPS-204) | R/W per subcommand | fleet | JSON | retention policy guards (OPS-220) |
| `derya-gh` | `security/github-app-token-broker` | per `gh` command | org App installation | `gh` | short-lived token, no personal SSH |
| `ntn` + `notion_ops.py` | vendor CLI + `notion-cli` skill | R/W | profile Notion token | JSON | `notion-knowledge-ops` dedup/read-back |
| `hermes sessions …`, `hermes config get/set` | upstream core | R/W | profile | text/JSON | native; no direct `state.db` / `config.yaml` edits |

Deprecated (enforced by `tests/test_cli_capability_registry.py` over docs and skills): `sqlite3 …linear-bridge.sqlite3`, `sqlite3 …linear-outbound-mcp.sqlite3`, `sqlite3 …restart-coordinator/queue.sqlite3`, raw `api.linear.app/graphql` snippets. Remaining audited gaps are tracked as follow-up issues, not here.
