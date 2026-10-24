# Claude Code model routing: Sonnet default, Haiku opt-in (SPE-122, SPE-126)

Status: evidence-gated. Nothing here is deployed to live configuration, and no Haiku agent is configured anywhere. The baseline is unchanged: Sonnet medium as main, selective Opus high through `opus-architect` ([ADR 0003](adr/0003-model-routing-strategy.md)). Codex routing belongs to SPE-93 ([CODEX-ROUTING.md](CODEX-ROUTING.md)); the engineering contract belongs to SPE-108 and SPE-121. This file adds only the Haiku admission table and the measured context rule.

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

There is nothing to roll back: no Haiku agent, no `CLAUDE_CODE_SUBAGENT_MODEL`, no change to the main model, Auto Mode or permissions. If a canary is ever approved it starts with one worker on one low-risk task class, widens to two workers only after measured accepted throughput, quarantines any class that fails back to Sonnet, and is disabled by removing the opt-in. Concurrency limits stay those of the engineering contract.
