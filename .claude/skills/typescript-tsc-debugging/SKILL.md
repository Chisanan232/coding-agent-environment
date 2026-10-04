---
name: typescript-tsc-debugging
description: "Diagnose and fix TypeScript type errors reported by `tsc --noEmit` so the type checker exits clean before committing."
---

# SKILL.md — typescript-tsc-debugging

> **Language**: TypeScript. This skill is specific to the `tsc` type checker.
> For other languages, see `the installed language-repair skills`.

## Purpose
Diagnose and fix TypeScript type errors reported by `tsc --noEmit` so the
type checker exits clean before committing.

## Type
Auto-used. The coding agent invokes this skill when `tsc` reports type errors.

## Do Not Assume
- Do not assume the first error is the root cause — `tsc` cascades errors;
  fix upstream errors first and re-run before addressing downstream ones.
- Do not suppress errors with `@ts-ignore` or `@ts-expect-error` without a
  code comment explaining why the suppression is justified.
- Do not change logic to satisfy the type checker — fix the annotation.
- Do not widen types to `any` as a shortcut. Use the narrowest correct type.

## Steps

### Phase 1 — Understand the errors
1. Run `tsc --noEmit` and capture all output:
   ```bash
   npx tsc --noEmit 2>&1 | tee /tmp/tsc-errors.txt
   ```
2. Count total errors and group by file and error code.
3. Read the full error list before touching any code — do not fix the first
   error blindly. Cascading errors vanish when the root cause is fixed.
4. Identify root causes:
   - `TS2345` (argument not assignable): wrong type passed — fix the call site or signature.
   - `TS2339` (property does not exist): missing property on type — add to interface or check the type.
   - `TS2322` (type not assignable): assignment mismatch — correct the type annotation.
   - `TS2304` (cannot find name): missing import or declaration — add import or declare type.
   - `TS7006` (implicit any): parameter needs explicit type annotation.
   - `TS2532` / `TS2531` (possibly undefined/null): add null check or use non-null assertion with comment.

### Phase 2 — Fix
5. Fix errors from the simplest (missing annotation, wrong import) to the most complex
   (structural interface mismatches).
6. Prefer fixing the type annotation over changing the runtime code.
7. If a suppression is genuinely needed, use scoped `@ts-expect-error` with a comment:
   ```typescript
   // @ts-expect-error — third-party library missing type for X, tracked in [issue]
   ```
   Never use `@ts-ignore` — it does not verify the suppression is still needed.
8. Re-run `tsc --noEmit` after each fix. Confirm the targeted error is gone.

### Phase 3 — Verify
9. Run `tsc --noEmit` — zero errors required before proceeding.
10. Run impacted tests to confirm no regressions:
    ```bash
    npx jest --testPathPattern="<affected-module>" --passWithNoTests
    ```

## Safe-Fix Guidance
- Never suppress a type error without a code comment explaining the reason.
- Do not add `as any` casts — they silently hide real bugs.
- If fixing a type requires changing a public API signature, flag it for review
  — callers in other files may be affected.
- `strict: true` errors in `tsconfig.json` are expected and must be fixed, not
  disabled by loosening `tsconfig.json`.
