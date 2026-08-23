# ADR-0005: MCP/plugin/config source-of-truth boundaries

## Status
Accepted (SPE-70/SPE-71)

## Context
The repo has two MCP config files: `.mcp.json` (repo root) and
`.claude/mcp-servers.runtime.json`. On the surface this looks like
duplication — SPE-70's audit found one genuine accidental duplicate
(`codebase-memory-mcp`, declared in both, one copy with a hardcoded
`/Users/bryant/.nvm/...` path) and removed it.

## Decision
Keep the two-file split as the intended model, not a bug: `.mcp.json` is
the **global template** — tiered `always-active`/`project-opt-in`,
placeholder secrets, meant to be copied to `~/.claude/.mcp.json`.
`mcp-servers.runtime.json` is the **project-runtime** set — servers
actually wired for this repo's own development (GitHub, Cloudflare,
CircleCI, Neon, gcloud, codegraph), secrets redacted. One canonical source
per use case, not one file trying to serve both purposes.

## Consequences
- Adding a new MCP server means deciding which file it belongs in — a
  global capability template vs a project-specific runtime wiring — not
  just appending to whichever file is open.
- Every entry meant to be always-on now has an explicit `_tier`/`disabled`
  flag (SPE-71 fixed the two entries that were "active by omission").
- Skills/hooks/capability-agents still in use are versioned
  (`.claude/skills/`, `.claude/agents/opus-architect.md`); deprecated
  role-agent artifacts are absent (ADR-0002).
