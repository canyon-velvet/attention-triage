# ADR-0004: User-level install only in v1

Date: 2026-10-07 · Status: accepted

## Context
Hooks can be installed in `~/.claude/settings.json` (user level, every project) or in a repo's `.claude/settings.json` (project level, committed for a team). The v1 user is an engineer observing their own agent across all workspaces, including company repos. Project level serves a team or security lead who wants every developer's agent observed in a repo, which needs a shared server to be useful.

## Decision
v1 installs at user level only, via `triage install`, with one local database per machine. Every event records `project_root`, `cwd` and `install_scope` (`user`).

## Consequences
- Works in any repo without committing anything to it.
- Project-level installs and team aggregation are deferred. Open design notes for later: duplicate capture when both levels fire (already handled by the unique `dedup_key`); policy combination as a union (project adds rules/allowlists, can't disable user rules); suppressions stay personal.
- Recorded project and scope fields let a future central server ingest v1 data unchanged.
