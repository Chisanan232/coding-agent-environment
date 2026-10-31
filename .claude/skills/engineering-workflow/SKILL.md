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

All repository code changes go through pull requests; never push directly to the
default or protected branch (main/master); merge only via PR.
Inspect the repository, its instructions, the ticket, current Git state, and the
actual base branch before editing. Preserve unfamiliar or uncommitted work. Use an
isolated worktree for ticketed changes when the repository supports worktrees.
Resolve and fetch the actual tracking remote; check base-branch CI, ensure required
dependencies are installed, and ensure configured hooks are active before
implementation without disturbing unrelated state.

## GitHub tool selection

Use the authenticated `gh` CLI as the GitHub interface (`gh pr`, `gh issue`, `gh run`, `gh release`, and
`gh api` for anything those do not cover) together with local `git`, for inspection, PRs, reviews, CI,
releases, branch sync, approved merges and post-merge verification. The GitHub MCP server is retired on this
workstation: do not reconnect, rebuild or repair it, and do not call `mcp__github__*` tools. Verify the account and
repository (`gh auth status`) before a mutation, never print or copy a token, and never switch to another account.
Using `gh` first is not permission to bypass host authorization, the PR class A/B/C rules, required checks,
reviews or the independent merge gate. A repository-protection, ruleset or required-review refusal from GitHub
itself is a gate, not an integration error. A host permission or classifier denial of a GitHub action is binding
(`HARNESS_PERMISSION_BLOCK`): do not wrap, split or reroute it, never retry it through another interface, and
report the exact operator command. Never approve a PR with the account that authored it.

Name a ticket branch with a change-type segment:

```
<version-or-phase>/<ticket-id>/<type>/<short_summary>
```

`<type>` is one of `feat`, `fix`, `refactor`, `test`, `docs`, `config`, `deps`, `remove`, `lint`. A repository whose
established branch style differs keeps it (product-specific override). Keep the summary at 2-4 snake_case words and 30 characters or fewer. Resolve the
version/phase and ticket from the authoritative tracker or established local
context; do not invent them.

Create the worktree as a sibling of the main checkout. Derive its directory name
from the branch by replacing `/` with `-`: `<repo>-<phase>-<ticket>-<type>-<summary>`.

## Implement and commit

Keep each commit atomic and bisectable: one logically small change per commit,
one new test per commit, and utilities and mocks land in earlier commits than the
tests that use them. An implementation and its tests are separate commits when each is
independently meaningful; they share one commit only when together they form one inseparable
correctness change. Use imperative summaries under 72 characters:

```
<GitEmoji> (<scope>): <imperative summary>
```

Parentheses around the scope are required. Example:
`✨ (model): Add property to TestModel`.

The 72-character limit applies to the imperative summary, not the entire commit
subject. Choose the GitEmoji by the primary change:

| GitEmoji | Meaning |
|---|---|
| ✨ | Feature |
| 🐛 | Bug fix |
| ♻️ | Refactor |
| ✅ | Tests |
| 📝 | Documentation |
| 🔧 | Configuration |
| ⬆️ | Dependencies |
| 🗑️ | Removal |
| 🚨 | Lint or type repair |
| 🔒 | Security |

Commit granularity and validation frequency are separate: a commit is never a reason to run a test.
Batch related atomic commits into a validation checkpoint, run the narrowest meaningful affected
checks first, reuse valid incremental build artifacts, and repeat validation only for new changes,
failures, invalidated evidence or unresolved risk. Record the exact tested revision and never claim
that untested intermediate commits were tested.

Do not bypass hooks, security checks, or a genuinely failing product check. Run
focused checks while iterating, then the repository's full relevant build, lint,
type, test, security, privacy, documentation, and link gates before review.
Before pushing, confirm the worktree has no uncommitted changes, the branch includes
the current tracking base, and configured pre-commit hooks pass.
Never force-push a protected, main, or release branch, or during active review.
Force-pushing a feature branch requires explicit engineer permission.

Public repositories require green CI before merge, except a qualified
`CI_UNAVAILABLE_EXTERNAL` exception below. Private repositories require the full
local suite to pass instead.

Classify CI as `CI_UNAVAILABLE_EXTERNAL` only when evidence shows an external
infrastructure or account condition prevented the job from running or completing.
Record that cause and run the full local equivalent. A product, test, security, or
quality failure is never `CI_UNAVAILABLE_EXTERNAL` and must be fixed.

## Delegation

Use at most seven concurrent subagents (dynamic 0-7), with one owner per scope.
Seven is a ceiling, never a target; choose the count from dependencies, cost,
expected speedup, mutable-state overlap, and integration overhead.

## Review and open the pull request

