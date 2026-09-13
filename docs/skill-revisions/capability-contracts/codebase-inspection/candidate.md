---
name: codebase-inspection
description: "Inspect codebases w/ pygount: LOC, languages, ratios."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [LOC, Code Analysis, pygount, Codebase, Metrics, Repository]
    related_skills: [github]
prerequisites:
  commands: [pygount]
---

# Codebase inspection with pygount

Count source lines, languages, files and comment ratios. These are lexer-based code metrics, not physical newline counts or a semantic code review.

## Run

Resolve the requested repository and exclusions. Check the existing `pygount --help`; if unavailable, use an authorized isolated environment or `uvx pygount`. Do not override an externally managed Python installation or suppress installation failures.

```bash
uvx pygount --format=summary --folders-to-skip="[...],node_modules,venv,__pycache__,dist,build,vendor" /path/to/repo
```

The `[...]` prefix preserves pygount's default exclusions; without it an explicit pattern list replaces the defaults. Adapt exclusions to the requested coverage, and state if dependencies, generated code or vendored source are included. For language filtering, add `--suffix=py,yaml,yml` (or the requested suffixes) to the same scoped command.

Use `--format=json` for programmatic sorting and aggregation, `--format=summary` for a human-readable overview, or the default format for individual files. Do not parse variable-width summary tables as stable TSV. Bound the displayed results without silently narrowing the counted repository.

## Interpret and verify

- Report the root, revision when relevant, exclusions, filters, tool version and actual result.
- Summary columns include language, file count, code, comments and their percentages; use the returned JSON schema for automated consumers.
- Duplicate contents are counted once by default; `--duplicates` counts them separately. Do not confuse deduplicated LOC with physical file totals.
- Pseudo-languages identify empty, binary, generated, duplicate, unknown or error files. Surface parse/read failures instead of silently calling the scan complete.
- Classification depends on the lexer and version: do not assert that all Markdown is comments or that JSON lines always count in a particular way. Use `read_file` or explicit newline counting when physical lines are requested.
- A successful scan is a metric result, not proof of correctness, maintainability or full semantic review.

References: https://pygount.readthedocs.io/en/latest/usage/ and https://pygount.readthedocs.io/en/latest/installation/
