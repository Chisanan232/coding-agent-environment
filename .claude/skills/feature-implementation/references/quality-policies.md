# Quality policies

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

## Testing Expectations

- All new features require tests. All bug fixes require a regression test.
- Tests must be deterministic, isolated, and fast.
- Test behavior, not implementation. Tests must not assert on private internals.
- Do not disable failing tests. Fix them or escalate.
- Coverage is a metric, not the goal. Meaningful tests matter more than coverage %.
- Run impacted tests during iteration. Run the full suite before committing.

> **Project-specific**: test runner, coverage tool, fixture strategy, mocking library,
> test directory layout, coverage threshold, and database test strategy belong in the
> project's `.claude/CLAUDE.md`.

