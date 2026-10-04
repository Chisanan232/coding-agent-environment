---
name: code-review-prep
description: "Apply the shared engineering contract and prepare the current branch for review."
---

# Code review preparation

## Purpose

Adapt the host-neutral
[engineering-workflow](../engineering-workflow/SKILL.md) contract to Claude Code
before opening a pull request. Read and apply that contract first; it owns the
branch, commit, validation, review, title, body, merge, reconciliation, and
cleanup rules.

## Type

Auto-used. Claude Code invokes this skill before any pull request is opened.

## Procedure

1. Resolve the tracking remote and base branch from Git instead of assuming
   `origin` or `main`, then review the complete diff and commit range.
2. Check scope, public contracts, test coverage, secrets, temporary code, and
   project-specific safety or compatibility boundaries.
3. Run the repository's full relevant build, lint, format, type, test,
   pre-commit, security, privacy, documentation, and link checks. Use the
   matching language repair skill when a real check fails.
4. Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to the PR
   body. Preserve required repository templates and project fields.
5. Confirm the title, branch, commits, independent-review evidence, merge gates,
   and issue references against `engineering-workflow`.

Do not open the pull request with a genuine red gate or present an implementation
assertion as independent review.
