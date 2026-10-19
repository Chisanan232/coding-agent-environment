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
