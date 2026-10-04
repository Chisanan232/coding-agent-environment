---
name: python-pytest-failure-debugging
description: "Diagnose and fix pytest failures \u2014 distinguishing test bugs from implementation bugs, reading tracebacks correctly, and resolving fixture, import, and assertion errors so the test suite returns to green."
---

# SKILL.md — python-pytest-failure-debugging

> **Language**: Python. This skill is specific to pytest.
> For other languages, see `the installed language-repair skills`.

## Purpose
Diagnose and fix pytest failures — distinguishing test bugs from implementation
bugs, reading tracebacks correctly, and resolving fixture, import, and
assertion errors so the test suite returns to green.

## Type
Auto-used. The coding agent invokes this skill when pytest reports failures or errors.

## Do Not Assume
- Do not assume the failure is in the implementation — the test itself may be wrong.
- Do not assume the first failure in the output is the root cause — read all failures
  before fixing any. Related failures often share a single root cause.
- Do not skip or mark a test `xfail` to make CI green — fix the root cause.
- Do not delete a failing test — it is catching a real problem.
- Do not modify a test to match broken behavior — fix the implementation.

## Steps

### Phase 1 — Capture all failures
1. Run pytest and capture the full output:
   ```bash
   python -m pytest --tb=short 2>&1 | tee /tmp/pytest-output.txt
   ```
2. Count total failures and errors. Group by failure type:
   - `FAILED` — assertion error or unexpected exception in test body
   - `ERROR` — failure during collection, setup, or teardown (not in test body)
   - `ERROR collecting` — import error or syntax error prevents collection
3. Read every failure before fixing any. Failures with the same root cause
   should be fixed together, not one at a time.

### Phase 2 — Classify each failure

**Collection errors (`ERROR collecting`):**
- Usually a syntax error or bad import in the test file or the module under test.
- Fix the import or syntax error first — all other tests in that file are blocked.

**Setup/teardown errors (`ERROR` in setup/teardown):**
- A fixture is raising an exception. Read the fixture code, not the test.
- Common causes: missing environment variable, unavailable external resource,
  wrong fixture scope (`function` vs `session`), fixture dependency cycle.

**Test body failures (`FAILED`):**
- Read the traceback from bottom to top — the assertion or exception is at the bottom.
- Common patterns:
  - `AssertionError: assert X == Y` — implementation returns wrong value. Fix the implementation.
  - `AssertionError: assert X == Y` in a test written after the fact — the test may
    assert the wrong expected value. Verify the expected value is correct first.
  - `AttributeError` / `ImportError` — missing attribute or wrong import path.
  - `TypeError` — wrong argument type or count. Check the function signature.
  - `KeyError` / `IndexError` — test data or fixture is missing expected structure.
  - `pytest.raises` block does not raise — implementation is not raising the expected exception.

### Phase 3 — Fix

4. Fix collection errors first — blocked test files must be unblocked before
   addressing individual test failures.
5. Fix fixture errors next — they affect multiple tests at once.
6. Fix individual test failures last, from simplest to most complex.
7. For each fix, confirm:
   - Is this a bug in the implementation? → Fix the implementation.
   - Is this a bug in the test? → Fix the test (and document why the expectation changed).
   - Is this a fixture scope problem? → Fix the fixture scope or parameterization.
8. Re-run only the failing tests after each fix:
   ```bash
   python -m pytest path/to/test_file.py::test_function_name -v
   ```

### Phase 4 — Verify
9. Run the full test suite with verbose output:
   ```bash
   python -m pytest --tb=short -q
   ```
   Zero failures and zero errors required before proceeding.
10. If coverage is configured, confirm no regression:
    ```bash
    python -m pytest --cov=src --cov-report=term-missing
    ```

## Useful pytest flags for diagnosis

| Flag | Purpose |
|---|---|
| `--tb=long` | Full traceback including local variables |
| `--tb=short` | Compact traceback (default for diagnosis) |
| `-x` | Stop after first failure (isolate one failure at a time) |
| `-v` | Verbose: show each test name and its result |
| `-s` | Show `print()` output (useful for tracing execution) |
| `--lf` | Run only tests that failed in the last run |
| `--pdb` | Drop into debugger on first failure |
| `-k "test_name"` | Run only tests matching the expression |
| `--no-header -q` | Minimal output for scripted checks |

## Safe-Fix Guidance
- Never add `@pytest.mark.skip` or `@pytest.mark.xfail` to a failing test
  without a code comment linking to a tracking issue. These are temporary
  workarounds, not permanent solutions.
- If a test was passing before your change and is now failing, assume your
  change is the cause unless `git blame` proves otherwise.
- If a fixture uses a real database or external service and is failing due to
  connectivity, do not mock the dependency to make the test pass — fix the
  environment or mark the test as needing the service.
- Parametrized test failures: when one parameter set fails, check all parameter
  sets before fixing — the root cause may be in the shared setup, not the value.
