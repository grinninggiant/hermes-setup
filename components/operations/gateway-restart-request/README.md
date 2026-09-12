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

## Scoped artifact-only installation

For the current general-only rollout, preserve configuration and do not install into coder:

```bash
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py --profile general --plugin-only
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py --profile general --plugin-only --apply
```

The installed allowlist contains `plugin.yaml`, `__init__.py`, `continuation_store.py`, and `continuation_delivery.py`. Packaging tests require exact source bytes and an isolated installed-tree import; native integration tests invoke this installer before exercising SQLite-to-ingress behavior. Missing required files fail before the old plugin is moved, and failed final promotion restores the old discoverable directory.

`--plugin-only` does not grant gateway injection, change configuration, start a worker, or activate continuation. The ledger and delivery bridge remain helpers: restart-result reconciliation, authoritative event fencing, startup ownership and actual completion/delivery evidence must be integrated and verified before claiming automatic restart continuation.

A gateway restart is required for each installed profile to load changed plugin code. Use the existing external coordinator for activation; staged files are not loaded-code evidence.
