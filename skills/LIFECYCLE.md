# Skill source and creation lifecycle

## Ownership and placement

Keep origin separate from current maintenance ownership. A locally modified upstream skill remains external-derived. Missing upstream matches or a local filesystem path do not prove studio authorship.

- Shared external packages live in the shared external library. Byte-different variants retain distinct roots; do not overwrite one profile's reviewed variant with another.
- Shared studio-authored methods belong in the existing canonical library.
- Only irreducible persona/domain extensions belong in profile-local `internal-skills` directories. Identity, credentials, routing and state remain in their governed configuration or state stores.
- Existing packages with uncertain origin remain explicitly unclassified; do not relocate or relabel them by assumption.

Before creating a skill, use the canonical-before-create gate: reuse or extend the owning canonical capability when possible. A creation directory is a routing setting, not an ownership or authorization decision.

## Native creation routing

The approved profile mapping is:

- `general`: `skills.create_dir` points to the existing shared canonical directory.
- `assistant`, `coder`, `writer`, `researcher`, `marketing`, `finance`, `health`, `producer`: `skills.create_dir` points to that profile's `internal-skills` directory.

Resolve installation-specific absolute paths from the actual profile and shared roots; never derive another profile's home from the active profile's `$HOME`. Apply through `hermes -p <profile> config set skills.create_dir <resolved-path>`. Preserve existing `skills.external_dirs` and all unrelated keys.

The native loader includes an existing create directory automatically. New creations use that target; existing skill edits remain at the discovered package location. This does not automatically move existing packages, enforce canonical ownership, or alter filesystem permissions.

## External installation and update boundary

Hub installation and bundled synchronization use the profile-local `skills` root. `skills.create_dir` does not redirect them. A Hub `external` category is still under that local root, not the shared external library.

Preserve native Hub provenance, scanner results and update source identity. External promotion requires review, full-package comparison, dependency/path checks, a recoverable package and bookkeeping baseline, then exact loader verification. This document does not claim that an automatic promotion/update implementation exists.

Do not put absolute shared paths into Hub lock entries to bypass their profile-root validation. Do not mark external packages internal merely to route them through `create_dir`. Until a shared-source update path is tested, treat it as an open capability rather than advertising native Hub management of promoted packages.

The Hub discovery index cache is a remote catalog, not the installed-package inventory. Remove stale installation records through native uninstall only after verifying the target is absent or removal is explicitly authorized. Do not rewrite discovery caches to hide upstream catalog entries.

## Verification and recovery

Before changing routing, capture private profile config backups and baseline resolver/index results. Test general first, return to the baseline via native config commands, verify it, and reapply the candidate before expanding to authorized profiles. Compare parsed configs: only the intended key may differ. Retain byte-exact backups outside public source and active skill discovery.

Verify each profile's resolved create root, discovery/index parity, and rollback/return result. Record isolated native file tests, fresh-process loader checks, existing serving-process behavior, and actual model/tool execution separately. A passing source test or config read-back is not live gateway acceptance.

Creation routing is not a security boundary. Do not change approval controls, credentials or access permissions as a side effect. Never bypass a rejected operation through another tool or identity.

## Repository boundary

Keep reusable source and nonsecret contracts here. Profile config backups, runtime snapshots, session records, raw personal health/finance content and credentials remain outside this repository. This lifecycle contract is not a deployment script or a complete inventory of the runtime library.
