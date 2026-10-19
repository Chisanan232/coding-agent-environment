# Live GitHub validation plan for the merge gate (plan only, nothing created)

Nothing here has been run. It needs the owner's authorization for the one external resource
it creates. Until then the gate is `GATE_TESTED_LOCAL` only.

## Fixture

- One private repository owned by a trusted owner, named `zz-owner-merge-fixture`, created and
  later disposed of by the owner (or by an agent only after the owner authorizes that exact repository).
  Default branch `main`, merge commits allowed, auto-merge off, no branch protection, no secrets or variables.
- Contents are synthetic only: a README, `a.txt`, a dummy `uv.lock` with `v1`, and one
  workflow `ci.yml` that runs `test "$(cat state.txt)" = pass` (flip `state.txt` to make
  a genuine failing check). No corporate code, no data from any private repository, no
  tokens, no `.env`, no real names beyond the committer identity.
- Before every push: `gitleaks detect --no-git` on the working tree and a read of
  `git diff --stat` (never a secret value). Commits use a placeholder author address.
- The agent's Actions permissions stay at the default; no PAT, deploy key or secret is created.

## Cases (each PR is opened from a disposable branch of the fixture)

| Case | Setup | Expected |
|---|---|---|
| A | green PR, review record at head | gate PASS; merge by the route the host allows; `verify` VERIFIED |
| B | `state.txt=fail` | REFUSE `CI_FAILED`; nothing merged |
| C | an unresolved review thread, then a `REQUEST_CHANGES` review from the owner | REFUSE |
| D | push after the review record | REFUSE `REVIEW_STALE`; PASS only after a new record |
| E | two PRs editing `uv.lock` | first PASS and merged; second REFUSE until updated and re-reviewed |
| F | Actions disabled for the fixture (a real "CI did not run") | REFUSE without the record and flag; with both, PASS with `checks: 0` and `ci_exception` |
| G | run A's merge as the agent under the live settings | whatever the host answers is reported verbatim; a denial is not rerouted |
| H | after A | main contains the merge commit; ticket updated after `verify`; only task-owned worktree removed |

Control for G: run the same merge once with a message that names the PR and once without,
to record which path the host honors. That result is the evidence for `HOST_AUTHORIZATION_SUPPORTED`.

## Authorizations needed (owner)

1. Create the fixture repository (or create it yourself).
2. Disposition after the run: archive or delete. Deletion needs `gh repo delete`, which the host asks for.
