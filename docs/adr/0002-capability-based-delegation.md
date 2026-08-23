# ADR-0002: Capability-based delegation, not organizational-role agents

## Status
Accepted (SPE-70)

## Context
The repository previously modeled Claude Code sub-agents as fixed
organizational roles — `dev-agent`, `qa-agent`, `dev-lead-agent`,
`release-agent` — with hand-off rules between them ("dev-agent implements,
qa-agent validates, dev-lead-agent decides"). SPE-70's audit found the
referenced agent definition files (`.claude/agents/dev-agent.md` etc.)
never actually existed in the repository — 19+ files (CLAUDE.md, 15 skill
files, 2 hook scripts, 2 MCP `_role` notes) referenced a role hierarchy
that was aspirational, not implemented.

## Decision
Retire the role roster. The main agent (Sonnet) executes routine work
directly. A sub-agent is spawned only when one of these is concretely
true: stronger reasoning helps (delegate to `opus-architect`), isolated
context reduces noise on a broad search/audit, independent verification
benefits from a fresh perspective, or genuine parallelism exists on
non-overlapping work. No new fixed role names replace the old ones.

## Consequences
- Skills no longer say "invoked by dev-agent" — they describe the actual
  trigger condition ("run before opening a PR") instead.
- Workflow *capability* (decomposition, implementation-loop phases,
  circuit breaker, workflow-state, session-memory) was preserved verbatim
  — only the role-hand-off framing was removed.
- Delegation decisions are made per-task by the main agent, not
  pre-assigned by a role table — requires judgment each time rather than
  a lookup, which is the intended trade-off (capability match beats
  role simulation).
