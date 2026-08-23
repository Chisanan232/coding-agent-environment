# SKILL.md — release-preparation  [COMMAND-LIKE SKILL]

## Purpose
Prepare release artifacts (changelog, version config updates, release notes)
for the upcoming release window. This skill is preparatory — it does not trigger
the release or create tags.

## Type
Command-like. Run when a release window opens, or explicitly via
`/release-preparation`.

## When to use
- When the milestone is complete and a release is due.
- Before the automated release workflow runs, to prepare required inputs.

## When not to use
Do not invoke mid-sprint to check release readiness for a future window.
Use `release-readiness` for that instead.

## Steps

### Phase 1 — Identify release-window changes
1. Determine the current release base: the last tag on the release branch.
   Run: `git tag --sort=-creatordate | head -5` to identify recent tags.
2. List all commits since the last tag:
   `git log <last-tag>..HEAD --oneline --no-merges`
3. For each commit, classify the change type:
   - `feat` — new feature
   - `fix` — bug fix
   - `breaking` — breaking change (look for `BREAKING CHANGE` in commit body)
   - `deps` — dependency update
   - `chore` — internal / maintenance (typically omitted from user-facing notes)
4. Note changed packages or services with their version increments if applicable.

### Phase 2 — Draft release notes
5. Group classified changes by type.
6. Write human-readable release note entries following the project's format.
   [PROJECT-SPECIFIC — e.g., Keep a Changelog format, GitHub Releases format]
7. Link each entry to its PR or commit.
8. Flag any `breaking` changes prominently at the top.

### Phase 3 — Update release intent configuration
9. Update version references in project files if required:
   [PROJECT-SPECIFIC — e.g., `pyproject.toml`, `package.json`, `CHANGELOG.md`]
10. Stage the update.
11. Commit as an isolated commit:
    `📝 release: Prepare vX.Y.Z release notes and version bump`
12. Do not push — the automated release workflow will handle the push and tag.

### Phase 4 — Handoff to release-watch
13. Record the release window state: version, last tag, commit count, draft notes.
14. Preparation is complete — run the `release-watch` skill next.

## Output format

```
## Release preparation — vX.Y.Z

### Changes since [last-tag]

#### Breaking changes
- [entry] ([PR/commit link])

#### New features
- [entry] ([PR/commit link])

#### Bug fixes
- [entry] ([PR/commit link])

#### Dependency updates
- [entry] ([PR/commit link])

### Files updated
- [file]: version bumped X.Y.Z-1 → X.Y.Z
- [file]: CHANGELOG updated

### Status
- [ ] Release notes drafted
- [ ] Version config updated
- [ ] Commit staged and committed
- [ ] Handed off to release-watch
```

## Safe-Fix Guidance
- Do not create a tag manually — the automated workflow owns tagging.
- Do not push the version bump commit — let the automated workflow pull it.
- If the last tag cannot be determined, stop and ask the engineer.
