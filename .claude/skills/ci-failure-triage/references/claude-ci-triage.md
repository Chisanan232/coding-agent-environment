# Claude CI triage expectations

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

## CI/CD Triage Expectations

When CI fails, Claude Code must follow this sequence:

1. **Identify** the failure type: test failure, lint error, type error, build error,
   coverage drop, security alert, infrastructure issue.
2. **Reproduce locally** before proposing a fix. Do not guess from CI logs alone.
3. **Analyze** the root cause. Do not apply surface-level fixes.
4. **Fix** the underlying problem. Do not bypass or suppress CI checks.
5. **Verify** the fix resolves the failure locally.
6. **Commit** the fix as a focused, isolated commit.
7. **Push** and confirm CI passes.

### What not to do

- Do not merge on red CI.
- Do not add suppression annotations to silence failures.
- Do not delete failing tests.
- Do not bypass pre-commit with `--no-verify`.
- If a failure cannot be reproduced locally, say so before proposing a fix.

### Flaky tests

Flaky tests indicate real problems. Do not mark them as skip. Investigate
the root cause: race conditions, shared state, external dependencies, timing assumptions.

