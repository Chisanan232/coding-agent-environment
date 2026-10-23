# Owner merge: ALLOW / ASK / DENY operator guide (SPE-118)

Status: source and candidate are in this repository; **nothing here has been applied to the live host**. Every live behavior below marked
"expected" is unobserved until you run the matching test in section 7 and record the result.

## 1. The model

Five dimensions are judged separately. A PASS in one is never a PASS in all.

| Dimension | Answers | Source |
|---|---|---|
| `PR_OWNER_CLASS` | A owner-authored, B verified dependency bot, C anyone else | `owner-merge-gate.py` (verified identity, never a PR claim) |
| `ENGINEERING_GATE` | independent review at this head, CI, review threads | gate |
| `GITHUB_APPROVAL_REQUIREMENT` | class A: none. B/C: a genuine owner `APPROVED` review on the current head | gate |
| `GITHUB_MERGE_ELIGIBILITY` | open, not draft, same repository, no conflicts, not behind, repository rules | gate and GitHub |
| `HOST_TOOL_PERMISSION` | what your `permissions` rules say about the exact pinned `gh pr merge` | `check --settings` |

| Outcome | Meaning | Example |
|---|---|---|
| **ALLOW** | every dimension passes and a host rule allows the exact pinned merge | owner PR, green CI, review recorded, host allow rule present |
| **ASK** | not allowed yet, and someone can resolve it | host prompt needed; non-owner PR without owner review; stale head to revalidate; missing review evidence |
| **DENY** | a non-waivable constraint | failed CI; fork or cross-repository head; closed PR; GitHub repository rules; a host deny rule; a gate error |

`ASK` names who resolves it (`resolver`): `host_prompt`, `owner_github_approval`, `agent_runs_independent_review`, `agent_revalidates`,
`author_fixes_findings`, `author_addresses_review`, `author_fixes_ci`, `author_resolves_threads`, `author_resolves_conflicts`, `author_marks_ready`, `founder_risk_decision`,
`founder_authorizes_target`, `owner_review`. An ASK never simulates the answer: the agent does not submit a GitHub approval or a prompt
approval, and an approved prompt covers exactly the one printed command at its pinned head. GitHub's rejection stays binding.

`CI_FAILED` is DENY because a genuinely failed check is not waivable; remediate it. `NO_CI_EVIDENCE` (no completed checks and no qualified
`CI_UNAVAILABLE_EXTERNAL` record) is a founder risk decision; a qualified record clears it. A gate `ERROR` prints a DENY decision (exit 3).
`UNAUTHORIZED_REPO` and `NOT_CANONICAL_REPO` are DENY: a trust-boundary failure is never an ASK.

Print the decision for a PR (reads GitHub and the permission rules of the settings file; prints no rule text or env values):

```bash
python3 scripts/owner-merge-gate.py check --repo OWNER/REPO --pr N --owners OWNER --settings ~/.claude/settings.json
python3 scripts/owner-merge-gate.py queue --repo OWNER/REPO --prs 5,6 --owners OWNER --settings ~/.claude/settings.json   # decision per PR
```

Pass `--settings` once per layer you have (user, project `.claude/settings.json`, `.claude/settings.local.json`, managed): the rules are pooled and deny beats
ask beats allow. Only the files you pass are read, so ALLOW means "allowed by these files", not "allowed by the host"; the Auto Mode classifier is a further,
unmodelled layer. An unparseable or non-string rule, or a bare `Bash` in ask, fails closed to ASK. Without `--settings` the host dimension is `not_evaluated`
and a passing PR is reported ASK, never ALLOW. Exit codes keep their meaning: 0 = gate PASS (run the pinned command; for ASK a host prompt will appear and you
answer it), 1 = REFUSE, or PASS with a host DENY (no `merge_command` is printed), 2 = WAIT, 3 = ERROR. Read `decision.outcome`, not the exit code, to tell ALLOW from ASK.

Evidence independence is unchanged and still open: the review record is written by the same authenticated identity that runs `check`, so ALLOW depends on the
process being followed (`OWNER-MERGE-AUTONOMY.md` section 8, evidence-independence decision), not on GitHub proving a second reviewer.

## 2. Paths

| What | Path |
|---|---|
| Live user settings (the only file the candidate changes) | `~/.claude/settings.json` |
| Backups written by `apply` | `~/.claude/backups/settings.<first 12 hex of live sha>.json` (mode 0600) |
| Candidate | `docs/examples/candidate-settings-merge-scope.example.json` in this repository |
| Tool | `scripts/candidate-rollout.py`; renderer `scripts/workstation-policy.py render-settings --kind personal --merge-scope` |

