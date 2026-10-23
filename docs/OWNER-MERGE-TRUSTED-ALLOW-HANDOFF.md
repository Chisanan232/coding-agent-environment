# Trusted ALLOW: operator handoff (SPE-118)

ASK-first is accepted and live (see [the ledger](OWNER-MERGE-ACCEPTANCE-LEDGER.md)). This adds the one thing that lets a *verified* merge run without a prompt: the
validating `owner-merge-runner`. Every direct `gh pr merge` keeps asking. Nothing here is applied until you run step 4.

## What the three outcomes mean after this

| Route | Outcome | Why |
|---|---|---|
| `owner-merge-runner` in its exact literal form, all runner guards pass | **ALLOW**: runs with no prompt, merges, writes an audit record | one allow rule; the runner is the enforcement boundary |
| Runner invoked, a guard fails (stale head, red CI, missing/invalid evidence, unlisted reviewer, non-owner PR without owner approval, …) | no merge, exit 2, audit record `REFUSED`; the PR stays open | the runner refuses; fix the cause and run it again (not a permanent denial) |
| Direct `gh pr merge` / `rtk gh pr merge` / `gh api …/merge` / GraphQL merge / MCP merge | **ASK**: you answer a prompt; Decline = no merge | the merge asks from ASK-first are untouched; ask beats allow |
| Known-prohibited operations (`--admin`, repository rules, a host deny rule, failed CI at the runner) | **DENY** / refused | non-waivable |

Precedence: deny > ask > allow. The runner command contains no `gh`, so no merge ask matches it; the runner starts `gh pr merge` itself as a subprocess, which the host
permission layer does not see. The plan refuses to apply if an ask or deny would swallow the runner command, or if ASK-first is missing.

## 0. Status

| Flag | Status |
|---|---|
| `TRUSTED_ALLOW_IMPLEMENTED` | PASS (source, tests, scripts) |
| `TRUSTED_ALLOW_DEPLOYED` | NOT_RUN: needs step 4 |
| `RUNNER_ENFORCEMENT_VERIFIED` | PASS for the runner code against real GitHub (ledger section 3B); the deployed path is verified by step 6 |
| `DIRECT_MERGE_ASK_PRESERVED` | PASS on the real host (founder canary; matrix T02-T04 with the rule in a scratch file); re-checked live in step 6 (L3) |
| `AUTONOMOUS_MERGE_CANARY_PASS` | NOT_RUN: step 6 |
| `MERGE_AUTONOMY_READY` | BLOCKED until `TRUSTED_ALLOW_DEPLOYED` and `AUTONOMOUS_MERGE_CANARY_PASS` are PASS |

## 1. Checkout (once; after the PR that adds this page is merged)

```bash
git -C ~/Bryant-Developments/coding-agent-environment fetch origin
git -C ~/Bryant-Developments/coding-agent-environment worktree add --detach ~/Bryant-Developments/cae-operator origin/main   # skip if it exists: git -C ~/Bryant-Developments/cae-operator checkout --detach origin/main
cd ~/Bryant-Developments/cae-operator
```

## 2. Install the runner (one stable path; grants nothing by itself)

```bash
scripts/install-owner-merge-runner.sh
# expect: installed: /Users/bryant/.claude/bin/owner-merge-runner sha256=<64 hex>
```

It copies `scripts/owner-merge-runner.py` to `~/.claude/bin/owner-merge-runner` (mode 0755, atomic), refuses to replace a different existing file unless you pass
`--update`, and re-running it prints `already installed and identical`. Without the allowlist (step 3) the runner refuses every merge.

## 3. The runner allowlist: the registry you review (this is where "registry wording" lives)

The runner reads `~/.claude/owner-merge-runner-allowlist.json`. It must be owned by you, not a symlink, not group/world-writable, and outside any git worktree. It
decides which repositories the runner may merge and what must be true. Create it (the text below is the exact content to review; nothing is generated for you):

```bash
umask 077
cat > ~/.claude/owner-merge-runner-allowlist.json <<'JSON'
{
  "repos": {
    "Chisanan232/coding-agent-environment": {
      "required_checks": ["isolated-validation", "gitleaks", "native-resource-wire"],
      "reviewers": ["independent-review-agent"],
      "require_server_side_protection": true,
      "accept_bypass_actors": false,
      "max_evidence_age_hours": 24,
      "require_signed_evidence": false,
      "protection_capability": "server-enforced"
    }
  }
}
JSON
chmod 600 ~/.claude/owner-merge-runner-allowlist.json
python3 -c 'import json,os; json.load(open(os.path.expanduser("~/.claude/owner-merge-runner-allowlist.json"))); print("valid json")'
```

