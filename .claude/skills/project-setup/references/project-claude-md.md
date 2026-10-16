# Global versus project CLAUDE.md

Moved verbatim from `.claude/CLAUDE.md`; that file keeps the layering rule.

## Global vs Project Configuration

This file lives at `~/.claude/CLAUDE.md` and applies to **all projects** you work
on with Claude Code. It contains durable behavioral rules, workflow conventions,
and agent coordination policies that do not change from project to project.

### What belongs in this global file

- Safe implementation policy
- Commit and PR conventions
- CI triage, push gate, and auto-merge rules
- Workflow state, circuit breaker, and agent delegation model
- MCP capability routing
- Skill invocation conventions and agent coordination

### What belongs in each project's `.claude/CLAUDE.md`

Create a `.claude/CLAUDE.md` at the repository root and fill in these sections:

| Section | Content |
|---|---|
| Repository Identity | Repo name, owner, primary language, runtime, dependency policy |
| Architecture Constraints | Directory conventions, layer boundaries, design rules for this repo |
| Package and Build Commands | Exact install, test, lint, build commands |
| Testing Tooling | Test runner, coverage tool, fixture strategy, mocking library |
| Type Checker | Which type checker, config file, project-specific suppression rules |
| Linting Tooling | Linter name, formatter, pre-commit config location |
| Source-of-Truth Systems | Issue tracker URL, wiki/docs space, Slack channel |
| Merge Gates | Additional repository-specific approvals or protected-branch checks |
| Polling Intervals | PR health check and release watch cadence for this repo |
| Language-Specific Repair Skills | Which `<language>-*` repair skills apply (e.g., `python-ruff-fixing`) |
