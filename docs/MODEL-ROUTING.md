# Claude Code model routing: Sonnet default, Haiku opt-in (SPE-122, SPE-126)

Status: evidence-gated. Nothing here is deployed to live configuration, and the only Haiku agent this repository ships is the opt-in project worker `.claude/agents/haiku-worker.md` (an enabled plugin ships its own, see the tier audit). The baseline is unchanged: Sonnet medium as main, selective Opus high through `opus-architect` ([ADR 0003](adr/0003-model-routing-strategy.md)). Codex routing belongs to SPE-93 ([CODEX-ROUTING.md](CODEX-ROUTING.md)); the engineering contract belongs to SPE-108 and SPE-121. This file adds only the Haiku admission table, the measured context rule, the tier audit and the four-layer canary evidence.

## Admission table

A person or a script reads this table; no model call is made to choose a model. Size, line count and ticket label never decide the route.

| Situation | Route |
|---|---|
| Ordinary implementation, tests, CI repair, tooling | Sonnet main, no subagent |
| Meaningful architecture, security-sensitive design, genuine ambiguity, important trade-offs | `opus-architect` (Opus, high), selectively |
| Explicit behavior, stable interface, objective test oracle, reversible, low blast radius, **and** a direct tool call is not cheaper | Haiku worker is eligible, opt-in only |
| Security, permissions, authorization, data integrity, concurrency, public contract, merge or release authority | Never Haiku |
| Unresolved root cause, ambiguous acceptance criteria, no adequate oracle, cross-module interface design | Sonnet; Opus if the decision is genuinely hard |
| Classification unknown, or the effective model id is not verified | Sonnet |

A task that needs no subagent uses none. A coherent task is not split to create workers.

## Tier audit (2026-11-14): configured and proposed

Read from the repository's `.claude/settings.json` and `.claude/agents` and from the user-level `~/.claude/settings.json` and `~/.claude/agents` on the owner's host. "Configured" means the value is present in those files; the effective model id seen by a running session is not measured by this table.

| Tier | Model | Effort | Routing trigger | Fallback | Status |
|---|---|---|---|---|---|
| Main | `sonnet` | `medium` (`effortLevel`) | Default for implementation, tests, CI, orchestration | None needed; it is the floor | Configured, in both settings files |
| Advisor | `opus` (`advisorModel`) | Not set; the advisor default applies, UNKNOWN | Important decisions, repeated failure, security or architecture doubt, final review | Continue on Sonnet main | Configured |
| Architect | `opus` via `opus-architect` | `high` (agent frontmatter) | Meaningful architecture, migration, security-sensitive design, genuine ambiguity | Sonnet main | Configured |
| Worker | Haiku, pinned by explicit id | Not set | Admission table row 3 only, opt-in | Sonnet | Project worker `.claude/agents/haiku-worker.md` (`model: claude-haiku-5-5`, owner-approved 2026-11-14); no user-level Haiku agent exists. The enabled `caveman` plugin ships `cavecrew-investigator` and `cavecrew-reviewer` agents that declare the bare alias `model: haiku`, which the worker rule above does not allow for admission. Its `cavecrew-builder` declares no model and inherits the session model |
| Fable 5.2 | `claude-fable-5-2` | Not set | One read-only consult after a genuine reasoning bottleneck | Sonnet main plus `opus-architect` | Proposed; no agent file, not enabled, owner approval pending (SPE-171); blocked by an unverified `unrecognized_model` observation on Claude Code 2.1.306, see [fable/enablement-runbook.md](fable/enablement-runbook.md) |

The Fable decision (retention, spend cap, billing route) is owner-held in SPE-171; nothing in this table enables it.

## Four-layer design and canary evidence (2026-11-14)

Haiku for bounded worker tasks, Sonnet for routine implementation, Opus for complex design and important decisions, Fable for exceptional read-only reasoning (proposed, not enabled; SPE-171). Advisor and Architect are two distinct roles on the one Opus layer: the Advisor is the `advisorModel` tool that reviews a running session's transcript, the Architect is the `opus-architect` subagent that designs and returns a plan.

| Layer | Effective model, measured | Evidence | Fallback |
|---|---|---|---|
| Haiku worker | `claude-haiku-5-5` | The project worker `.claude/agents/haiku-worker.md` (`claude -p` from this repository, see below) and, earlier, a `cavecrew-investigator` subagent canary and a `claude -p --agents` run of a project-style agent definition with `model: haiku` and no plugin both recorded only `claude-haiku-5-5` in the session record on Claude Code 2.1.306, 2026-11-14 (subagent transcript model fields and `claude -p` modelUsage; list-price estimate $0.0097 for the `claude -p` run alone; no Jira record, the transcripts are not committed) | Sonnet |
| Sonnet main | `claude-sonnet-5-5` | The `claude -p --model sonnet` runs below (the project-worker run used the session default, which is Sonnet) recorded `claude-sonnet-5-5` as the orchestrating model in `modelUsage` on 2.1.306, 2026-11-14; the settings files configure `sonnet` with `medium` effort (the effort level in effect was not measured) | None |
| Opus Architect | `claude-opus-5-5` | An `opus-architect` canary on 2.1.306, 2026-11-14, recorded `claude-opus-5-5` in the subagent transcript (not committed) | Sonnet main |
| Opus Advisor | `claude-opus-5-5` | A `claude -p --model sonnet` run that called the advisor tool once (user-level `advisorModel` `opus`) recorded `claude-opus-5-5` beside `claude-sonnet-5-5` in `modelUsage` on 2.1.306, 2026-11-14 (list-price estimate $0.33 for the advisor call, $0.37 for the run; the transcript is not committed) | Continue on Sonnet |
| Fable | not measured | Preparation only: `tests/test-fable-enablement.py` passes and `claude --help` now names a `fable` alias, which is not evidence that a Fable call works or which model answers | Sonnet main plus `opus-architect` |

