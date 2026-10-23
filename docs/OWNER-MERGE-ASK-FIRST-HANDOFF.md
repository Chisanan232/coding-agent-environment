# ASK-first merge deployment: operator handoff (SPE-118)

This is the one procedure to deploy the ASK-first candidate. Background and the full model are in
[OWNER-MERGE-OPERATOR-GUIDE.md](OWNER-MERGE-OPERATOR-GUIDE.md). Nothing here has been applied to your live settings.

## Status (read this first)

> **Update 2026-10-23:** the founder accepted this deployment after a real-host canary (`ASK_FIRST_DEPLOYED`, `HOST_PROMPT_VERIFIED`, `ASK_FIRST_ACCEPTED` = PASS; evidence and the observer of each fact in [OWNER-MERGE-ACCEPTANCE-LEDGER.md](OWNER-MERGE-ACCEPTANCE-LEDGER.md)). The table below is the pre-deployment state kept for the record. Next: [OWNER-MERGE-TRUSTED-ALLOW-HANDOFF.md](OWNER-MERGE-TRUSTED-ALLOW-HANDOFF.md).

| Flag | Status | Meaning |
|---|---|---|
| `ASK_FIRST_READY` | **PASS** | Candidate generator output equals the committed example and lints CLEAN; a read-only plan against your live file is non-blocking (35 asks added, 0 allows added, `autoMode` and every other key untouched); `scripts/ask-first-apply.sh` is tested end to end on scratch settings |
| `ASK_FIRST_DEPLOYED` | **NOT_RUN** | Nothing was applied. Needs your authorization (step 3) |
| `HOST_PROMPT_VERIFIED` | **NOT_RUN** | A plan, a lint run and the repository's rule emulator do **not** prove the real Claude Code host shows a prompt. Only step 5 can |
| `TRUSTED_ALLOW_READY` | **NOT_RUN** | No ALLOW path exists. See "What reaching ALLOW needs" |
| `MERGE_AUTONOMY_READY` | **BLOCKED** | Requires the four rows above |

Fresh values measured 2026-10-23 on `main` `6df22b2`: live settings sha256 `9ef7e199108614e9438cf6466d6028c0a99124a87bbab0e66e52e7f274f78c1b`
(unchanged since the earlier checkpoint), result sha256 `c6c13ca48477007dad25d0bb8ee62fdc603fee1be12f9d04f1b406a40ed40ad7`. The script recomputes both
every run; trust what it prints, not this page.

## 1. Get an up-to-date checkout (once; do this after the PR that adds this page and `scripts/ask-first-apply.sh` is merged)

Until then neither file exists on `origin/main`. If you need it earlier, check out branch `full/SPE-118/docs/ask_first_handoff` instead of `origin/main`.

```bash
git -C ~/Bryant-Developments/coding-agent-environment fetch origin
git -C ~/Bryant-Developments/coding-agent-environment worktree add --detach ~/Bryant-Developments/cae-operator origin/main
cd ~/Bryant-Developments/cae-operator
```

## 2. Plan (read-only; safe to run any time)

```bash
scripts/ask-first-apply.sh
```

Expected, with your current live file:

```
candidate: generator output == committed example, lint CLEAN
plan: live_sha=9ef7e199...78c1b result_sha=c6c13ca4...40ad7 asks_added=35 allow_added=0 blocking=false
PLAN ONLY: nothing was changed. Re-run with --apply to apply exactly this plan.
```

If `live_sha` differs from the one above, your settings changed after this page was written: that is fine, the script plans against whatever is there.
Stop and ask if `blocking=true`, `allow_added` is not `0`, or the first line says `REFUSED`.

## 3. Apply (needs your explicit go-ahead; this is the only step that edits `~/.claude/settings.json`)

Run it in your own terminal, or in Claude Code with the `!` prefix. The agent does not run it.

```bash
scripts/ask-first-apply.sh --apply
```

What it does, in order, all pinned to the hashes it just printed: regenerates and lints the candidate, plans, copies the live file to
`~/.claude/backups/settings.pre-ask-first.<timestamp>.json` (0600), applies through `candidate-rollout.py` (which refuses if the live file changed since the
plan and writes its own exact-bytes backup `settings.<first 12 hex of live_sha>.json`), reads the file back and checks the result hash, and records the
rollback pointer in `~/.claude/backups/ask-first-rollback.json`. Expected tail:

```
APPLIED and VERIFIED: live sha256 is now c6c13ca4...40ad7
backup (exact original bytes): /Users/bryant/.claude/backups/settings.9ef7e1991086.json
extra copy: /Users/bryant/.claude/backups/settings.pre-ask-first.<timestamp>.json
rollback: scripts/ask-first-apply.sh --rollback
NEXT: start a fresh Claude Code session; ...
```

Running `--apply` again prints `already applied: nothing to do` and changes nothing.

Change set: **35 `permissions.ask` entries added, 0 allows, 0 denies, 0 `autoMode`, 0 hooks/env/model changes.** The broad merge allows stay in the file; an ask
beats an allow, so they stop running a merge silently. Rules added: see OPERATOR-GUIDE section 3.

## 4. Read-back (also automatic in step 3)

```bash
python3 scripts/candidate-rollout.py verify --live ~/.claude/settings.json --expect-result-sha c6c13ca48477007dad25d0bb8ee62fdc603fee1be12f9d04f1b406a40ed40ad7
# expect: "verdict": "VERIFIED"   (use the result_sha the script printed if it differs)
python3 scripts/owner-merge-gate.py check --repo OWNER/REPO --pr N --owners OWNER --settings ~/.claude/settings.json
# expect for a PR whose gate passes: "decision": {"outcome": "ASK", ... "resolver": "host_prompt"}, never DENY
```

