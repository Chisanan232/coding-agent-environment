# ADR-0004: Status line is first-class reproducible operational config

## Status
Accepted (SPE-72)

## Context
Long-running autonomous coding sessions need visibility into model/
reasoning state, context pressure, rate-limit quota, Git state, and
(after SPE-74) which directory-scoped profile is active. Losing this on a
new machine isn't cosmetic — it's losing the primary signal for "am I
about to run out of something."

## Decision
Version `.claude/statusline.py`, `.claude/subagent-statusline.py`, and
their data-source hook (`bg-track.py`) as tracked source, not left as
undocumented live-machine state. Every subprocess call gets a sub-second
timeout and fails silently (segment omitted) rather than blocking the
prompt — reproducibility must not come at the cost of resilience.

## Consequences
- Dependency closure is explicit and documented (`docs/ALLOWLIST.md`):
  `python3` stdlib-only, plus base macOS tools (`git`, `df`, `sysctl`,
  `vm_stat`) — nothing installed specially for the status line.
- The optional caveman-mode plugin badge intentionally reads a plugin-cache
  path (untracked, machine-specific) and degrades to omitted elsewhere —
  documented as a known, accepted gap rather than hidden.
- Codex's `tui.status_line`/`terminal_title` ordering is versioned in
  `codex/config.toml` as plain data (no custom script needed on that
  side — the native mechanism was sufficient).
