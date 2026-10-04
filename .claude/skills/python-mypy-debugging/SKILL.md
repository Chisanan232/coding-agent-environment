---
name: python-mypy-debugging
description: "Diagnose and fix mypy type errors correctly, without suppressing them or degrading type coverage."
---

# SKILL.md — python-mypy-debugging

> **Language**: Python. This skill is specific to the mypy type checker.
> For other languages, create an equivalent skill following the naming convention
> `<language>-<tool>-debugging` (e.g., `typescript-tsc-debugging`, `go-vet-debugging`).

## Purpose
Diagnose and fix mypy type errors correctly, without suppressing them or
degrading type coverage.

## Type
Auto-used when mypy reports errors.

## Do Not Assume
- Do not assume `# type: ignore` is the right fix — it almost never is.
- Do not assume the error is in the line mypy points to — the root cause may be
  in the function signature or a dependency's stubs.
- Do not assume the type annotation is correct just because the code runs.

## Steps

1. Run `mypy src/` (or the project-specific mypy command from the active repository instructions).
2. Read the first error completely — do not fix errors in bulk.
3. Navigate to the flagged line and read the surrounding context.
4. Identify the error category:
   - **Missing annotation**: add the type hint
   - **Incompatible type**: trace the actual type vs expected type through the call chain
   - **Missing return type**: add return type annotation
   - **Untyped function called**: add annotations or use a typed wrapper
   - **Optional not handled**: add a `None` check before use
   - **Missing stub**: install `types-<package>` or add inline stub
5. Fix the root cause, not the symptom.
6. Re-run mypy and confirm the error count decreased.
7. Repeat for each remaining error.
8. Run the full test suite after all mypy fixes to confirm runtime behavior is unchanged.

## Safe-Fix Guidance
- Use `# type: ignore[specific-code]` only as a last resort, with a comment.
- Never use bare `# type: ignore`.
- If fixing a type error requires changing a function signature, check all callers.
- If a stub package is missing, prefer installing `types-<package>` over suppressing.

## Validation Guidance
- After each fix: re-run mypy on the affected file.
- After all fixes: run mypy on the full `src/` directory.
- After mypy is clean: run the full test suite.