Review the complete diff and commit range yourself. Then obtain a
meaningful independent review from a separate reviewer or isolated review context. Address or
explicitly disposition every reviewer comment: fix all Critical and High findings, material Medium
issues (unless an owner-approved exception applies) and cheap, meaningful Low findings; track other
Lows with a specific rationale. Rerun affected checks, and do not loop on review without bound. The implementation author's assertion
does not count as independent evidence.

Keep one concern per pull request and under 500 changed lines when practical. Split
larger work into a reviewable sequence rather than bundling unrelated changes.

Use this exact pull-request title shape:

```
[<TICKET>] <GitEmoji> (<scope>): <imperative summary under 60 chars>
```

The 60-character limit applies to the imperative summary, not the entire title.
Preserve the authoritative ticket identifier's case and the established scope
spelling. Automation can check title structure and length; reviewers must assess
whether its summary actually uses imperative meaning.
Examples:

```
[PROJ-123] ✨ (restapi): Add user authentication
[42] 🐛 (api): Fix retry leak
```

Use `$evidence-first-briefing` for the body. Lead with resulting behavior or the
changed boundary. Include actual validation, material risks or unknowns, and issue
references. Use the repository's `.github/PULL_REQUEST_TEMPLATE.md` when present.
Preserve any required repository pull-request template and fields otherwise.
Omit file chronology and unsupported completion claims.

**PR classes and formal approval** (canonical; other files reference this, and
`docs/OWNER-MERGE-WORKFLOW.md` has the gate table). The class is decided by
`scripts/owner-merge-gate.py` from verified author identity, never from a PR claim:

- **Owner-authored (Class A)**: no formal GitHub review approval is required merely because
  the PR is owner-authored; no artificial self-approval and no second GitHub identity. An
  empty review list or `reviewDecision` NONE is not a blocker. The PR merges automatically
  once checks, independent engineering review, security and host gates pass.
- **Non-owner and bot-authored (Class B/C)**: a genuine formal `APPROVED` review by the
  authorized owner on the **current** head is required. Dependabot, Renovate and other bots
  are non-owner even with write access. The agent never submits, simulates or substitutes
  that approval; it requests it and continues automatically once it exists.
- Independent agent engineering review, GitHub review approval, repository merge
  eligibility and host authorization are four separate things; none stands in for another.
  GitHub's own required-review rules (branch protection, rulesets, CODEOWNERS) still
  apply to every class: a repository that genuinely requires a formal review
  (`REVIEW_REQUIRED`) blocks, and the classes above never waive it; only narrow
  exception 2 below addresses an owner-only same-identity deadlock.
- A merge decision is **ALLOW / ASK / DENY**, judged per dimension (owner class, engineering gate, GitHub approval
  requirement, GitHub merge eligibility, host tool permission); `owner-merge-gate.py check --settings` prints it. Anything a
  human, the author or the agent can resolve is ASK with a named resolver (a host prompt, a genuine owner review, a
  revalidation), never an unrecoverable failure; DENY is for non-waivable constraints such as a failed check or a
  cross-repository head. An approved ASK covers exactly the one pinned operation; the agent never simulates the answer.
  Details: `docs/OWNER-MERGE-OPERATOR-GUIDE.md`.

Do not merge until required checks, CODEOWNERS-required approvals, the class approvals above,
conflict resolution, branch currency, and blocking review threads satisfy the
repository's real gates.
Administrator merge is never a bypass for a genuine product, test, security,
quality, conflict, or substantive review failure. Exactly two narrow exceptions
are authorized below; neither grants blanket administrator authority.
Only the main coordinator may trigger the merge; an independent reviewer reports
findings and evidence back to that coordinator.
Check a merge before and after with `scripts/owner-merge-gate.py` (read-only PASS/REFUSE/WAIT at the
exact head; see `docs/OWNER-MERGE-WORKFLOW.md`). Its PASS is evidence, not a GitHub
approval and not host authorization; a host denial of the merge stands and is reported.

**Canonical merge executor** (interface independence; `docs/OWNER-MERGE-ENTRYPOINTS.md`). The agent never picks a merge
spelling. `owner-merge-gate.py check --settings <user settings>` prints one `merge_command` and its `executor`:
`owner-merge-runner` when the runner is installed, the repo is in the owner's runner allowlist and the host rule allows exactly that
command (outcome ALLOW; run it as printed after writing the review-evidence file the gate names, and only after an independent review
PASS at that exact head), otherwise the pinned `gh pr merge` (outcome ASK; the host prompts and a human answers). The runner re-validates
class, head, checks and repository limits itself; its refusal, like any host or GitHub denial, is final: do not retry through `gh`, `rtk gh`,
the GitHub MCP merge tool, the API or a wrapper. Non-owner and bot PRs still need the genuine owner approval first.

