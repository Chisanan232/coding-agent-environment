---
name: python-ruff-fixing
description: "Fix ruff lint violations correctly and consistently."
---

# SKILL.md — python-ruff-fixing

> **Language**: Python. This skill is specific to the ruff linter/formatter.
> For other languages, create an equivalent skill following the naming convention
> `<language>-<tool>-fixing` (e.g., `typescript-eslint-fixing`, `go-golangci-fixing`, `rust-clippy-repair`).

## Purpose
Fix ruff lint violations correctly and consistently.

## Type
Auto-used when ruff reports errors.

## Do Not Assume
- Do not assume `# noqa` is acceptable without a code and explanation.
- Do not assume auto-fix is always correct — review `ruff --fix` diffs before committing.
- Do not assume the ruff config is default — read `pyproject.toml [tool.ruff]` first.

## Steps

1. Run `ruff check .` to see all violations.
2. Run `ruff check . --fix` to apply safe auto-fixes.
3. Review the diff produced by `--fix` before staging it.
4. For violations that ruff cannot auto-fix, fix them manually:
   - **Unused imports**: remove them (check nothing else imports from the same module via this file)
   - **Line too long**: wrap the line correctly for the language construct
   - **Undefined name**: trace where the name should come from
   - **Bare except**: replace with specific exception type(s)
5. Re-run `ruff check .` and confirm zero violations.
6. Run the full test suite to confirm no behavior changed.

## Safe-Fix Guidance
- Never add `# noqa` without a specific code (e.g., `# noqa: E501`) and a comment.
- If a rule is consistently inappropriate for this project, add it to `pyproject.toml`
  `[tool.ruff.lint] ignore = [...]` rather than suppressing per-line.
- Do not silence violations in files you were not already modifying.

## Validation Guidance
- After auto-fix: review the diff, then run ruff again.
- After manual fixes: run ruff, then run the test suite.
