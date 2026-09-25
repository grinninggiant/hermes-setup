# Exact-commit Agent candidate builder

Canonical owner: this existing component owns the managed runtime launchers and
candidate deployment boundary. `agent_release_builder.py` is packaging only;
it does not call the credential bootstrap, select a live release, or restart anything.
Promotion/rollback is a separate reviewed operation. No shared launcher is changed.

```sh
python3 components/secrets/1password-hermes-bootstrap/deployment/agent_release_builder.py \
  --repo /absolute/operator-verified/hermes-agent \
  --commit <exact-40-hex-existing-commit> \
  --target /absolute/new-final-release-directory \
  --python /absolute/python3 --extra mcp
```

Use a uv version supporting the committed lock schema (be141053 tested with
uv 0.11.33; host uv 0.9.24 cannot parse it). Select uv via PATH. Dependencies
are installed with `uv sync --frozen`; no lock generation or upgrades are permitted.
Hermes intentionally refuses non-editable wheel builds, so the supported editable
install points at the candidate's own sealed source, never the working checkout.
The virtual environment is created at its final path and never renamed.

The caller supplies the already trusted repository: this tool verifies exact
commit object identity, not authorship/signature or remote trust. It exports only
committed source, rejecting archive links/special files. No working-tree profile,
credential, Git global configuration, or caller credential environment is copied.
Package installation can use public network dependencies. Build code from the
trusted commit executes: this is not a sandbox for untrusted repositories.

The schema-3 manifest records repository/target, commit/tree, archive and lock
SHA256, requested/resolved interpreter and its SHA256, uv version, extras, and
import checks. `source_files` is derived from the trusted Git archive before any
build execution, never from backend output. `generated_source_files` separately
records the exact-commit `.hermes_build_sha` marker plus the project's named
egg-info directory and allowlisted metadata. The marker is written only from
the verified Git object and any later marker drift fails static verification;
`venv_files` inventories every directory/file (hash and executable bit), including
bootstrap `.pth`, editable finders, direct URLs, console scripts and pyvenv.cfg.
Only bin/python-family symlinks resolving to the requested interpreter are allowed;
their link text, resolved path and target hash are recorded. Other links/special
files and structural symlinks are rejected. External editable/data mappings fail.
Legitimate executable `.pth` bootstrap from trusted build dependencies is preserved,
not sandboxed; its later drift is rejected before candidate Python executes.

Success requires actual interpreter execution at the final prefix, `uv pip check`,
imports (default: hermes_cli.main, run_agent, tui_gateway.server, and
hermes_cli.web_server:start_server with a callable assertion), console-script
final-path validation, and actual `hermes --help` execution. This establishes
packaging readiness, **not Desktop/live-profile/plugin/credential readiness**.

Files are sealed owner-read-only (owner-executable where required), directories
owner-read/execute. This is operational immutability, not protection against the
owner intentionally chmod'ing or replacing files. No venv symlink target is chmod'd.
Matching re-runs rederive archive/lock/source provenance from Git, compare the
full source and venv inventories before candidate execution, execute readiness,
and recheck the static seal afterward without rewriting the manifest. Older
manifests are rejected without repair. Matching re-runs never reinstall;
mismatched, incomplete, symlink or noncanonical targets fail closed. Builds claim
an absent target exclusively. Failures retain all evidence; there is deliberately
no retry-overwrite, cleanup, activation, pointer, rollback, or deletion command.
Choose a new final path after reviewing a failed candidate.

Tests (real temporary Git project and uv-created venv; public setuptools build dep):

```sh
python3 -m unittest discover \
  -s components/secrets/1password-hermes-bootstrap/tests \
  -p test_agent_release_builder.py -v
```

Covers exact commit/tree, final interpreter prefix and executable console script,
matching idempotence, collision/invalid commit refusal, restrictive manifest,
failed-import evidence retention, and preservation of an unrelated rollback file.