Haiku through project-owned configuration: on Claude Code 2.1.306 an inline `--agents` definition with the bare alias `model: haiku` resolved to `claude-haiku-5-5` without the `caveman` plugin. That shows the alias resolves, not that a project worker is admitted: the worker rule above still requires an explicit model id, and the alias resolved to a different Haiku on 2.1.287 to 2.1.292. The owner then approved a project worker, and `.claude/agents/haiku-worker.md` pins the explicit id `claude-haiku-5-5`. A `claude -p` run from a checkout of this repository (Claude Code 2.1.306, 2026-11-14) delegated one read-only task to it and recorded `claude-sonnet-5-5` and `claude-haiku-5-5` in `modelUsage` (list-price estimate $0.055; the transcript is not committed). The Haiku layer therefore no longer depends on the `caveman` plugin; the plugin's own Haiku agents still run on the owner's host. `--disallowedTools "Agent(caveman:cavecrew-investigator)"` made that agent refuse and recorded no Haiku usage, so the duplicate route can probably be closed without disabling the rest of the plugin; the equivalent `permissions.deny` entry is expected to behave the same but was not exercised, and this repository's settings keep `deny` empty by policy (SPE-106), so no rule is installed here. The worker's no-merge and no-push rule is an instruction plus the host permission and classifier layer, not a property of its tool list: it has `Bash`, and the session's allow rules apply to it as to any subagent.

## Worker rules and stops

A Haiku worker is pinned by explicit model id (the `haiku` alias resolved to Haiku 4.5 on Claude Code 2.1.287 to 2.1.292 and to Haiku 5.5 on 2.1.306), has the narrowest tools its task needs, never spawns agents, and never merges, publishes, closes tickets, touches credentials or certifies its own work. Sonnet main reviews the result and owns integration, the PR and Jira. The worker stops and returns the observed result, evidence and open decision when requirements are ambiguous, a public contract or schema must change, security or privacy appears, root cause stays unknown, scope grows, a test contradicts the intended behavior, or two targeted repairs fail. There is no retry loop.

## What was measured

Sources: the pilot figures are in [ROUTING-EVAL.md](ROUTING-EVAL.md#pilot-result-2026-10-24); the start-up and context figures are recorded in Jira (SPE-123 comment 28888 with correction 28890, SPE-127 comment 28923) from session transcripts and live `claude -p` probes on Claude Code 2.1.306, not from a committed script.

- Route: claude.ai subscription, first-party, no proxy. Dollar cost is UNKNOWN: the subscription exposes no per-model weighting and `total_cost_usd` is a list-price estimate.
- Subagent start-up (first call prompt tokens, median): Sonnet 5.5 about 70k, Opus 5.5 about 38k, Haiku 5.5 about 37k. A scout pays that before it does any work.
- Pilot (six tasks, one trial per arm, [ROUTING-EVAL.md](ROUTING-EVAL.md), SPE-124): with the admission table available, the Sonnet orchestrator delegated in 0 of 6 tasks. Forced delegation on the 3 bounded tasks passed oracle and review but raised wall time on every task and moved a large amount of work onto Haiku; net usage cannot be priced. Verdict: inconclusive, keep opt-in. Promotion needs at least 30 paired tasks and a recorded gate.

## Context rule for scripted `claude -p`

Measured on Claude Code 2.1.306: a one-word reply cost 190,961 prompt tokens with the default context and about 30,800 with `--strict-mcp-config --disable-slash-commands`; adding `--setting-sources local` brought a tool-using arm to about 16k. So scripted runs that do not need MCP servers, skills or project settings pass those flags. Do not apply them to tests whose purpose is to observe instruction or skill loading. Repeated Jira or PR reads across parallel workers are avoided by handing over references and summaries.

## Rollout and rollback

The pilot verdict (inconclusive, keep opt-in) is accepted. Expansion to 30 or more paired trials is deferred until a narrower pilot shows that delegation actually happens and that a benefit is plausible; Haiku delegation is not activated globally by this repository or by a user-level agent. Separately, the `caveman` plugin is enabled user-wide and its `cavecrew-investigator` and `cavecrew-reviewer` agents declare Haiku, so Haiku-pinned subagents are available on this host outside this opt-in rule (disable path: set `caveman@caveman` to false in `enabledPlugins`, an owner action). There is nothing to roll back in this repository: no user-level Haiku agent and no Haiku agent in this repository other than the opt-in `haiku-worker`, no `CLAUDE_CODE_SUBAGENT_MODEL`, no change to the main model, Auto Mode or permissions. If a canary is ever approved it starts with one worker on one low-risk task class, widens to two workers only after measured accepted throughput, quarantines any class that fails back to Sonnet, and is disabled by removing the opt-in. Concurrency limits stay those of the engineering contract.
