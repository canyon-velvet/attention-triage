# Attention Triage

See the few Claude Code actions that need your decision, without reading the whole transcript.

> **Status:** early development. The commands below describe the planned v1; none are released yet.

## The problem

In auto mode, Claude Code takes hundreds of actions per session. Most are routine. A few deserve a human decision:

- a command retried **outside the sandbox**
- a file written **outside the project**
- network access to a **new domain**
- an edit to **Claude Code settings or hooks**

Those few are buried in the transcript, so you either stop watching or watch everything.

Attention Triage records every action and shows you **only the few that a policy says need review**, with enough context to decide quickly. It observes only: it never blocks or prompts. Claude Code's permissions and sandbox stay the enforcement layer.

## Goals

**v1**
- Capture every Claude Code hook event reliably, without slowing the agent.
- Flag actions against a plain-YAML policy and show them in a one-screen review inbox.
- Make "0 flags" trustworthy: capture gaps are surfaced, never silent.

**Not in v1:** enforcement; agents other than Claude Code; project-level installs or a shared team server; phone alerts, trend dashboards, a custom rule language or one-click remediation; full network visibility (there is no proxy).

**Long term:** a public tool for teams and multiple agents. v1 keeps that open by storing events in an agent-neutral shape, keeping the raw payload, and recording the project on every event.

## How it works

```
Claude Code ──(hook events, async)──▶ triage-hook ──▶ SQLite (~/.attention-triage/triage.db)
                                          │  normalize → redact/truncate → store once
                                          │  → evaluate rules → store flags
                                          └─▶ if the DB fails: append to spool.jsonl

triage ui ──▶ FastAPI (127.0.0.1) ──▶ review inbox (React)
                    └─ warns in the inbox when `triage doctor` finds a capture problem
```

- Python owns the hook, storage, rules, API and CLI; TypeScript is the UI only.
- Everything stays on your machine in `~/.attention-triage/`: `triage.db`, `policy.yaml`, `spool.jsonl`, `hook-errors.log`.
- Installed with `uv tool install`, which provides the `triage` command.

## Capture

`triage install` adds hooks to `~/.claude/settings.json` (after a backup, a diff and your confirmation): an async capture hook for `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`, `SessionStart`, `SessionEnd`, `Stop`, `SubagentStart` and `SubagentStop`, plus one small synchronous `SessionStart` hook that warns you in the session if capture has been failing. `triage uninstall` removes exactly the entries it added.

On every event the hook:

1. reads the payload and always exits 0, so it can never break the agent;
2. normalizes it into an agent-neutral event (session, tool, what ran, stated reason, project);
3. redacts secrets and truncates content;
4. stores it exactly once (a dedup key makes repeated deliveries harmless);
5. evaluates the policy rules and stores any flags.

If the database is unavailable, the event goes to `spool.jsonl` and is stored on the next successful run, so failures become delays, not loss.

**Privacy.** Triage stores what ran and where, not contents. File contents become a hash, a size and a short preview; tool output is cut to about 2,000 characters; common token formats and `key/secret/token/password = value` patterns are masked before anything is written. Events older than 7 days (configurable) are deleted each time `triage ui` starts, or when you run `triage purge`.

## Commands

| command | does |
|---|---|
| `triage install [--dry-run]` | add the hooks to `~/.claude/settings.json` (backup, diff, confirm); create the data dir and a default policy |
| `triage uninstall` | remove Triage's hook entries (with a backup) |
| `triage ui [--port]` | open the review inbox on 127.0.0.1 |
| `triage doctor` | check that capture is healthy and print fixes in plain language |
| `triage reeval [--since]` | re-run the rules after a policy change |
| `triage purge` | delete events older than the retention period now |
