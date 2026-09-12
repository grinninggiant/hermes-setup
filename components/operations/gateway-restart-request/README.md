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

## Runtime ownership and recovery gate

Each newly recorded intent is bound to a random loaded-module runtime nonce plus PID. `claim`, `submit`, and the native dispatch guard require that runtime identity. A fresh process cannot inherit execution authority by reading a saved owner ID, and duplicate `record` does not rebind an old operation. The PID component also rejects a forked child that inherited the nonce. Reloading the store module invalidates queued dispatch guards.

Source migration adds `runtime_id` with an empty default for legacy rows; it preserves their metadata without assigning current runtime authority. An old row may still say `pending` or `claimed`: use `runtime_owns` to distinguish it from executable local work. This has only run on test databases; do not mix old and new writers.

This is a **deny-by-default recovery boundary**, not implemented automatic continuation. No API currently rebinds a prior-runtime operation. A future reviewed reconciliation path must verify the coordinator's actual result, exact session/checkpoint, native recovery ownership and newer human/Stop/Done evidence before any controlled transfer. Do not bypass this by replacing the operation ID, setting the runtime column directly, or clearing state. Read-only evidence survives the boundary; successful restart continuation and rollback acceptance remain open.

## Native inbound fence adapter (not activated)

`continuation_delivery.fence_authorized_inbound` can be registered on `pre_gateway_dispatch` with an immutable owner home and store. It checks owner/store/source scope and invokes native source authorization itself, because this hook runs before native auth. Internal events cannot fence. Native additive telemetry kwargs are accepted but not persisted. The helper returns no routing directive and does not alter the human command.

The installed-tree fixture registers it through a real `PluginContext` and invokes native `_hm_admit_event`; authorization, session lookup and routing resolution are controlled test doubles. It covers `/stop` and ordinary new-message ingress, not the complete Stop handler, live Telegram or restart. The production plugin does not register it yet.

**Failure boundary:** native observer dispatch still suppresses a fence-write exception and lets the human event continue. The helper now trips a sticky, canonical-path-scoped circuit breaker in the loaded store module after an authorized session lookup/fence failure. `submit` and the native ingress guard reject continuation while latched; another handle in that same module cannot clear it. Pending metadata is preserved. There is no reset API.

This is process-local defense, NOT durable recovery. Process replacement or module reload does not retain the latch; another independent process/module instance does not share it. Activation therefore still requires authoritative startup reconciliation and a verified single-owner/module lifetime. Never restart merely to clear the latch or interpret the passing same-process test as restart safety. Human Stop ingress does not depend on a successful plugin write.

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
