# Owner PR merge autonomy (SPE-118)

Status: **Option A selected by the founder: a structurally validated merge runner (`scripts/owner-merge-runner.py`) is implemented and unit-tested. It is NOT deployed and NOT live-validated; no live setting was changed and the runner has never been run against a real PR.** The glob-only allow block in section 2 is **NOT RECOMMENDED** and is not to be deployed: independent review found a new way past a trailing-wildcard allow in each of eight consecutive rounds (section 2, Residual), the host's matching rules were only learned by measuring them, and the remaining holes cannot be closed with glob rules. The runner and its still-open owner step are in section 8.

## 1. Blocking layer (measured, not assumed)

Four independent layers decide a merge. They are not interchangeable and one never stands in for another:

| Layer | Owner of the rule | Role here |
|---|---|---|
| GitHub permission + branch protection/rulesets | GitHub, repo owner | Decides whether the merge is *allowed by GitHub*. Not the source of the denials. |
| Host permission rules (deny, ask, allow) | `settings.json` | A matching allow clears a command before the classifier. |
| Auto Mode classifier | Claude Code built-in soft_deny rules (#59 Merge Without Review, #60 Self-Approval, #55 Auto-Mode Bypass) | Source of every recorded denial. |
| SPE-115 owner-merge gate | this repo, read-only | Decides whether the merge is *safe*; never merges. |

Evidence (`1,814` recorded `gh pr merge` calls):

- 324 single commands that start with `gh pr merge` ran with 0 denials (the live allow `Bash(gh pr merge:*)` resolves first).
- 76 denials, all compound or wrapped: `VAR=… ;`, `cd … &&`, loops, pipes, `$(…)`, heredocs. Bare simple form: 64 ran, 0 denied. 54 of 76 denials involved `--admin`.
- Controlled probe (nonexistent repo, standing-policy prompt, no PR number from the user): no allow rule blocked 2 of 4 with `[Merge Without Review]`; narrow allow, broad allow, `autoMode.allow` exception and live config executed 4 of 4. An explicit per-PR user command executed with no rule (explicit intent clears soft_deny).
- GitHub side, read-only check on 2026-10-20 of PRs merged in the previous 36 hours: 47 PRs across 9 repositories, every one with author equal to merger (the owner account) and `reviewDecision` NONE. These merges are consistent with GitHub not requiring a formal approval for owner-authored PRs here, but the evidence cannot establish it: `gh pr merge --admin` merges PRs that do not meet requirements, and author, merger and `reviewDecision` do not distinguish an unrestricted merge from an administrator bypass. Branch protection or ruleset and bypass-actor settings were not read. What the evidence does show is that the host, not GitHub, was where the denials occurred. The agent and the owner share one GitHub identity, so `mergedBy` cannot tell an agent merge from a manual one, and a shared identity is not proof of independent review. The nine PRs the owner merged by hand after host denials are closed evidence and are not reopened or replayed.
- Denied `gh pr review --approve` on agent-authored PRs is the correct built-in Self-Approval protection. It is not a fault and is never worked around: Class A needs no formal self-approval.

Conclusion: the block is the built-in classifier, reached only because the command shape missed the allow rule. The current broad allow `Bash(gh pr merge:*)` makes the outcome depend on shape, and also lets `--admin` and `--squash` through unreviewed. Only hard_deny #0 is unconditional; the merge rules are soft_deny and are overridable by an allow rule or explicit user intent.

## 2. Smallest supported fix (glob-only allow: NOT RECOMMENDED, superseded by section 8)

One standard form, emitted by the gate (`scripts/owner-merge-gate.py check`), run as a single simple command:

```
gh pr merge <N> --repo <OWNER/REPO> --merge --match-head-commit <40-hex>
```

Rules: no `cd`, no env prefix, no `;`/`&&`, no pipe, no `$(…)`, no loop, no heredoc. One merge per tool call. Measured history: every denial was a compound or wrapped shape; the host evaluates compound commands per subcommand, so a shape that misses the allow rule reaches the classifier, but the exact classifier input for each wrapped form is not documented.

Settings change (owner-applied, user scope):

```json
{
  "remove_allow": [
    "Bash(gh pr merge:*)",
    "Bash(gh pr *)",
    "Bash(rtk gh pr *)"
  ],
  "add_allow": [
    "Bash(gh pr merge * --merge --match-head-commit *)",
    "Bash(rtk gh pr merge * --merge --match-head-commit *)",
    "Bash(gh pr view:*)",
    "Bash(rtk gh pr view:*)",
    "Bash(gh pr list:*)",
    "Bash(rtk gh pr list:*)",
    "Bash(gh pr checks:*)",
    "Bash(rtk gh pr checks:*)",
    "Bash(gh pr diff:*)",
    "Bash(rtk gh pr diff:*)",
    "Bash(gh pr status:*)",
    "Bash(rtk gh pr status:*)",
    "Bash(gh pr comment:*)",
    "Bash(rtk gh pr comment:*)",
    "Bash(gh pr create:*)",
    "Bash(rtk gh pr create:*)",
    "Bash(gh pr edit:*)",
    "Bash(rtk gh pr edit:*)",
    "Bash(gh pr ready:*)",
    "Bash(rtk gh pr ready:*)",
    "Bash(gh pr checkout:*)",
    "Bash(rtk gh pr checkout:*)",
    "Bash(gh pr update-branch:*)",
    "Bash(rtk gh pr update-branch:*)",
    "Bash(gh pr review * --comment*)",
    "Bash(rtk gh pr review * --comment*)",
    "Bash(gh pr reopen:*)",
    "Bash(rtk gh pr reopen:*)",
    "Bash(gh pr lock:*)",
    "Bash(rtk gh pr lock:*)",
    "Bash(gh pr unlock:*)",
    "Bash(rtk gh pr unlock:*)"
  ],
  "add_ask": [
    "Bash(gh pr merge * --admin*)",
    "Bash(rtk gh pr merge * --admin*)",
    "Bash(gh pr merge --admin*)",
    "Bash(rtk gh pr merge --admin*)",
    "Bash(gh pr merge *--auto*)",
    "Bash(rtk gh pr merge *--auto*)",
    "Bash(gh pr merge *--disable-auto*)",
    "Bash(rtk gh pr merge *--disable-auto*)",
    "Bash(gh pr merge *--squash*)",
    "Bash(rtk gh pr merge *--squash*)",
    "Bash(gh pr merge * -s*)",
    "Bash(rtk gh pr merge * -s*)",
    "Bash(gh pr merge *--rebase*)",
    "Bash(rtk gh pr merge *--rebase*)",
    "Bash(gh pr merge * -r*)",
    "Bash(rtk gh pr merge * -r*)",
    "Bash(gh pr merge *-m *)",
    "Bash(rtk gh pr merge *-m *)",
    "Bash(gh pr merge * -m?*)",
    "Bash(rtk gh pr merge * -m?*)",
    "Bash(gh pr merge *--merge=*)",
    "Bash(rtk gh pr merge *--merge=*)",
    "Bash(gh pr merge *--body*)",
    "Bash(rtk gh pr merge *--body*)",
    "Bash(gh pr merge * -b*)",
    "Bash(rtk gh pr merge * -b*)",
    "Bash(gh pr merge * -F*)",
    "Bash(rtk gh pr merge * -F*)",
    "Bash(gh pr merge *--delete-branch*)",
    "Bash(rtk gh pr merge *--delete-branch*)",
    "Bash(gh pr merge * -d*)",
    "Bash(rtk gh pr merge * -d*)",
    "Bash(gh pr merge *--subject*)",
    "Bash(rtk gh pr merge *--subject*)",
    "Bash(gh pr merge * -t*)",
    "Bash(rtk gh pr merge * -t*)",
    "Bash(gh pr merge *--author-email*)",
    "Bash(rtk gh pr merge *--author-email*)",
    "Bash(gh pr merge * -A*)",
    "Bash(rtk gh pr merge * -A*)",
    "Bash(gh pr merge *--match-head-commit=*)",
    "Bash(rtk gh pr merge *--match-head-commit=*)",
    "Bash(gh pr merge *--match-head-commit*--match-head-commit*)",
    "Bash(rtk gh pr merge *--match-head-commit*--match-head-commit*)",
    "Bash(gh pr merge *--match-head-commit ''*)",
    "Bash(rtk gh pr merge *--match-head-commit ''*)",
    "Bash(gh pr merge *--match-head-commit \"\"*)",
    "Bash(rtk gh pr merge *--match-head-commit \"\"*)",
    "Bash(gh pr merge *--match-head-commit -*)",
    "Bash(rtk gh pr merge *--match-head-commit -*)",
    "Bash(gh pr merge *--match-head-commit * *)",
    "Bash(rtk gh pr merge *--match-head-commit * *)",
    "Bash(gh pr merge * -m*)",
    "Bash(rtk gh pr merge * -m*)",
    "Bash(gh pr merge * -h*)",
    "Bash(rtk gh pr merge * -h*)",
    "Bash(gh pr merge*$*)",
    "Bash(rtk gh pr merge*$*)",
    "Bash(gh pr merge*`*)",
    "Bash(rtk gh pr merge*`*)",
    "Bash(gh pr merge*{*)",
    "Bash(rtk gh pr merge*{*)",
    "Bash(gh pr merge*\"*)",
    "Bash(rtk gh pr merge*\"*)",
    "Bash(gh pr merge*'*)",
    "Bash(rtk gh pr merge*'*)",
    "Bash(gh pr merge*;*)",
    "Bash(rtk gh pr merge*;*)",
    "Bash(gh pr merge*&*)",
    "Bash(rtk gh pr merge*&*)",
    "Bash(gh pr merge*|*)",
    "Bash(rtk gh pr merge*|*)",
    "Bash(gh pr merge*<*)",
    "Bash(rtk gh pr merge*<*)",
    "Bash(gh pr merge*>*)",
    "Bash(rtk gh pr merge*>*)",
    "Bash(gh pr merge*(*)",
    "Bash(rtk gh pr merge*(*)",
    "Bash(gh pr merge*)*)",
    "Bash(rtk gh pr merge*)*)",
    "Bash(gh pr merge*[*)",
    "Bash(rtk gh pr merge*[*)",
    "Bash(gh pr merge*\\**)",
    "Bash(rtk gh pr merge*\\**)",
    "Bash(gh pr comment *--body-file*)",
    "Bash(rtk gh pr comment *--body-file*)",
    "Bash(gh pr comment * -F*)",
    "Bash(rtk gh pr comment * -F*)",
    "Bash(gh pr comment *--template*)",
    "Bash(rtk gh pr comment *--template*)",
    "Bash(gh pr comment * -T*)",
    "Bash(rtk gh pr comment * -T*)",
    "Bash(gh pr create *--body-file*)",
    "Bash(rtk gh pr create *--body-file*)",
    "Bash(gh pr create * -F*)",
    "Bash(rtk gh pr create * -F*)",
    "Bash(gh pr create *--template*)",
    "Bash(rtk gh pr create *--template*)",
    "Bash(gh pr create * -T*)",
    "Bash(rtk gh pr create * -T*)",
    "Bash(gh pr edit *--body-file*)",
    "Bash(rtk gh pr edit *--body-file*)",
    "Bash(gh pr edit * -F*)",
    "Bash(rtk gh pr edit * -F*)",
    "Bash(gh pr edit *--template*)",
    "Bash(rtk gh pr edit *--template*)",
    "Bash(gh pr edit * -T*)",
    "Bash(rtk gh pr edit * -T*)",
    "Bash(gh pr review * --approve*)",
    "Bash(rtk gh pr review * --approve*)",
    "Bash(gh pr review * -a*)",
    "Bash(rtk gh pr review * -a*)",
    "Bash(gh pr review * -c*)",
    "Bash(rtk gh pr review * -c*)",
    "Bash(gh pr review * -r*)",
    "Bash(rtk gh pr review * -r*)",
    "Bash(gh pr review * -h*)",
    "Bash(rtk gh pr review * -h*)",
    "Bash(gh pr review * --request-changes*)",
    "Bash(rtk gh pr review * --request-changes*)",
    "Bash(gh pr review *--body-file*)",
    "Bash(rtk gh pr review *--body-file*)",
    "Bash(gh pr review * -F*)",
    "Bash(rtk gh pr review * -F*)",
    "Bash(gh pr review *--template*)",
    "Bash(rtk gh pr review *--template*)",
    "Bash(gh pr review * -T*)",
    "Bash(rtk gh pr review * -T*)"
  ]
}
```

Properties: no global allow of `gh pr merge`; ask rules are evaluated before allow, so `--admin`/`--squash`/`--rebase`/`--auto`, a second `--match-head-commit` (which could blank the pin) and `--match-head-commit=` prompt in both the `gh` and `rtk gh` spellings even though the allow glob also matches them; `gh pr review --approve` and `gh pr close` stop being silently allowed. No built-in rule is removed, no autoMode text is added, no hook is installed. The classifier still evaluates anything that is not this exact shape.

The measured gap: the allow clears the *classifier*, not the *judgment*. The decision to run the command comes from the gate verdict plus the head-pinned evidence comment. The `--match-head-commit` pin makes a stale verdict fail closed at GitHub.

### Measured on the real host (2026-10-22)

`scripts/permission-host-matrix.py` (SPE-119) was run against these rules with `docs/permission-corpus/owner-merge-corpus.json` (47 commands, every target a nonexistent repository; hook-recorded ground truth, matched to the tested command by program and arguments (rtk may append an argument such as a trailing dot, and the stored `recorded` text is truncated to 160 characters, so stored commands are not byte-identical to the corpus); zero probe errors; results in `docs/permission-corpus/owner-merge-results-2026-10-22.json`).

| | Result |
|---|---|
| The pinned merge (both spellings), routine PR verbs (checkout, update-branch, reopen, view, checks, diff, comment), comment-only reviews including `-b` and quoted bodies with `#12` | **14 of 14 run with no prompt** |
| Risky spellings (29): admin, squash, `-s`, `-d`, `-md` clusters, delete-branch, disable-auto, subject, body, `--merge=false`, empty and doubled pins, trailing argument, quoted merge flags, plain review approvals, `--body-file` and `-F` on comment, create, edit and review, literal-star globbing | **29 of 29 prompt** |
| Measured residuals (4, below) | backslash flag and substitution-disguised review approval ran with no prompt; brace-expanded review did not run; a trailing-comment merge was approved by the classifier |

