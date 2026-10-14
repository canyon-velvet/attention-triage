# Attention Triage — v1 Spec

Status: agreed 2026-10-07 (planning interview). Decisions with lasting weight are recorded as ADRs in `docs/adr/`.

## 1. Problem

In auto mode, Claude Code takes hundreds of actions per session. Most are routine; a few deserve a human decision — a command retried outside the sandbox, a write outside the project, a new network domain, an edit to Claude settings or hooks. Those are buried in the transcript, so you either stop watching or watch everything.

Attention Triage records every action and shows you **only the few that a policy says need review**, with enough context to decide quickly.

## 2. Goals and non-goals

**Goals (v1)**
- A tool the author uses daily on their own Claude Code sessions; built as a learning vehicle.
- Capture every hook event reliably, without slowing the agent.
- Flag actions against a plain-YAML policy; present them as a one-screen review inbox.
- Make "0 flags" trustworthy: capture gaps are surfaced, never silent.

**Long-term goal:** a public tool usable by broader users and teams (multi-agent, project-level, shared server). v1 must not paint us into a corner there (neutral event schema, raw payload kept, project recorded on every event).

**Non-goals (v1)**
- Enforcement. Triage never blocks or prompts; Claude Code's permissions and sandbox enforce (ADR-0002).
- Agents other than Claude Code.
- Project-level installs, team aggregation, shared server (ADR-0004).
- Phone alerts, trends dashboard, custom rule DSL, one-click remediation.
- Full network visibility (no proxy).

## 3. Architecture

```
Claude Code ──(hook events, async)──▶ triage-hook ──▶ SQLite (~/.attention-triage/triage.db)
                                          │  normalize → redact/truncate → insert-or-ignore
                                          │  → evaluate rules (capture time) → insert flags
                                          └─▶ on DB failure: append to spool.jsonl, log error

triage ui ──▶ FastAPI (127.0.0.1) ──▶ JSON API ──▶ React/TS SPA (static, served by FastAPI)
                    │
                    └─ runs `triage doctor`'s read-only checks to warn of capture problems
```

- **Language split:** Python owns storage, rules, hook, API, CLI. TypeScript is UI only.
- **Data dir:** `~/.attention-triage/` — `triage.db`, `policy.yaml`, `spool.jsonl`, `hook-errors.log`.
- **Install:** `uv tool install` → `triage` CLI.

### Repo layout (proposed)

```
pyproject.toml
src/attention_triage/
  cli.py          # install, uninstall, ui, doctor, reeval, purge
  hook.py         # triage-hook entry point
  normalize.py    # Claude Code payload → neutral event
  redact.py       # truncation + secret masking
  store.py        # SQLite schema, migrations, spool ingest
  policy.py       # load/validate policy.yaml, policy_version
  rules/          # one module per rule
  outcome.py      # derive approved/denied/failed per tool_use_id
  server.py       # FastAPI app + static SPA
  static/         # built web assets (generated)
tests/
  fixtures/payloads/   # recorded hook payloads (from spike)
  test_rules_*.py, test_hook.py, test_store.py, ...
web/              # Vite + React + TS
```

## 4. Capture

### Hook registration
`triage install` merges entries into `~/.claude/settings.json` (backup first, show the diff, confirm). One command handles every event; it dispatches on `hook_event_name`. All entries are `async: true` so the agent never waits.

