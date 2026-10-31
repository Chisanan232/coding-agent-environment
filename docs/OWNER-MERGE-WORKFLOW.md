# Owner-operated merge workflow

Reduces the repeated work around a merge without changing who authorizes it. See
`docs/adr/0013-owner-merge-safety-gate.md` for the layers (GitHub permission, GitHub
protection, host authorization, gate verdict).

## Per pull request

1. The author finishes the PR and records an independent review at the final head. The
   reviewer is a separate context, never the author or coordinator:

   ```bash
   python3 scripts/owner-merge-gate.py evidence-line --head <full sha> --reviewer <reviewer-id>
   # post the printed line as one PR comment with `gh pr comment`
   ```

2. Gate it (read-only):

   ```bash
   python3 scripts/owner-merge-gate.py check --repo OWNER/REPO --pr N --owners <owners>
   ```

   - `PASS` (exit 0) prints the `merge_command` and its `executor`: the owner-merge-runner when it is installed, allowlisted and host-allowed (ALLOW), otherwise the pinned `gh pr merge` (ASK). See `OWNER-MERGE-ENTRYPOINTS.md`.
   - `WAIT` (exit 2): CI pending or mergeability unknown. Run it again.
   - `ERROR` (exit 3): `gh` or the network failed. Fix that first; do not loop.
   - `REFUSE` (exit 1): read the reason codes and fix the cause; never reclassify a failure.

3. Run the printed command. The host decides whether the agent may run it:
   - If it is allowed, the agent runs it and then runs `verify` (below).
   - If the host denies it, the denial stands. Run the same printed command yourself in the
     session with the `!` prefix (`! gh pr merge N --repo R --merge --match-head-commit <sha>`),
     or retry it from `/permissions` > Recently denied. Do not have the agent reroute it.

4. Verify the result, read-only:

   ```bash
   python3 scripts/owner-merge-gate.py verify --repo OWNER/REPO --pr N --head <reviewed sha>
   ```

   `VERIFIED` means the merge commit exists, has the reviewed head as a parent and is on the base.
   Update the ticket only after this, then release only worktrees and branches this task owns.

## Several PRs

```bash
python3 scripts/owner-merge-gate.py queue --repo OWNER/REPO --prs 5,6,7 --owners <owners>
```

prints each verdict in order and one runnable command. After a merge, every other PR is
behind the base: update it, re-review at its new head, and run `check` again. Lockfile PRs
therefore merge strictly one at a time.

## External CI unavailable

Only with a verified billing, quota or outage cause: run the full local equivalent, post
`OWNER-MERGE-CI-UNAVAILABLE head=<sha> cause=<billing|quota|outage> local_equivalent=PASS`,
and pass `--ci-unavailable`. Any genuinely failing check still refuses.

With `cause=billing` only, a failed or cancelled GitHub Actions job that GitHub refused to start for billing is not treated as a
genuine failure. Every failed check must qualify, otherwise the PR is `CI_FAILED` as before:

- It is a completed `CheckRun` of this repository's Actions (`detailsUrl` is `.../actions/runs/<run>/job/<job>`) on the exact head,
  concluded `FAILURE` or `CANCELLED`, confirmed by the check-run API (GitHub Actions app, same head and name) and the job API.
- The job executed zero steps (steps empty or all `skipped`).
- Its check-run annotations contain "recent account payments have failed or your spending limit needs to be increased"; or it is
  `CANCELLED` and its annotation says the strategy was canceled because job `X` failed, where `X` is exactly one billing-annotated,
  zero-step job of the same workflow run. A cancelled sibling of a real failure refuses.
- Anything else refuses: a job that ran a step, a non-Actions check (for example SonarCloud) that is not successful, a missing,
  truncated (100 or more) or unrecognised annotation, any API error or unexpected shape, or a record for another head or another
  cause (the latest record at the head must be `cause=billing`; `quota` and `outage` keep the old behaviour where any failed check
  refuses). Pending checks still `WAIT`, and any non-failed check must be successful.

The waived jobs stay `FAILURE`/`CANCELLED` in `facts.ci_unavailable_checks` (name, conclusion, job id, reason) and `facts.ci_verdict`
says `NOT_A_CI_PASS`: the verdict records that the local-equivalent evidence stands in for jobs that never ran, not that CI passed.
If the waiver is declined, `facts.ci_unavailable_refused` lists why. The record and flag are supplied by the authenticated
identity, so the gate bounds what the record can cover; it does not independently verify the billing cause beyond GitHub's own annotation.

## Repository with no hosted CI (founder-approved per PR)