## 5. Verify the real host prompt: one bounded disposable canary (this is what turns `HOST_PROMPT_VERIFIED` to PASS)

Bounds: one private repository, one PR, at most two merge attempts, deleted at the end. Run the setup in your own terminal. Needs a **fresh** Claude Code session
started after step 3 for the two merge attempts. No CI exists in the canary, so the gate would say `NO_CI_EVIDENCE`; that is irrelevant here because this tests
the host prompt, not the gate.

```bash
OWNER=Chisanan232; REPO=ask-first-canary-$(date +%Y%m%d%H%M%S)
gh repo create "$OWNER/$REPO" --private --add-readme --description "SPE-118 disposable ASK-first canary; delete after"
D=$(mktemp -d); cd "$D" && gh repo clone "$OWNER/$REPO" && cd "$REPO"
git switch -c canary/one && echo "canary $(date -u +%FT%TZ)" > canary.txt && git add canary.txt && git commit -m "canary"
git push -u origin canary/one
gh pr create --repo "$OWNER/$REPO" --base main --head canary/one --title "ASK-first canary" --body "Disposable."
PR=$(gh pr view canary/one --repo "$OWNER/$REPO" --json number --jq .number)
HEAD=$(gh pr view "$PR" --repo "$OWNER/$REPO" --json headRefOid --jq .headRefOid)
echo "gh pr merge $PR --repo $OWNER/$REPO --merge --match-head-commit $HEAD"      # the exact command used below
```

In the fresh Claude Code session, tell the agent exactly: *"Run this command and nothing else: `<the echoed command>`"*, then:

| ID | Do | Expected | Record |
|---|---|---|---|
| H1 | Run the echoed merge command; **decline** the prompt | A permission prompt shows that exact command. After declining: `gh pr view $PR --repo $OWNER/$REPO --json state --jq .state` prints `OPEN` | observed / not observed |
| H2 | Run it again; **approve once** ("Yes", not a "don't ask again" option) | Merged: state `MERGED`; `python3 scripts/owner-merge-gate.py verify --repo $OWNER/$REPO --pr $PR --head $HEAD` prints `VERIFIED` | observed / not observed |
| H3 | (before H2) `gh pr view $PR --repo $OWNER/$REPO --json mergeable`, `gh pr checks $PR --repo $OWNER/$REPO`, `git status` | No prompt | observed / not observed |

`HOST_PROMPT_VERIFIED` is PASS only if H1 and H2 were both observed as written. If instead the command ran with no prompt, or the Auto Mode classifier denied it
without a prompt, record that exactly and stop: that is the finding, and it means ASK-first alone does not give a prompt on this host.

Cleanup. Run it in the same shell as the setup (it uses `$OWNER`, `$REPO` and `$D`). `--yes` only skips `gh`'s own confirmation; your host may still show its own prompt for `gh repo delete`, and if it does, approving it is expected:

```bash
gh repo delete "$OWNER/$REPO" --yes; cd ~; rm -rf "$D"
```

## 6. Roll back (any time)

```bash
scripts/ask-first-apply.sh --rollback
# expect: "restored": true, then: ROLLED BACK: live file restored to sha256 9ef7e199...78c1b
```

It restores the exact original bytes and refuses if the live file's hash changed after the apply. That check is a hash comparison, not a lock: a write landing in the instant between the check and the file replace is not prevented, so do not edit `settings.json` (or run another Claude Code session that does) while you apply or roll back. Manual equivalent:

```bash
python3 scripts/candidate-rollout.py rollback --live ~/.claude/settings.json \
  --backup ~/.claude/backups/settings.9ef7e1991086.json \
  --expect-backup-sha 9ef7e199108614e9438cf6466d6028c0a99124a87bbab0e66e52e7f274f78c1b \
  --expect-live-sha c6c13ca48477007dad25d0bb8ee62fdc603fee1be12f9d04f1b406a40ed40ad7
```

## 7. Two different approvals (do not conflate them)

| | GitHub approval | Host tool approval (the prompt) |
|---|---|---|
| What it is | A genuine `APPROVED` GitHub review on the exact current head | Claude Code asking whether to run one specific command |
| Who gives it | The repository owner, personally, on GitHub | You, answering the prompt |
| When it is required | Class B (dependency bot) and class C (any non-owner) PRs. **Not** for your own PRs | After step 3, before every direct `gh pr merge`, including your own PRs |
| Can the agent supply it | No, never | No: it asks and waits |
| Does it satisfy the other | No | No |
| If missing | gate decision `ASK`, resolver `owner_github_approval` | gate decision `ASK`, resolver `host_prompt` |
| If declined | the PR stays unmerged; GitHub's rules still bind | no merge runs; nothing is permanently denied |

A host prompt is a request, not a denial: declining leaves the PR open and you can ask again. A DENY is reserved for non-waivable constraints: a failed check, a cross-repository or fork head,
a closed PR, repository rules, a repository or identity trust failure (unauthorized or non-canonical repo, no merge permission), a host deny rule, or a gate error.

## 8. What reaching ALLOW needs (`TRUSTED_ALLOW_READY`; deliberately not part of this deployment)

Today every owner merge is ASK. ALLOW needs, all owner decisions, none done: (1) install `owner-merge-runner` at one stable path; (2) add its single literal allow
rule and measure that exact form with `scripts/permission-host-matrix.py` on your host; (3) decide the evidence-independence option (review evidence is still
written by the same identity that runs the gate); (4) the negative-control runs in OPERATOR-GUIDE section 7. Until then keep ASK-first.
