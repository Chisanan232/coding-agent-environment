# SPE-118 owner-merge acceptance ledger

One row per acceptance fact, with who observed it and how. Observer tags: **founder-observed** (a person saw it on the real host), **agent-observed**
(the agent ran it and read the result; it states which layer), **emulator** (the repository's prefix-rule matcher; proves nothing about the host),
**not run**. A founder-observed row is never rewritten as agent-observed or the reverse. Dates are 2026-10-23.

## 1. ASK-first (accepted by the founder)

| Fact | Status | Observed by | Evidence |
|---|---|---|---|
| ASK-first candidate applied to the live `~/.claude/settings.json` | **PASS** | agent-observed (read-only hash); applied with `scripts/ask-first-apply.sh --apply` (rollback record dated 16:02) | live sha256 is `c6c13ca48477007dad25d0bb8ee62fdc603fee1be12f9d04f1b406a40ed40ad7`, equal to the plan's `result_sha`; `~/.claude/backups` holds `ask-first-rollback.json` (0600) and `settings.pre-ask-first.20261023-160255.json` (original, sha `9ef7e199…`) |
| Real host shows a prompt for a head-pinned direct merge, and allowing once merges | **PASS** | **founder-observed** | private repo `Chisanan232/ask-first-canary-20261023160905`, PR #1: prompt seen, "Allow once", `MERGED` at 2026-10-23T08:10:21Z |
| Declining the prompt rejects the tool call and nothing merges | **PASS** | **founder-observed** | same repo, PR #2: prompt seen, "Deny", tool execution rejected by the host, PR still `OPEN`, `mergedAt=null` (agent re-read: `OPEN`) |
| Read-only `gh` commands produce no merge permission prompt | **PASS** | **founder-observed** (canary) and agent-observed (real-host matrix T05, section 3) | unrelated command failures during the canary were not permission failures |
| `ASK_FIRST_DEPLOYED`, `HOST_PROMPT_VERIFIED`, `ASK_FIRST_ACCEPTED` | **PASS** | founder decision | These accepted tests are not rerun unless the relevant settings or the host's behavior change |

### Reconciling "35 ASK rules" with "16 merge rules"

Neither number is wrong; they count different things. From the live file and the pre-apply backup (both read, nothing changed):

| Count | Value | What it is |
|---|---|---|
| Asks in the rendered candidate file | 37 | 18 bare `Bash(...)` + their 18 `rtk ` twins + 1 `mcp__github__merge_pull_request` |
| Of those, already present before the apply | 2 | `Bash(gh api *pulls/*/merge*)` and its `rtk` twin |
| **Added by the apply** | **35** | 37 − 2; total asks 231 → 266 |
| Merge-related asks in the live file now | 41 | 6 that existed before (4 `--admin` forms + the 2 `gh api` ones) + 35 added |
| …of which `gh pr … merge` shapes | 34 | 17 bare + 17 `rtk` (15 added shapes + 2 `--admin` forms, each with a twin) |
| Bare (non-`rtk`) merge asks | 20 | |
| Ask rules whose pattern matches the canary's exact command | 2 | one per spelling (`gh pr merge:*` and `gh pr merge *`; `rtk` spelling likewise) |

I could not reproduce "16" exactly from the file; it is most likely a different filter on the same rules (for example bare `gh pr … merge` shapes without the two
`--admin` forms plus one more, or the rules shown in a permissions view). The number is not behavior: what matters is that at least one ask matches each merge
spelling, which the canary confirmed on the real host. No setting was changed to reconcile counts.

## 2. Trusted ALLOW: what is implemented

| Piece | Status | Where |
|---|---|---|
| Validating runner (structured args, exact head pin, evidence, checks, class rules, audit) | implemented, unit-tested (`tests/test-owner-merge-runner.py`) | `scripts/owner-merge-runner.py` |
| Single allow-rule candidate, lint exception limited to exactly that rule, rollout guard (needs ASK-first, no ask/deny may swallow the runner command) | implemented, tested | `scripts/workstation-policy.py`, `scripts/candidate-rollout.py`, `tests/test-trusted-allow.py` |
| Runner installer; apply/rollback wrapper; evidence-file helper | implemented, tested on scratch | `scripts/install-owner-merge-runner.sh`, `scripts/trusted-allow-apply.sh`, `owner-merge-gate.py evidence-json` |
| Deployed to the live host | **not deployed** | needs owner authorization |

## 3. Trusted ALLOW: what was observed (agent-observed, no live settings change)

**A. Real Claude Code permission matcher** (`scripts/permission-host-matrix.py --candidate`, headless `claude -p --permission-mode auto`, a scratch file holding
only the live permission rules plus the runner allow rule; user settings excluded; corpus `docs/permission-corpus/trusted-allow-corpus.json`; probes use a nonexistent
repository and `--dry-run`):

| ID | Command shape | Observed |
|---|---|---|
| T01 | the runner's literal allow form | ran, no prompt |
| T02 | the runner form `;` a direct `gh pr merge` | **prompted** (the host splits compound commands; the glob does not let the chained merge through) |
| T03 / T04 | direct pinned `gh pr merge`, plain and `rtk` | **prompted** (ASK-first preserved with the runner allow present) |
| T05 | `gh pr view … --json mergeable` | ran, no prompt |
| T06 | runner form missing `--review-evidence` | not run, no prompt (the host refused without asking) |
| T07 / T08 | `python3 <runner> …`; another program in the same directory | ran, no prompt |

Control (same matrix, scratch file **without** the runner allow rule): T01, T07 and T08 also ran with no prompt. So these probes cannot show that the allow
rule is what lets the runner run: for a nonexistent program the Auto Mode classifier already allows it. T07/T08 are baseline Auto Mode behavior, not caused by the new
rule. Whether the allow rule is what lets a real, installed runner merge without a classifier denial is observable only in the live canary (handoff step 6).

**B. Runner enforcement against real GitHub** (the real runner code, called through its module API with a scratch allowlist and audit path, against the disposable
private repo above; nothing under `~/.claude` was touched):

| ID | Scenario | Observed |
|---|---|---|
| E2 | required check `ci` is FAILURE | refused (exit 2), PR #3 stayed OPEN |
| E3a | expected head differs from the PR head after another push | refused, PR #4 stayed OPEN |
| E3b | evidence was recorded for the old head | refused, PR #4 stayed OPEN |
| E4 | reviewer id not in the allowlist | refused, PR #5 stayed OPEN |
| E5 | required check missing | refused, PR #6 stayed OPEN |
| E6 | evidence file missing | refused, PR #7 stayed OPEN |
| E1a | eligible PR, `--dry-run` | validated, **no merge** (PR #8 OPEN) |
| E1b | eligible PR, real run | **MERGED** (PR #8, 2026-10-23T08:26:08Z); `owner-merge-gate.py verify`: VERIFIED, merge commit has two parents, the reviewed head is one |

Audit (scratch audit file): 9 records for 8 runs: 6 `REFUSED`, 1 `DRY_RUN`, and for the merge a `PENDING` record written before the call followed by `MERGED`
with `merge_attempted: true`. The merge record's `guarantees` say what is and is not backed: `ci: CI_PASS`, `head_pin: github-enforced (gh pr merge --match-head-commit)`,
`review: local-evidence-attestation (not a GitHub approval)`, `server_side: private-free-app-level`. The check was a commit status (no Actions on the repo).

The five refused canary PRs (#3-#7) were closed by the agent. The agent's token has no `delete_repo` scope, so the canary repo itself is left for you to delete.

## 4. Not observed (stays NOT_RUN)

- Non-owner PR without owner approval on a real repo: needs a second GitHub identity; covered by unit tests and the gate's decision tests only.
- The runner allow rule on the live host with an installed runner (no prompt, merge succeeds, no classifier denial): needs the apply (owner authorization).
- Any live-settings change for Trusted ALLOW: none was made.

## 5. Flags

| Flag | Status |
|---|---|
| `ASK_FIRST_ACCEPTED` | PASS (founder) |
| `TRUSTED_ALLOW_IMPLEMENTED` | PASS (source, tests, candidate, scripts; merge pending review at the time of writing, see the PR) |
| `TRUSTED_ALLOW_DEPLOYED` | NOT_RUN |
| `RUNNER_ENFORCEMENT_VERIFIED` | PASS for the real runner code against real GitHub (section 3B); not for the deployed `~/.claude/bin` path |
| `DIRECT_MERGE_ASK_PRESERVED` | PASS on the real host for the accepted ASK-first settings (founder) and with the runner allow rule present (matrix T02-T04, scratch settings); not yet with the rule live |
| `AUTONOMOUS_MERGE_CANARY_PASS` | NOT_RUN (needs the live apply, handoff step 6) |
| `MERGE_AUTONOMY_READY` | BLOCKED until the two NOT_RUN rows above pass |
