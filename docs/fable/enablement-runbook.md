# Fable 5.2 enablement runbook (SPE-172) - PREPARED, NOT APPLIED

Status: nothing here is enabled. No Fable agent, setting or env var exists, and no paid call was made to write this. Every dollar figure is an owner-approvable **proposal**, not a measurement. Source: Jira SPE-171 comment 30628 (decision packet) and SPE-172 comment 30621 (draft agent). Claims marked `[UNVERIFIED]` come from the Jira packet or a past session observation and cannot be checked from this repository; re-verify them before relying on them. Run these steps only **after** the owner approves the packet (retention accepted, provider-side limit set, billing route confirmed).

## Known blocker

[UNVERIFIED: session observation 2026-11-12, not reproducible from the repo] Claude Code 2.1.306 logs `unrecognized_model` for `claude-fable-5-2`, and Advisor is disabled for that id ("no advisor rank in the model catalog"). So only option A (one read-only subagent) is a candidate; Fable-as-advisor is unavailable. Effective-model identity of a Fable subagent, per-agent `model` precedence, refusal behaviour and accepted effort levels are UNVERIFIED; each needs a paid call.

Re-check without spending (no model call):

1. `claude --version`; if it is newer than 2.1.306, read its release notes/changelog for Fable 5.2 catalog support.
2. Search the installed CLI's changelog or model list text for `claude-fable-5-2` (for example `claude --help` and the release notes). Do not run `claude` with a Fable model, and do not call the API.
3. Treat "unknown" as "still blocked". Only a paid canary call can confirm the effective model in the session record.

## Where the agent file goes

The template is `docs/fable/fable-reasoner.agent.md.template`, deliberately not loadable. To enable, an owner copies it to **one** place only:

```
cp docs/fable/fable-reasoner.agent.md.template .claude/agents/fable-reasoner.md
```

`.claude/agents/` (or `~/.claude/agents/`, or a plugin agent dir) is the only place the harness loads it. No file with a Fable model id may exist in those directories before approval; `tests/test-fable-enablement.py` checks the repo's own `agents` directories only and is not wired into CI, so run it by hand before and after the copy. It cannot see `~/.claude/agents` or installed plugins: before approval, an owner checks those with `grep -ril fable ~/.claude/agents` (no output expected). Keep `CLAUDE_CODE_SUBAGENT_MODEL` unset (it could override the per-agent model). Do not add Bash, Edit, Write, Agent or MCP tools.

## Verify in a fresh process (first call only, after the limit below is set)

Effective model in the session record is `claude-fable-5-2` (not an alias or fallback); no inherited Opus advisor; only Read/Grep/Glob available; `/usage` shows the cost. Stop on the first unexplained figure.

## Fallback routing when Fable is unavailable

Unrecognised model, refusal, fallback, or any unverified identity routes to the existing baseline: Sonnet medium main, `opus-architect` (Opus, high) for hard design, `advisorModel: opus` unchanged. Escalation rules stay in `docs/MODEL-ROUTING.md` (genuine reasoning bottleneck after evidence is sufficient; not file count, one test failure, uncertainty, enthusiasm, or a safety refusal). A refusal or fallback is a gate, not a retry.

## Retention

- **Provider side (Anthropic):** [UNVERIFIED: from Jira SPE-171 comment 30628, not checked against the provider's current policy] Fable 5.2 is a Covered Model: prompts and completions are retained at least 30 days, then deleted automatically (exceptions: safety investigation, legal obligation). Zero data retention is not available unless Anthropic separately authorises it. Storage location for the Claude API: NOT STATED. Early customer-initiated deletion, human review of flagged data, training use, and whether this applies to a Pro/Max Claude Code login: NOT STATED / UNKNOWN. Deletion cannot be verified from here; the record is first-call date plus 30 days.
- **Local:** Claude Code transcripts under `~/.claude/projects` hold the same content independently of Fable. [UNVERIFIED: refers to the owner's `~/.claude/settings.json`, outside this repo; the 30-day default is unsourced] `cleanupPeriodDays` is unset, so the default 30 days applies.
- A consult sends only what the main agent puts in the prompt plus Read/Grep/Glob excerpts. Rule: no secrets, customer, corporate or private-individual data; owned repos only.
- Expiry: provider side automatic after the retention period; local by `cleanupPeriodDays`. The route stores nothing in Jira, git or any new store.

## Proposed limits and how each is enforced (proposals only)

| Limit | Proposal | Enforcement |
|---|---|---|
| Monthly ceiling | $60 month 1, then $0 unless pilot verdict is OPT_IN_CANDIDATE, then $40/month | Provider side, set by the owner: claude.ai usage-credits monthly limit (Settings > Usage), or the Console workspace spend limit if calls use an API key. The billing route is unconfirmed. This is the only true hard stop; the agent cannot set it. |
| Per headless run | $4 Fable, $3 Opus comparison | `claude -p --max-budget-usd <cap>`; [UNVERIFIED] understood as a CLI-side list-price estimate, not billed money. |
| Per consult turns | 12 turns | `maxTurns: 12` in the agent file. [UNVERIFIED] no agent in this repo uses `maxTurns`; treat the cap as absent until a canary shows it is enforced. |
| Interactive subagent dollars | none locally | No local dollar cap exists, so do not install the agent file until the provider-side limit is set. |
| Reconciliation | after each batch | Read `/usage` and the provider dashboard. |

Fast mode stays off. Fable is never the default.

## Rollback

Delete `.claude/agents/fable-reasoner.md` (or set its `model` to `sonnet`). No settings.json change is needed. Lower or remove the provider-side limit if desired. Provider-side data from calls already made still follows the 30-day retention; local transcripts follow `cleanupPeriodDays`.