Events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`, `SessionStart`, `SessionEnd`, `Stop`, `SubagentStart`, `SubagentStop`. Matcher: all tools.

`triage uninstall` removes exactly the entries it added (identified by command path), with backup.

### Hook behavior
1. Read stdin JSON. Never raise; always exit 0.
2. Normalize into the neutral event shape (below). Keep the raw payload (after redaction).
3. Redact and truncate (see "Storage limits").
4. `INSERT OR IGNORE` by `dedup_key`. If ignored (duplicate), stop.
5. Evaluate rules for this event (§5); insert flags.
6. On any DB error: append the processed event to `spool.jsonl`, log to `hook-errors.log`. The next successful hook run (or `triage ui` / `doctor`) ingests the spool first.
7. SQLite: WAL mode, `busy_timeout` ~2 s.

### Neutral event shape
| field | notes |
|---|---|
| `agent` | `claude-code` in v1 |
| `session_id`, `agent_id` | subagent id when present |
| `event_type` | neutral name mapped from the hook event |
| `tool_name`, `tool_use_id` | |
| `target_kind`, `target` | `command` / `path` / `url` / none, and its value — what the digest shows as "what ran" |
| `stated_reason` | e.g. Bash `description` |
| `project_root`, `cwd` | project = git root of cwd, else cwd |
| `ts` | |
| `summary` | small JSON: key inputs/outputs, truncated |
| `raw` | redacted, truncated raw payload |
| `dedup_key` | sha256(agent, session_id, event_type, tool_use_id or canonical payload); a `PermissionRequest` adds its capture time, since a call repeated in one turn and prompted again sends a byte-identical request |
| `install_scope` | `user` in v1 |

### Storage limits (privacy)
- Store **what and where**, not contents. File contents in Write/Edit inputs → sha256 + byte size + first ~500 chars (redacted). Tool outputs truncated to ~2,000 chars. Limits configurable.
- Secret masking before write: common token formats (AWS `AKIA…`, GitHub `gh[pousr]_…`, `sk-…`, bearer tokens, PEM private-key blocks) and `key/secret/token/password = value` patterns.
- Retention: delete events older than `retention_days` (default 7; a longer history isn't useful for review) — on `triage ui` start and via `triage purge`.
  - (#22) The async capture hook also purges, right after storing each `SessionStart` event, so retention works without opening the UI. `retention_days` is a whole number from 1 to 36500 (a century: effectively "keep everything", while the purge cutoff stays a representable date). With an invalid or unreadable `policy.yaml` nothing is purged, and `triage ui` still starts: deleting by the default could remove days the user asked to keep. `triage purge` says how many events it deleted. Purging frees space for reuse but doesn't shrink the file (no `VACUUM`). `hook-errors.log` and `notified-sessions` are not trimmed.

## 5. Rules

Built-in rules in Python, enabled and configured in `policy.yaml`. Evaluated at capture time (ADR-0003). One event may trip several rules; the UI shows each.

### Policy file

```yaml
version: 1
retention_days: 7
rules:
  outside_project_write:
    enabled: true
    severity: high          # high | review
    allowed_paths: []       # extra allowed roots, e.g. ~/.cache/myapp
  sandbox_bypass:
    enabled: true
    severity: high
  config_edit:
    enabled: true
    severity: high
    extra_protected_paths: []
  unlisted_domain:
    enabled: true
    severity: review
    allowed_domains: [github.com, "*.githubusercontent.com", pypi.org, files.pythonhosted.org,
                      localhost, 127.0.0.1, "::1"]   # localhost entries: this machine
