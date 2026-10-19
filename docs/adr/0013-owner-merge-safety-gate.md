# ADR 0013: Owner merge safety gate, separate from host authorization

Status: accepted for the gate (SPE-115). Standing host authorization: **not implemented; blocked by the host**.

## Context

Merges in owner-controlled repositories repeatedly stop at the Claude Code Auto Mode
classifier (`[Merge Without Review]`), so the owner runs every merge by hand. Four
independent layers decide whether a merge happens. They were being discussed as one, which
makes it easy to claim automation that no single layer provides.

| Layer | Question it answers | Who/what decides | Can this repo change it? |
|---|---|---|---|
| GitHub repository permissions | May this identity merge at all? | GitHub (collaborator role, admin) | No |
| GitHub branch protection / rulesets | Which reviews and checks does GitHub enforce? | GitHub, server-side | Only the owner, as a security change |
| Claude Code host authorization | May the agent run this command? | `permissions` rules, then the Auto Mode classifier | Only through the owner's `~/.claude` settings |
| Merge gate (`scripts/owner-merge-gate.py`) | Is this PR, at this exact head, safe by our evidence rules? | The script: PASS / REFUSE / WAIT | Yes |

A gate PASS is **not** a GitHub approval, **not** host authorization and **not** a
repository-permission change. It is engineering evidence for a human or an authorized agent
to act on. Conversely, host authorization never excuses a REFUSE.

## Decision

1. **The gate only verifies.** `check` reads GitHub through an allowlisted, read-only `gh`
   wrapper and prints PASS, REFUSE or WAIT with stable reason codes and, on PASS, the one
   pinned command `gh pr merge N --repo R --merge --match-head-commit <sha>`. It never
   merges, approves, comments, posts a status, edits a branch or enables auto-merge; a
   write verb, `-X`, `--input`, a field override or a GraphQL mutation raises before `gh`
   starts. The wrapper is an exact allowlist: only `repo view`, `pr view`, `api user`, a `repos/` or `orgs/` read and one GraphQL query are accepted, and the only flags are `--json`, `--jq` and `--repo` (plus query variables for that GraphQL read). Attached short flags (`-XPOST`), headers, `--hostname`, `--input` and `--paginate` are refused. The merge is a separate, visible command judged by the repository and the host.
2. **Review evidence is head-pinned.** An independent technical review is recorded as one
   PR comment by the authenticated user: `OWNER-MERGE-REVIEW head=<sha> verdict=PASS
   blocking=0 reviewer=<id>`. A push changes the head and invalidates it
   (`REVIEW_STALE`). The gate reports `formal_github_approval` separately and labels the
   record "not a GitHub approval". A repository that requires a formal review still
   returns `FORMAL_APPROVAL_REQUIRED`.
3. **Fail closed.** Missing, ambiguous or unrecognized evidence refuses: unknown merge
   state, more than 100 review threads, no checks, a CI exception record for another head,
   a non-owner repository, a fork or cross-repository head, a non-default or deployment base.
   A pending check or a mergeability still being computed is WAIT, never PASS.
4. **Unavailable CI is a bounded exception, never a PASS.** It needs the explicit
   `--ci-unavailable` flag, a head-pinned `OWNER-MERGE-CI-UNAVAILABLE ... local_equivalent=PASS`
   record, and zero genuine failures. The verdict carries `checks: 0` and `ci_exception`.
5. **Sequencing without a lock manager.** A PR must not be behind its base
   (`STALE_BRANCH`). After one lockfile PR merges, the next is stale or conflicting until it
   is updated and re-reviewed at its new head.

## Limits of the evidence the gate can see

- **CI means every reported check passed, not that the required set passed.** Personal repositories
  have no branch protection, so GitHub does not tell the gate which checks are required. A
  repository whose workflow never triggered (path filters, a disabled workflow) can show one
  trivial passing check. The gate requires at least one SUCCESS and treats all-skipped or neutral
  as no evidence, but cannot prove completeness. Treat CI coverage as a repository property.
- **The CI-unavailable exception applies only when GitHub reports no checks at all.** When
  billing or quota blocks Actions, runs are often reported as failed or cancelled; the gate keeps
  refusing that as `CI_FAILED`, and queued-forever checks stay WAIT. The owner decides those
  cases. The record's `cause=` is self-attested text and is not verified.
- **Trust anchor.** `--owners` is policy input, but a repository counts only if its owner is the
  authenticated account or an organization that account administers (membership role admin and
  active) and the viewer has ADMIN on the repository. An invented owner string does not widen it.
- **Review records are self-attested.** The same authenticated identity writes the record, so
  independence rests on the reviewer id and the engineering-workflow rules. A marker inside a
  code fence is ignored. With 100 or more comments or reviews the gate refuses
  (`EVIDENCE_TRUNCATED`) because later records could be hidden.
- **Deployment.** The gate cannot detect a repository whose default branch deploys on merge
  (for example a hosting integration on `main`); it guards base-branch names only. A merge that
  triggers a deployment needs its own authorization.
- **Exit codes:** PASS 0, REFUSE 1, WAIT 2, ERROR 3 (a retry loop must not treat ERROR as WAIT).
  `formal_github_approval` is true only for an APPROVED review on the current head.

## What the host documents (read 2026-10-19, code.claude.com auto-mode-config)

- The classifier reads `autoMode` from `~/.claude/settings.json`, managed settings and the
  `--settings` flag. It does **not** read `.claude/settings.json` or `.claude/settings.local.json`.
- `autoMode.allow` entries are exceptions to `soft_deny`; `hard_deny` is never overridable.
  A user may add them (by editing `~/.claude/settings.json`, the `/permissions` Auto mode
  tab, or `/auto-mode-setup`).
- Explicit user intent overrides a remaining soft block only when the user's message
  "directly and specifically describes the exact action"; general requests do not count.
- A denied call can be retried by the user from `/permissions` > Recently denied (`r`).
- The documentation says nothing that lets an agent, a repository file, a script or a
  subagent clear a denial. This decision therefore does not try.

## Limits, stated plainly

- The classifier denied the edit that would have written a standing owner-merge
  authorization into the baseline settings. It is treated as binding. This repository does
  not ship, migrate, template or roll out that exception, and it is not retried through
  another tool, script, partial edit or agent.
- The pre-existing per-PR `Authorized pull request merge` entry is unchanged. Whether the
  classifier honors it for a given merge is observed behavior (ADR 0012, Amendment 1/2).
- Therefore **fully automated merge is not supported by the current host workflow**: the
  owner's own message or action supplies host authorization for each merge, or the owner
  adds a user-level rule themselves. The gate removes the verification work and the
  repeated manual commands, not the host decision.
- The review record is a convention. The same authenticated identity can write it, so its
  independence rests on the reviewer id and the engineering-workflow review rules, and an
  auditor can see the record, the head and the reviewer in the PR.

## Alternatives considered

- **GitHub-native auto-merge / merge queue.** Not viable on the current plans: both orgs
  are on the free plan, the private coordinator repository cannot have branch protection on
  a free plan, there is no merge queue, and `allow_auto_merge` is off. The host's own rule
  blocks `gh pr merge --auto` on an unprotected repository. Branch protection is a
  security-configuration change reserved for the owner; no paid plan is introduced here.
- **A script that performs the merge.** Rejected: it would move the host decision out of
  view and reads as a workaround for a denied action.
