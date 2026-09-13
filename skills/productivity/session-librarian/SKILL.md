---
name: session-librarian
description: "Organize sessions by prompt: find, rename, archive, prune."
version: 1.0.0
author: Hermes Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Sessions, Organization, Cleanup, Library, Productivity]
    category: productivity
    related_skills: [weekly-review-planning]
---

# Session Librarian

Manage the user's session library conversationally: find past sessions about a
topic, summarize what they decided, rename them meaningfully, split work into
parallel sessions, and propose stale ones for archive or deletion — all from a
plain-language request like *"find my sessions about Q3 pricing, keep the
useful ones, and clean up the duplicates."*

Resolve the active profile before reading or managing sessions. Search and metadata inspection are read-only; mutation follows the explicit plan and confirmation boundary below.

## When to Use

- "What sessions do I have about X?" / "What did we decide about X?"
- "Rename these sessions to something meaningful."
- "Clean up my session library" / "archive the stale ones."
- "Fork that session into a follow-up focused on Y."
- "Split this into one session per ticket" (see Parallel workstreams below).

## The Two Surfaces

| Task | Surface |
|---|---|
| Find sessions by topic, read content, summarize decisions | `session_search` tool (FTS5 over the message store) |
| List/filter by metadata (age, source, cost, tokens, workspace) | `hermes sessions list` / `stats` via terminal |
| Rename | `hermes sessions rename <session_id> <title...>` |
| Bulk soft-hide (reversible) | `hermes sessions archive <filters>` |
| Delete (destructive) | `hermes sessions delete` / `hermes sessions prune <filters>` |
| Export before deleting anything valuable | `hermes sessions export --session-id <id> --format md` |
| Continue work in a new place | `/branch` (fork current session) or start a fresh session and cite the summary |

## Procedure

① **Discover.** Use `session_search(query=..., limit=5-10)` with topic
keywords; vary phrasing (feature name, symptom, project name). For metadata
sweeps ("sessions older than 60 days from telegram"), use
`hermes sessions list --source telegram --limit 50` instead.

② **Summarize per session.** The discovery result's `bookend_start` (goal),
match window, and `bookend_end` (resolution) are a starting point. Expand the relevant message window when evidence is incomplete or contradictory; do not infer a final decision from titles or bookends alone. Use the tool's current read/scroll schema. Report each as: link (`@session:` form) — one-line goal —
one-line outcome.

③ **Plan before acting (MANDATORY for anything that mutates).** Present a
plan table first: which sessions get renamed to what, which get archived,
which are proposed for deletion and why (duplicate of which keeper, stale,
empty). Wait for the user's go-ahead. Exception: a single rename the user
explicitly dictated can be done directly.

④ **Act with the safest primitive.**
- Prefer `archive` (reversible soft-hide) over `delete`/`prune`.
- Always run destructive commands with `--dry-run` first and show the output,
  then re-run with `--yes` after confirmation.
- Before deleting anything with meaningful content, offer
  `hermes sessions export --format md` as a backup.

⑤ **Report.** Renames applied, sessions archived (count + how to undo:
archived sessions remain in the DB and are listed with `--include-archived`),
anything exported, anything skipped and why.

## Parallel Workstreams

For multiple workstreams, first honor the user's current worker/session limits. Do not drive unrelated live sessions. Use a supported, explicitly authorized delegation surface only when separate execution is allowed; otherwise organize and work sequentially in the current session. Verify actual child/session identities and searchable records rather than promising transcript availability from an example.

## Pitfalls

- **Never delete without a dry-run + explicit confirmation in this
  conversation.** A standing "clean things up" is authority to *propose*, not
  to prune.
- **`session_search` finds content, not metadata.** Age/cost/source filters
  live in the CLI; combine both when the request mixes them ("old sessions
  about pricing").
- **Titles are identity for `/resume <title>`.** When renaming, keep titles
  short, unique, and prefix-friendly; warn the user if a rename collides with
  an existing title.
- **Archived ≠ deleted.** Archive hides sessions from default listings only.
  Say which one you did.
- **Cross-profile session links** (`@session:<profile>/<id>`) are read-only
  from another profile; management commands act on the current profile's DB.

## Verification

After a cleanup pass, re-run the discovery query and `hermes sessions list`
to confirm the library reflects the plan (keepers present with new titles,
archived ones gone from the default listing).
