# Bot PR policy

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

## Bot PR Policy

Dependency bots (Dependabot, Renovate) and pre-commit maintenance bots produce
automated PRs that follow a distinct handling path.

### Standard bot PR handling

If a bot PR meets all of the following:
- CI is green
- No merge conflicts
- No scope expansion beyond the automated update

Then: **verify a genuine formal GitHub APPROVED review by the owner on the current PR
head**. A bot PR is non-owner, so this approval is required; if it is absent, request it
and keep the PR pending. Never submit it as the owner and never treat an agent review as
a GitHub approval. Once owner approval and every other gate pass, **merge automatically**
with a merge commit.

### Bot PR with lock-file conflicts

If a bot PR has lock-file conflicts (e.g., `poetry.lock`, `uv.lock`, `package-lock.json`):

1. Request a rebase using the bot's supported rebase mechanism
   (e.g., comment `@dependabot rebase` or `@renovatebot rebase`).
2. Wait for the bot to rebase and CI to rerun.
3. Re-evaluate the PR against the standard bot PR handling criteria.
4. Require genuine owner approval on the new head, then merge only when it is clean and
   CI is green.

Do not manually resolve lock-file conflicts in bot PRs — let the bot handle it.

### Bot PR with CI failure

If a bot PR has CI failure after rebase:
- Investigate the failure root cause.
- If the failure is unrelated to the update, record it and keep the PR unmerged
  until the required check is green or repaired through its owning work.
- If the failure is caused by the update itself, escalate to the engineer — do not merge.

### Bot PR oversight

The `bot-pr-maintainer` skill and `pr-health-check` skill manage this loop;
the main agent coordinates bot PR state at each polling interval.

