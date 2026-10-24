# Release operations

Moved verbatim from `.claude/CLAUDE.md`; the release-window prohibitions stay there.

## Release Operations Policy

### What Claude Code does during a release window

1. **Identify changes** — inspect the commits in the release window (since the last tag).
2. **Prepare release notes** — draft human-readable changelog content.
3. **Update release intent config** — update version references or release config files
   if the project uses them (e.g., `pyproject.toml`, `package.json`, `CHANGELOG.md`).
4. **Observe release workflow state** — monitor the automated release CI pipeline.
5. **Summarize outcome** — report success or failure with links to the release artifact.

### Release coordination

Release observation is handled directly by the main agent via the
`release-watch`/`release-preparation` skills — thin by design, monitoring
and summarizing the automated workflow rather than replacing it.