What to read before you keep it, line by line:

- `repos`: only these repositories can ever be merged by the runner; add a repository by adding an entry, never by loosening one.
- `required_checks`: must equal the real GitHub check names (the three above are what #110-#116 show). A renamed check makes the runner refuse until you edit this.
- `reviewers`: the reviewer id the evidence file must carry. Use one stable id (`independent-review-agent`); the actual agent ids go in the PR's `OWNER-MERGE-REVIEW` comment. It is
  self-attested by the same GitHub identity (the open evidence-independence decision); `require_signed_evidence` stays false until you choose an HMAC key (`evidence_key_file`).
- `require_server_side_protection`: with `true` the runner refuses a repository that GitHub itself does not protect. **`Chisanan232/coding-agent-environment` is public and currently has
  no branch protection or rulesets** (read-only check, 2026-10-23), so with this entry the runner refuses until you enable protection (public repos can on the free plan;
  `scripts/branch-protection-rollout.py` is the existing tool, owner-run). The alternative is an explicit waiver, `"require_server_side_protection": false`, which the audit record
  states; decide that consciously. For a private Free repository use `"protection_capability": "private-free-app-level"` instead of `server-enforced`.
- `accept_bypass_actors`: `false` is the safe default; leave it.

## 4. Plan, then apply the single allow rule (needs your explicit authorization; the only step that edits `~/.claude/settings.json`)

```bash
scripts/trusted-allow-apply.sh            # plan only; changes nothing
# expect:
#   runner: installed at /Users/bryant/.claude/bin/owner-merge-runner and identical to this checkout
#   candidate: lint CLEAN (trusted-allow)
#   plan: live_sha=c6c13ca4...40ad7 result_sha=<new> asks_added=0 allow_added=1 blocking=false
#   PLAN ONLY: nothing was changed. Re-run with --apply to apply exactly this plan.
scripts/trusted-allow-apply.sh --apply    # backup, pinned apply, readback, rollback pointer
# expect: APPLIED and VERIFIED: live sha256 is now <result_sha>   (and the backup/rollback lines)
```

`plan` was run against your live file on 2026-10-23 (before this was merged): `asks_added 0`, `allow_added` exactly one rule, `blocking false`, `autoMode`/hooks/env/model and all
existing allows, asks and denies unchanged (live `c6c13ca4…`, result `1feb301d…`); the script prints fresh values, trust those. The added rule is exactly:

```
Bash(/Users/bryant/.claude/bin/owner-merge-runner --repo * --pr * --expect-head * --review-evidence *)
```

It refuses (exit 1, nothing written) if the runner is missing or differs, ASK-first is absent, or an ask/deny would swallow the runner command.

## 5. Verify the effective configuration (real host, owner-run; 8 headless model calls, no real PR)

```bash
python3 scripts/permission-host-matrix.py --corpus docs/permission-corpus/trusted-allow-corpus.json --live --out /tmp/trusted-allow-live-matrix.json
# expect: "valid": true, "routine_ran_no_prompt": 2 of 2 (T01 runner form, T05 read-only), "protected_held": 3 of 3 (T02 chained merge, T03/T04 direct merges), "protected_leaked": []
```

T06-T08 are recorded residuals, not scored. A measured fact to keep in mind: without the allow rule, T01/T07/T08 also ran unprompted on the scratch run (the Auto Mode classifier
allows a harmless nonexistent program), so step 5 proves the ask side and the chained-command split, not that the allow rule is what lets a *real* runner merge. Step 6 proves that.

## 6. The Trusted ALLOW canary (bounded: one disposable private repo, three runs; this is what makes `AUTONOMOUS_MERGE_CANARY_PASS`)

Use the repo you already have (`Chisanan232/ask-first-canary-20261023160905`, private, no Actions) or a new private one. First add its entry to the live allowlist
(remove it again at the end), with the commit-status check the canary will post:

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/owner-merge-runner-allowlist.json")
d = json.load(open(p))
d["repos"]["Chisanan232/ask-first-canary-20261023160905"] = {"required_checks": ["ci"], "reviewers": ["independent-review-agent"], "require_server_side_protection": True,
    "protection_capability": "private-free-app-level", "accept_bypass_actors": False, "max_evidence_age_hours": 24, "require_signed_evidence": False}
json.dump(d, open(p, "w"), indent=2); os.chmod(p, 0o600)
PY
```

Prepare a PR (your terminal), post a green commit status, and write the evidence file:

```bash
R=Chisanan232/ask-first-canary-20261023160905; D=$(mktemp -d); cd "$D" && gh repo clone "$R" . && git switch -c canary/trusted-$(date +%H%M%S)
echo "trusted $(date -u +%FT%TZ)" > trusted.txt && git add trusted.txt && git commit -qm "trusted canary" && git push -q -u origin HEAD
gh pr create --repo "$R" --base main --head "$(git branch --show-current)" --title "trusted-allow canary" --body "Disposable."
N=$(gh pr view --repo "$R" --json number --jq .number); HEAD=$(git rev-parse HEAD)
gh api -X POST "repos/$R/statuses/$HEAD" -f state=success -f context=ci -f description=canary
mkdir -p ~/.claude/owner-merge-evidence; E=~/.claude/owner-merge-evidence/canary-$N.json
( cd ~/Bryant-Developments/cae-operator && python3 scripts/owner-merge-gate.py evidence-json --repo "$R" --pr "$N" --head "$HEAD" --reviewer independent-review-agent ) > "$E"; chmod 600 "$E"
echo "/Users/bryant/.claude/bin/owner-merge-runner --repo $R --pr $N --expect-head $HEAD --review-evidence $E"
```

In a **fresh Claude Code session** (started after step 4) tell the agent: *"Run this command and nothing else: `<the echoed runner command>`"*. Then record:

| ID | Do | Expected | Record |
|---|---|---|---|
| L1 ALLOW | run the echoed runner command | **no permission prompt**; output JSON `"outcome": "MERGED"`; `gh pr view $N --repo $R --json state` is `MERGED`; `owner-merge-gate.py verify --repo $R --pr $N --head $HEAD` prints `VERIFIED`; a `MERGED` record is appended to `~/.claude/audit/owner-merge-runner.jsonl` | observed / not observed |
| L2 refuse | make a second PR and post evidence for its head, then push another commit, then run the runner with the old head | no prompt; `"outcome": "REFUSED"`; PR stays `OPEN` | observed / not observed |
| L3 ASK kept | in the same session run `gh pr merge <the L2 PR> --repo $R --merge --match-head-commit <its head>` and **decline** | a permission prompt appears; after Decline the PR is `OPEN` | observed / not observed |

`AUTONOMOUS_MERGE_CANARY_PASS` is PASS only if L1, L2 and L3 are observed as written. If L1 shows a prompt or the classifier denies it without one, record that exactly and stop:
the only remaining lever is `autoMode.allow` classifier guidance, which is wording only you write (the checked mechanism is `--owner-allow-file`, see OWNER-MERGE-AUTONOMY.md "Owner allow
registry"); do not add it unless L1 demonstrates the block, and it would still not replace the runner's own checks.

Cleanup after the canary: delete the canary entry from the allowlist, `rm -rf "$D"`, and `gh repo delete "$R"` (needs `gh auth refresh -s delete_repo`; the agent's token has no such scope).

## 7. Day to day (once L1-L3 pass)

After CI is green and the independent review has passed, the agent runs `owner-merge-gate.py check ... --settings ~/.claude/settings.json`, writes the evidence file
with `evidence-json` (reviewer id `independent-review-agent`, the exact current head), and runs the runner in its literal form. A non-owner or bot PR additionally needs your real
GitHub approval first (the runner refuses without it). Any refusal is a message, not a lockout: fix the cause and re-run.

## 8. Roll back (any time)

```bash
scripts/trusted-allow-apply.sh --rollback     # settings back to the exact bytes before the apply (ASK-first stays); expect: ROLLED BACK ...
rm ~/.claude/bin/owner-merge-runner           # optional: remove the program (with the rule gone it is just an unreachable file)
```

Remove an allowlist entry to stop the runner merging one repository without touching settings. The audit log is append-only; keep it.

## 9. Residual risks (what this does not remove)

- The runner's allow rule is a glob: it lets the program start with any arguments in that flag order. The runner validates every argument itself, and the real host held a chained
  `runner … ; gh pr merge …` for a prompt (matrix T02), but this is a boundary you rely on, not a proof for every spelling. A different command shape is simply not allowed and falls to Auto Mode.
- Review evidence is self-attested: the same GitHub identity that runs the runner writes the evidence file, so "independent" means the process was followed. Choosing an HMAC key or a second
  identity is the open evidence-independence decision (OWNER-MERGE-AUTONOMY.md section 8).
- The runner is a copy at a path your own account can write; the allow rule trusts that path. `install-owner-merge-runner.sh` and the apply refusal on a differing file detect drift; they
  do not stop a process running as you from replacing it.
- A public repository without branch protection is refused by default (step 3); waiving that removes GitHub-side enforcement for that repository.
- Auto Mode's classifier is an extra, non-deterministic layer: it may still deny a call the rules allow. That is reported, never rerouted.