A repository that defines no CI workflow at all (its `.github/workflows` directory does not exist) can never report a check, so
`NO_CI_EVIDENCE` would block every PR. When the founder has approved the exception for a named PR, post, as the authenticated
user and in a comment that also states the actual local validation and review evidence (commands, revision, exit status, test
counts, review verdict, what was not run),
`OWNER-MERGE-CI-EXCEPTION head=<sha> basis=no-hosted-ci local_equivalent=PASS` (`owner-merge-gate.py ci-exception-line --head SHA`
prints it) and pass `--ci-exception`. The gate accepts it only for that exact head, with zero checks on the head, when the
workflows directory is absent at both the base branch and the reviewed head (a 404 in gh's own error text; a successful answer, empty
output, a rate limit, a 5xx or anything else counts as "has workflows"), the PR changes no file under `.github/workflows/` (an unknown or
truncated file list refuses), and with a clean review record. It speaks for GitHub Actions only: an external CI app that has not
reported yet looks the same as no CI. The record and the flag are both supplied by the authenticated identity, so the gate cannot
verify the founder's per-PR approval; it bounds what the record can cover, it does not make it independent. Checks that
ran and failed or are pending still refuse or wait. It records that local evidence stands in for CI; it is not a CI pass and is
never reported as one. It does not replace `--ci-unavailable`, which is for a verified external outage of CI that exists.

## Version integration branches (Development-Ahead / Sequential Release)

`main` is the protected cumulative release baseline. A founder-approved version integration branch (for example
`integration/mvp-5.0`, or `mvp-4.0/HORO-2046/integration`) may receive eligible feature PRs before that version ships; future-version
work never enters `main` early. The gate recognises such a base only when it is listed in an explicit policy file:

```json
{"schema": 1, "repos": {"Owner/Repo": {"branches": [{"branch": "integration/mvp-5.0", "version": "5.0"}],
                                       "future_head_prefixes": ["mvp-6.0/"]}}}
```

- Path: `--version-policy FILE`, else `~/.config/coding-agent-environment/local/version-branches.json` when it exists. It is local, owner-governed,
  a regular file owned by the caller and not group/world-writable. It lives outside every product repository, so a PR cannot authorize its own
  base. No file means the previous behaviour (every non-default base is refused).
- Naming is never inferred; only listed branches count, per repository. A `release`/`prod`/`deploy`/`gh-pages` branch cannot be listed (the
  policy refuses to load).
- Listing a base changes one thing: `BASE_NOT_DEFAULT_OR_DEPLOY` no longer applies to it. Exact-head independent review, CI, threads,
  conflicts, staleness, class and merge-commit rules are evaluated exactly as for `main`.
- A PR into the default branch whose head is a listed integration branch (or starts with a `future_head_prefixes` entry) is refused with
  `FUTURE_VERSION_INTO_MAIN`. Promoting an integration branch to `main` is a release step with its own founder GO; this gate never performs it.
- Known limit: the `FUTURE_VERSION_INTO_MAIN` check is by head name (case-insensitive), not commit ancestry. A differently named branch cut from an
  integration branch is not detected by this gate; the exact-head independent review must check that no future-version commits are included.
- A path segment that starts with an environment word cannot be listed: `staged`/`stage`, `staging`, `stg`, `beta`, `alpha`, `canary`, `hotfix`, `uat`, `preprod`, `qa`, `live` as a bare prefix (so `staging-eu`, `stagingeu`, `betafix`, `stg2`, `qa2` and `livefix` too), and any of those words (plus `prd`, `prod`, `production`, `release`, `deploy`, `sandbox`) as a whole `-`/`/`/`.`/`_`-separated token anywhere in the name (`mvp-staging`, `x-beta`, `mvp-qa`). A future-head prefix is a literal `startswith` (`mvp-6.0/` does not cover a head named exactly `mvp-6.0`; list that branch under `branches`), and a future head is refused only into the default branch, not into another listed version line. This is a deliberate fail-closed over-match: rename the branch instead of listing it. A name that is not a valid git ref (an empty, dot-leading, dot-trailing or `.lock` component, `..`) cannot be listed either. Base matching is case-exact; head (future-version) matching is case-insensitive. An invalid entry makes the whole policy fail to load
  (exit 3, nothing is approved), never a partial approval.
- Loader limits (SPE-133): the file is read without blocking (a FIFO or device is refused), at most 64 KiB, with `schema` exactly integer `1`, a regular file rather than a symlink, no duplicate JSON keys, `future_head_prefixes` a list of non-empty refs, and no `//` in any ref. Directories above the owner-config root are not permission-checked.
- Adding or removing an entry is a founder decision recorded outside the PR it unblocks.

## Independent AI review (founder correction 2026-10-31)

A second human reviewer is not required for the founder's owner-controlled repositories. The review before an agent-performed merge may come from another AI agent or session, as long as it is genuinely independent of the author and the coordinator.