Host facts measured here, each of which corrected an earlier assumption (five review rounds had used only a Python glob emulation, and the first real-host positive control failed):

- Whitespace in a command is normalized: a tab or doubled space still matches a space in a rule. A tab or newline inside a **rule** is normalized to a space and matches every command, so such rules made the standard pinned merge itself prompt; none exist.
- A trailing shell comment is stripped before matching, so comment text can neither hide nor supply a pin. `--merge # --merge --match-head-commit <sha>` is not rule-allowed; it reaches the classifier like any unlisted merge (measured: the classifier ran it against the nonexistent repository, which is a reason not to rely on the classifier for merges). No ask can match comment text, so none exist for `#`.
- `?` is a wildcard in a rule. A literal `*` is asked with an escaped star (`*\**`), a literal `[` works, and a literal backslash cannot be matched by `\\` or `\`.

### Residual: what a glob allow cannot close

A trailing-wildcard allow cannot say "exactly these tokens", so safety rests on asks that enumerate everything else: every `gh pr merge` flag of the installed `gh` (2.102), the pin-blanking forms, short-flag clusters, shell expansions and quotes in merge commands, shell metacharacters, a literal star, and `--body-file`/`-F`/`--template` on every allowed body-taking verb. `tests/test-owner-merge-autonomy.py` reads the installed `gh`'s own flag list and fails when a flag or cluster is not covered. Independent review rounds kept finding another spelling, which is the nature of the approach. What remains open on this host, by measurement:

- **A backslash inside a merge flag** (`--ad\min`, which the shell turns into `--admin`) matches the allow and no ask. The agent itself would have to type it; an attacker who can only influence PR text cannot.
- **Disguised review approval** (`--ap""prove`, brace expansion or command substitution reassembling `--approve`). The review allow carries no generic quote, brace or substitution asks, because ordinary comment bodies contain quotes and `$(...)`; asking on them made routine comment reviews prompt. The only other control is the built-in Self-Approval protection, which is not deterministic.
- **`?` globbing** (`--admi?` with a file named `--admin` in the working directory) cannot be asked because `?` is a rule wildcard.
- **An unpinned merge in any spelling** is not rule-allowed but is left to the classifier, which is not a control to rely on.

These are accepted-or-not decisions for the owner, not defects to keep patching. If they are not acceptable, do not apply the narrow allow; use the validating runner below or keep the `!` fallback.

The stronger design is a merge runner that re-validates the exact argument vector (40-hex pin, repository, PR number, `merge` strategy only) and the gate verdict in code before running `gh pr merge`, with the allow rule covering that program only. It moves the bypass surface into code that can be tested exhaustively. It is now built (section 8). ADR 0013 still makes the gate itself read-only and never-merging; the runner is a separate program, and an allow rule over a merge-executing program is a policy change that only the owner applies. Until that rule is applied and verified the supported fallback is unchanged: if the host asks or denies, the owner runs the printed command with the `!` prefix.

The list of holes is open-ended, not just the four above. Reviews also reproduced, against allows that take a body or attachments: `gh pr comment ... --attach ./private.png` (local upload), a short-flag cluster such as `gh pr create ... -dF./private.txt` (draft plus body file), a quoted long flag such as `gh pr edit ... --bo""dy-file ./private.txt`, and local-file exfiltration through command substitution inside an inline body (`--body "$(cat file)"`), which today's broad `gh pr *` allow already permits. Conversely, asks that fire on flag text also fire on prose: a comment body that contains `-h` or `--approve` as words asks. Each fix to one class made another instance appear; this is the review-loop circuit breaker of `docs/ENGINEERING-CONSTITUTION.md`, so the instances are not patched further and the architectural options are presented instead.

## 3. PR classes

| Class | Author | Needed before the pinned merge |
|---|---|---|
| A | verified owner account, owner is ADMIN (or code owner with WRITE/MAINTAIN) | independent PASS comment `OWNER-MERGE-REVIEW head=<sha> verdict=PASS blocking=0 reviewer=<id>` at the current head; gate `PASS`. No formal self-approval. |
| B | verified dependency bot | real GitHub APPROVE at the exact head from the authenticated owner identity, as the gate requires (`scripts/owner-merge-gate.py`). The owner submits it personally; the agent runs under the same identity, so the gate cannot tell the two apart and the separation is process-only. One at a time per repo; reconcile and re-gate after each merge. |
| C | any other human | owner's genuine APPROVED review at the exact head, never submitted on the owner's behalf. |

A new commit changes the head, so earlier evidence no longer matches and the gate refuses until a fresh review is recorded.

## 4. Host denial is binding

If the host refuses the standard form, report the exact operation (secrets redacted) and stop. Do not re-run it with another shape, tool, script, subagent or operator. The owner may run the printed command with the `!` prefix, and that is the supported path. A denial is `HARNESS_PERMISSION_BLOCK`, not evidence the PR is unsafe, and not a product decision.

## 5. Install, verify, rollback (owner-run; protected file; only if the owner chooses option D in section 6)

1. Back up and pin: `cp -p ~/.claude/settings.json ~/.claude/backups/settings.json.$(date +%Y%m%d-%H%M%S)`; record `shasum -a 256` of the live file and of the backup.
2. Apply the JSON block in section 2 through `/permissions` (remove the three listed allows, add the listed allows and asks), or edit the three arrays by hand. `scripts/candidate-rollout.py` does not apply this block: it only adds asks and the scoped `rtk proxy` allows from SPE-119, and it preserves existing broad allows. Do not apply while an active session may be affected.
3. Verify the effective policy in a **new** session (`/permissions`): the allow list has no `gh pr merge:*` and no `gh pr *`; the ask list has the `--admin` rule.
4. Negative controls on fixtures only (nonexistent repo): `--admin`, `--squash`, a doubled `--match-head-commit` and the `rtk` spellings of each prompt. Compound forms are evaluated per subcommand by the host, so do not rely on a compound being refused; the gate only ever prints the simple form.
5. Positive control on the disposable owner PR described in `docs/OWNER-MERGE-LIVE-VALIDATION-PLAN.md`.
6. Resumed sessions keep the policy loaded at start; start a new session after any change.

Rollback: check that `shasum -a 256` of the backup equals the recorded pin, copy it over `~/.claude/settings.json`, start a new session.

## 6. Decision needed (owner), with the evidence

The merge gate (`scripts/owner-merge-gate.py`) is deterministic when it is run, but it is read-only and prints the pinned command after PASS; nothing forces a merge through it, and the head pin enforces SHA equality only, not that a review or the gate happened. What is undecided is how the host is allowed to run the pinned command without a per-merge prompt, and the measured answer is that a glob allow cannot do it safely. Options:

| Option | What it is | Cost and limits |
|---|---|---|
| A. Validating merge runner (SELECTED by the founder; implemented, not deployed) | A small program that re-checks the exact argument vector (40-hex pin, repository, PR number, merge strategy only) and the gate verdict, then runs `gh pr merge`; the allow rule covers that program only | Moves the bypass surface into code that can be tested exhaustively. A policy change: ADR 0013 makes the gate read-only and non-merging, and an allow rule over a merge-executing program must be the owner's decision. Built in section 8; the allow rule is still an owner step |
| B. PreToolUse hook | A hook that receives the Bash command as an unexpanded string (the documented PreToolUse input; it does not see the resolved argument vector) and approves only the exact pinned shape, rejecting any expansion, quoting or escape syntax rather than interpreting it | Same property as A without a new executable; whether a hook's allow bypasses the classifier is untested |
| C. Keep the per-merge `!` run | Fallback only when no supported approval path exists: the agent has already reached the normal host ASK for the pinned `gh pr merge` and been refused or given no path; it then reports the exact restriction once and the owner may run the pinned command with the `!` prefix | No new exposure; one owner action per merge. The agent never defaults to this |
| D. Apply section 2 knowingly | Accept the residuals above | Not recommended: the holes are open-ended and some are exfiltration paths |

The founder selected A. Until the runner's allow rule is applied and verified, the supported state is the agent running the pinned `gh pr merge` through the normal host ASK (C only as the no-approval-path fallback), and the live broad `Bash(gh pr merge:*)` allow remains the unresolved exposure it is today.

## 7. What is and is not claimed

Designed and unit-tested in this repository; the classifier behavior above is measured on the owner's workstation. LIVE_DEPLOYED and LIVE_VALIDATED stay false until the owner applies the runner allow rule (section 8) and a disposable-PR run succeeds. This document is not permission to bypass any earlier host denial and must not be read as `MERGE_AUTONOMY_READY`.

## 8. Option A: the validating merge runner (implemented, not deployed)

`scripts/owner-merge-runner.py` (stdlib Python 3, `tests/test-owner-merge-runner.py`, wired into `capability-validation.yml`):

```
owner-merge-runner.py --repo OWNER/NAME --pr N --expect-head SHA40 --review-evidence PATH [--dry-run]
```

What it enforces in code, each with a negative-control test and a mutant that fails the suite:

- Structured arguments only. `--repo`, `--pr` and `--expect-head` are matched by strict regexes before any use (shell metacharacters, trailing newline, uppercase hex, wrong length all refuse before gh is started). `gh` is resolved once as an absolute path, run with `shell=False`, and every call passes an argv-shape check: only `gh api user`, `gh pr view ... --json <fixed field list>`, two fixed `gh api` reads of the base branch's rulesets and branch protection, and the single merge argv are accepted. There is no flag to choose another `gh`; tests inject a fake through the module API only.
- Guards (all must hold; every reason is reported): PR OPEN and not draft; head equals the pinned `--expect-head`; `mergeable == MERGEABLE` and `mergeStateStatus == CLEAN`; no unresolved `CHANGES_REQUESTED` (latest decisive review per reviewer, plus `reviewDecision`). The PR fields the guards read (state, draft flag, author, base and head refs, head SHA, mergeability, review list, check rollup, files) refuse when missing or mistyped; `reviewDecision` must be present but may be empty or null, as GitHub returns it.
- Required checks, named per repo. The allowlist entry lists `required_checks`; every named check must be present in the rollup at the expected head with conclusion/state `SUCCESS`, and every other entry (including a duplicate of a required name) must also be `SUCCESS`, except that a `SKIPPED` entry of a check that is not in `required_checks` (a job whose `if:` did not select this event, such as a release job that only runs on tag pushes) is ignored and counted in the audit field `skipped_ignored`; a required check that skipped still blocks. A rollup entry without a name, or without a conclusion/state once completed, refuses. An empty `required_checks` (or empty `reviewers`) makes that repo's config invalid and refuses. SonarCloud counts only if it is named or reports a non-success.
- Server-side protection. Unless the repo entry sets `require_server_side_protection: false` (default true; anything but a boolean is invalid), the runner reads, for the pinned base branch, the rulesets (`rules/branches/<base>`) and branch protection and builds the set of required status-check names: ruleset `parameters.required_status_checks[].context`, protection `required_status_checks.contexts` and `checks[].context`. An empty or malformed definition refuses, the set must be non-empty, and every name in the repo's `required_checks` must be in it. Classic branch protection must have `enforce_admins.enabled` exactly `true`. For each ruleset that carries a required-checks rule the runner reads its `bypass_actors`: **any** listed actor refuses (User, Team, OrganizationAdmin, RepositoryRole, EnterpriseOwner, EnterpriseRole, DeployKey, Integration and unknown types alike); only an empty list is `none`. **Fail closed on missing metadata:** a ruleset detail that cannot be read (for example HTTP 403) or whose `bypass_actors` is not a list of objects refuses, and so does a branch-protection read that fails for any reason other than a real HTTP 404 (the first `HTTP <token>` in gh's error decides and the whole token must be exactly three digits: `HTTP 4040:`, `HTTP 404abc:`, a 404 mentioned elsewhere, a branch name containing 404, or no parseable status all count as unreadable, so `enforce_admins` is unverified). `accept_bypass_actors: true` (default false) is the only waiver of all of these; every waiver actually exercised is recorded in the audit record's `server_side`: `waivers` lists each one (`enforce_admins_false`, `protection_unreadable`, `bypass_unreadable`, `bypass_actors_present:<types>`), `enforce_admins` is `true`, `n/a` (no classic protection), `false-accepted` or `unverified-accepted`, and `bypass` is `none` only when no waiver of any kind was used, otherwise `accepted` (actors present), `unreadable-accepted` (ruleset metadata unreadable) or `protection-waiver-accepted` (only the classic-protection waivers). Setting `accept_bypass_actors: true` with nothing to waive records `none` and an empty list.
- Review evidence (a JSON record the runner only reads, never writes) must bind `repo`, `pr`, `base` (the PR's base branch, pinned from the first read, see below), `head` and `verdict` PASS, plus string fields `reviewer`, `reviewer_kind` (`human` or `independent-agent`) and `reviewed_at` (`YYYY-MM-DDTHH:MM:SSZ`). `reviewed_at` may not be more than 60 seconds in the future or older than `max_evidence_age_hours` (default 24, allowlist range 1-168). Any missing or mistyped field, mismatch, or a `reviewer` not in the repo's allowlisted `reviewers` refuses. Class A needs it too.
- PR class (section 3): A author equals the authenticated `gh` user; B dependency bot on a bot branch in the same repository, with an APPROVED review by the authenticated user at the exact head; C an APPROVED review at the exact head by a non-author whose `authorAssociation` is OWNER, MEMBER or COLLABORATOR; a cross-repository (fork) Class C PR additionally needs the authenticated user's own APPROVED review at the exact head.
- One mutating command, byte-for-byte: `gh pr merge N --repo R --merge --match-head-commit SHA`. Never `--admin`, `--squash`, `--rebase`, `--auto` or `--delete-branch`. One attempt only: a gh refusal is recorded as `DENIED` (or `ERROR`) and the runner exits nonzero without retrying, trying another route or touching settings.
- Optional signed evidence. If the allowlist names a top-level `evidence_key_file`, each repo defaults to `require_signed_evidence: true` and the evidence must carry an `hmac` (HMAC-SHA256 over the canonical, sorted-key JSON of the record without `hmac`) that verifies against that key; `false` is an explicit per-repo waiver, and `true` without a key file is an invalid config. The key file gets the allowlist's trust walk and descriptor read with a stricter mode (no group/other bits at all), outside any git worktree, and must be 32 to 4096 bytes; the key is never printed, logged or put in the audit record. The signature is produced with `scripts/owner-merge-evidence-sign.py --key-file K --in evidence.json --out signed.json` (the output is created 0600 and never overwrites). **The runner verifies with the same key, so the key file must be readable by the OS user that runs the runner (the merging agent's user). Signing from a separate user therefore does NOT give isolated verification today: whoever can run the runner can read the key and mint a valid signature.** It adds tamper evidence against edits by something that lacks the key, nothing more. Where the agent and the owner share one OS user and one GitHub identity, as on this workstation today, it adds tamper evidence, not independence.
- Base pinning. The base branch is taken from the first PR read and carried through everything after it: the pre-exec pass must report the same base, the protection and ruleset reads use that base, the evidence base must equal it, a further PR read after the protection reads must still report the same base and head and must pass the same full decision as the first read (state, draft, reviews including CHANGES_REQUESTED, every required check, malformed check entries, mergeability, PR class), and the post-merge read-back must report the same base (otherwise `UNVERIFIED`). The evidence file is hashed on the first pass and must be byte-identical on the second, so evidence re-signed between passes refuses.
- Staleness. Immediately before the merge the runner repeats the whole evaluation from fresh reads (PR facts, evidence, every guard, server-side protection); any change, including a different head or PR class, refuses. A window still remains between that last read and GitHub executing the merge, and `--match-head-commit` pins only the head: a new review, a failing check, a changed merge state or a **retargeted base** in that window is not seen, and `gh pr merge` offers no base precondition. The real server-side gate is GitHub branch protection or rulesets with required status checks, which is why the runner refuses repos without them by default; the runner is not a substitute.
- After a zero exit the PR is read back. Anything other than `MERGED` with a merge commit is `UNVERIFIED` (exit 5). `MERGED` with a base that differs from the reviewed base is exit 7 with the marker `POSSIBLE MERGE TO UNREVIEWED BASE` on stderr and in the audit record (`incident` field and a reason): the merge may already have happened onto a branch nobody reviewed, so **UNVERIFIED and exit 7 must be treated as an incident**, not a retry or a nuisance. The runner cannot undo it; GitHub-side branch protection or rulesets on every branch a PR could be retargeted to are the real control.
- Audit. Fixed path `~/.claude/audit/owner-merge-runner.jsonl`. Every ancestor directory up to `/` must be a non-symlink owned by root or the current user and not group/world-writable (a sticky root-owned directory such as `/tmp` is the only writable exception). The file is opened with `O_NOFOLLOW|O_APPEND|O_CREAT` at mode 0600 and the descriptor is checked (regular file, owned by the user, one link, mode exactly 0600; a looser existing file is refused, not repaired). Writes loop until every byte is written (zero progress raises), and the `PENDING` record is `fsync`ed before the merge; if it cannot be written the runner refuses before executing. If the final record cannot be written the runner exits 6 with the outcome on stderr, never 0. Secret-shaped text is redacted before writing: the whole value after `Authorization:`, `Bearer`/`Basic`, and `token`, `password`, `passwd`, `secret`, `api_key` or `access_token` (quoted values including spaces, otherwise to the end of the line), plus GitHub token shapes. No gh stderr text is ever stored or printed, whole or in part: no excerpt, no digest or hash of it. A failed gh call is recorded only as `gh failure stage=<pr-view|rules-read|protection-read|merge> status=<three-digit HTTP status or unknown> category=<denied|not-found|error|unreadable>`, all derived by the runner's own parsing. (Redaction by pattern is kept only for other strings, such as exception messages from the runner itself, and is not relied on for gh output.) The allowlist, key file, evidence file and audit file are opened non-blocking and must be regular files, so a FIFO or device is refused promptly instead of hanging the runner.
- Malformed input fails closed. A signature must be exactly 64 lowercase hex characters before it is compared, review entries are type-checked, and the whole run is wrapped: any unexpected exception becomes an audited `ERROR` record with exit 8 and no merge (if the merge command had already started, the record says the PR may already be merged). The `PENDING` record still precedes the merge.
- Exit codes: 0 success or validated dry run, 2 refused, 3 denied, 4 error, 5 unverified, 6 final audit record not written, 7 merged onto an unreviewed base (incident), 8 unexpected internal error, 64 usage.

The allowlist is required and fixed: `~/.claude/owner-merge-runner-allowlist.json` (never loaded from the repo; there is no CLI override for it, the audit log path or `gh`; only the module API used by tests can pass others). Format: `{"evidence_key_file": "/abs/path" (optional), "repos": {"OWNER/NAME": {"required_checks": [...], "reviewers": [...], "require_server_side_protection": true, "accept_bypass_actors": false, "max_evidence_age_hours": 24, "require_signed_evidence": <default: true iff a key file is set>}}}`; unknown keys refuse. Before it is trusted the runner walks every ancestor directory with the same rule as the audit path, opens the file with `O_NOFOLLOW`, checks the open descriptor (regular, owned by the current user, not group/world-writable, not inside a git worktree) and reads from that descriptor, not the pathname. The runner refuses until the owner creates it. `docs/examples/owner-merge-runner-allowlist.example.json` shows the format and is refused if pointed at, being in a worktree.

Mutation check: `tests/mutate-owner-merge-runner.py` removes or weakens one guard at a time in a scratch copy and the unit suite must fail (a hang counts as a failure); it is deterministic, needs no network, and is not part of the all-tests loop or CI. Result at the head recorded in the PR: 125 of 125 mutants killed. The mutants cover the protection-error classifier, signature and review shape checks, the generic handler, the final-read full decision, unreadable or malformed bypass and protection metadata, the base-mismatch exit code and incident markers, the head pin, the full pre-exec re-evaluation, base pinning (later pass, re-read after protection, evidence byte-equality, pinned-base protection reads, read-back), server-side set parsing and coverage, `enforce_admins`, bypass actors, required checks, evidence bindings, age, signature and key checks, argv fixedness, every allowlist/key/ancestor trust check and every audit check.

### Owner step still open: the host permission (unmeasured)

The runner does not change what the host asks. To run it without a per-merge prompt, the owner would copy the script to one stable path and allow only that literal form. Proposed rule (user scope, owner-applied; not applied, not measured):

```
Bash(/Users/bryant/.claude/bin/owner-merge-runner --repo * --pr * --expect-head * --review-evidence *)
```

invoked exactly as `/Users/bryant/.claude/bin/owner-merge-runner --repo OWNER/NAME --pr N --expect-head SHA --review-evidence PATH` (no `python3`, no `rtk` prefix, no `cd`, no env prefix; the rtk-rewritten spelling may need its own rule). Whether the host matches that form, normalizes it, or still reaches the classifier is **unmeasured** until verified with `scripts/permission-host-matrix.py` against a corpus of this exact command (plus negative controls) on the real host. Use `--dry-run` only for such probes; do not point a probe at a real PR.

### Binding limits, residuals

- Explicit host denial stays binding. If the host denies the runner call, report the exact operation and stop; do not re-shape it, route around it or ask a subagent. The runner is not a bypass of the Auto Mode classifier, including Self-Approval and Merge Without Review (#59, #60): an allow rule may clear the classifier for this literal form only if the owner applies and verifies one, and nothing here asserts that it would.
- Evidence forgeability (P1-3) is NOT closed and is an owner decision. A same-user process can mint the evidence file, read the verification key and sign it, edit the allowlist, and the agent and owner share one GitHub identity, so no in-file field and no GitHub comment is unforgeable. What exists is bounded: field binding (repo, PR, base, head, reviewer allowlist, age), byte-identity across the two passes, and optional HMAC verification that is tamper evidence only (see above). Options are listed under Decisions needed.
- Checks are trusted as GitHub reports them, and the allowlist's `required_checks` names must be kept in step with the repository's real required checks.
- Late retarget: between the last read and the merge `gh` offers no base precondition, so a retargeted merge may already have happened when UNVERIFIED (exit 7) is reported; GitHub-side protection on every possible target is the control. Not checked or not verifiable by the runner: organization-level rules or administrator action outside what `rules/branches` and `protection` return; whether the named checks cover the right jobs; GitHub-side changes after the last read. GitHub remains the final authority.
- The runner never uses `--admin`, so a same-identity `REVIEW_REQUIRED` deadlock is not resolved by it (the separate owner-only exception is out of scope).
- Nothing here was exercised against a real PR. Verified: unit tests with a fake `gh`. Not verified: real `gh` JSON field behavior for every PR shape (for example bot author spelling), real merge, host matching.

### Decisions needed (owner)

1. Evidence independence (P1-3), not closed by this runner. Options: (a) accept same-user evidence as tamper-evident only (the position today); (b) a required review, check or status from a second identity (another human or a GitHub App) that the runner reads back from GitHub, which needs a runner change; (c) verification of evidence by a separate trusted process or OS user that holds the key and returns only a verdict, so the merging user never holds the key (needs a runner change and a service boundary). Until one is chosen, nothing here claims independent review.
2. Whether to waive per repo: `accept_bypass_actors`, `require_server_side_protection`, `require_signed_evidence`. The defaults refuse; waivers are visible in the audit record.
3. The host allow rule from this section (unmeasured until verified with `scripts/permission-host-matrix.py`).

## 9. LOCAL_EQUIVALENT_CI exception (founder policy 2026-10-22)

Policy text lives once, in `docs/ENGINEERING-POLICY.md` section 8. This section only describes how the runner enforces it.

Off by default. Per repository, the owner sets `"local_equivalent_ci": true` in the allowlist. It does **not** relax server-side protection, the review evidence, the author/class rules, mergeability, the audit trail or any other guard.

A check is exempted only when all of these hold, re-evaluated on both passes and on the final read. The check may be a required one or a non-required one (for example a path-filtered matrix job on a private repo without branch protection, which cannot be allowlisted as required); either way it must be listed as passed in the `local_ci` block (point 4):

1. Every non-SUCCESS rollup entry of that check is a hosted `CheckRun` with verdict `FAILURE`, `STARTUP_FAILURE` or `CANCELLED` (not `TIMED_OUT`, `SKIPPED`, `NEUTRAL`, `ACTION_REQUIRED`, pending, or a status context).
2. Its `detailsUrl` is a job URL of this same repository, and `gh api repos/R/actions/jobs/ID` reports for that job: the same name, `head_sha` equal to the expected head, `status` completed, **no steps**, and **no runner** (`runner_id` null or 0, empty `runner_name`). That is GitHub's own statement that the job never executed, as opposed to a test failure (`CI_FAILED`, never bypassed).
3. Every check run of that name at the expected head (`gh api repos/R/commits/SHA/check-runs?check_name=...`, complete listing) belongs to the GitHub Actions app, so an integration cannot publish an executed failure under the same name and details URL as a never-started job.
4. The review evidence carries a `local_ci` block that lists the check with `exit_code` 0, pinned to the same head, fresh within `max_evidence_age_hours`, classification `LOCAL_EQUIVALENT_PASS`. The block is covered by the evidence HMAC when signing is configured. Exact key sets; duplicates, booleans, extra keys refuse.

```json
"local_ci": {"classification": "LOCAL_EQUIVALENT_PASS", "head": "<40-hex head>", "run_at": "2026-10-22T10:00:00Z",
             "checks": [{"name": "isolated-validation", "exit_code": 0, "tests": 125}]}
