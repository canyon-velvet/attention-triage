# Attention Triage

See the few Claude Code actions that need your decision, without reading the whole transcript.

> **Status:** v1 works end to end on macOS and Linux. Not yet published; install from a clone.

## The problem

In auto mode, Claude Code takes hundreds of actions per session. Most are routine. A few deserve a human decision:

- a command run **outside the sandbox**
- a file written **outside the project**
- network access to an **unlisted domain**
- an edit to **Claude Code settings or hooks**

Attention Triage records every action and shows **only the ones its policy flags**, with enough context to decide quickly. It observes only: it never blocks or prompts. Claude Code's permissions and sandbox stay the enforcement layer.

## Quick start

```bash
git clone https://github.com/canyon-velvet/attention_triage && cd attention_triage
uv tool install .        # provides `triage` and `triage-hook`
triage install           # adds the hooks to ~/.claude/settings.json (backup, diff, confirm)
```

Restart running Claude Code sessions, work as usual, then:

```bash
triage ui                # review inbox at http://127.0.0.1:8765
triage doctor            # if anything looks off
```

## What gets flagged

| rule | flags | severity |
|---|---|---|
| `sandbox_bypass` | a Bash command run with the sandbox disabled; labelled `retry-after-block` (the sandbox blocked it first, with the error as evidence) or `preemptive` | high |
| `outside_project_write` | Write/Edit outside the project, temp dirs, Claude Code's own plans/jobs/memory dirs and `allowed_paths` | high |
| `config_edit` | edits to Claude Code settings or hooks, Triage's own data, and `extra_protected_paths` | high |
| `unlisted_domain` | WebFetch, or an `http(s)://` URL in a Bash command (best-effort, labelled `partial`), to a host outside `allowed_domains` | review |

## Reviewing

The inbox lists open flags by project and session, high severity first: what ran, the stated reason, the outcome (ran, approved at a prompt, failed, denied), and the evidence. For each flag:

- **Fine — this one only** dismisses it; **Acknowledge** means "seen, not fine, I'll handle it".
- **Fine — this and future ones** creates a suppression: later matches (exact, or a widened command prefix or folder) are stored as suppressed. The **Suppressions** page lists them with hit counts, suggests a config change for recurring ones, and can undo them.
- **Fix it** (when the action changed files or was followed by commits) gives copy-ready `git diff`, `git restore` and `git revert` commands. Triage never runs them.

**Mark all reviewed** clears the page and starts a new review window. Filter by rule, project or what ran (command, path or URL) to work through a long inbox; marking then clears only what's shown.

If capture looks broken (hooks missing, events stuck, hook errors), a warning above the headline says so.

## Commands

| command | does |
|---|---|
| `triage install [--dry-run]` | add the hooks to `~/.claude/settings.json` (backup, diff, confirm) |
| `triage uninstall` | remove exactly the entries it added (with a backup) |
| `triage ui [--port]` | serve the review inbox on 127.0.0.1 (default port 8765); purges old events on start |
| `triage doctor` | check hooks, database, buffered events, policy, recent hook errors and hook-format changes; print fixes; exit 1 on failure |
| `triage reeval [--since DATE]` | re-check stored events against the current policy: add flags a tightened rule or a failed capture missed, correct ones that differ. Never removes a flag |
| `triage purge` | delete events older than `retention_days` now |

## Policy

`~/.attention-triage/policy.yaml` is created with the defaults on first use:

```yaml
version: 1
retention_days: 7          # events (and their flags) older than this are deleted
rules:
  outside_project_write: {enabled: true, severity: high, allowed_paths: []}
  sandbox_bypass:        {enabled: true, severity: high}
  config_edit:           {enabled: true, severity: high, extra_protected_paths: []}
  unlisted_domain:       {enabled: true, severity: review, allowed_domains: [github.com, pypi.org, …]}
```

Unknown keys are rejected, so a typo can't silently leave a rule on. New settings apply to new events; run `triage reeval` to apply a tightened rule to past ones.

## Reducing noise

Most flags in practice are `sandbox_bypass`: a command failed in Claude Code's sandbox and was retried outside it. Fix the cause in Claude Code's own settings rather than in Triage, so the command stays sandboxed:

- `retry-after-block`: the evidence names what was blocked. Add the path to `sandbox.filesystem.allowWrite` or the host to `sandbox.network.allowedDomains` (e.g. `pypi.org` and `files.pythonhosted.org` for `uv`).
- `preemptive`: Claude skipped the sandbox without trying. Add `"ask": ["Bash(dangerouslyDisableSandbox:true)"]` under `permissions` to be asked first.
- Anything you accept as routine: **Fine — this and future ones**.

See [Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing).

## How it works

```
Claude Code ──(hook events, async)──▶ triage-hook ──▶ SQLite (~/.attention-triage/triage.db)
                                          │  normalize → redact/truncate → store once
                                          │  → evaluate rules → store flags
                                          └─▶ if the DB fails: append to spool.jsonl

triage ui ──▶ FastAPI (127.0.0.1) ──▶ review inbox (React)
```

- The hook always exits 0, so it can never break the agent. A failed write is buffered in `spool.jsonl` and stored on the next run: a delay, not a loss. A small synchronous `SessionStart` hook tells you in-session if capture is failing.
- **Privacy:** Triage stores what ran and where, not contents. File contents become a hash, a size and a short preview; tool output is cut to about 2,000 characters; common token formats and `key/secret/token/password = value` patterns are masked before anything is written. Everything stays in `~/.attention-triage/`.
- Events are kept for `retention_days`: purged at each session start, when `triage ui` starts, and by `triage purge`.

**Not in v1:** enforcement, agents other than Claude Code, project-level installs or a team server, alerts and dashboards, full network visibility. See `SPEC.md` for the design and `docs/adr/` for the decisions behind it.

## Development

```bash
uv sync && uv run pytest -q && uv run ruff check .
cd web && pnpm install && pnpm test && pnpm build   # the built UI is committed in src/attention_triage/static/
```
