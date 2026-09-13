# GitHub PR workflow layout revision

OPS-239 source package for the existing `github-pr-workflow` skill. This directory is not an active loader root and does not install a new skill.

The revised entrypoint selects operation references; the prior body is reconstructible byte-for-byte using the ordered references and unchanged frontmatter in `manifest.json`. `baseline.md` is the exact rollback source. Existing supporting references and templates are not replaced.

Run `python3 test_router.py -v` in this directory. Seven structural checks cover reconstruction, identity, omission detection, fenced PR examples, support-file preservation, isolated rollback and honest acceptance scope. They do not constitute old/new model-behavior comparison, live rollback or fleet acceptance.

The bounded general canary uses these files. No other profile rollout, credential change, security-control change or restart is authorized by this package. Historical fallback examples are preserved, not endorsed over current policy. Machine-specific receipts remain local and are intentionally excluded.