```

The exemption is part of the audit-compared result: if the CI classification differs between the two passes, nothing merges. A required check missing from the rollup, a failing check that is neither required nor listed in the local evidence, an executed failure, a malformed or stale block, a job read that fails, or a job that changes between reads all refuse (or error); nothing merges. The audit record carries `checks.ci_class` (`CI_PASS` only when nothing was exempted, else `CI_UNAVAILABLE_EXTERNAL`) and `checks.local_equivalent` (the exempted names): an exempted check is never reported as passed hosted CI.

`mergeStateStatus` must be `CLEAN`, with one exception: `UNSTABLE` is accepted only when at least one check was exempted and every non-SUCCESS rollup entry belongs to an exempted check (so nothing is pending, skipped, or an executed failure). `BLOCKED`, `DIRTY`, `BEHIND`, `UNKNOWN`, `HAS_HOOKS` and the rest still refuse. Required checks must still be present and SUCCESS unless exempted by the same proof. The audit record adds `checks.merge_state`.

A `local_ci` block is the author's own record of a local run. Its listed jobs may all be backed by one machine and one OS/Python version, so it does not prove coverage of the hosted matrix platforms (for example a macOS-only local run listed for both an ubuntu and a macos job), `pytest --cov`, or network-dependent steps. The review comment and the founder exception message must therefore name the uncovered platforms, and the runner's audit record never presents the exemption as a CI pass (`ci_class` is `CI_UNAVAILABLE_EXTERNAL`).

Not closed, stated plainly:
- The runner cannot tell a billing or quota cause from any other reason a job never started; it verifies "never executed", and the founder policy's "verified billing or quota restriction" remains the person's attestation in the evidence.
- The job-shape condition (`steps: []`, no runner) is not yet observed against a real billing outage; until it is, it is `NOT_VERIFIED` against reality and only unit-tested with a fake `gh`.
- A workflow in the same repository can create check runs under the Actions app with its own token; that writer is inside the trust boundary the owner already has to accept.
- The local evidence has the same same-user forgeability as the review evidence (section 8, residuals).
- If the base branch has server-side required checks, GitHub itself blocks the merge while the check is unavailable; the runner does not and must not change that.

## 10. Server-side required checks: rollout, readback, owner/admin behavior, rollback (owner-run)

The runner refuses a base branch without server-side required status checks (section 8). `scripts/branch-protection-rollout.py` is the hash-pinned way to add them. `plan` and `verify` only read; `apply` and `rollback` change repository settings and are run by the repository owner, not by an agent session. It creates protection only where there is none (an existing protection or any ruleset coverage is reported and refused) and never touches review, restriction or linear-history settings (merge commits stay possible).

```bash
R=Chisanan232/coding-agent-environment; C=isolated-validation,gitleaks,native-resource-wire
python3 scripts/branch-protection-rollout.py plan --repo $R --branch main --checks $C         # prints payload + payload_sha256
python3 scripts/branch-protection-rollout.py apply --repo $R --branch main --checks $C --expect-payload-sha <sha>
python3 scripts/branch-protection-rollout.py verify --repo $R --branch main --checks $C       # readback + the runner's own check
python3 scripts/branch-protection-rollout.py rollback --repo $R --branch main --checks $C --expect-payload-sha <sha>
```

`apply` writes a 0600 backup record (`~/.claude/backups/branch-protection.*.json`), PUTs, then reads the protection back and compares every field it set; a mismatch exits 5 and is reported, not assumed. `rollback` refuses unless the live protection still equals what `apply` wrote, so later edits are not silently lost, and then deletes it and reads back that it is gone.

Owner/admin behavior to expect and to check after `apply` (the default sets `enforce_admins=true`):
- Required checks then bind the owner's own identity, which is what lets the runner trust the server-side check (the runner refuses `enforce_admins=false` unless `accept_bypass_actors` is set). A PR whose three checks pass still merges normally for the owner. A PR with a failing or missing check is blocked for the owner too; `gh pr merge --admin` no longer bypasses it.
- `--no-enforce-admins` keeps the owner's bypass; then the runner correctly refuses, because the check does not bind its identity.
- Force pushes and deletion of `main` are forbidden by the same rule.
- Check (read-only): `verify` prints `matches_payload` and `runner_server_side_check`; then run `owner-merge-runner.py --dry-run` on a green PR to see APPROVE, and open any throwaway PR with a failing check to see GitHub block the owner's merge. Do not claim `MERGE_AUTONOMY_READY` until one real merge through the runner has been read back as `MERGED` on the pinned base.

`apply` re-reads the protection and rulesets immediately before the write, but GitHub offers no compare-and-swap: protection created in that gap would be overwritten (the backup would then record `prior: null`). Run it when no one else is editing the branch settings. The same applies to `rollback`: it compares a GET snapshot and then issues an unconditional DELETE, so protection strengthened between those two requests would be deleted. Do not run either while anyone else is changing the branch's protection (found in the bounded delta review; no GitHub-side fix exists, so it is a documented residual, not a code defect). `apply` records GitHub's own readback (GitHub binds recent Actions checks to the Actions app automatically) in the 0600 backup file, and `rollback` deletes only when the live protection is byte-for-byte that recorded readback, so any later change (reviews, restrictions, signatures, linear history, conversation resolution, a changed check source, lock or creation blocks) or a missing apply record makes it refuse.

Interaction with the LOCAL_EQUIVALENT_CI exception (section 9): where a required hosted check cannot run (a private repository with a billing outage), GitHub blocks the merge while that check is required, and the runner will not and must not change that. For such a repository the owner either keeps protection off and relies on the runner's exception, or runs `rollback` for the outage window; that choice is the owner's, per repository. This repository's CI runs, so it does not need the exception.

The existing `scripts/owner-merge-gate.py --ci-unavailable` path (a posted `OWNER-MERGE-CI-UNAVAILABLE` record) and the runner's `local_equivalent_ci` are the two executable forms of the one policy in `docs/ENGINEERING-POLICY.md` section 8; neither is a second policy.

## 11. Independently verifiable review for security-sensitive merges

Local review evidence, even HMAC-signed, is written and verified by the same OS user the runner runs as: tamper evidence, not independent approval. For a repository the owner marks security-sensitive, set `"independent_github_reviewers": ["<login>", ...]` in its allowlist entry. The runner then also requires, on every pass including the final read, a GitHub review that is `APPROVED` at the exact head by one of those logins, whose latest decisive review it is, whose `authorAssociation` is OWNER, MEMBER or COLLABORATOR, and who is neither the PR author nor the authenticated user. GitHub, not a local file, is the record, so a same-user process cannot mint it. The local evidence file, the checks, server-side protection and all other guards still apply; the key is additive, off by default, and a malformed list refuses.

Limits: the second identity must exist and approve for real (another human, or a GitHub App that approves); this runner never creates or requests that approval. A reviewer's GitHub account can itself be shared or compromised, and the runner cannot tell. Org-level required-review settings remain a GitHub-side control beyond this.

## 12. Measured on a real GitHub repository (2026-10-22, temporary public test repo, deleted afterwards)

A temporary repository (private repositories on a free plan return 403 for branch protection and rulesets, so it had to be public; it held only synthetic files) was used with the founder's authorization. Evidence is kept in the private QA directory (`protection-test-2026-10-22.md`).

| Check | Result |
|---|---|
| `plan` / `apply` / `verify` of required status check `ci`, `enforce_admins=true` | applied, readback matches, runner's own server-side check accepted it (first real-GitHub confirmation) |
| PR with no `ci` status | runner REFUSED (`BLOCKED`, `ci` missing); plain `gh pr merge` refused by GitHub ("base branch policy prohibits the merge") |
| `ci` failing | runner REFUSED |
| `ci` success | runner APPROVE (dry run), then a real runner merge: `MERGED`, merge commit `fe82657…` with parents base+head, read back from GitHub; audit PENDING and MERGED records written; the host did not deny |
| `independent_github_reviewers` configured, no such approval | runner REFUSED |
| `rollback` | deleted, readback 404, repeat is a no-op |
| protection strengthened after `apply` (review requirement added) | `rollback` REFUSED and kept it |

Not shown, so not claimed: `gh pr merge --admin` was not executed (an owner-ask command), so `enforce_admins=true` is confirmed by readback only; no second reviewer identity existed, so the independent-review path was exercised only as a refusal; the review evidence file was a synthetic fixture; the check was a commit status, not an Actions run. This is one merge in a throwaway repository and does not make `MERGE_AUTONOMY_READY` true for any production repository.

## 13. Why merges were denied in one session and allowed in another (measured 2026-10-22)

Failure classes, kept apart: A GitHub authorization, B branch protection, C Claude Code permission rule, D auto-mode soft denial, E auto-mode hard denial, F custom hook, G the runner's own fail-closed refusal. Every denial seen in the Config & Environment session was class D; none was A, B, C, E or F.

| Observation | Source |
|---|---|
| Both sessions ran in the same project directory with the same user settings: `autoMode` is empty (`claude auto-mode config` shows only shipped defaults, `critique` finds no custom rules), `permissions.allow` contains `Bash(gh pr merge:*)`, no merge `ask` or `deny`, no managed settings, Claude Code 2.1.306 | `claude auto-mode config/critique`, `~/.claude/settings.json` |
| The same GitHub identity (Chisanan232, ADMIN) merged `horonomy/internal-docs` #207 and #208 (no reviews, private repository with no protection) with plain `gh pr merge N -R R --merge --match-head-commit SHA` | GitHub merge events, session transcript |
| In that session every merge without `--admin` succeeded (about 14); the only denials, all `[Merge Without Review]`, were `--admin` merges | transcript |
| In the Config session, the same plain shape was denied twice in four days (`[Merge Without Review]` on 10-18, `[Self-Approval]` on #102) and allowed three times on 10-22 in disposable repositories: plain merge, plain merge after posting an OWNER-MERGE-REVIEW comment, and a runner merge | transcript, this section's experiments |
| After a denial, unrelated benign commands in the same turn (a wait loop, reading another transcript) were also denied with a merge label | transcript |

Conclusion, with what is not shown: the rules involved are the shipped soft-deny entries `Merge Without Review` and `Self-Approval`, which the classifier applies by judgment over the transcript, not by command shape or by any user setting, so the outcome is not deterministic and no configuration difference explains the two sessions. A regression of the model or Claude Code is not shown either. The denial text itself says a denial covers the outcome, so after a denial the classifier tends to deny related actions in that turn; that cascade is a reason to stop at the first denial, not a reason to re-route. Two things are known to trigger denials: `--admin`, and writing a positive review stamp on a PR the agent authored in the same turn as the merge (suspected for #102, not reproduced: the same sequence was allowed on a disposable PR).

What a user setting can do: the only documented lever is `autoMode.allow` and `autoMode.environment` text that tells the classifier a specific action is authorized. The agent may not write that text itself; the classifier denies it as instruction poisoning and self-modification, correctly. It is an owner edit, applied through `candidate-rollout.py`, and its effect cannot be measured until it is applied and a fresh session is tested.

### Owner allow registry (SPE-118; mechanism only, nothing deployed)

`workstation-policy.py lint_settings` still rejects every custom `autoMode.allow` entry unless the owner lists that exact string in a registry file (JSON array of strings, at most 3) passed with `--owner-allow-file` to `lint-settings`, `render-settings` and `candidate-rollout.py plan|apply`. A registered entry must name `owner-merge-runner` and `--expect-head`, stay under 400 characters, and must not mention `--admin`, `gh pr merge`, "any/every/all pull requests", or merging without review. Everything else in the candidate is linted as before, `$defaults` stays mandatory, and the rollout keeps its SHA pins, live-key preservation and byte-exact rollback. `plan` lists every registry entry in its findings and reports `registry_sha`; `apply` with `--owner-allow-file` is refused unless `--expect-registry-sha` matches the reviewed file, so a registry edited after review cannot be applied. The registry path itself is not location-restricted (an agent-writable path cannot be reliably detected); the owner's hash pin is the provenance control. The entry lint is a keyword tripwire (printable ASCII only, required tokens, a blocklist of broadening phrases), not a proof that the wording is narrow: no blocklist is complete, so the owner must read the entries `plan` prints before pinning the hash. Every registry entry is linted in `plan`, used by the candidate or not. The agent writes no registry text. An accepted entry is classifier guidance only: the runner and gate remain the enforcement, and the entry proves no review or GitHub authorization. A candidate containing it is not deployed until the owner applies it and a fresh session is tested.

#### Disposition of the registry-lint review finding (owner decision relayed in the 2026-10-23 session; no ticket or ADR record yet, so cite one before relying on this as durable founder consent)

- **Invalid proof assumption, withdrawn.** The keyword lint was never able to prove that a natural-language entry is narrow, and no blocklist can. The requirement that it do so is removed. The lint still refuses an apply when it finds something (its findings are BLOCKING), but it is not a proof of narrowness; a clean lint result is not evidence of safety, and reviews will keep finding phrasings it misses (four rounds did).
- **What is relied on instead.** The owner reads the exact entries `plan` prints, pins `registry_sha` (`--expect-registry-sha`), and the existing candidate, result and live SHA pins, atomic apply, byte-exact rollback and live-key preservation remain. These prove which bytes were reviewed and applied, not that the wording is narrow.
- **Remaining risk (open, not closed).** A reviewed-and-pinned entry that is broader than intended reaches the classifier as guidance. The risk is bounded only by what the runner, the gate and the host rules actually enforce, and by the owner's reading of the text. An accepted entry proves no review and no GitHub authorization.
- **Runtime enforcement that must hold for the entry to be safe** (none of it comes from the entry or the lint):
  1. The only way to merge without a prompt is the validating runner at one stable path, invoked in exactly one literal form: the proposed `/Users/bryant/.claude/bin/owner-merge-runner --repo OWNER/NAME --pr N --expect-head SHA --review-evidence PATH` (section 8, unmeasured), with the runner's own guards (which mirror the gate's conditions; the runner does not call `owner-merge-gate.py`), exact head pin, server-side check read and audit record. The path is not protected from the same OS user that runs the agent, so the host allow rule for that literal form is the only gate, and it has not been measured.
  2. Every other merge route must still ask or be denied: `gh pr merge`, `rtk gh pr merge`, `gh api .../pulls/*/merge` (and the `rtk` twins), `--admin`/`--squash`/`--rebase`/`--auto`, and any GitHub MCP merge tool.
  3. Host denial stays binding and is reported, never rerouted.
- **Measured against the live host, 2026-10-23 (read-only `jq` on rule keys; `settings.json` sha256 begins `9ef7e199`).** Enforcement is **insufficient**:
  - `permissions.allow` contains `Bash(gh pr merge:*)`, `Bash(gh pr *)`, `Bash(rtk gh pr *)` and `mcp__github__merge_pull_request`. A direct merge is therefore already permitted by rule; only the Auto Mode classifier's judgment stands in the way, which section 13 shows is not deterministic. An `autoMode.allow` entry would not narrow this: it can only widen what the classifier permits.
  - `owner-merge-runner` is not installed under `~/.claude/bin` (only `sync-neon-secret` is), so there is no rule that scopes unprompted merges to it.
  - Merge-related asks cover only `--admin` forms and `gh api *pulls/*/merge*`; there is no ask for a plain `gh pr merge`, and `permissions.deny` is empty. Section 13's 2026-10-22 measurement recorded "no merge `ask` or `deny`"; the two statements are unreconciled (the rules may have changed in between, or one reading was incomplete), so re-measure before relying on either.
  - `MERGE_AUTONOMY_READY` is therefore BLOCKED, whatever candidate is applied, until the owner removes the broad merge allows, adds asks for the direct merge routes, installs the runner at its stable path and verifies the exact literal form with `scripts/permission-host-matrix.py`.
- **Negative controls still required before any readiness claim:** direct `gh pr merge` (bare, `rtk`, `--match-head-commit`), `gh api` merge, `--admin`, `--squash`, `--rebase`, `--auto`, a runner call with a wrong or missing pin, a stale head, no review evidence, a non-owner or bot PR without genuine owner approval, red CI, and an unlisted repository must each ask, deny or be refused, on a real host, in a fresh session. Each of these runs is owner-authorized live validation and has not been run.

#### Merge-scope candidate (prepared, not applied)

`python3 scripts/workstation-policy.py render-settings --kind personal --merge-scope` renders `docs/examples/candidate-settings-merge-scope.example.json`: asks (and their `rtk` twins) for the common direct merge shapes: `gh pr merge` (including a double space, and `-R`/`--repo` before `pr` or between `pr` and `merge`), `gh api .../pulls/*/merge`, GraphQL `mergePullRequest`, and the `mcp__github__merge_pull_request` tool. Asks win over allows, so the broad merge allows stop working silently for those shapes without being removed; the candidate adds no allow, changes no `autoMode` and names no runner. `candidate-rollout.py plan` blocks the candidate if those shapes would not ask, checked with the prefix-rule emulator (not the host matcher, which is unmeasured).

Limits, stated plainly: prefix rules cannot cover every shape (a tab, `command gh`, `env X=1 gh`, `sh -c`, an absolute `gh` path, aliases, and a GraphQL merge or auto-merge mutation passed by file or stdin such as `gh api graphql -F query=@f` or `--input`, since those asks match only the literal mutation name in the command text). Those are not asked by a rule and fall to the Auto Mode classifier, and a test pins them as known-uncovered. The runner is invoked as its own command, which matches no ask, so this candidate neither gates nor blocks it; and because an ask beats an allow, the pinned-form `gh pr merge * --merge --match-head-commit *` allow in `owner-merge-pinned-allow.example.json` would still prompt after this is applied. Direct `gh` merges therefore prompt until the owner narrows or removes these asks or adds the runner allow (owner steps). Apply only through `plan` then `apply` with the live, candidate and result SHA pins. `apply` re-serializes the live file (`indent=2`), so its bytes change in form; the backup it writes keeps the original bytes and `rollback` restores them. This narrows enforcement requirement 2 above for the common direct routes; it does not close it, and requirement 1 (the installed runner and its measured host rule) and the negative-control runs remain open and owner-run.

## 14. Private Free capability contract

GitHub Free private repositories return `403 Upgrade to GitHub Pro or make this repository public` for branch protection and for rulesets. That does not stop the merge API (`gh pr merge` works there); it removes the server-side guarantee. The runner now says so instead of pretending:

- Opt in per repository: `"protection_capability": "private-free-app-level"` (with `require_server_side_protection` left true). Without it, a plan-limited repository still refuses.
- The runner accepts the capability only when GitHub's own plan-limit refusal (HTTP 403 and its fixed sentence) comes back for both the rules read and the protection read. If either read works, or fails any other way, the strict server-side check decides, so a repository that can be protected but is not still refuses.
- Everything else still applies: exact head (`--match-head-commit`, enforced by GitHub), observed required checks all SUCCESS, allowlisted review evidence bound to the head, freshness, class rules, final re-read, audit before exec.
- The audit record's `guarantees` field states what is and is not backed: `server_side` is `private-free-app-level` (not `server-enforced`), `review` is `local-evidence-attestation (not a GitHub approval)` unless `independent_github_reviewers` is configured and satisfied (then `github-independent-approval`), `head_pin` is GitHub-enforced, `ci` is `CI_PASS` or `CI_UNAVAILABLE_EXTERNAL`.

Measured on a disposable private repository (2026-10-22): a real runner merge, merge commit `1818b90b30884f6efcd91f515a54f886554d5bd0` with parents base and head read back from GitHub, a PENDING and a MERGED audit record carrying the `guarantees` above. Not shown: a second reviewer identity, a real review (the evidence was a synthetic fixture), an Actions check (the check was a commit status). Same-user evidence is never presented as a GitHub approval.
