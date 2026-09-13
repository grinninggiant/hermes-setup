# Versioned skill library

This directory is the maintained source for reviewed, reusable skill packages. Each package uses the Hermes `SKILL.md` format; references, scripts, templates and assets belong beside their owning skill when present. The initial packages have no support files. Preserve upstream authorship and license metadata.

`manifest.json` records exact content hashes and profiles with verified deployment. Repository source, installed files, fresh loader results and existing-session injection are different evidence layers. A merged source is not a fleet rollout.

## Changes

Use a branch and PR. Review the entire changed capability, not just file size. Run the structural tests and a risk-appropriate behavior comparison. Deploy only to authorized profiles, verify exact installed/loader hashes, and retain byte-exact rollback artifacts outside active skill discovery. Historical revision fixtures under `docs/skill-revisions/` are test/evidence material, not a second active source.

## Public repository boundary

Publish only reviewed reusable methods. Do not bulk-copy a profile directory here. Exclude credentials, auth stores, config with private identifiers, session databases, logs, personal health/finance facts and runtime snapshots. Sensitive domain state stays in its governed private storage; a private repository is not a secret vault either.

Run `uv run --with-requirements skills/requirements-test.txt python skills/test_library.py` from the repository root, or use an existing isolated environment with those dependencies. Frontmatter is parsed as YAML, including quoted, unquoted and multiline descriptions. These source checks are not end-to-end model or deployment acceptance.