The merge is its own tool call: never combine it with a review comment, CI polling, a readback (`gh pr view`, `gh api`), a pipe, `head`/`tail`, `&&`, `;` or any other chaining. The order is: independent review, check the exact PR head, verify required CI, run `owner-merge-gate.py check`, write the review evidence at the printed `evidence_path` (its own call; only by an authorized path, never self-approved), run the printed `merge_command` alone, then verify the merged state with `owner-merge-gate.py verify` in a separate call. A compound shell call matches no merge rule, so the host asks about the whole thing; that is a workflow error, not a runner failure.

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
2. **Owner-only same-identity review deadlock (owner-admin merge)**: the PR author
   is a repository/org owner with effective admin-bypass rights, freshly verified
   by an actual ownership check (`orgs/<org>/memberships/<login>` role admin, or the
   repository permission for a personal repository), never inferred from admin-capable
   API access alone. A separate human GitHub Approve is not required, and zero GitHub
   reviews is not by itself a blocker. Complete self-review and an applicable
   independent risk-based review (an adversarial agent review at minimum) must both
   be clean at the current head. Every genuine required check and security gate must
   be green, except a separately qualified CI_UNAVAILABLE_EXTERNAL condition. There
   may be no unresolved blocking findings, `REQUEST_CHANGES`, or conflicts. Pin the
   exact head (`--match-head-commit <full sha>`) and record the justification
   durably before admin merge. A reviewer's outstanding `REQUEST_CHANGES` still
   blocks. A host or tool refusal of the merge is `HARNESS_PERMISSION_BLOCK`: report
   it and never route around it.

If either exception lacks its evidence, keep the normal gate pending. Fix a
genuine failed check rather than reclassifying or bypassing it. No other approval,
security, production, release, payment, or destructive-operation authority changes.

## Campaign autonomy

A campaign is the approved scope of related tickets. The campaign, not one task or
one prompt, is the unit of completion: `TASK_DONE` is not `CAMPAIGN_DONE`,
`PR_MERGED` is not `INDEPENDENT_QA_SIGNED_OFF`, and `INDEPENDENT_QA_SIGNED_OFF` is not
`RELEASED`. Never invent new mandatory release scope.

- **Task-scoped blockers.** Classify each task independently as `READY`, `RUNNING`,
  `WAITING_RESOURCE`, `WAITING_DEPENDENCY`, `WAITING_OPERATOR`,
  `VERIFICATION_PENDING`, `DONE` or `OUT_OF_SCOPE`. One blocked task never freezes
  the campaign: run the next safe eligible task, and stop a worker only when it has
  no authorized executable work left. A worker checkpoint is not campaign completion.
- **Fresh evidence.** A cached checkpoint or historical ticket status is not proof.
  Before declaring `WAITING_RESOURCE`, measure the current physical volume and read
  the canonical admission state; an admission snapshot older than its freshness
  window (120 seconds) is stale and never a current decision. Before declaring
  `WAITING_DEPENDENCY`, recheck the exact merged PR and source state and recompute
  the dependency graph.
- **Automatic reconciliation.** When a dependency merges, or its verification evidence is complete and recorded: revalidate the
  exact merged SHA, update the acceptance matrix, mark newly unblocked work `READY`,
  acquire resources, run the applicable tests, record real results and defects, and
  continue to the next real blocker. Ordinary accepted work needs no repeated
  "continue" confirmation.
- **Resources.** Honor the admission contract: at most the authoritative number of
  heavy lanes, a valid process-bound byte reservation before heavy work, no second
  heavy build while the lane belongs to another owner, and reuse of compatible
  build targets. After every task persist evidence, stop only exclusively owned
  child processes, release task-owned reservations, and reclaim only disposable
  resources through the authorized storage policy. Preserve active or resumable
  peer resources. Never change host policy or bypass a refusal to go faster.
- **Escalation and ledger.** Routine engineering is not escalated. This list adds to,
  and never narrows, gates defined elsewhere (force-push, publication, production,
  IAM, payment, CI-definition changes and explicit-confirmation actions). Escalate only a
  host refusal, sensitive real-home access, credential or privileged settings
  change, unapproved spend, unauthorized destructive action, a material scope or
  release decision, or required personal founder acceptance, with exact action,
  resource, SHA, risk, scope and operator path. Record each decision and operator
  action durably and do not re-ask a decided issue without new material evidence.
- **Resumption.** A passive checkpoint is not automatic resumption. Persist the
  campaign state, dependency graph, owners, exact SHAs and pending events, release
  idle resources, and register a resume trigger only with a coordinator that is
  actually installed. Valid resume events are resource admission available, owning
  PR merged, required CI or security result changed, ticket dependency completed,
  operator approval completed, and a new QA artifact. Never poll an unchanged state
  with model calls (bounded polling of an authoritative handle runs in a non-model
  watcher that wakes the agent on change), and never claim a closed session can wake itself. Without a real
  scheduler, say so and hand the missing capability to the environment owner.
