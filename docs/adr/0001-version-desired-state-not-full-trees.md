# ADR-0001: Version desired state, not entire `~/.claude`/`~/.codex` trees

## Status
Accepted (SPE-71)

## Context
A direct copy of `~/.claude` or `~/.codex` would pull in OAuth tokens,
session history, telemetry, plugin caches, and other machine-specific
runtime state — unsafe to publish and meaningless to reproduce.

## Decision
Track only an explicit allowlist of files intentionally required to
reproduce behavior (`docs/ALLOWLIST.md`). A file is tracked because it's
portable desired state, not because it happened to exist in a home
directory. Secrets in tracked config are `${ENV_VAR}` references only.

## Consequences
- New files require a deliberate decision to add them to the allowlist —
  slower to extend, but nothing gets committed by accident.
- `scripts/sync-check.sh` exists specifically because an allowlist-based
  repo can silently drift from the live machine; the drift must be
  surfaced, never assumed away.
