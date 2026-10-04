---
name: go-vet-debugging
description: "Diagnose and fix errors reported by `go vet` and `go build` \u2014 including suspicious constructs, misused sync primitives, and structural type errors."
---

# SKILL.md — go-vet-debugging

> **Language**: Go. This skill is specific to `go vet` and the Go compiler.
> For other languages, see `the installed language-repair skills`.

## Purpose
Diagnose and fix errors reported by `go vet` and `go build` — including
suspicious constructs, misused sync primitives, and structural type errors.

## Type
Auto-used. The coding agent invokes this skill when `go vet` or `go build` reports errors.

## Do Not Assume
- Do not assume `go vet` errors are cosmetic — they detect real runtime bugs
  (race conditions, unreachable code, misused Printf verbs).
- Do not suppress vet errors with build tags unless the tool itself is broken.
- Do not change logic to silence the tool — fix the actual construct.

## Steps

### Phase 1 — Understand the errors
1. Run vet and build, capturing all output:
   ```bash
   go vet ./... 2>&1 | tee /tmp/govet-errors.txt
   go build ./... 2>&1 | tee /tmp/gobuild-errors.txt
   ```
2. Group errors by package and type.
3. Common `go vet` error categories and their fixes:
   - `printf: X arg list ends with redundant newline` — remove `\n` from format string.
   - `copylocks: X passes lock by value` — pass pointer to the struct containing the mutex.
   - `unreachable: unreachable code` — remove or restructure the dead branch.
   - `structtag: struct field X has malformed json tag` — fix the struct tag syntax.
   - `shift: shift count too large` — check bit-width assumptions.
   - `composites: X literal uses unkeyed fields` — add field names to composite literals.
4. Common `go build` errors:
   - `undefined: X` — missing import or wrong package path.
   - `cannot use X (type Y) as type Z` — type mismatch; fix the call site or interface.
   - `too many arguments in call to X` — wrong number of arguments; check the signature.

### Phase 2 — Fix
5. Fix errors from the simplest (missing import, struct tag) to the most complex
   (interface mismatches, pointer/value receiver conflicts).
6. For `copylocks`: pass `*sync.Mutex` or `*sync.WaitGroup`, never by value.
7. Re-run `go vet ./...` after each fix group.

### Phase 3 — Verify
8. Run `go vet ./...` — zero errors required.
9. Run `go build ./...` — zero errors required.
10. Run impacted tests:
    ```bash
    go test ./... -run TestAffectedPackage
    ```

## Safe-Fix Guidance
- Do not use `//nolint` or build-tag workarounds unless the vet tool itself has
  a known false positive — document the reason in a code comment if you do.
- `go vet` errors often indicate real bugs. Treat them as bugs, not warnings.
