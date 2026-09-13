---
name: github-auth
description: "GitHub auth setup: HTTPS tokens, SSH keys, gh CLI login."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Authentication, Git, gh-cli, SSH, Setup]
    related_skills: [github-pr-workflow, github-code-review, github-issues, github-repo-management]
---

# GitHub Authentication Setup

Use for GitHub authentication readiness and explicitly authorized setup. Prefer the existing profile-scoped broker and repository credential helper. Git and GitHub CLI availability do not authorize personal tokens, SSH identity fallback or new credential scope. Inspect installed commands rather than assuming a client exists.

## Detection Flow

When the environment has an approved profile-scoped GitHub App broker, use that existing surface before proposing login or new credentials. Bare `gh auth status` may be intentionally unauthenticated: the broker injects a short-lived installation token only into each child invocation. Discover the repository credential helper and documented gh wrapper, inspect their scope, then perform a metadata-only repository read through the wrapper. Do not fall back to personal SSH, PATs, embedded remote tokens, plaintext storage, or wider scope. New credential/scope changes require explicit approval. Never print a credential helper's raw output.

For restricted broker wrappers, inspect the allowlist before invoking commands: `gh pr ...`, `--jq`, `-H` or query-string routes may be intentionally denied. Use only approved `gh api` routes and raw fields (`-X POST -f base=... -f head=... -f title=...` for PR creation; `-X PUT -f merge_method=squash -f sha=...` for merge), and filter JSON locally without printing tokens. Never modify the broker allowlist to make an operation work. Git HTTP 429 is a transport rate limit, not evidence of missing credentials: bound retries and preserve the existing auth surface. If fetching a squash-merged commit is rate-limited, a reviewed local commit may be built only after the remote merged commit's Git tree SHA matches the local tree exactly; report the distinct reviewed/merged SHAs rather than pretending the merge commit was fetched.

When a user asks you to work with GitHub and no approved broker is present, run this check first:

```bash
# Check what's available
git --version
gh --version 2>/dev/null || echo "gh not installed"

# Check if already authenticated
gh auth status 2>/dev/null || echo "gh not authenticated"
git config --global credential.helper 2>/dev/null || echo "no git credential helper"
```

Authentication status is diagnostic, not blanket authorization. Verify the approved actor, target repository and requested operation separately. If no approved authentication surface is available, follow the setup boundaries below; do not infer permission to create a token, use personal SSH or start a second login from CLI availability alone.

---

## Typed fields through restricted wrappers

`gh api -f/--raw-field` sends strings, not JSON booleans. Never substitute `-f draft=false` for a typed boolean: the nonempty string may be interpreted as true and create a draft unexpectedly. The CLI's `-F/--field` performs typed conversion, but a restricted wrapper may reject it. Validate both field type and wrapper support before a mutation; a permitted field name does not prove correct serialization.

When typed serialization is unavailable, do not test coercion against a live write or change the allowlist without approval. Establish a supported, authorized path before mutation. If an existing PR has an unexpected state, read back that exact PR and recover it through the authorized state transition or human input; do not create duplicates or switch identity. GitHub repository permissions and local broker command policy are distinct layers: identify which rejected the operation before asking for broader access.

## Setup and failure boundaries

Keep the existing approved profile-scoped broker and repository credential helper. Confirm repository identity, installation scope and a metadata-only authenticated read without printing token values. For new machines without a broker, choose a vendor-supported OAuth flow and OS credential manager only within explicitly approved identity and permission scope.

Never recommend plaintext `credential.helper store`, token-bearing remote URLs, printing credential-helper output, copying secrets through chat/clipboard, broad classic PATs as a default, or hand-written plaintext credential files. A broker failure is not permission to fall back to a personal identity. Do not inspect raw `.env`, keyring or credential-store contents to discover a token.

For setup, consult current GitHub CLI documentation and the local `gh auth login --help`; verify approved scopes before starting. Browser/device authorization must remain an explicit human authorization step. Mobile authorization needs a usable expiry window; do not regenerate an uncertain completed flow without reading its state. Do not change global Git URL rewrites, credential helpers or user identity as a side effect of a repository task.

## Troubleshooting and proof

- Authentication failure: distinguish expired credentials, installation/repository scope and transport failure through metadata-only errors. Do not revoke, rotate or expand scopes without the applicable approval.
- `gh` unauthenticated outside the broker: this may be intentional; verify through the approved wrapper rather than running a second login.
- HTTP 429: respect bounded backoff and provider retry guidance; do not switch identities to evade throttling.
- Credential manager unavailable: stop secret delivery; do not fall back to plaintext files.
- Noninteractive login: use supported device/OAuth interaction instead of faking a PTY success or asking for a secret in chat.

Verify the requested operation through the exact authorized repository API and read back writes. Report source tests, auth readiness, remote acceptance and CI state separately. Keep evidence to nonsecret actor/repository identities, scope descriptions and result metadata. A successful login alone does not prove repository write permission.