Project-level `.claude/settings.json` files and managed policy are not touched. Edit and Write on `~/.claude/settings.json` ask by design,
so you run `apply` yourself in a terminal (or with the `!` prefix in Claude Code); the agent does not.

## 3. Exactly what changes

The candidate adds **35 `permissions.ask` entries and nothing else**: no allow, no deny, no `autoMode`, no hook, no env, no model change.
Measured by a read-only `plan` against your live file on 2026-10-23 (live sha256 begins `9ef7e199`): `asks_added 35`, `allow_added 0`,
`asks_retired 0`, `autoMode` unchanged (environment 2 before and after), `blocking: false`.

Added (each bare form has an `rtk ` twin, 17 + 17) and one MCP tool:

```
Bash(gh pr merge:*)          Bash(gh pr merge *)            Bash(gh pr  merge*)
Bash(gh -R * pr merge*)      Bash(gh --repo * pr merge*)    Bash(gh --repo=* pr merge*)   Bash(gh -R=* pr merge*)
Bash(gh pr -R * merge *)     Bash(gh pr -R * merge)         Bash(gh pr --repo * merge *)
Bash(gh pr --repo * merge)   Bash(gh pr --repo=* merge *)   Bash(gh pr --repo=* merge)
Bash(gh pr -R=* merge *)     Bash(gh pr -R=* merge)
Bash(gh api graphql*mergePullRequest*)   Bash(gh api graphql*enablePullRequestAutoMerge*)
mcp__github__merge_pull_request
```

Rules to **retire**: none. Rules to **modify**: none. The broad allows `Bash(gh pr merge:*)`, `Bash(gh pr *)`, `Bash(rtk gh pr *)` and
`mcp__github__merge_pull_request` stay in the file; an ask beats an allow, so they stop running these shapes silently. (Narrowing them is a
separate, optional owner step and is not needed for this candidate.)

Must remain untouched, and `plan` keeps them: all 231 existing asks (including `--admin`, `gh api *pulls/*/merge*`, `gh api ... DELETE`,
`git push --force`, publish and IAM asks, `Edit/Write` on settings and hooks), every existing allow (terraform, gcloud secrets, git, cargo,
pytest and the rest), `permissions.deny`, `autoMode.environment`, `hooks`, `statusLine`, `env`, `model`, `effortLevel`, `advisorModel`,
`enabledPlugins` and the other top-level keys listed in the `plan` output's `untouched_keys`.

The runner allow rule that would let an owner merge reach ALLOW is **not** in this candidate: it widens host authorization, so it stays a
separate owner decision (`OWNER-MERGE-AUTONOMY.md` section 8). Until it exists, every pinned owner merge is ASK (host prompt).

## 4. Apply, verify, roll back

Run from an up-to-date checkout. The values below were measured against your live file on 2026-10-23; **they are only valid while that file is
unchanged, so re-run `plan` and use the numbers it prints.**

```bash
git -C ~/Bryant-Developments/coding-agent-environment fetch origin
git -C ~/Bryant-Developments/coding-agent-environment worktree add --detach ~/Bryant-Developments/cae-operator origin/main
cd ~/Bryant-Developments/cae-operator

# 1. read-only plan (writes nothing)
python3 scripts/candidate-rollout.py plan --live ~/.claude/settings.json \
  --candidate docs/examples/candidate-settings-merge-scope.example.json
#   expect: "blocking": false, asks_added 35, allow_added 0, asks_retired 0
#   observed 2026-10-23: live_sha     9ef7e199108614e9438cf6466d6028c0a99124a87bbab0e66e52e7f274f78c1b
#                        candidate_sha 04169305bad4e7d2cc640612441d12d52287623074960e92bb4bd01344e5ad36
#                        result_sha    c6c13ca48477007dad25d0bb8ee62fdc603fee1be12f9d04f1b406a40ed40ad7

# 2. apply, pinned to the hashes plan printed (refuses if the live file changed since)
python3 scripts/candidate-rollout.py apply --live ~/.claude/settings.json \
  --candidate docs/examples/candidate-settings-merge-scope.example.json \
  --expect-live-sha <live_sha> --expect-result-sha <result_sha> --backup-dir ~/.claude/backups

# 3. verify
python3 scripts/candidate-rollout.py verify --live ~/.claude/settings.json --expect-result-sha <result_sha>
#   expect: "verdict": "VERIFIED"

# rollback (restores the exact original bytes; refuses if the live file changed after apply)
python3 scripts/candidate-rollout.py rollback --live ~/.claude/settings.json \
  --backup ~/.claude/backups/settings.<first 12 hex of live_sha>.json \
  --expect-backup-sha <live_sha> --expect-live-sha <result_sha>
```

