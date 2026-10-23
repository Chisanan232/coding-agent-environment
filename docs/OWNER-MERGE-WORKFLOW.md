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

   - `PASS` (exit 0) prints the pinned merge command.
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

## What this does not do

`PASS` means every check GitHub reported on that head passed (at least one succeeded); it cannot know
which checks a repository should have. It does not approve a PR, satisfy a required formal review, change branch protection, merge
into a release or deployment branch, or authorize deployment or publication (it cannot see whether merging to the default branch deploys).

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
head, not who typed it. For class C the agent must never submit the approval (the host's
Self-Approval rule blocks it). For class B the approval is the owner's personal action: the
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
