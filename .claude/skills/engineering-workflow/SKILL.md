---
name: engineering-workflow
description: Apply the shared branch, worktree, commit, validation, review, merge, reconciliation, and cleanup contract for engineering changes.
---

# Engineering workflow

Use this contract for engineering work intended to enter a repository. Repository
instructions supply project commands and product constraints; this skill owns the
shared lifecycle. A later explicit user instruction may authorize a different
choice for that work.

## Start from known state

Inspect the repository, its instructions, the ticket, current Git state, and the
actual base branch before editing. Preserve unfamiliar or uncommitted work. Use an
isolated worktree for ticketed changes when the repository supports worktrees.

Name a ticket branch:

```
<release-or-phase>/<ticket>/<type>/<2-4_word_snake_summary>
```

Keep the summary at 30 characters or fewer. Resolve the release/phase and ticket
from the authoritative tracker or established local context; do not invent them.

## Implement and commit

Keep each commit atomic and bisectable. Put one concern and its necessary tests in
the same commit. Use imperative subjects under 72 characters:

```
<emoji> <scope>: <imperative summary>
```

Do not bypass hooks, security checks, or a genuinely failing product check. Run
focused checks while iterating, then the repository's full relevant build, lint,
type, test, security, privacy, documentation, and link gates before review.

Classify CI as `CI_UNAVAILABLE_EXTERNAL` only when evidence shows an external
infrastructure or account condition prevented the job from running or completing.
Record that cause and run the full local equivalent. A product, test, security, or
quality failure is never `CI_UNAVAILABLE_EXTERNAL` and must be fixed.

## Review and open the pull request

Review the complete diff and commit range yourself. Then obtain a
meaningful independent review from a separate reviewer or isolated review context. Resolve
material findings and rerun affected checks. The implementation author's assertion
does not count as independent evidence.

Use this exact pull-request title shape:

```
[<ticket-number>] <emoji> <scope>: <imperative summary under 60 chars>
```

The 60-character limit applies to the imperative summary, not the entire title.
Preserve the authoritative ticket identifier's case and the established scope
spelling.
Examples:

```
[PROJ-123] ✨ restapi: Add user authentication
[42] 🐛 api: Fix retry leak
```

Use `$evidence-first-briefing` for the body. Lead with resulting behavior or the
changed boundary. Include actual validation, material risks or unknowns, and issue
references. Omit file chronology and unsupported completion claims.

Do not merge until required checks, code-owner approvals, conflict resolution,
branch currency, and blocking review threads satisfy the repository's real gates.
Never use administrator privileges to bypass them.

## Merge, reconcile, and clean up

Use **Create a merge commit**. Do not squash or rebase-merge unless a later explicit
user instruction authorizes that different strategy. Record the resulting merge
commit and verify the merged state and any deployed or published surface the work
changes.

Reconcile the authoritative Jira ticket or established issue tracker with the PR,
merge commit, outcome, validation, and material remaining uncertainty. Transition
it only when its acceptance criteria are actually satisfied.

Remove the owned worktree, local and remote topic branch when appropriate, temporary
artifacts, and owned background processes after merge verification. Never remove
unrelated state or a resource still owned by active work.
