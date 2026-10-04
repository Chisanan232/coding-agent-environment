---
name: python-precommit-repair
description: "Diagnose and fix pre-commit hook failures so that commits can proceed cleanly."
---

# SKILL.md — python-precommit-repair

> **Language**: Python (primary). This skill covers pre-commit hook repair for
> Python-tooled hooks (ruff, mypy, black, isort, etc.). The general pre-commit
> repair sequence applies to any language — adapt the tool-specific fix steps
> for your stack's hooks.

## Purpose
Diagnose and fix pre-commit hook failures so that commits can proceed cleanly.

## Type
Auto-used when `pre-commit run` fails.

## Do Not Assume
- Do not assume `--no-verify` is acceptable — it is not.
- Do not assume all pre-commit hooks run the same tools as your local install.
- Do not assume the pre-commit environment is up to date — it may need `autoupdate`.

## Steps

1. Run `pre-commit run --all-files` and read the full output.
2. Identify which hooks failed.
3. For each failed hook:
   - If it is a formatter (black, ruff format): run the formatter directly, then
     re-stage and retry.
   - If it is a linter (ruff, flake8): run `python-ruff-fixing` skill.
   - If it is a type checker (mypy): run `python-mypy-debugging` skill.
   - If it is a secrets detector (detect-secrets, gitleaks): review the flagged
     content — if it is a false positive, update the baseline; if it is a real
     secret, remove it from the code immediately.
   - If it is a YAML/TOML/JSON validator: fix the syntax error in the flagged file.
   - If it is an outdated hook: run `pre-commit autoupdate` and re-run.
4. Re-run `pre-commit run --all-files` and confirm all hooks pass.
5. Stage the corrected files and commit.

## Safe-Fix Guidance
- Never use `--no-verify`. Fix the underlying problem.
- If a secrets detector flags real credentials: remove them from the file, rotate
  the credential immediately, and rewrite git history if they were committed.
- If a hook environment is broken: rebuild it with `pre-commit clean && pre-commit install`.

## Validation Guidance
- After each fix: re-run the specific hook that failed.
- After all hooks pass: run `pre-commit run --all-files` once more for confirmation.
- Then run the full test suite.
