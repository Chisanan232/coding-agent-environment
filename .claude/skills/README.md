# Skills Directory

This directory contains all Claude Code skills for this configuration.

---

## Skill types

| Type | Invocation | Examples |
|---|---|---|
| Auto-used | Triggered automatically by agent/skill context | `dev-impl-loop`, `acceptance-validation` |
| Command-like | Invoked explicitly via `/skill-name` | `/pr-readiness`, `/project-setup` |

---

## Language Repair Skills Guide

Language-specific repair skills fix type errors, lint violations, test failures,
and pre-commit failures for a given language and toolchain. Python, TypeScript,
and Go skills are provided. Add skills for other languages using the naming
convention and template below.

### Naming convention

```
<language>-<tool>-<action>
```

| Action | When to use |
|---|---|
| `debugging` | Tool reports errors that must be analyzed and fixed (type checkers, compilers) |
| `fixing` | Tool reports violations that can be directly corrected (linters, formatters) |
| `repair` | Tool blocks a gate and must be cleared before proceeding (pre-commit, build) |

### Provided skills

**Python**

| Skill | Tool | Action |
|---|---|---|
| `python-mypy-debugging` | mypy (type checker) | Diagnose and fix type errors |
| `python-ruff-fixing` | ruff (linter/formatter) | Fix lint violations with auto-fix review |
| `python-precommit-repair` | pre-commit | Repair hook failures without `--no-verify` |
| `python-pytest-failure-debugging` | pytest | Diagnose collection, fixture, and test body failures |

**TypeScript / JavaScript**

| Skill | Tool | Action |
|---|---|---|
| `typescript-tsc-debugging` | tsc (type checker) | Diagnose and fix TypeScript type errors |
| `typescript-eslint-fixing` | ESLint | Fix lint violations (auto-fix first, then manual) |
| `node-precommit-repair` | pre-commit (JS hooks) | Repair hook failures without `--no-verify` |

**Go**

| Skill | Tool | Action |
|---|---|---|
| `go-vet-debugging` | go vet / go build | Diagnose vet errors and build failures |
| `go-golangci-fixing` | golangci-lint | Fix lint violations per linter category |

### Extending the inventory

Search existing skills first. Add a language/tool skill only for a current
consumer whose semantics do not match an existing capability; hypothetical
language coverage or symmetry alone does not justify a new skill.

Canonical subtraction skills are installed as `requirement-zero@requirement-zero`
from `Chisanan232/requirement-zero`; their source remains in that repository.
Invoke `requirement-zero:requirement-zero` for NEW unvalidated scope and
`requirement-zero:codebase-zero` to audit EXISTING artifacts. Installation is
specified in the repository [README](../../README.md).

### Minimal SKILL.md template

```markdown
# SKILL.md — <language>-<tool>-<action>

> **Language**: <Language>. This skill is specific to <tool>.
> For other languages, see ~/.claude/skills/README.md.

## Purpose
<One sentence: what problem this skill fixes and how.>

## Type
Auto-used. Claude Code invokes this skill when <tool> reports errors.

## Do Not Assume
- Do not assume the first error is the root cause — read all reported errors first.
- Do not suppress errors without understanding them.
- Do not change logic to make errors disappear — fix the actual type/lint issue.

## Steps

### Phase 1 — Understand the errors
1. Run <tool command> and capture all output.
2. Group errors by file and category.
3. Read the error messages — do not guess at fixes before understanding root causes.

### Phase 2 — Fix
4. Address errors from the simplest (missing annotation, wrong type) to the
   most complex (structural issues).
5. Apply the minimal fix for each error.
6. Re-run <tool> after each fix to confirm progress.

### Phase 3 — Verify
7. Run the full <tool> check. Zero errors required before proceeding.
8. Run impacted tests to confirm no regressions.

## Safe-Fix Guidance
- Never suppress errors with ignore comments unless the type/lint system is wrong
  and the suppression is documented with a reason.
- Do not change logic to satisfy the tool — fix the annotation or rule violation.
```

### Registering a new skill

After creating a new skill, add it to the **Language-Specific Repair Skills**
section in your project's `.claude/CLAUDE.md`.
