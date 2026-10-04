---
name: release-readiness
description: "Confirm that the codebase is in a releasable state before tagging a release."
---

# SKILL.md — release-readiness  [COMMAND-LIKE SKILL]

## Purpose
Confirm that the codebase is in a releasable state before tagging a release.

## Type
Command-like. Invoke explicitly by naming `release-readiness` or by asking The coding agent
to "Run the release readiness check."

## When to use
Before tagging a release version. After all PRs for the release are merged.

## Steps

### 1. CI status
- [ ] CI is green on the main branch.
- [ ] No pending PRs that were intended for this release.

### 2. Version consistency
- [ ] Version number is updated in all required locations.
  [PROJECT-SPECIFIC — e.g., `pyproject.toml`, `__init__.py`, `package.json`]
- [ ] Changelog is updated.
  [PROJECT-SPECIFIC — e.g., `CHANGELOG.md`]

### 3. Full validation
- [ ] Run the full test suite on the release branch.
- [ ] Run lint and type checks.
- [ ] Run pre-commit hooks.
- [ ] All pass.

### 4. Security review
- [ ] Run dependency audit: [PROJECT-SPECIFIC — e.g., `pip-audit` or `npm audit`]
- [ ] No critical or high severity vulnerabilities in dependencies.
- [ ] No secrets committed in the release diff.

### 5. Dependency health
- [ ] All dependencies are at their pinned versions.
- [ ] No dependency is pinned to a development or pre-release version.
  [REFINE FOR THIS REPO]

### 6. Documentation
- [ ] All public API changes are documented.
- [ ] Release notes are written and reviewed.

### 7. MCP-assisted checks (if available)
- [ ] If `observability` MCP capability is available: check for active alerts or incidents.
- [ ] If `static_analysis` MCP capability is available: confirm quality gate passes.
- [ ] If `coverage_reporting` MCP capability is available: confirm coverage meets threshold.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Report release readiness, validation evidence, blocking risks/unknowns and
required action. A tag message describes shipped capability and compatibility
impact. Keep routine successful checklist items internal.
