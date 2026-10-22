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
| B | verified dependency bot | real GitHub APPROVE at the exact head from the authenticated owner identity, as the gate requires (`scripts/owner-merge-gate.py`); one at a time per repo; reconcile and re-gate after each merge. |
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
| C. Keep the per-merge `!` run | The agent prints the pinned command; the owner runs it with the `!` prefix | No new exposure; one owner action per merge. This is the current supported fallback |
| D. Apply section 2 knowingly | Accept the residuals above | Not recommended: the holes are open-ended and some are exfiltration paths |

The founder selected A. Until the runner's allow rule is applied and verified, the supported state is C, and the live broad `Bash(gh pr merge:*)` allow remains the unresolved exposure it is today.

## 7. What is and is not claimed

Designed and unit-tested in this repository; the classifier behavior above is measured on the owner's workstation. LIVE_DEPLOYED and LIVE_VALIDATED stay false until the owner applies the runner allow rule (section 8) and a disposable-PR run succeeds. This document is not permission to bypass any earlier host denial and must not be read as `MERGE_AUTONOMY_READY`.

## 8. Option A: the validating merge runner (implemented, not deployed)

`scripts/owner-merge-runner.py` (stdlib Python 3, `tests/test-owner-merge-runner.py`, wired into `capability-validation.yml`):

```
owner-merge-runner.py --repo OWNER/NAME --pr N --expect-head SHA40 --review-evidence PATH [--dry-run]
```

What it enforces in code, each with a negative-control test:

- Structured arguments only. `--repo`, `--pr` and `--expect-head` are matched by strict regexes before any use (shell metacharacters, trailing newline, uppercase hex, wrong length all refuse before gh is started). `gh` is resolved once as an absolute path, run with `shell=False`, and every call passes an argv-shape check: only `gh api user`, `gh pr view ... --json <fixed field list>` and the single merge argv are accepted. There is no flag to choose another `gh`; tests inject a fake through the module API only.
- Guards (all must hold; every reason is reported): PR OPEN and not draft; head equals the pinned `--expect-head`; `mergeable == MERGEABLE` and `mergeStateStatus == CLEAN`; no unresolved `CHANGES_REQUESTED` (latest decisive review per reviewer, plus `reviewDecision`); every check in the rollup completed with `SUCCESS` (empty, pending, skipped or neutral all refuse, SonarCloud included when present); repo on the allowlist file; a review-evidence JSON whose `head` equals the pinned head and `verdict` is `PASS` (the runner only reads it, never writes it); any PR field missing or malformed refuses.
- PR class (section 3): A author equals the authenticated `gh` user; B dependency bot on a bot branch in the same repository, with an APPROVED review by the authenticated user at the exact head; C an APPROVED review at the exact head by a non-author whose `authorAssociation` is OWNER, MEMBER or COLLABORATOR; a cross-repository (fork) Class C PR additionally needs the authenticated user's own APPROVED review at the exact head.
- One mutating command, byte-for-byte: `gh pr merge N --repo R --merge --match-head-commit SHA`. Never `--admin`, `--squash`, `--rebase`, `--auto` or `--delete-branch`. The head is re-read immediately before it runs. One attempt only: a gh refusal is recorded as `DENIED` (or `ERROR`) and the runner exits nonzero without retrying, trying another route or touching settings.
- After a zero exit the PR is read back; anything other than `MERGED` with a merge commit is `UNVERIFIED` and fails.
- Append-only JSONL audit (fixed path `~/.claude/audit/owner-merge-runner.jsonl`, mode 0600, secret-shaped text redacted) with a `PENDING` record written before the merge; if the audit cannot be written, no merge happens.
- Exit codes: 0 success or validated dry run, 2 refused, 3 denied, 4 error, 5 unverified, 64 usage.

The allowlist is required and fixed: `~/.claude/owner-merge-runner-allowlist.json` (never loaded from the repo; there is no CLI override for it, the audit log path or `gh`; only the module API used by tests can pass others). Before it is trusted the runner checks it is a regular non-symlink file, owned by the current user, not group/world-writable (same for its directory), and not inside a git worktree, so an agent-writable checkout cannot widen scope. The runner refuses until the owner creates it. `docs/examples/owner-merge-runner-allowlist.example.json` shows the format (`{"repos": ["OWNER/NAME"]}`) and is refused if pointed at, being in a worktree.

Mutation check (removed one guard at a time in a scratch copy; the suite failed every time): head pin, pre-exec head re-check, check status, empty rollup, merge argv gaining `--admin`, argv-shape check disabled, `mergeStateStatus`, changes-requested, allowlist, evidence verdict, evidence head, draft, argument-syntax validation, post-merge read-back, audit 0600, secret scrub, class C approval. 23 of 23 killed (this list plus class C association, cross-repository rule, and the allowlist symlink, owner, mode, worktree and default-path guards).

### Owner step still open: the host permission (unmeasured)

The runner does not change what the host asks. To run it without a per-merge prompt, the owner would copy the script to one stable path and allow only that literal form. Proposed rule (user scope, owner-applied; not applied, not measured):

```
Bash(/Users/bryant/.claude/bin/owner-merge-runner --repo * --pr * --expect-head * --review-evidence *)
```

invoked exactly as `/Users/bryant/.claude/bin/owner-merge-runner --repo OWNER/NAME --pr N --expect-head SHA --review-evidence PATH` (no `python3`, no `rtk` prefix, no `cd`, no env prefix; the rtk-rewritten spelling may need its own rule). Whether the host matches that form, normalizes it, or still reaches the classifier is **unmeasured** until verified with `scripts/permission-host-matrix.py` against a corpus of this exact command (plus negative controls) on the real host. Use `--dry-run` only for such probes; do not point a probe at a real PR.

### Binding limits, residuals

- Explicit host denial stays binding. If the host denies the runner call, report the exact operation and stop; do not re-shape it, route around it or ask a subagent. The runner is not a bypass of the Auto Mode classifier, including Self-Approval and Merge Without Review (#59, #60): an allow rule may clear the classifier for this literal form only if the owner applies and verifies one, and nothing here asserts that it would.
- The evidence file is only as independent as whoever wrote it; the runner checks head and verdict, not the reviewer's identity or the review's quality. Class A and the evidence record both rest on one shared GitHub identity.
- Checks are trusted as GitHub reports them; a repository with no required checks (empty rollup) is refused rather than merged.
- Branch protection and bypass-actor settings are not read; GitHub remains the final authority and `--match-head-commit` fails a stale pin there.
- The runner never uses `--admin`, so a same-identity `REVIEW_REQUIRED` deadlock is not resolved by it (the separate owner-only exception is out of scope).
- The evidence path is read as given; whoever can write that file can supply a PASS, so it must come from the independent review, not the merging agent. The allowlist is checked for ownership, mode, symlink and worktree location, but an actor running as the same OS user can still edit a file in `~/.claude`; the check removes the agent-writable-checkout path, not same-user tampering.
- Nothing here was exercised against a real PR. Verified: unit tests with a fake `gh`. Not verified: real `gh` JSON field behavior for every PR shape (for example bot author spelling), real merge, host matching.
