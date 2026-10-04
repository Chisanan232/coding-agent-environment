---
name: ci-failure-triage
description: Diagnose and reproduce CI failures, repair the cause, and verify the affected capability.
---

# SKILL.md — ci-failure-triage

## Purpose
Identify the root cause of a CI failure, reproduce it locally, apply a targeted fix,
and verify the fix before pushing.

## Type
Auto-used. Claude Code invokes this skill whenever CI is red.

## Do Not Assume
- Do not assume the failure is caused by your most recent change — check the history.
- Do not assume the CI log contains the complete error — look for upstream failures.
- Do not assume a failure that passes locally is a flaky test — investigate first.
- Do not assume suppressing the check is ever acceptable.

## Steps

### Phase 1 — Identify failure type
1. Read the full CI log. Identify the exact failing step.
2. Classify the failure:
   - **Test failure**: assertion error, unexpected exception, timeout
   - **Lint violation**: ruff, eslint, or equivalent
   - **Type error**: mypy, pyright, tsc
   - **Build failure**: compilation error, missing dependency, bad import
   - **Coverage drop**: coverage fell below threshold
   - **Security alert**: bandit, semgrep, or dependency audit
   - **Infrastructure**: CI runner issue, flaky network, Docker pull failure

### Phase 2 — Reproduce locally
3. Run the exact failing command locally.
4. If it passes locally but fails in CI, investigate CI-specific differences:
   - Environment variables
   - Python / Node / runtime version
   - Operating system differences
   - Missing secrets or credentials
   - Network access to external services
5. Do not propose a fix until you can reproduce or explain the CI-specific cause.

### Phase 3 — Root cause analysis
6. Read the failing test or failing code fully.
7. Identify the underlying cause — not just the symptom.
8. Ask: did this fail because of my change, or was it already failing?
9. Check git blame and recent commits on the affected code.

### Phase 4 — Apply targeted fix
10. Fix only the root cause. Do not sweep unrelated code.
11. If it is a test failure: fix the code (preferred) or fix the test if the test
    itself is wrong. Never delete a failing test.
12. If it is a lint violation: fix the code. Do not add `# noqa` without a specific
    code and explanation.
13. If it is a type error: run the language-appropriate type-checking repair skill
    (e.g., `python-mypy-debugging` for Python, or the equivalent for your language).
14. If it is a build failure: investigate the dependency or import.
15. If it is a coverage drop: add the missing tests.

### Phase 5 — Verify and commit
16. Run the full failing command locally and confirm it passes.
17. Run the complete validation suite.
18. Commit the fix as a focused, isolated commit.
19. Push and monitor CI.

### Phase 6 — Prevent recurrence
20. Ask: can a regression test prevent this failure from recurring?
21. If yes, add it.
22. If the failure was caused by an environment difference, document it in the
    project's `.claude/CLAUDE.md` (not the global `~/.claude/CLAUDE.md`) or the
    project's troubleshooting guide.

## Safe-Fix Guidance
- Never add `skip`, `xfail`, `# noqa`, or `# type: ignore` to silence a CI failure.
- Never modify the CI pipeline definition to make a failing job non-required.
- If a fix requires a dependency change, review it with `dependency-upgrade-review`.

## Validation Guidance
- After the fix: run the exact command that was failing in CI.
- After verification: run the full suite.
- Confirm CI passes on the pushed branch before closing the loop.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) when sharing a
failure or repair record. Identify the failing capability/gate, observed cause
and scope of local reproduction, actual verification evidence and remaining
owner action. Reference the run rather than copying its full log.
