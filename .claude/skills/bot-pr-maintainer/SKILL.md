---
name: bot-pr-maintainer
description: "Handle dependency bot and pre-commit maintenance bot PRs according to the Bot PR Policy in CLAUDE.md: approve and merge clean PRs; trigger rebase and re-evaluate conflicted PRs; escalate only when the update itself causes CI failure."
---

# SKILL.md — bot-pr-maintainer

## Purpose
Handle dependency bot and pre-commit maintenance bot PRs according to the
Bot PR Policy in CLAUDE.md: approve and merge clean PRs; trigger rebase and
re-evaluate conflicted PRs; escalate only when the update itself causes CI failure.

## Type
Auto-used. Run via `pr-health-check` when a PR is classified as
`bot-pr-clean` or `bot-pr-conflict`.

## Do Not Assume
- Do not assume a bot PR is safe just because CI was green at one point — re-check.
- Do not assume a lock-file conflict is resolvable manually — let the bot rebase.
- Do not assume a CI failure is caused by the update — check the failure details.

## Steps

### Path A — Clean bot PR (CI green, no conflict)

1. Confirm the PR author is a recognized bot
   (e.g., `dependabot[bot]`, `renovate[bot]`, `pre-commit-ci[bot]`).
2. Confirm CI is green (all required checks pass).
3. Confirm no merge conflicts.
4. Confirm scope is limited to the automated update (no unexpected file changes).
5. Approve the PR using GitHub MCP.
6. Merge the PR using **Create a merge commit**, as required by
   `engineering-workflow`.
7. Record the merge in the health report.

### Path B — Bot PR with lock-file conflict

1. Confirm the conflict is in lock files only
   (e.g., `poetry.lock`, `uv.lock`, `package-lock.json`, `Pipfile.lock`).
2. Trigger rebase using the bot's supported mechanism:
   - Dependabot: comment `@dependabot rebase`
   - Renovate: comment `@renovatebot rebase`
   - pre-commit.ci: check if auto-rebase is configured; if not, close and wait for
     a new bot PR.
3. Record the rebase request in the health report.
4. Wait for the next polling interval.
5. At the next interval, re-run `pr-health-check` on this PR.
6. If CI is green and conflict is resolved → proceed to Path A.
7. If conflict remains after two rebase attempts → escalate to engineer.

### Path C — Bot PR with CI failure

1. Inspect the CI failure details.
2. Determine whether the failure is caused by the update or by an unrelated issue.
3. **If unrelated to the update**: record the evidence and keep the PR unmerged
   until required CI is green or the failure is repaired through its owning work.
4. **If caused by the update**: do not merge. Escalate to engineer with:
   - PR link
   - Failure step and log excerpt
   - Assessment of what the update broke

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Add only the bot update’s semantic/security/compatibility impact and actual
merge/rebase/blocker evidence to the health report. Reference the PR and failing
run where relevant; omit path labels and duplicate successful checks.

## Safe-Fix Guidance
- Do not resolve lock-file conflicts manually — always let the bot rebase.
- Do not merge a bot PR when CI is red due to the update itself.
- Do not use `--no-verify` or override CI status checks to force a merge.
- Limit to two rebase attempts before escalating to the engineer.
