# Root script ownership

## Placement rule

- `scripts/` holds only fleet-wide utilities, runtime-compat launchers, and explicitly labelled recovery/retired artifacts.
- A script owned by one component lives in `components/<domain>/<component>/scripts/` with its tests (see [components/README.md](../components/README.md)).
- Source paths are not runtime paths. LaunchAgents, cron jobs, Git config and profile shells call **deployed copies** under `~/.hermes/scripts/` and `~/.hermes/runtime/`. Moving source never changes a runtime path; changing a runtime path is a separate change with its own 9-profile impact scan and canary.
- Promotion = tests pass → copy to the deployed path → byte-for-byte `cmp` → canary. A deployed copy that is ahead of source is drift: fold it back into source in the next change.
- Nothing is deleted without a caller scan (repo, `~/Library/LaunchAgents/ai.hermes.*`, all nine `profiles/*/cron/jobs.json`, profile configs/shells, docs/runbooks) and a rollback coordinate (the removing commit).

## Generated artifacts

`__pycache__/` and `*.py[cod]` are ignored by `.gitignore` and never tracked (tracked count: 0). On-disk caches in the source checkout and `~/.hermes/scripts/__pycache__` are disposable; nothing may import from or reference them.

## Inventory (OPS-204, 2026-09-27)

Deployed column: `=` byte-identical to source, `-` not deployed.

| Script | Kind | Authoritative caller | Deployed | Decision |
|---|---|---|---|---|
| `backup_ops.py` | fleet utility | `profile-backup-quick.sh`, Honcho backup/restore, `tests/test_backup_ops.py` | `=` `~/.hermes/scripts/backup_ops.py` | keep |
| `backup-retention-policy.json` | fleet config | `backup_ops.py retention-report` (docs/14) | - | keep |
| `profile-backup-quick.sh` | fleet utility | `ai.hermes.backup-state` | `=` | keep |
| `backup-honcho.sh` | compat wrapper | manual; execs `~/.hermes/services/honcho-stack/backup-honcho.sh` (launchd calls that target directly) | `=` | keep (thin wrapper, tested) |
| `config-snapshot.sh` | fleet utility | `ai.hermes.config-snapshot` | `=` | keep |
| `notify-online.sh` | fleet utility | `ai.hermes.fleet-online` | `=` | keep |
| `watchdog.sh` | fleet utility | `ai.hermes.watchdog` | `=` | keep |
| `manage_hermes_agent_patches.py` | upgrade tool (manual) | `patches/hermes-agent/*.patch` lifecycle, own test | - | candidate: manual-only, keep while core patches exist |
| `wire-tinyfish.sh` | installer (manual) | docs/08, docs/10 | - | candidate: manual-only |
| `cleanup_honcho_workspaces.py` | recovery-only, destructive | own test only | - | recovery-only; never scheduled |
| `recover-marketing-state-approved.sh` | recovery-only | `tests/test_backup_ops.py` fail-closed contract | - | recovery-only |
| `setup-bots.sh` | retired (exits 1) | none; docs mark retired | - | retired, kept as history |
| `bot-tokens.env.example` | retired template | `setup-bots.sh`, docs | - | retired, never accepts real values |

### Moved out of root (OPS-204)

| Old source | New source | Deployed runtime (unchanged) |
|---|---|---|
| `scripts/hermes-agent-ssh-guard.sh` | `components/security/github-personal-ssh-guard/scripts/` | `~/.hermes/scripts/hermes-agent-ssh-guard` |
| `scripts/github_app_token_broker.py` | `components/security/github-app-token-broker/scripts/` | `~/.hermes/runtime/github-app-token-broker/` |
| `scripts/derya-gh-keychain.sh` | same component | `~/.hermes/scripts/derya-gh` |
| `scripts/derya-gh-credential.sh` | same component | `~/.hermes/scripts/derya-gh-credential` |
| `tests/test_github_app_token_broker.py` | same component `tests/` | - |
| `scripts/hermes-gateway-keychain.sh` | `components/secrets/1password-hermes-bootstrap/scripts/onepassword_hermes_gateway_launcher.sh` | `~/.hermes/scripts/hermes-gateway-keychain.sh` |
| `scripts/hermes-send-keychain.sh` | merged into the component's existing `onepassword_hermes_send_launcher.sh` (stale pin replaced by the deployed copy) | `~/.hermes/scripts/hermes-send-keychain.sh` |

The component's four launchers (gateway, send, serve, desktop) are byte-identical to their deployed copies.

### Retired (OPS-204)

- `scripts/test_general_canary_wrapper.py` — no caller; pinned release hashes no longer matched the live wrapper (11/11 errors). Superseded by `components/secrets/1password-hermes-bootstrap/tests`. Rollback: revert the OPS-204 commit.
