---
name: typescript-eslint-fixing
description: "Fix ESLint violations in TypeScript and JavaScript files so the linter exits clean. Covers both auto-fixable and manual violations."
---

# SKILL.md — typescript-eslint-fixing

> **Language**: TypeScript / JavaScript. This skill is specific to ESLint.
> For other languages, see `the installed language-repair skills`.

## Purpose
Fix ESLint violations in TypeScript and JavaScript files so the linter exits
clean. Covers both auto-fixable and manual violations.

## Type
Auto-used. The coding agent invokes this skill when ESLint reports violations.

## Do Not Assume
- Do not assume all violations are auto-fixable — run `--fix` first, then
  address remaining violations manually.
- Do not suppress violations with `// eslint-disable` without a code comment.
- Do not change logic to silence a violation — fix the code pattern.
- Do not disable rules in `.eslintrc` to avoid fixing violations.

## Steps

### Phase 1 — Understand the violations
1. Run ESLint and capture all output:
   ```bash
   npx eslint . --ext .ts,.tsx,.js,.jsx 2>&1 | tee /tmp/eslint-errors.txt
   ```
2. Count total violations. Group by rule name.
3. Identify which violations are auto-fixable (marked with `[fix]` in output)
   and which require manual intervention.

### Phase 2 — Auto-fix
4. Apply auto-fixes:
   ```bash
   npx eslint . --ext .ts,.tsx,.js,.jsx --fix
   ```
5. Re-run ESLint to see remaining violations. Only manual violations remain.

### Phase 3 — Fix remaining violations manually
6. Address remaining violations from the simplest to the most complex.
   Common patterns:
   - `no-unused-vars` / `@typescript-eslint/no-unused-vars`: remove the unused
     variable, or prefix with `_` if it must be declared (destructuring).
   - `@typescript-eslint/explicit-function-return-type`: add the return type annotation.
   - `@typescript-eslint/no-explicit-any`: replace `any` with the correct type.
   - `no-console`: replace with proper logger or remove debug output.
   - `@typescript-eslint/no-floating-promises`: `await` the promise or add `.catch()`.
   - `import/order`: reorder import blocks to match the configured order.
7. If a suppression is genuinely necessary, use scoped inline disable with a reason:
   ```typescript
   // eslint-disable-next-line @typescript-eslint/no-explicit-any -- external SDK returns untyped response
   ```
8. Re-run ESLint after each manual fix group.

### Phase 4 — Verify
9. Run ESLint — zero violations required before proceeding.
10. Run impacted tests to confirm no regressions.

## Safe-Fix Guidance
- Never use `/* eslint-disable */` file-level disables — too broad.
- If a rule fires on generated code, configure `.eslintignore` to exclude the
  generated path rather than suppressing per-line.
- Do not remove rules from `.eslintrc` to avoid violations — fix the code.
