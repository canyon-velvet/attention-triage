# ADR-0003: Evaluate rules at capture time, with policy versioning

Date: 2026-10-07 · Status: accepted

## Context
Rules could run inside the hook (capture time) or when the digest is opened (review time). Review time keeps the hook minimal and makes rule changes retroactive. Capture time gives real-time flags, which phone alerts will need later. The v1 rules are cheap (path/string checks and one indexed lookup for sandbox retries), and Claude Code supports async hooks that don't block the agent.

## Decision
Evaluate rules in the hook at capture time. Each flag records the `policy_version` (hash of `policy.yaml`) that produced it. `triage reeval [--since]` re-runs rules over stored events after a policy change. Outcome (approved / denied / failed) is derived at read time by joining events on `tool_use_id`, not stored on the flag.

## Consequences
- Real-time flags from day one; alerts can be added without moving the rules.
- Rule changes apply retroactively via `reeval`, so "tighten a rule" still works on past events.
- The hook depends on the installed package (YAML parsing), so it isn't pure standard library; acceptable because Triage is installed as a `uv tool`.
- Async hooks may arrive out of order: the sandbox-retry correlation can rarely miss a just-written failure, which `reeval` corrects. Read-time outcome derivation avoids ordering bugs.
