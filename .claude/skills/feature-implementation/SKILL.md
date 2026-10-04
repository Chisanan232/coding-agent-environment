# SKILL.md — feature-implementation

## Purpose
Implement new features safely, consistently, and traceably using the repository's
established conventions and a test-first mindset.

## Type
Auto-used. Claude Code invokes this skill whenever a feature implementation
task is confirmed by the engineer.

## Do Not Assume
- Do not assume you understand the requirement fully until you have asked at least
  one clarifying question.
- Do not assume the existing code is correct or idiomatic — read it first.
- Do not assume the test suite is complete — check what tests already exist.
- Do not assume a dependency is available — check the lock file.
- Do not assume CI is passing on the current branch — check before starting.

## Steps

### Phase 1 — Clarification
1. Read the relevant existing code in the affected area.
2. Use the approved requirement and acceptance criteria as canonical; do not
   duplicate or reopen them. Search before proposing new scope.
3. Identify relevant edge cases and error conditions from code and criteria.
4. Identify performance, security and compatibility constraints.
5. Confirm: what does "done" look like? What test would prove it works?

### Phase 2 — Planning
6. Identify the minimal set of files that must change.
7. Identify tests that will need to be added or modified.
8. Choose the smallest approach supported by the evidence. Use canonical
   `requirement-zero:requirement-zero` if proposing NEW unvalidated scope.
9. Ask only for consequential unresolved decisions; continue authorized work.

### Phase 3 — Test Design (before implementation)
10. Write or update tests that define the expected behavior.
11. Confirm tests fail against the current code (red state).
12. Do not skip this step — tests written after implementation tend to pass vacuously.

### Phase 4 — Implementation
13. Implement the feature to make the tests pass.
14. Follow all conventions from CLAUDE.md: naming, structure, type hints, error handling.
15. Change only what is necessary. Do not refactor adjacent code.
16. Run impacted tests after each logical unit of change.

### Phase 5 — Validation
17. Run the full test suite.
18. Run the linter.
19. Run the type checker.
20. Run pre-commit hooks.
21. All checks must pass before proceeding.

### Phase 6 — Commit
22. Stage only the files changed for this feature.
23. Write a commit message following the CLAUDE.md commit conventions.
24. Commit in small, logical increments — one concern per commit.
25. Confirm the repository is in a healthy state after each commit.

## Safe-Fix Guidance
- If a test fails unexpectedly during implementation, stop and investigate before
  continuing. Do not comment out or skip the failing test.
- If a lint violation appears in code you did not write, fix it only if it is in
  a file you are already modifying. Do not sweep the codebase.
- If a type error appears, resolve it properly. Do not add `# type: ignore` without
  a specific error code and an explanation comment.

## Validation Guidance
- Impacted validation: run tests in the module or package you changed.
- Full validation: run the complete test suite plus lint and type checks.
- Order: impacted validation during iteration → full validation before committing.
