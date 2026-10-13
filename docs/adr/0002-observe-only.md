# ADR-0002: Observe only — no enforcement in v1

Date: 2026-10-07 · Status: accepted

## Context
Hooks can block or prompt (`permissionDecision: deny/ask`). Existing tools (agentjail, Patchwork) focus on enforcement. Our differentiator is the review digest: showing the few actions that need a human decision after the fact.

## Decision
Triage never blocks or prompts. Claude Code's permissions and sandbox remain the enforcement layer. Hooks only record events and flags. The UI never runs git or edits the policy; it shows guidance and copy-ready commands. The only UI writes are review state (dismiss, acknowledge, suppressions) in Triage's own database.

## Consequences
- A bug in Triage cannot stop or alter an agent session; hooks can run async.
- Flags store "what the rule said" separately from "what actually happened" (outcome derived from later events), so enforcement could be added later without changing the data model.
- A risky action is reported after it happens, not prevented. That is the intended trade-off for an observability tool.
