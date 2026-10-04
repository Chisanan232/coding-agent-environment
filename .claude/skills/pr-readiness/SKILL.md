---
name: pr-readiness
description: "Run the pre-PR readiness gates when explicitly requested."
---

# SKILL.md — pr-readiness  [COMMAND-LIKE SKILL]

## Purpose
Run a complete pre-PR checklist to confirm the branch is ready for code review.
This is a deliberate, engineer-initiated procedure — not an automatic check.

## Type
Command-like. Invoke explicitly via `/pr-readiness` or by asking Claude Code to
"Run the PR readiness check."

## When to use
Before opening any pull request. Run this after all implementation work is done.

## When not to use
Do not run this mid-implementation — it is a completion gate, not a progress check.

## Steps

### 1. Branch health
- [ ] Confirm the branch is based on the latest base branch (main / master / develop — check the project's CLAUDE.md).
- [ ] Confirm there are no merge conflicts.
- [ ] Confirm there are no uncommitted changes.

### 2. Diff review
- [ ] Detect the base branch and read every changed line (do not hardcode `origin`):
      ```bash
      REMOTE=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null \
          | cut -d'/' -f1 || echo "origin")
      BASE=$(git rev-parse --abbrev-ref "${REMOTE}/HEAD" 2>/dev/null \
          | sed "s|${REMOTE}/||" || echo "main")
      git diff "${BASE}...HEAD"
      ```
- [ ] Confirm all changes are within scope.
- [ ] Confirm no debug code, temporary scaffolding, or stray print statements.
- [ ] Confirm no secrets or credentials in any changed file.
- [ ] Confirm no commented-out code without an explanation.

### 3. Test validation
- [ ] Run the full test suite. All tests must pass.
- [ ] Confirm coverage has not dropped below the project threshold.
- [ ] Confirm new behavior is covered by tests.

### 4. Code quality validation
- [ ] Run linter. Zero violations.
- [ ] Run type checker. Zero errors.
- [ ] Run pre-commit hooks. All pass.

### 5. Commit history review
- [ ] All commits are atomic and have descriptive messages.
- [ ] No "WIP", "fixup", or "temp" commits in the history.
- [ ] Commit messages follow the CLAUDE.md GitEmoji conventions.
- [ ] If any cleanup commits exist, squash them now (before the PR is open).

### 6. PR description
- [ ] Write the PR title following CLAUDE.md PR conventions.
- [ ] Write the PR body: use `.github/PULL_REQUEST_TEMPLATE.md` if it exists,
      otherwise use the PR description format from CLAUDE.md Pull Request Policy
      via `evidence-first-briefing`. Respect required project fields; omit optional
      empty boilerplate. Explain the semantic impact the reviewer must assess.
- [ ] Link the relevant issue(s).

### 7. MCP-assisted checks (if available)
- [ ] If `static_analysis` MCP capability is available: run SonarQube quality gate.
- [ ] If `coverage_reporting` MCP capability is available: check coverage trend.
- [ ] If `issue_tracking` MCP capability is available: confirm the linked issue exists.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Report the readiness verdict, aggregate validation evidence, draft PR title/body,
and unresolved blockers or unverified gates. Keep the checklist internal; expand
only failures, unknowns and checks needed for review.
