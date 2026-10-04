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
Resolve and fetch the actual tracking remote; check base-branch CI, ensure required
dependencies are installed, and ensure configured hooks are active before
implementation without disturbing unrelated state.

Name a ticket branch:

```
<release-or-phase>/<ticket>/<type>/<2-4_word_snake_summary>
```

Keep the summary at 30 characters or fewer. Resolve the release/phase and ticket
from the authoritative tracker or established local context; do not invent them.
Use these established change types and meanings:

| Type | Gitmoji | Meaning |
|---|---|---|
| `feat` | ✨ | Feature |
| `fix` | 🐛 | Bug fix |
| `refactor` | ♻️ | Refactor |
| `test` | ✅ | Tests |
| `docs` | 📝 | Documentation |
| `config` | 🔧 | Configuration |
| `deps` | ⬆️ | Dependencies |
| `remove` | 🗑️ | Removal |
| `lint` | 🚨 | Lint or type repair |
| `security` | 🔒 | Security |

Create the worktree as a sibling of the main checkout and replace `/` in the
branch name with `-` in the worktree directory name.

## Implement and commit

Keep each commit atomic and bisectable. Put one concern and its necessary tests in
the same commit. Use imperative summaries under 72 characters:

```
<emoji> <scope>: <imperative summary>
```

The 72-character limit applies to the imperative summary, not the entire commit
subject.

Do not bypass hooks, security checks, or a genuinely failing product check. Run
focused checks while iterating, then the repository's full relevant build, lint,
type, test, security, privacy, documentation, and link gates before review.
Before pushing, confirm the worktree has no uncommitted changes, the branch includes
the current tracking base, and configured pre-commit hooks pass.
Never force-push a protected, main, or release branch, or during active review.
Force-pushing a feature branch requires explicit engineer permission.

Classify CI as `CI_UNAVAILABLE_EXTERNAL` only when evidence shows an external
infrastructure or account condition prevented the job from running or completing.
Record that cause and run the full local equivalent. A product, test, security, or
quality failure is never `CI_UNAVAILABLE_EXTERNAL` and must be fixed.

## Review and open the pull request

Review the complete diff and commit range yourself. Then obtain a
meaningful independent review from a separate reviewer or isolated review context. Resolve
material findings and rerun affected checks. The implementation author's assertion
does not count as independent evidence.

Keep one concern per pull request and under 500 changed lines when practical. Split
larger work into a reviewable sequence rather than bundling unrelated changes.

Use this exact pull-request title shape:

```
[<ticket-number>] <emoji> <scope>: <imperative summary under 60 chars>
```

The 60-character limit applies to the imperative summary, not the entire title.
Preserve the authoritative ticket identifier's case and the established scope
spelling. Automation can check title structure and length; reviewers must assess
whether its summary actually uses imperative meaning.
Examples:

```
[PROJ-123] ✨ restapi: Add user authentication
[42] 🐛 api: Fix retry leak
```

Use `$evidence-first-briefing` for the body. Lead with resulting behavior or the
changed boundary. Include actual validation, material risks or unknowns, and issue
references. Preserve any required repository pull-request template and fields.
Omit file chronology and unsupported completion claims.

Do not merge until required checks, code-owner approvals, conflict resolution,
branch currency, and blocking review threads satisfy the repository's real gates.
Administrator merge is never a bypass for a genuine product, test, security,
quality, conflict, or substantive review failure. Exactly two narrow exceptions
are authorized below; neither grants blanket administrator authority.
Only the main coordinator may trigger the merge; an independent reviewer reports
findings and evidence back to that coordinator.

### Narrow administrator merge exceptions

Use normal eligible independent review whenever it exists. Before any exception,
verify the current PR head and mergeability, resolve conflicts and blocking
findings, and record the qualifying evidence durably in the PR/tracker. Use
**Create a merge commit**, never squash or rebase-merge for these exceptions.

1. **CI_UNAVAILABLE_EXTERNAL**: independent evidence must prove an external,
   account, or runner condition prevented CI from genuinely running or completing.
   There must be no genuine product/test/security/quality failure. The full local
   equivalent must be green, and the PR must be mergeable and conflict-free.
   Record the external cause and local-equivalent evidence before admin merge.
   A slow or healthy running job, missing observation, or author assertion alone
   does not qualify; continue bounded polling of its authoritative handle.
2. **Owner-only same-identity review deadlock**: a real governance/ownership check
   must prove the PR author is the sole genuinely eligible required reviewer,
   codeowner, or owner, with no alternative eligible independent reviewer/team.
   Freshly verify the owner/admin identity. Self-review and an independent
   adversarial agent review must both be clean. Every genuine required check
   must be green, except a separately qualified CI_UNAVAILABLE_EXTERNAL condition.
   There may be no unresolved blocking findings or conflicts. Record the
   deadlock and justification durably before admin merge. An agent review does
   not replace an available eligible independent repository reviewer.

If either exception lacks its evidence, keep the normal gate pending. Fix a
genuine failed check rather than reclassifying or bypassing it. No other approval,
security, production, release, payment, or destructive-operation authority changes.

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