```

`policy_version` = hash of the normalized rules' settings (`retention_days` changes no flag, so it isn't part of it). Every flag stores the version that produced it. `triage reeval [--since]` re-runs rules over stored events (#19): it adds the flags they lack (a tightened policy, an insert that failed), corrects those that differ while keeping their status, and reopens a flag left suppressed by a deleted suppression. It never removes a flag; after loosening the policy, clear old flags in the inbox. Path rules see the disk and settings files as they are now; a flag's recorded path is kept, as it names the file the write really reached. `--since` takes a local date or date-time.

### Flag record
`rule_id`, `severity`, `reason` (human sentence), `label` (rule-specific, below), `evidence` (JSON), `policy_version`, `status` (`open` / `dismissed` / `acknowledged` / `suppressed`), `suppression_id`, timestamps.
- **Known limit** (fix deferred): a flag insert that fails (e.g. DB locked past the busy timeout) loses those flags until `reeval` (#19).

**Outcome** ("was it approved / did it run") is **derived at read time**, not stored on the flag (async hooks arrive out of order). Tool events join on `tool_use_id`; `PermissionRequest` carries no `tool_use_id`, so it joins to its `PreToolUse` by same session + `tool_name` + identical `tool_input` (confirmed by the spike, §10):
- `PostToolUseFailure` → failed (with error text); "approved after prompt, then failed" when a request matched
- `PostToolUse` with a matching `PermissionRequest` → approved after prompt
- `PostToolUse` without one → ran without prompt
- `PermissionDenied` → denied by auto mode (per docs; not observed in the spike)
- `PreToolUse` + matching `PermissionRequest`, no result, and a later `Stop`/`SessionEnd` in the session → denied by the user (the hook stream has no denial event; the transcript's tool result says "The user doesn't want to proceed…")
- otherwise → unknown / in progress

### R1 — `outside_project_write`
- Tools: `Write`, `Edit`, `NotebookEdit` (any file-writing tool with a path).
- Resolve the path to a real absolute path (resolve the parent if the file doesn't exist yet; follow symlinks; collapse `..`).
- Allowed: project root; temp dirs (`/tmp`, `/private/tmp`, `$TMPDIR`, `/var/folders/…`); Claude Code's own per-session dirs (plans, memory, job dirs); `allowed_paths`.
- Otherwise flag. Evidence: resolved path, project root.
- Known gap: writes done inside Bash commands are not detected in v1.

### R2 — `sandbox_bypass`
- `Bash` `PreToolUse` with `tool_input.dangerouslyDisableSandbox == true` → flag.
- Label `retry-after-block` if the same session has a recent `PostToolUseFailure` whose `error` looks like a sandbox denial (contains `Operation not permitted`, or a `<sandbox_violations>` block for network denials; see §10) and whose command shares the same program (first word); evidence includes that error and the original command. Otherwise label `preemptive`.
- Race note: with async hooks, the failure event could in rare cases be stored after the retry is evaluated, leaving it labelled `preemptive`; it is still flagged high. `reeval` can't correct this: payloads carry no time, so events are stamped when captured, and the late failure's stamp is after the retry's. It takes a failure hook delayed longer than the agent's next model turn (measured hook start to stamp: 36–148 ms), so it is rare (#19).

### R3 — `config_edit`
- File-writing tools targeting: `~/.claude/settings.json`, `~/.claude/settings.local.json`, `<project>/.claude/settings.json`, `<project>/.claude/settings.local.json`, anything under `~/.claude/hooks/` or `<project>/.claude/hooks/`, script files referenced as hook commands in those settings, and `~/.attention-triage/**` (self-protection: the agent editing Triage's own policy or DB).
- `extra_protected_paths` adds more.
- v1 covers file tools only; Bash-based edits are a known gap.

### R4 — `unlisted_domain`
- `WebFetch`: host of `tool_input.url` not matching `allowed_domains` (wildcards supported) → flag. Reliable.
- `Bash`: best-effort — extract `http(s)://host` URLs from the command; unlisted hosts → flag with label `partial`. The UI states plainly that shell network access is not fully visible.

## 6. Review (digest UI)

### Inbox (main screen)
- Headline: **"214 actions · 3 need review"** for everything since the last review. Above it, a warning when `triage doctor`'s read-only checks find a capture problem (§7): "Actions may be missing from this inbox. Run `triage doctor` for the fix.", with what is wrong.
- Open flags grouped by project → session, high severity first.
- Each item: what ran (command / path / URL), stated reason, outcome (§5), rule + severity + label, evidence (e.g. the sandbox error that preceded a retry), time, project.
- Item actions:
  - **Fine → this one only** (dismiss).
  - **Fine → this and future ones** (creates a suppression; default match = same rule + same exact target + same project; "edit pattern" lets you widen it before confirming, e.g. command prefix or directory).
    - Matching (#15): "same project" is the repo (a worktree counts as its main checkout). Commands take exact or prefix; paths exact or directory, compared as the rule resolved them; URLs and `unlisted_domain` flags exact only. Open flags that match are suppressed too.
    - It fails closed: a prefix matches whole words, never a command with shell syntax (`;` `&` `|` `<` `>`, backticks, newline, `$(`), and must name its program in plain words, not a launcher (bash, sudo, env, …). A target that redaction masked or cut never matches.
    - **Known limit:** the launcher list can't be complete (`find -exec`, `uv run`), so a prefix trusts its program; the form says so. Fix deferred.
  - **Acknowledge** ("seen; not fine; I'll handle it").
- **Mark all reviewed** clears the rest. Opening the page clears nothing.
- "N suppressed" count with a link to the Suppressions page.
- Guidance, not action: for each item show files changed, the session's commits, and copy-ready git commands (`git -C <root> diff -- <file>`, `git restore <file>`, `git revert <sha>`). The UI never runs git or edits the policy.
  - Built from captured events at read time (#18), never by running git. Files: the flagged call's own (a file tool's path; for Bash, Claude Code's `changedFiles`). Commits: those the call or a later one in its session reports (`git commit` prints `[branch sha] subject`); best-effort. Commands use `git -C <project>` with shell-quoted paths, only for files inside a project that is (still) a git checkout; other files are listed without commands. A path that redaction cut is left out. An item with neither files nor commits shows no guidance. No per-item policy hint: it was the same for every item of a rule, and stopping a rule flagging something is what suppressions and their config suggestions are for.

No session detail page (#17 dropped): a full action list rebuilds the transcript this tool spares you. The inbox is the review surface.

### Suppressions page
Every "future" dismissal: rule, pattern, project, note, created, hit count, last hit, **undo**. Recurring suppressions show a config suggestion, e.g. "matched 14× this week — consider adding `docker *` to sandbox `excludedCommands`."
- Hits are the flags a suppression stored as suppressed. Undo (#16) reopens them; the flag dismissed when it was made stays dismissed.
- Recurring = 5+ hits in the last 7 days. `sandbox_bypass` suggests sandbox `excludedCommands`; `unlisted_domain` suggests adding its hosts to `allowed_domains`. Other rules get none.
- **Known limit** (fix deferred): a hook that read the suppression just before an undo can still store its flag as suppressed by it, so that flag stays out of the inbox.

### API (FastAPI, bound to 127.0.0.1)
`GET /api/digest`, `POST /api/flags/{id}/dismiss {scope: once|future, pattern?}`, `POST /api/flags/{id}/ack`, `POST /api/review/mark-all`, `GET /api/suppressions`, `DELETE /api/suppressions/{id}`, `GET /api/health`.

## 7. Capture health

In v1 (item 2 dropped, #21):
1. **Spool fallback** — DB write failure → `spool.jsonl`; ingested on the next successful run. Failures become delays, not loss.
2. **Transcript coverage** — dropped (#21). Comparing Claude Code's transcript with captured events would also catch losses on Claude Code's side, but it depends on an undocumented format, and `triage doctor` covers the known causes (hooks missing or gone, hook errors, a waiting spool). Instead the inbox shows doctor's read-only checks as a warning, so a problem is seen without running doctor. A headline "N captured" count, which only repeated "N actions", is gone too.
3. **In-session notice** — on the first hook failure in a session, emit one `systemMessage` ("attention-triage: capture failing (<reason>); events buffered. Run `triage doctor`."), rate-limited to once per session. Async hooks' output is ignored by Claude Code, so the notice comes from a separate synchronous `SessionStart` hook that checks for recent failures.
   - **Known limits** (fixes deferred; to be thought through later):
     - *Late:* a failure is reported at the next `SessionStart` (start, resume, `/clear`, compact), not when it happens.
     - *Only spooled failures:* errors before the spool step (bad stdin, a bug in `normalize`/`redact`, a failed spool write) lose the event and are only logged; the notice doesn't mention them.
     - *Hook never runs:* e.g. `triage-hook` uninstalled or its editable source moved. Nothing inside the hook can report this; `triage doctor` does (#20).
4. **`triage doctor`** — checks hooks still present in settings, DB writable, spool empty, policy valid, recent hook errors; prints the fix in plain language.
   - (#20) Exits 1 if any check fails. "Recent" hook errors are those of the last 24 hours, so doctor passes again once none recur. Hook-format drift (§10 spike fields that went missing or changed type) is a warning, not a failure: an update to Triage fixes it, not the user's setup. It is reported while seen in the last 7 days (`drift.last_seen`).

## 8. CLI

| command | does |
|---|---|
| `triage install [--dry-run]` | merge hooks into `~/.claude/settings.json` (backup, diff, confirm); create data dir + default policy |
| `triage uninstall` | remove our hook entries (backup) |
| `triage ui [--port]` | ingest spool, purge old data, serve UI on 127.0.0.1 |
| `triage doctor` | health checks (§7) |
| `triage reeval [--since]` | re-run rules after a policy change |
| `triage purge` | apply retention now |

## 9. Testing (the checks Claude runs)

- **Rule evals first:** pytest fixtures = recorded hook payloads (from the spike) + expected flags, with both should-flag and should-not-flag cases per rule. Written before each rule.
- **Hook test:** pipe fixtures into the real `triage-hook` command; assert exit 0, exactly one row, a duplicate run inserts nothing (except a `PermissionRequest`; a replayed spool still inserts it once), and the DB-unavailable path writes the spool.
- **Store/redaction tests:** secrets masked, contents truncated, retention purge.
- **UI:** `tsc --noEmit` + a few component tests. Screenshot check later.
- Tooling: `uv`, Python 3.12+, pytest; `pnpm`, Vite, React, TypeScript.

## 10. Milestones

- **M0 — Spike (first ticket).** A throwaway hook that dumps every payload to files, installed by the user. Run a session that triggers: a sandbox block, a `dangerouslyDisableSandbox` retry, a preemptive bypass, an approved prompt, a denied prompt, a Write outside the project, a WebFetch, a subagent. Output: `tests/fixtures/payloads/` + answers to the questions below, recorded in this spec.
- **M1 — Capture:** hook, normalize, redact, store, dedup, spool; `install` / `uninstall` / `doctor`.
- **M2 — Rules:** policy loading/versioning, R1–R4, outcome derivation, `reeval`. Fixture tests first.
- **M3 — Digest:** API + inbox, dismiss/ack/mark-all, suppressions page.
- **M4 — Health & retention:** in-session notice, doctor, capture warning in the inbox, purge.
- Then: dogfood on real sessions for a week before any v2 work.

### Questions the spike must answer
1. Does Bash `tool_input` include `dangerouslyDisableSandbox`? Exact field name/values.
2. Exact error text of a sandbox denial in `PostToolUseFailure`.
3. `PermissionRequest` / `PermissionDenied` payloads; can we tell approved vs denied, and user vs auto-mode classifier?
4. Is `tool_use_id` present on all tool events, and does it equal the transcript's tool_use id?
5. Do async hooks surface `systemMessage`? Do they arrive out of order?
6. Is `permission_mode` present in Claude Code payloads?
7. Subagent events: shared `session_id` with the parent? `agent_id` present on tool events?

### Spike findings (2026-10-07, Claude Code 2.1.293)
Payloads: `tests/fixtures/payloads/` (two sessions: one in auto mode, one in default mode).

1. **Bypass field.** `tool_input.dangerouslyDisableSandbox: true` on Bash `PreToolUse`; the key is absent (not `false`) when not set. `PostToolUse.tool_response` echoes it.
2. **Sandbox denial text.** The text is in `PostToolUseFailure.error` (not `error_message`, as the docs say). Filesystem: `Exit code 1\ntouch: <path>: Operation not permitted`. Network: `Exit code 56\ncurl: (56) CONNECT tunnel failed, response 403 … X-Proxy-Error: blocked-by-allowlist …` followed by a `<sandbox_violations>` block (`deny network-outbound example.com:443 …`).
3. **Permissions.** `PermissionRequest` carries `tool_name`, `tool_input`, `permission_mode` and `permission_suggestions`, but **no `tool_use_id` and no decision**. Approval shows only as a later `PostToolUse`/`PostToolUseFailure`. A user denial produces **no further hook event**. In auto mode no `PermissionRequest` fires at all, even for sandbox bypasses. `PermissionDenied` (auto-mode classifier) has `tool_use_id` per the docs but was not observed. Mapping in §5 "Outcome".
4. **tool_use_id.** Present on `PreToolUse`, `PostToolUse` and `PostToolUseFailure` (format `toolu_…`); absent on `PermissionRequest`. Identical to the transcript's `tool_use` ids (11/11 matched).
5. **Async and ordering.** Async hook output (`systemMessage` etc.) is ignored (docs), so the in-session notice needs a synchronous `SessionStart` hook (§7). Events for one tool call can arrive out of order: `PermissionRequest` landed before its `PreToolUse` twice, in the same millisecond.
6. **permission_mode.** Present on tool events, `PermissionRequest`, `Stop` and `SubagentStop`; absent on `SessionStart`, `SessionEnd` and `SubagentStart`. It changes mid-session (Shift+Tab).
7. **Subagents.** These share the parent's `session_id`. Their tool events carry `agent_id` and `agent_type` (e.g. `Explore`); main-agent tool events have neither. The `Agent` tool's `PostToolUse` arrives at launch (`status: async_launched`), before the subagent's own tool calls. A subagent ends with an internal `SubagentHandback` tool call.

Other observations:
- Every turn's `Stop` is followed by an internal `SubagentStop` with an empty `agent_type` and no matching `SubagentStart`. Store it, but ignore it in the session view and coverage.
- Common fields on every event: `session_id`, `transcript_path`, `cwd`, `scratchpad_dir`. `prompt_id` and `effort` appear on most. `SessionStart` has `source` and `model`; `SessionEnd` has `reason`.
- In default mode with `autoAllowBashIfSandboxed: false`, even sandboxed Bash prompts.

## 11. Deferred (v2+)

- Project-level installs and a shared team server. Open design notes: dedupe when user- and project-level hooks both fire (unique `dedup_key` already makes writes idempotent, except `PermissionRequest`, stored twice but pairing with the same call); policy combination = union (project may add rules/allowlists, cannot disable user rules); suppressions stay personal.
- Translators for Codex, Cursor and others. In v1, `normalize.py` is the Claude Code adapter and holds Claude Code's payload format; the one exception is `hook.py` reading `hook_event_name` to label drift rows, which moves into the adapter when a second agent arrives. Storage, rules and UI read neutral event fields only. To add an agent:
  1. move `normalize.py` to `adapters/claude_code.py`;
  2. add `adapters/<agent>.py`, built from that agent's real payloads (recorded with the spike method) with its own drift expectations;
  3. have the installed hook name its agent (`triage-hook --agent <agent>`) so the hook picks the adapter instead of guessing from the payload shape.

  Don't build the adapter layer before a second agent is in scope.
- Phone alerts for high-severity flags (ntfy/Pushover); trends dashboard.
- Filter and search in the review UI.
- **Plain-language risk explanations.** Each flag gets a risk level (low / medium / high, replacing v1's high / review) and one or two plain sentences for non-technical readers: why this could be dangerous, and when it's usually safe to ignore. Example: *"The agent ran `docker` outside the safety sandbox, so it could reach files and the network without the usual limits. Usually fine if you asked it to start containers for this project."* Two steps:
  1. Per-rule templates filled with the event's details (deterministic, cheap, unit-testable). Could land early, since v1 flags already carry a `reason` field.
  2. Optionally, an LLM-written explanation that uses session context. This makes part of the product an agent, so it starts with eval cases (flag → qualities the explanation must have) and tracing, per the "feature is an agent" practices.
- Custom rule DSL in YAML; one-click remediation.
- More observability: secret-file reads, destructive commands, package/lockfile and MCP changes, weakened or deleted tests, retry loops, prompt-injection signals (WebFetch followed by egress or outside writes), permission analytics, cost/time per session, OpenTelemetry import.
- Bash-level detection for R1/R3.
