# Quality policies

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

## Testing Expectations

- All new features require tests. All bug fixes require a regression test.
- Tests must be deterministic, isolated, and fast.
- Test behavior, not implementation. Tests must not assert on private internals.
- Do not disable failing tests. Fix them or escalate.
- Coverage is a metric, not the goal. Meaningful tests matter more than coverage %.
- Run impacted tests during iteration. Run the relevant full validation at meaningful checkpoints and before a PR is ready, not after every commit; record the exact tested revision.

> **Project-specific**: test runner, coverage tool, fixture strategy, mocking library,
> test directory layout, coverage threshold, and database test strategy belong in the
> project's `.claude/CLAUDE.md`.

## Type Checking Policy

- All public APIs must have complete type annotations.
- All function signatures must be annotated (parameters + return type).
- All class attributes must be annotated at the class level.
- Do not use the language's "any" escape hatch without a code comment explaining why.
- Suppress type errors only with the language-specific scoped suppression mechanism,
  never suppress an entire file or block without a documented reason.
- Type annotations must never change runtime behavior.
- Run the configured type checker at each validation checkpoint and before a PR is ready; pre-commit keeps only fast lint and format checks per commit.

> **Project-specific**: which type checker (mypy, pyright, tsc, etc.), config file
> location, and language-specific annotation syntax belong in the project's `.claude/CLAUDE.md`.

## Linting and Formatting Policy

- All lint errors must be fixed before committing.
- Do not suppress lint warnings without a comment explaining the exception.
- Pre-commit hooks enforce both linting and formatting.
- Config lives in the project's standard config file (see project-level CLAUDE.md).

> **Project-specific**: linter name, formatter, and config file location belong in the
> project's `.claude/CLAUDE.md`.

