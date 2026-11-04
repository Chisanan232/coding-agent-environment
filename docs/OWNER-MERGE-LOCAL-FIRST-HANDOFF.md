# Owner merge: local-first CI hand-off for the NDA repository (SPE-135)

Target: a private pull request whose head commit ends in `[skip actions]` (NDA PR 181), so GitHub shows no checks. The rules and
threat notes are in `docs/OWNER-MERGE-WORKFLOW.md`; this is only the order of steps and who does them.

| # | Who | Step |
|---|---|---|
| 1 | Agent | Install `~/.config/coding-agent-environment/local/local-first-ci.json` (directory and file mode 0700/0600, schema 1) from content the owner approved, with an entry for the repository: `checks` (name + argv, in order), `max_age_hours`, `pinned_paths`, `not_covered`. Preserve unrelated entries and validate with the gate's `load_local_first` before use. If an enforced host restriction blocks the write, request approval for that exact action through the supported mechanism. |
| 2 | Agent | Commit the change with a skip directive (`[skip actions]`) and push the branch, then confirm GitHub shows no Actions run for the head. |
| 3 | Agent | Run `scripts/owner-merge-local-ci.py record --repo OWNER/REPO --pr N --base BRANCH --worktree PATH --expect-head SHA` through the heavy-lane wrapper. It needs a clean worktree and prints one `OWNER-MERGE-LOCAL-CI` line. |
| 4 | Agent | Post that line as a PR comment, unedited and unfenced. |
| 5 | Independent reviewer | Review the exact head; the agent posts `owner-merge-gate.py evidence-line ...` as the `OWNER-MERGE-REVIEW` comment. |
| 6 | Agent | `owner-merge-gate.py check --repo OWNER/REPO --pr N --owners ... --ci-exception --settings ...`. Expect `ci_exception=local-first`, `hosted_checks=NOT_RUN`, `NOT_A_CI_PASS`. Any other outcome is the answer; do not work around it. |
| 7 | Agent | Run the printed pinned `gh pr merge` alone; the host prompt, if shown, is the supported approval mechanism. The runner is not used for this repository. |
| 8 | Agent | `owner-merge-gate.py verify --repo OWNER/REPO --pr N --head SHA`. |
| 9 | Agent | Retry the product gate that was waiting on the merge. |

Not covered by a local run is listed in `not_covered` and in the check output; it is not hidden. A new push invalidates the record,
the review and the skip-directive check, so repeat steps 2 to 6.

Local CI never replaces real end-to-end or realistic user-workflow acceptance: the product's own acceptance criteria stay open until agents execute them and record truthful evidence. Final release approval remains the founder's.
