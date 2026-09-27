# GitHub App Token Broker

Derya's organization-scoped GitHub App broker: short-lived installation tokens for `gh` and Git HTTPS, restricted to `grinninggiant/<repository>`. Full contract: [Credential Management](../../../docs/15-credential-management.md).

| Source | Deployed runtime (unchanged by source moves) | Caller |
|---|---|---|
| `scripts/github_app_token_broker.py` | `~/.hermes/runtime/github-app-token-broker/github_app_token_broker.py` | both wrappers |
| `scripts/derya-gh-keychain.sh` | `~/.hermes/scripts/derya-gh` | manual/agent `gh` runbooks (docs/15) |
| `scripts/derya-gh-credential.sh` | `~/.hermes/scripts/derya-gh-credential` | Git credential helper in agent sessions |

Secrets never enter this repository; wrappers resolve the pinned 1Password references at execution time.

## Tests

```bash
python3 -m unittest discover -s components/security/github-app-token-broker/tests -v
```

## Promote

Run the tests, copy each source to its deployed path with mode `0700`, require a byte-for-byte `cmp` match, then run `~/.hermes/scripts/derya-gh auth status`.
