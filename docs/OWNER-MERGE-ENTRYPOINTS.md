# Merge entrypoints and the canonical executor (SPE-118)

Goal: an eligible owner-authored PR merges without a founder prompt, whichever interface the agent happens to prefer, and nothing becomes a blanket merge
permission. The mechanism is **one executor, not one permission per spelling**.

## The canonical executor

`scripts/owner-merge-runner.py`, installed at `~/.claude/bin/owner-merge-runner`. It is the only command the single Trusted ALLOW rule names, and it
re-validates everything itself before it starts the one mutating call, `gh pr merge N --repo R --merge --match-head-commit SHA`:

| Guard | Where decided |
|---|---|
| Repository is in the owner's runner allowlist (`~/.claude/owner-merge-runner-allowlist.json`, owner-reviewed) | runner |
| Author class: A = the authenticated owner identity (no GitHub approval needed), B = verified dependency bot, C = anyone else; B and C need a genuine `APPROVED` review at the exact head | runner (`decide`) |
| Exact current head equals `--expect-head`; GitHub enforces it again with `--match-head-commit` | runner + GitHub |
| Independent engineering review evidence for that head (reviewer id on the allowlist, not older than the allowlist's limit) | runner |
| The allowlisted required checks are green on that head; no `CHANGES_REQUESTED`; PR open, not draft, mergeable and clean | runner |
| Repository restrictions (server-side protection, or the honest `private-free-app-level` mode when GitHub's plan forbids them) | runner |
| Audit record before and after (`PENDING`, then `MERGED`) | runner |

`scripts/owner-merge-gate.py check --settings <user settings>` is the single *decision* step. It prints `merge_command` and `executor`:

| Situation | `executor` | Outcome |
|---|---|---|
| Gate PASS, runner installed, repo allowlisted, host rule allows exactly the runner command | `owner-merge-runner` | **ALLOW** (also prints the `evidence_path` to write after the independent review) |
| Gate PASS, any of those missing (for example ASK-first only) | `direct-gh` (pinned `gh pr merge`) | **ASK**: the host prompts, a human answers |
| Non-owner PR without the genuine owner approval, CI pending, unresolved review | none | **ASK** with a named resolver; no command is printed |
| Failed CI, fork head, closed PR, repository rules, host deny rule, gate error | none | **DENY** |

`ALLOW` means allowed by the settings layers passed with `--settings`; pass every layer you have (a project or managed deny still blocks, which fails safe). The review evidence is self-attested and unsigned unless the allowlist sets `require_signed_evidence`; that is the open evidence-independence decision, not something this selection step changes.

`ALLOW` authorizes the executor, not the outcome: the runner can still refuse (for example a public repository with no branch protection, missing or stale evidence), and that refusal stands.

A runner refusal is binding: the gate never prints a direct-merge command next to a runner command, and a refusal, a host denial or a GitHub rejection is not
retried through another interface.

## Audit of the merge entrypoints (what actually exists)

| Entrypoint | Claude Code | Codex | Status |
|---|---|---|---|
| `owner-merge-runner` (absolute path) | the one allow rule; **ALLOW** | a plain command; same validation | **supported, canonical** |
| `gh pr merge ...` | ask rules (merge-scope candidate) | live config is `approval_policy = "never"`: no host layer, instruction only | supported as the **ASK fallback**; Codex limitation below |
| `rtk gh pr merge ...` | ask twins of every rule | not used | supported as ASK fallback |
| GitHub MCP `merge_pull_request` | ask rule `mcp__github__merge_pull_request` | template `codex/config.toml` now `approval_mode = "prompt"` (was `approve`, i.e. unprompted) | ASK fallback; **not** an autonomous route |
| `gh api .../pulls/N/merge` | ask rules | none | ASK fallback |
| GraphQL `mergePullRequest`, `sh -c`, `command gh`, env prefixes, aliases, glued flags | no prefix rule can cover every shape; Auto Mode classifier only | none | **unsupported**: documented, not chased |

Not built, on purpose: a `PreToolUse` hook that approves or rewrites arbitrary merge spellings into the runner. Claude Code hooks can allow, deny, ask and rewrite
input, so one is possible, but a hook that approves any spelling is the blanket merge permission this design avoids, and the runner needs review evidence the hook
cannot invent. The supported agent workflow therefore routes through the gate's `merge_command` (skills, `.claude/CLAUDE.md`, `codex/AGENTS.md` say so); the ask rules
catch the cases where it does not.

### Codex limitation

The live `~/.codex/config.toml` is `approval_policy = "never"` with `danger-full-access` and an auto-approved GitHub MCP merge tool. That is the owner's local file and was not
changed. Under it, Codex has no prompt for any merge spelling, so for Codex the guarantee is the canonical-executor instruction plus GitHub's own rules, not a host ASK. To get a
host ASK for Codex, the owner sets the merge tool to `prompt` (the shipped template now does) and adds an exec-policy `prompt` rule for `gh pr merge`; that is an owner
settings change and is not made here.

## Observed limit: who writes the review evidence (2026-10-23, host-observed by the agent)

On the real host the Auto Mode classifier denied the agent writing the runner's review-evidence file for its own coordinated PR (`[Self-Approval]`). The denial was recorded once and not
retried through another tool, wrapper or sub-agent. So the end of the autonomous journey (step 4) holds only when the evidence file is produced by something the host does not read as the
agent approving its own work; the runner and the gate cannot fix that. The founder's L1 canary worked because the founder wrote the evidence. This is the open evidence-independence
decision, now with a measured symptom. Options for the owner, none implemented here: keep a one-line owner-run evidence step per merge; install an owner-controlled review-recording
command as the only evidence writer (and allow exactly that command); or sign evidence with a key the agent cannot read (`require_signed_evidence`). Until one is chosen, `ALLOW` from the gate means
the executor is authorized, and an owner PR autonomously merges only when valid evidence already exists.

## Golden user journey (the acceptance bar)

1. Owner opens a PR; CI runs for real.
2. The agent gets an independent review PASS at the exact head.
3. `owner-merge-gate.py check --settings ~/.claude/settings.json` prints `ALLOW`, the runner command and the evidence path.
4. The agent writes the evidence file for that head and runs the printed command; no founder prompt.
5. `owner-merge-gate.py verify` reads back a merge commit whose parent is the reviewed head.
6. If anything differs (no runner, repo not allowlisted, non-owner PR, host ask), the same journey ends in an **ASK** the founder can answer once for that one pinned operation.

Dropped as disproportionate: covering every command spelling, signed evidence, a second GitHub identity, a hook framework, a paid plan for server-side protection, and a
test per interface for the same guard. Each guard is tested once at the executor; each interface is tested only for "ALLOW via runner, otherwise ASK".

## What is verified, and by whom

Accepted record: `OWNER-MERGE-ACCEPTANCE-LEDGER.md` sections 6 and 7. In short: the installed runner merged a Class A canary PR at its pinned head with real Actions CI and **no host
prompt** (L1, founder-observed; audit and `verify` read back by the agent); a direct `gh pr merge` prompted and was denied (L3, founder-observed); the stale-head guard is accepted by
equivalent evidence (real-GitHub E3a/E3b plus unit and mutation tests) because the live L2 call was blocked by the Auto Mode classifier before the runner started.

What this change adds is only the *selection* step: `tests/test-canonical-executor.py` covers which executor the gate offers and its ALLOW/ASK outcome (runner offered only when installed,
allowlisted and host-allowed; ASK-first-only, unallowlisted repo, missing runner, non-owner PR, failing CI and host deny each fall back or stop). It does not prove that a live host
accepts a given spelling; the rule-matching shape is the one already observed in L1. Not verified: a non-owner PR on a real repo, a live runner REFUSED verdict on the installed path,
and any Codex host ASK (see the Codex limitation above).
