---
name: test-design
description: "Design tests that are deterministic, fast, isolated, and focused on observable behavior rather than implementation details."
---

# SKILL.md — test-design

## Purpose
Design tests that are deterministic, fast, isolated, and focused on observable
behavior rather than implementation details.

## Type
Auto-used. The coding agent invokes this skill when designing or reviewing tests.

## Do Not Assume
- Do not assume the test framework configuration is default — read the project's
  test runner config first (e.g., `pyproject.toml`, `jest.config.js`, `go.mod`).
- Do not assume that high coverage means good tests.
- Do not assume mocking is appropriate — check the active repository instructions for the project's mock policy.
- Do not assume existing tests are correct templates — read them critically.

## Steps

### Phase 1 — Understand the unit under test
1. Read the function, class, or module being tested.
2. Identify all code paths: happy path, error paths, edge cases.
3. Identify external dependencies: database, HTTP, filesystem, time.

### Phase 2 — Design test cases
4. Write one test per behavior, not one test per line of code.
5. Name tests descriptively: encode the unit under test, the condition, and the
   expected result. Follow the project's language convention
   (e.g., `test_<unit>_<condition>_<result>` for Python/Rust,
   `Test<Unit><Condition>` for Go, `it('should <result> when <condition>')` for JS/TS).
6. For each external dependency, decide: real (integration) or stub (unit)?
   Follow the active repository instructions test strategy for this project.
7. Ensure tests are independent — no shared mutable state between tests.

### Phase 3 — Write tests
8. Write the test body: arrange → act → assert.
9. Assert on public outputs and observable side effects only.
10. Do not assert on internal state, private attributes, or call counts unless
    the call count is the actual behavior being tested.
11. If a test requires complex setup, use a fixture, not inline setup code.

### Phase 4 — Validate
12. Run new tests in isolation to confirm they pass.
13. Remove one behavior from the implementation and confirm the test fails.
    If the test still passes, it is not testing what you think it is.
14. Run the full test suite to confirm no regressions.

## Safe-Fix Guidance
- Never disable a failing test without understanding why it fails.
- If a test is intermittently failing, treat it as a real bug in the code or
  the test setup — not as a flaky test to be ignored.

## Validation Guidance
- After writing new tests: run only those tests first.
- After completing test design: run the full suite.
