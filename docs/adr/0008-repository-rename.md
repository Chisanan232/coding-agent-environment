# ADR-0008: Repository rename to `coding-agent-environment`

## Status
Accepted (SPE-71)

## Context
The repository was named `claude-code-config`, but its scope grew to
cover Claude Code, Codex, a shared toolchain, status-line config, and
directory-scoped profiles — no longer Claude-specific.

## Decision
Renamed `Chisanan232/claude-code-config` → `Chisanan232/coding-agent-environment`.
Name availability was verified before renaming (no collision). Visibility
was left unchanged (PUBLIC) — a visibility change is a separate,
consequential decision this rename did not make. All self-referencing
clone/install commands, the local `origin` remote, and the repo
description/topics were updated in the same change; a fresh clone from
the new URL and the old URL's redirect were both verified working.

## Consequences
- GitHub's redirect from the old URL works today, but is not relied upon
  permanently — every reference in this repo already points at the new
  URL.
- Any external bookmark/script still using `claude-code-config` will
  redirect for now but should be updated; this repo cannot control that.
- The rename was completed in the same PR as the decision — not deferred
  as a "documentation only" follow-up, per SPE-71's explicit requirement.
