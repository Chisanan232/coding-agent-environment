---
name: project-setup
description: "Onboard a repository with verified commands, architecture constraints and native agent instructions while preserving existing configuration."
---

# Project setup

Read the existing repository instructions, remote, language/package/test/lint/type
configuration, issue tracker and merge gates before writing. Reuse matching
configuration; do not overwrite existing instructions or invent project facts.

For Claude use `.claude/CLAUDE.md`; for Codex use root `AGENTS.md`. Shared
architecture, commands and project facts should reference canonical repo docs
rather than fork them. Include identity/owner, accepted architecture boundaries,
exact install/build/impacted/full-test/lint/type/pre-commit commands, testing
strategy, authoritative tracker/docs, extra merge gates and applicable repair
skills. Resolve evidence from config/code; label missing information. Ask only
for material unknowns or approval to overwrite unrelated authored content.

Read `engineering-runtime` for native ticket/worktree context. Add only its
session context paths to gitignore when needed. Verify configured hooks through
the native host's discovery/trust mechanism; executable file count is not proof
of registration. Never auto-trust a new hook. Verify actual tools and commands,
then report readiness and remaining gaps. Use `engineering-workflow` for merge
strategy and `evidence-first-briefing` for persistent output.
