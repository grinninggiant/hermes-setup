# Gateway Restart Request

Profile-local standalone plugin for exactly `general` and `coder`.

- Registers `request_gateway_restart`, a narrow model-facing facade over the external Restart Coordinator.
- Derives requester authorization from the existing facade (`HERMES_HOME` plus live gateway ancestry); there is no requester argument.
- Blocks model-driven direct `hermes gateway restart` and gateway-targeting `launchctl` mutations through `pre_tool_call`.
- Does not intercept the human `/restart` command; that remains documented emergency fallback only.
- Installs into no other profile and does not mutate coordinator state or restart gateways.

## Test

```bash
python3 -m unittest components/operations/gateway-restart-request/tests/test_gateway_restart_request.py -v
```

## Install

```bash
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py --apply
```

## Durable cancellation generation

`ContinuationStore.fence(session_id, authorized=True)` atomically increments the session's persistent generation and cancels its existing eligible intents. It records the fence even when there are no intents yet, so a delayed pre-fence `record()` cannot recreate work after cancellation.

An integration must capture `generation(session_id)` **before** reading authoritative authorization/checkpoint state, then pass it as `record(..., expected_generation=captured)`. The write compares generations inside the same SQLite write transaction as insertion. A mismatch is `stale_generation`; do not blindly reread the generation and retry old authorization. Generation is not authorization. Default generation zero permits legacy callers only for sessions never fenced. Existing operation IDs remain bound and cancelled rows remain terminal.

This source-only additive table has not been migrated into a live continuation database. Never mix pre-generation writers with this contract; they do not understand persistent fences. Deployment must establish one supported writer version and preserve the database on rollback. Real native event/Stop ownership and post-restart reconciliation remain separate integration gates.

## Scoped artifact-only installation

For the current general-only rollout, preserve configuration and do not install into coder:

```bash
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py --profile general --plugin-only
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py --profile general --plugin-only --apply
```

The installed allowlist contains `plugin.yaml`, `__init__.py`, `continuation_store.py`, and `continuation_delivery.py`. Packaging tests require exact source bytes and an isolated installed-tree import; native integration tests invoke this installer before exercising SQLite-to-ingress behavior. Missing required files fail before the old plugin is moved, and failed final promotion restores the old discoverable directory.

`--plugin-only` does not grant gateway injection, change configuration, start a worker, or activate continuation. The ledger and delivery bridge remain helpers: restart-result reconciliation, authoritative event fencing, startup ownership and actual completion/delivery evidence must be integrated and verified before claiming automatic restart continuation.

A gateway restart is required for each installed profile to load changed plugin code. Use the existing external coordinator for activation; staged files are not loaded-code evidence.
