---
name: dependency-upgrade-review
description: "Review a dependency upgrade change to assess safety, compatibility, and risk before merging."
---

# SKILL.md — dependency-upgrade-review  [COMMAND-LIKE SKILL]

## Purpose
Review a dependency upgrade change to assess safety, compatibility, and risk
before merging.

## Type
Command-like. Invoke explicitly via `/dependency-upgrade-review` or by asking
Claude Code to "Review this dependency upgrade."

## When to use
Before merging any PR that bumps one or more dependencies.

## Steps

### 1. Identify what changed
- [ ] Read the diff of the lock file and the dependency manifest.
- [ ] List every package that changed: name, old version, new version.
- [ ] Identify whether each change is: patch, minor, or major.

### 2. Assess each upgrade
For each upgraded package:
- [ ] Read the package's changelog or release notes for the version range.
- [ ] Identify breaking changes or deprecations.
- [ ] Identify security fixes — treat these as high priority.
- [ ] Identify performance changes that could affect production.

### 3. Compatibility check
- [ ] Run the full test suite against the upgraded dependencies.
- [ ] Run the type checker — new package versions may change type signatures.
- [ ] If any tests fail, identify whether the failure is in the upgrade or in the tests.

### 4. Security check
- [ ] Run `pip-audit` or `npm audit` or equivalent.
  [PROJECT-SPECIFIC]
- [ ] Confirm no new vulnerabilities are introduced.

### 5. Risk classification
Classify each upgrade:
- **Low risk**: patch version, no breaking changes, tests pass
- **Medium risk**: minor version with deprecations, or major version with no
  breaking changes in used APIs
- **High risk**: major version with breaking changes, or any upgrade that required
  code changes to pass tests

### 6. Recommendation
- [ ] Provide a merge recommendation: safe to merge / needs attention / do not merge.
- [ ] List any required code changes to adapt to the new versions.
- [ ] Note any packages that should be pinned more carefully.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Report merge recommendation based on changed compatibility/security/behavior,
validation evidence and material unknowns. Expand risky packages and required
action; aggregate uneventful upgrades rather than narrating package/file edits.
