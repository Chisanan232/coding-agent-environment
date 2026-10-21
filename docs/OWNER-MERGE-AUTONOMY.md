# Owner PR merge autonomy (SPE-118)

Status: POLICY_DESIGNED, PREDEPLOY partly verified. Not live. Nothing here edits live settings; the owner applies the diff.

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
- GitHub side, read-only check on 2026-10-20 of PRs merged in the previous 36 hours: 47 PRs across 9 repositories, every one with author equal to merger (the owner account) and `reviewDecision` NONE. GitHub therefore never required a formal approval for owner-authored PRs here; the gate was the host. The agent and the owner share one GitHub identity, so `mergedBy` cannot tell an agent merge from a manual one, and a shared identity is not proof of independent review. The nine PRs the owner merged by hand after host denials are closed evidence and are not reopened or replayed.
- Denied `gh pr review --approve` on agent-authored PRs is the correct built-in Self-Approval protection. It is not a fault and is never worked around: Class A needs no formal self-approval.

Conclusion: the block is the built-in classifier, reached only because the command shape missed the allow rule. The current broad allow `Bash(gh pr merge:*)` makes the outcome depend on shape, and also lets `--admin` and `--squash` through unreviewed. Only hard_deny #0 is unconditional; the merge rules are soft_deny and are overridable by an allow rule or explicit user intent.

## 2. Smallest supported fix

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

`scripts/permission-host-matrix.py` (SPE-119) was run against these rules with `docs/permission-corpus/owner-merge-corpus.json` (47 commands, every target a nonexistent repository; hook-recorded ground truth, matched to the tested command; zero probe errors; results in `docs/permission-corpus/owner-merge-results-2026-10-22.json`).

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

The stronger design is a merge runner that re-validates the exact argument vector (40-hex pin, repository, PR number, `merge` strategy only) and the gate verdict in code before running `gh pr merge`, with the allow rule covering that program only. It moves the bypass surface into code that can be tested exhaustively. It is **not built here**: ADR 0013 makes the gate read-only and never-merging, and an allow rule over a merge-executing program is a policy change for the owner to decide, not an implementation detail. Until then the supported fallback is unchanged: if the host asks or denies, the owner runs the printed command with the `!` prefix.

## 3. PR classes

| Class | Author | Needed before the pinned merge |
|---|---|---|
| A | verified owner account, owner is ADMIN (or code owner with WRITE/MAINTAIN) | independent PASS comment `OWNER-MERGE-REVIEW head=<sha> verdict=PASS blocking=0 reviewer=<id>` at the current head; gate `PASS`. No formal self-approval. |
| B | verified dependency bot | real GitHub APPROVE at the exact head from the authenticated owner identity, as the gate requires (`scripts/owner-merge-gate.py`); one at a time per repo; reconcile and re-gate after each merge. |
| C | any other human | owner's genuine APPROVED review at the exact head, never submitted on the owner's behalf. |

A new commit changes the head, so earlier evidence no longer matches and the gate refuses until a fresh review is recorded.

## 4. Host denial is binding

If the host refuses the standard form, report the exact operation (secrets redacted) and stop. Do not re-run it with another shape, tool, script, subagent or operator. The owner may run the printed command with the `!` prefix, and that is the supported path. A denial is `HARNESS_PERMISSION_BLOCK`, not evidence the PR is unsafe, and not a product decision.

## 5. Install, verify, rollback (owner-run; protected file)

1. Back up and pin: `cp -p ~/.claude/settings.json ~/.claude/backups/settings.json.$(date +%Y%m%d-%H%M%S)`; record `shasum -a 256` of the live file and of the backup.
2. Apply the JSON block in section 2 through `/permissions` (remove the three listed allows, add the listed allows and asks), or edit the three arrays by hand. `scripts/candidate-rollout.py` does not apply this block: it only adds asks and the scoped `rtk proxy` allows from SPE-119, and it preserves existing broad allows. Do not apply while an active session may be affected.
3. Verify the effective policy in a **new** session (`/permissions`): the allow list has no `gh pr merge:*` and no `gh pr *`; the ask list has the `--admin` rule.
4. Negative controls on fixtures only (nonexistent repo): `--admin`, `--squash`, a doubled `--match-head-commit` and the `rtk` spellings of each prompt. Compound forms are evaluated per subcommand by the host, so do not rely on a compound being refused; the gate only ever prints the simple form.
5. Positive control on the disposable owner PR described in `docs/OWNER-MERGE-LIVE-VALIDATION-PLAN.md`.
6. Resumed sessions keep the policy loaded at start; start a new session after any change.

Rollback: check that `shasum -a 256` of the backup equals the recorded pin, copy it over `~/.claude/settings.json`, start a new session.

## 6. What is and is not claimed

Designed and unit-tested in this repository; the classifier behavior above is measured on the owner's workstation. LIVE_DEPLOYED and LIVE_VALIDATED stay false until the owner applies the diff and the disposable-PR run succeeds. This document is not permission to bypass any earlier host denial and must not be read as `MERGE_AUTONOMY_READY`.