`apply` re-serializes the file with 2-space indentation, so its text form changes; the backup keeps the original bytes and `rollback`
restores them. Start a fresh Claude Code session after applying; do not rely on a session that was already running.

## 5. Expected prompts after apply (expected, unobserved)

| You or the agent run | Expected |
|---|---|
| `gh pr merge N --repo R --merge --match-head-commit SHA` (or `rtk gh ...`) | permission prompt showing that exact command. **Yes** once runs exactly it; **No** runs nothing. Do not choose a "don't ask again" option: it would only add an allow that the ask still overrides. |
| MCP `mcp__github__merge_pull_request` | permission prompt (the GitHub MCP server must be connected to test this) |
| `gh api repos/.../pulls/N/merge`, `--admin`, GraphQL `mergePullRequest` | prompt (the first two were already asks) |
| `gh pr view/list/checks N --json mergeable`, comments, `git status`, tests | no prompt, unchanged |
| a tab-separated, `command gh`, `env X=1 gh`, `sh -c`, absolute-path or aliased merge; GraphQL passed by file | **not covered by any rule**; falls to the Auto Mode classifier |

The last row is a known limit of prefix rules, accepted for the Golden User Journey: the shapes that matter (the pinned `gh` merge, its `rtk`
form, the GitHub API and GraphQL merge, the MCP tool) ask. Auto Mode's classifier remains an additional, non-deterministic layer; a denial from it
is recorded with `owner-merge-gate.py host-denied` and never rerouted.

## 6. What a merge looks like end to end

1. The agent opens the PR, waits for CI, runs the independent review, and posts the `OWNER-MERGE-REVIEW head=<sha> ...` record (`evidence-line`).
2. `owner-merge-gate.py check ... --settings ~/.claude/settings.json` prints `decision.outcome`.
3. ALLOW: run the printed pinned command. ASK: read `asks[].resolver`; for `host_prompt` the agent runs the pinned command and you answer the prompt;
   for `owner_github_approval` you submit a genuine GitHub review (class B/C); for anything else the named party resolves it and the agent re-runs `check`.
4. DENY: fix the cause (for example CI); there is no override path here.
5. After a merge: `owner-merge-gate.py verify --repo R --pr N --head SHA` must print `VERIFIED`.

## 7. Test procedures (owner-run, on a disposable private repository; never point them at a real PR)

Record each result next to its ID. Until a row has an observed result it is NOT_RUN.

Positive
- **P1 ASK then merge.** Fresh session. Open a disposable PR from an owner-authored branch, green CI, review record posted. `check --settings ~/.claude/settings.json`
  must print `outcome: ASK`, resolver `host_prompt`, and an `approval_scope` equal to the pinned command. Run that command, answer **Yes**. Expect: merged,
  `verify` prints `VERIFIED`, merge commit has two parents with the reviewed head as one.
- **P2 reads silent.** Run `gh pr view N --json mergeable`, `gh pr checks N`, `git status`: no prompt.

Negative
- **N1 decline.** Repeat P1 and answer **No**. Expect: PR still `OPEN` (`gh pr view N --json state`).
- **N2 stale head.** After the review record, push another commit. `check` must print `ASK` with `agent_runs_independent_review` (`REVIEW_STALE`);
  running the old pinned command must be refused by GitHub (`--match-head-commit` mismatch) even if you answer Yes.
- **N3 red CI.** A PR with a failing required check: `check` prints `DENY`, resolver `author_fixes_ci`; no merge command is offered.
- **N4 wrong route.** `gh pr merge N --admin`, `gh api repos/R/pulls/N/merge -X PUT`: each prompts (or is refused by GitHub). Decline both.
- **N5 MCP route** (only if the GitHub MCP server is connected): a merge through `mcp__github__merge_pull_request` prompts. It failed to connect on
  2026-10-23, so this is NOT_RUN there.
- **N6 non-owner without review** needs a second GitHub identity; it is covered by `tests/test-merge-decision.py` and is NOT_RUN live.
- **N7 rollback.** Run the rollback command from section 4; `verify` against the old hash prints `VERIFIED` and the pinned merge is allowed by rule again.

## 8. What this guide does not claim

`HOST_ENFORCEMENT_VERIFIED`, `POLICY_DEPLOYED`, `LIVE_VALIDATED` and `MERGE_AUTONOMY_READY` are not claimed. The candidate makes every common direct merge
ask; it does not install the runner, add an ALLOW path, or constrain the Auto Mode classifier. The registry wording for `autoMode.allow` is still yours to write
and review (`OWNER-MERGE-AUTONOMY.md`, "Owner allow registry"). Exhaustive coverage of every command spelling is intentionally out of scope.
