---
name: pr-readiness
description: "Run the shared engineering workflow's pre-PR gates when explicitly requested."
---

# Pull-request readiness

## Purpose

Run a deliberate readiness check after implementation is complete. Read and
apply [engineering-workflow](../engineering-workflow/SKILL.md) first; that shared
contract owns the lifecycle and title format.

## Type

Command-like. Invoke explicitly via `/pr-readiness` or a request to run the PR
readiness check.

## Procedure

1. Resolve the real base and tracking remote. Confirm branch currency, no merge
   conflicts, and no uncommitted state.
2. Review the complete diff and commit range for scope, secrets, debug or
   temporary code, public-contract changes, and missing tests.
3. Run the repository's full relevant build, lint, format, type, test,
   pre-commit, security, privacy, documentation, and link checks.
4. Check commit atomicity and the shared commit format. Do not rewrite history
   during active review or assume cleanup commits must be squashed.
5. Draft the shared title shape and an
   [evidence-first briefing](../evidence-first-briefing/SKILL.md) body. Preserve
   repository templates, material risks or unknowns, and issue references.
6. Report a readiness verdict with actual validation and any unresolved or
   unverified gate. Keep routine checklist chronology internal.

A genuine failing gate blocks readiness. Use `CI_UNAVAILABLE_EXTERNAL` only under
the narrow conditions in `engineering-workflow`.