- Runner evidence uses the stable allowlisted reviewer id `claude-independent-agent` (or `codex-independent` for a Codex session) with kind `independent-agent`. The id names a reviewer type, not a person and not a particular run.
- Provenance stays in the review comment: the actual agent, session or run identifier, the exact reviewed SHA, the findings and the evidence. If a later commit lands after the review, say what changed and whether it was re-reviewed. Never relabel a self-review as independent and never invent an identity.
- The author, or the coordinator that wrote a fix, is not the reviewer of that fix. A reviewed fix still needs a review of its own head.
- The runner reads `~/.claude/owner-merge-runner-allowlist.json`: a repository needs an entry with its real `required_checks` and `reviewers` before the runner will merge it, and its base branch needs server-side required checks that cover them (`protection_capability: private-free-app-level` only for a private repository on a plan without branch protection; that mode is app-level enforcement, not server-side protection). Editing that file is an owner-authorized step, so a session reports the repository name instead of editing it.
- Pull requests by anyone other than the owner (class B and C above) still need the owner's genuine GitHub approval on the exact head. Agent review does not replace it.
- Publication, tagging and deployment are not covered by any of this and keep their own approval.

## What this does not do

`PASS` means every check GitHub reported on that head passed (at least one succeeded); it cannot know
which checks a repository should have. It does not approve a PR, satisfy a required formal review, change branch protection, merge
into a release or deployment branch (or promote an integration branch to the default branch), or authorize deployment or publication (it cannot see whether merging to the default branch deploys).

## Merge decision: ALLOW / ASK / DENY (SPE-118)

`check` also prints a `decision` that combines five separate dimensions (owner class, engineering gate, GitHub approval requirement, GitHub merge
eligibility, host tool permission). With `--settings <settings.json>` it reads only the permission rules to judge the exact pinned command; without it the host
dimension is `not_evaluated` and a passing PR is ASK, never ALLOW. A missing approval, stale evidence or a host prompt is ASK with a named resolver; a failed
check, a cross-repository head, a closed PR, repository rules or a host deny rule is DENY. The gate verdict (PASS/WAIT/REFUSE) and its exit code are unchanged.
The operator procedure, exact rules, apply and rollback commands and the test list are in [OWNER-MERGE-OPERATOR-GUIDE.md](OWNER-MERGE-OPERATOR-GUIDE.md).

## Pull request classes (SPE-116)

`check` reports `class` in its output. The class decides who must approve; the gate never
changes it and fails closed when it cannot verify the author.

| Class | Author | Extra requirement before PASS | Gate prints |
|---|---|---|---|
| A | The authenticated owner identity | Repository admin, or a verified code owner with write permission for every changed file (CODEOWNERS read from the default branch; teams and unsupported syntax never count; `dir/*` covers direct children only; a PR editing CODEOWNERS, renaming files or changing 30+ files never qualifies; only a 404 falls through to another CODEOWNERS file) | the pinned merge command |
| B | A verified dependency bot (`dependabot`, `renovate`: bot flag, bot branch, bot-only commits, dependency manifests only) | A real GitHub APPROVE by the owner identity itself (not any other reviewer or bot) on the exact head | `WAIT` with `approve_command` until the owner (never the agent) approves, then the pinned merge command |
| C | Anyone else, including a failed bot check | A genuine GitHub APPROVED review by the owner on the exact head; the agent's comment never substitutes; `--admin` is never printed | the pinned merge command |
| unverified | Missing, deleted or ghost author | Refused: request owner review | nothing |

For B and C the owner identity must be the repository owner account or an active org
administrator; write access alone is not enough.

The review record's `reviewer` id is self-attested by the same identity that posts it; the gate rejects only obviously non-independent ids, so "independent" means the process was followed, not that GitHub proved it.

Limit: the gate runs under the owner's own GitHub token, so GitHub shows an agent-submitted
approval and a personal one identically. The gate can prove an approval exists for the exact
head, not who typed it. For class C the agent must never submit the approval; that is
process-only too, as the host's Self-Approval rule covers only agent-authored PRs. For class B the approval is the owner's personal action: the
agent must not run `approve_command`. That separation is process-only; neither the gate nor
the host enforces it, so an agent-submitted APPROVE would pass the gate and is a policy
violation, not a control.

Bot PRs merge one at a time: `queue` marks only the first PASS runnable. After each merge,
refresh the base, update the next branch with a merge from the base (no history rewrite), let
the bot or the owner resolve lockfile conflicts, and run `check` again; any new head needs a
new review record and a new approval.

If the host denies a merge, record it and stop:

```bash
python3 scripts/owner-merge-gate.py host-denied --repo OWNER/REPO --pr N --head <sha> --message "<host text>"
```

It prints `HARNESS_PERMISSION_BLOCK`, offers no alternate flag or tool, and exits 1.
