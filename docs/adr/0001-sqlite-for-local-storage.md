# ADR-0001: SQLite for local storage

Date: 2026-10-07 · Status: accepted

## Context
Every Claude Code tool call starts a fresh hook process that writes one event. The hook must be fast, must never break the agent, and must not lose events. The author is more familiar with PostgreSQL (14 is installed locally), and the long-term goal is a public tool.

## Decision
Use SQLite (WAL mode, short busy timeout) in `~/.attention-triage/triage.db`. No PostgreSQL in v1.

## Consequences
- Zero setup and no server that can be down; Python's built-in `sqlite3` is enough for the hook.
- Concurrent writers (parallel sessions, subagents) are fine at this volume with WAL; the spool fallback covers lock timeouts.
- Easy for future public users to install.
- The SQL needed (inserts, filters, group-by counts) transfers directly from PostgreSQL experience.
- A future shared team server may use a different database; the neutral event schema keeps that a migration, not a redesign.