- **Verdict.** `READY_FOR_RELEASE` requires every required in-scope engineering,
  integration, QA, security, dogfooding and cleanup gate to be satisfied by live
  evidence, not installation alone. If only a founder-only gate remains, report
  `RELEASE_BLOCKED_BY_OPERATOR`. Never tag, publish, deploy to production or spend
  unapproved funds on the basis of campaign state; each remains gated by its own
  explicit authorization, including when every engineering gate is satisfied.

## Resource admission and concurrency

Before heavy work (workspace or full builds, Cargo workspace builds, Docker suites, native QA, full E2E, large
parallel fan-out) assess the host; disk admission alone is not enough. Run
`scripts/memory-admission.py check --weight heavy` from the coding-agent-environment checkout (it is not installed into
`~/.claude`), then reserve through `scripts/disk-admission.py run`. The
memory check reads the kernel pressure level and trend, swap I/O over a measured window, the compressor's share of
RAM, load per core and other running heavy processes (free disk only to judge whether swap can grow), composes the
existing SPE-109 disk gate with its reservations and lane caps, and keeps heavy work refused until consecutive good
checks (hysteresis). A single swap-used figure is history, not proof of current exhaustion. Thresholds are proposed in
`docs/RESOURCE-ADMISSION.md` in the coding-agent-environment repository; do not change the disk gate's thresholds.

Run one admitted heavy lane today (a shadow budget is measured beside it; the cap is raised only after measurement, ADR 0017). Do not start competing Cargo workspace builds, Docker suites, native QA or full
E2E, and lower subagent fan-out while pressure is not normal; prefer reusable build artifacts and isolated targets
within the resource budget. Never kill another active session or delete its worktree, runtime, cache, container,
reservation or scratch. Clean only regenerable caches and scratch proven idle and owned (owner, process lifetime, open
handles, age, reproducibility). Age alone never justifies deletion; ask-only resources stay ask-only; never delete
unknown or active work to reach an admission threshold. Aim for at least 100 GiB free.

## Post-reboot recovery

After a reboot or crash, treat every earlier process, lock, reservation, background-task and job observation as stale.
Before resuming, reconcile GitHub (`gh pr list`, `gh run list`) and Jira with the persisted checkpoints, revalidate
process and reservation identities (pid and start time, not names), and read each earlier test job's durable result:
do not assume it survived or completed, and do not rerun finished work. Preserve completed QA evidence, resume only
incomplete tasks, and record exact blockers and resume triggers.

## Secret-safe testing

An existing credential may be consumed by its intended SDK or provider runtime when
the task is authorized. An agent must never inspect, print, copy, export, serialize,
hash for reporting, or reveal an API key or other secret value.

- Do not read credential-bearing shell startup files or configuration files (the
  prohibition does not depend on intent). Check presence only, with a test whose
  output cannot contain the value (a boolean, an exit code or a length).
- Pass a secret to a process through stdin or a pipe, never as a command-line
  argument. Do not use a tool or MCP call whose result is the secret value; use a
  path that keeps the value inside a pipe between processes.
- Do not use environment dumps, process command lines (`ps`, `pgrep -f`), shell
  tracing or verbose authenticated HTTP logs; they leak values by construction.
- Keep credential handling inside the approved runtime or provider boundary. Never
  copy a value into the repository, a fixture, a commit, a ticket, a log or
  generated documentation.
- If a value is exposed anyway, stop using it, disclose the exposure plainly and
  recommend rotation.

## Jira lifecycle

When Jira is the tracker: move the ticket to In Progress when work really starts,
not earlier; post a real start comment describing what is starting; set Start Date
to the actual date, set an estimated Due Date, and never fabricate dates. An Epic
or Story cascades In Progress only for items that truly started, and you close a
ticket only when every acceptance criterion passes, with evidence.

Frontend changes also need the frontend QA evidence defined in `acceptance-validation`.

## Merge, reconcile, and clean up

Use **Create a merge commit**. Do not squash or rebase-merge unless a later explicit
user instruction authorizes that different strategy. Record the resulting merge
commit and verify the merged state and any deployed or published surface the work
changes.

Reconcile the authoritative Jira ticket or established issue tracker with the PR,
merge commit, outcome, validation, and material remaining uncertainty. Transition
it only when its acceptance criteria are actually satisfied.

Remove the owned worktree, local and remote topic branch when appropriate, temporary
artifacts, and owned background processes after merge verification, but only when the
worktree is inactive, clean and fully reconciled; never run a broad `git worktree prune` or other
automatic destructive cleanup. Never remove
unrelated state or a resource still owned by active work.
