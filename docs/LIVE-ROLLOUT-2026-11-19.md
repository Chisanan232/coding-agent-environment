# Live rollout record, 2026-11-19 (SPE-171 common configuration)

Records what the owner approved and what was applied to the owner's host on 2026-11-19, the source commit, the exact diffs, the backups and the rollback of each. Source commit for every file installed from this repository: `86e46d14f5906c15f71de8bfa470a01bc0b1ccc2` (main after PR #258). Backup directory: `~/.claude/backups/live-rollout-20261119T025941Z` (`SHA256SUMS` inside). The installer's own prior-state copy: `~/.claude/backups/coding-agent-environment/20261119T030006940481Z`.

## Applied

| # | Change | Mechanism | Verified |
|---|---|---|---|
| 1 | Refresh five stale Claude skills (`acceptance-validation`, `decision-delegation`, `feature-implementation` and its `references/quality-policies.md`, `test-design`) | `scripts/profile-install.sh --claude-skills --shared-skills=acceptance-validation,decision-delegation,feature-implementation,test-design` (no `--apply` flag exists; the plain run applies) | `--check`: "Managed Claude practice drift: 0"; the before and after hash of every file under `~/.claude/skills` differs for exactly those five; `~/.claude/CLAUDE.md` hash unchanged. Each live file had equalled an older repo blob, so no local edit was overwritten |
| 2 | Install `~/.claude/agents/haiku-worker.md` | `cp -n` from `.claude/agents/haiku-worker.md` (the installer has no agent support) | `cmp` identical; `opus-architect.md` hash unchanged; fresh session in an unrelated repository lists `haiku-worker` |
| 3 | Remove ten invalid `Write(...)` entries from `permissions.ask` in `~/.claude/settings.json` | Edit of the owner-approved file | The ten `Edit(...)` rules for the same ten paths were already present, so converting in place would only duplicate them; removal keeps every path and the ask behaviour. Parsed diff: 10 removed, 0 added, everything else equal, mode 0600 |
| 4 | Deny the two plugin agents that declare the bare alias `model: haiku` | `permissions.deny`: `Agent(caveman:cavecrew-investigator)`, `Agent(caveman:cavecrew-reviewer)`; `caveman@caveman` stays enabled | Fresh session in an unrelated repository: those two agents are not offered, `cavecrew-builder` is, terse mode is on. Before editing, the same rule passed through `--settings` made a delegation attempt refuse ("blocked by permission rule") with no Haiku usage |

The caveman fix is a narrowing: the only Haiku route left is the admitted, id-pinned `haiku-worker`. Cost of the fix: `cavecrew-investigator` and `cavecrew-reviewer` are no longer available on this host.

## Rollback

1. Skills: copy the five files from `<backup>/skills/` back to `~/.claude/skills/` (the installer's prior-state directory holds the same files).
2. Haiku worker: `rm ~/.claude/agents/haiku-worker.md`.
3. Settings: restore `<backup>/settings.json` (mode 0600), or delete the two `deny` entries and re-add the ten `Write(...)` lines. Restoring re-introduces the invalid rules.

## Routing verification (fresh sessions, unrelated repositories, Claude Code 2.1.325)

| Route | Observation |
|---|---|
| Sonnet main | every run's `modelUsage` shows `claude-sonnet-5-5` |
| `haiku-worker` | forced diagnostic arm: `modelUsage` `claude-sonnet-5-5` plus `claude-haiku-5-5` ($0.0084), six tests passed |
| `opus-architect` | forced arm: `claude-opus-5-5` ($0.97) plus Sonnet |
| Natural delegation | not observed: with the admission policy in the prompt Sonnet implemented the one-function task itself (policy allows that), and designed a queue change itself instead of calling `opus-architect`. This is not a failure of either route and not manufactured; it adds one more not-delegated sample to the pilot's 0 of 6 |
| Opus advisor | `advisorModel: opus` is set in settings; it was not exercised in these headless runs, so its routing is NOT_VERIFIED here |

## Real-route Fable consult (functional verification, not effectiveness)

One consult ran through `scripts/fable-escalation.py` on a real blocker (full unattended merge during a hosted-CI outage, target `Chisanan232/coding-agent-environment#119`). Two Opus attempts with different approaches had ended at the same failure, which made it eligible (`two_distinct_attempts_same_failure`).

- Flow: `evaluate` eligible, `offer`, `answer approve` citing the owner's 2026-11-19 directive item 5 (single validation consult; stored as a hash), `consult --execute`, `verdict`.
- Restrictions observed in the printed command: `--tools Read,Grep,Glob`, `--strict-mcp-config` with an empty config, `--no-session-persistence`, `--max-budget-usd 4`, no API credential variable set (the script refuses otherwise), result accepted only when `modelUsage` is exactly `claude-fable-5-2`.
- Result: completed, 105 s, list-price estimate $1.29 (under the $4 cap); the ledger went OFFERED, APPROVED, CONSULTING, CONSULTED, CLOSED (`advanced`); breaker closed. A second `consult` and a second `offer` for the same blocker were both refused ("one offer and one consult per blocker").
- Not established: whether the consult drew included allowance (the `/usage` meter was not read), and whether Fable reduces rework. There is no Opus-only control, so no benefit is claimed. The advice partly overlaps what one Opus attempt had listed as a candidate; its added value is the reframing in the next section, which is unmeasured.

## Full unattended merge: what is and is not satisfiable

Verified: a class-A owner-authored PR in an allowlisted repository merges through the installed runner without a human prompt (#254 to #258 this campaign, `verify` = VERIFIED). `FULL_MERGE_AUTONOMY_READY` stays NO.

Two independent Opus designs and the Fable consult agree on the blocker: on a repository with a server-side required check, GitHub refuses the pinned merge unless that check really ran and passed, and every other route is excluded (`--admin`, a protection rollback, forged status, a second identity). Local-equivalent evidence cannot move the server. On repositories without server protection (private-free, app-level) the existing local-equivalent path already applies.

Concrete decision for the owner (one packet, nothing started): provision a self-hosted runner fallback so required jobs still execute and publish a real check, which needs (a) the runner registration and an always-on host (a LaunchAgent install was denied earlier), (b) a `runs-on` change reviewed under the CI-definition gate, and (c) confirmation that the outage class does not also stop self-hosted dispatch (NOT_VERIFIED; the billing API returns HTTP 410 to the agent token, so only the owner can read the cause). Fixing the Actions or billing state makes this unnecessary. Alternatively reword the criterion to "zero interaction per merge after one-time provisioning". The non-owner path (needs a second identity), evidence signing and non-allowlisted repositories remain unverified as listed in `OWNER-MERGE-ACCEPTANCE-LEDGER.md`.

## Codex routing 8 to 7 (still pending, not applied)

Commit `795eef6` changed only the repository's desired value (installer constant, ADR 0011 and its tests) from eight to seven; its message says live `~/.codex` was not touched, and it gives no further reason. The seven comes from the engineering contract's subagent ceiling (`engineering-workflow`: "at most seven concurrent subagents ... a ceiling, never a target", set 2026-10-16), which ADR 0011 now says Codex should match ("Treat seven as a concurrency ceiling"). `16d107b` tests that an eight-thread receipt reconciles to seven. Why the contract chose seven rather than eight is not recorded in either place; inferring it from the code would be a guess. Practical effect, stated as expectation not measurement: a Codex session started after the change allows at most seven concurrent agent threads instead of eight; running sessions keep their value. Live config and receipt still say eight, `--routing --check` reports the drift, and the change stays pending as instructed; rollback of the apply is to restore the two backed-up files (see the owner apply packet), not `--remove`.
