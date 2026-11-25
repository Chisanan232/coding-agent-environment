# Fable 5.2 enablement runbook (SPE-172) - PREPARED, NOT APPLIED

Status: no Fable agent, setting or env var exists, and none is needed. The owner approved Fable as a fourth tier on 2026-11-25 (SPE-171); the consult path is `scripts/fable-escalation.py` under a standing authorization file the owner installs (see [escalation.md](escalation.md)). The agent-file steps below remain a prepared alternative that nobody has applied. Three paid one-word canary calls are recorded under "Known blocker" (a measured list-price estimate); every other dollar figure is an owner-approvable **proposal**, not a measurement. Source: Jira SPE-171 comment 30628 (decision packet) and SPE-172 comment 30621 (draft agent). Claims marked `[UNVERIFIED]` come from the Jira packet or a past session observation and cannot be checked from this repository; re-verify them before relying on them. Run these steps only **after** the owner approves the packet (retention accepted, provider-side limit set, billing route confirmed).

## Known blocker

[UNVERIFIED: session observation 2026-11-12, not reproducible from the repo] Claude Code 2.1.306 logs `unrecognized_model` for `claude-fable-5-2`, and Advisor is disabled for that id ("no advisor rank in the model catalog"). So only option A (one read-only subagent) is a candidate; Fable-as-advisor is unavailable. Effective-model identity of a Fable subagent, per-agent `model` precedence, refusal behaviour and accepted effort levels are UNVERIFIED; each needs a paid call.

[MEASURED 2026-11-15, no model call, state before the upgrade] The installed 2.1.306 binary contains 13 strings for `claude-fable-5-1` and none for `claude-fable-5-2`, so its model catalog predates Fable 5.2; the `unrecognized_model` text is also present in it. Anthropic's support article 15424964 states Fable 5.2 needs Claude Code 2.1.321 or later. The CLI is the Homebrew cask `claude-code@latest` (2.1.306 installed, 2.1.325 available), upgraded by `brew upgrade --cask claude-code@latest`. That upgrade replaces the CLI every session uses, so it is a host-wide change that needs owner approval; it was not run at that point; the owner approved it the same day and it was applied (next paragraph).

[MEASURED 2026-11-15, one paid canary call] After the owner-approved cask upgrade to 2.1.325 (`claude --version` prints 2.1.325), `claude -p "Reply with the single word OK." --model claude-fable-5-2 --tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands --no-session-persistence --max-budget-usd 0.10`, run from an empty directory, recorded `claude-fable-5-2` as the only model in `modelUsage` (`canonicalModel` `claude-fable-5-2`, `provider` firstParty). The estimate was $0.74 for 46,190 cache-creation, 2 input and 4 output tokens, so the client-side budget stopped it before the answer (`error_max_budget_usd`); a recurring consult costs about the same context unless the session context is trimmed. Extra usage was off in the client's cached account state, not an authoritative statement. Canary verdict (corrected 2026-11-15): the model id was accepted and served (usage recorded under `claude-fable-5-2`), but the call did not complete: `is_error` true, `terminal_reason` `budget_exhausted`, `result` null and top-level `usage` all zero, so there is no answer text and no completed response. It is neither a PASS for a working Fable call nor a routing signal. Billing attribution is UNKNOWN: the owner's `/usage` after the call showed `Current week (Fable)` 0% and `Usage credits` Off, so the call is not visible as allowance consumption or as a credit charge, and the UI cannot attribute it. No further Fable call is made for this check. Per-agent `model` precedence, refusal behaviour and accepted effort levels are still UNVERIFIED, and routing and recurring spend remain a separate owner decision.

[MEASURED 2026-11-17, reconciles the canary above; supersedes its "billing UNKNOWN" and "no further call" wording] Three identical canary invocations (`claude -p "Reply with the single word OK." --model claude-fable-5-2 --tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands --no-session-persistence`, empty directory, Claude Code 2.1.325, claude.ai Max login, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK` and `CLAUDE_CODE_USE_VERTEX` unset, usage credits Off as the owner confirmed) differ only in the client-side cap and in prompt-cache state:

| Run | `--max-budget-usd` | First-call cache state | List-price estimate | Outcome |
|---|---|---|---|---|
| 2026-11-15 | 0.10 | cold: 46,190 cache-creation tokens | $0.739 | `error_max_budget_usd`, no answer |
| 2026-11-17 04:02Z | 0.25 | cold: 46,060 cache-creation tokens | $0.737 | `is_error` true, no answer; `/usage` unchanged (all models 67%, Fable 0%) |
| 2026-11-17 04:26Z | 0.25 | not printed; low cost suggests a cached prefix (inferred) | $0.032 | `terminal_reason` `completed`, answer `OK`; `/usage` all models 67% (04:03Z) to 68% (04:28Z) |

So the earlier `budget_exhausted` result is best explained by a cap below the cost of writing the roughly 46k-token cold prefix: the account, login, model id and flags were the same across the runs, only the cap and the apparent cache state differed. The 04:02Z run recorded no `subtype`, so its failure reason is inferred from 0.25 < $0.737 and from the later success. The same call completed once its cost fell to $0.032, which is consistent with a cached prefix. What the 46k prefix contains is not measured here; that user-level instructions make up most of it is an inference. The completed run shows the model is served. The all-models weekly figure moved by one point between 04:03Z and 04:28Z, which is consistent with an included-allowance draw but cannot be pinned to this call (other work ran on the same account in that window). After the later four-run comparison pilot (about 04:40Z, session record, not one of these three runs) `/usage` showed `Current week (Fable)` 1%, so a separate Fable meter does move; the figures are a coarse percentage, not billed money. The consult tool's `--max-budget-usd 4` sits well above the cold-prefix cost, so a cold consult is not expected to hit this cap; that has not been run through the real route. Remaining NOT_VERIFIED: a real consult with `Read,Grep,Glob` tools on a real eligible blocker (tool turns add cost beyond this one-word probe), and whether Fable reduces rework.

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

Unrecognised model, refusal, fallback, or any unverified identity falls back to the existing baseline: Sonnet medium main, `opus-architect` (Opus, high) for hard design, `advisorModel: opus` unchanged. Escalation rules stay in `docs/MODEL-ROUTING.md` (genuine reasoning bottleneck after evidence is sufficient; not file count, one test failure, uncertainty, enthusiasm, or a safety refusal). A refusal or fallback is a gate, not a retry. The fallback is not automatic: measured on Claude Code 2.1.325, an unrecognised main model id with no `--fallback-model` fails with `is_error` true and nothing answers; only a configured `--fallback-model sonnet` completes on Sonnet (see `docs/MODEL-ROUTING.md`). No setting or agent file configures that flag by default.

## Retention

- **Provider side (Anthropic):** [UNVERIFIED: from Jira SPE-171 comment 30628, not checked against the provider's current policy] Fable 5.2 is a Covered Model: prompts and completions are retained at least 30 days, then deleted automatically (exceptions: safety investigation, legal obligation). Zero data retention is not available unless Anthropic separately authorises it. Storage location for the Claude API: NOT STATED. Early customer-initiated deletion, human review of flagged data, training use, and whether this applies to a Pro/Max Claude Code login: NOT STATED / UNKNOWN. Deletion cannot be verified from here; the record is first-call date plus 30 days.
- **Local:** Claude Code transcripts under `~/.claude/projects` hold the same content independently of Fable. [UNVERIFIED: refers to the owner's `~/.claude/settings.json`, outside this repo; the 30-day default is unsourced] `cleanupPeriodDays` is unset, so the default 30 days applies.
- A consult sends only what the main agent puts in the prompt plus Read/Grep/Glob excerpts. Rule: no secrets, customer, corporate or private-individual data; owned repos only.
- Expiry: provider side automatic after the retention period; local by `cleanupPeriodDays`. The route stores nothing in Jira, git or any new store.

## Proposed limits and how each is enforced (proposals only)

| Limit | Proposal | Enforcement |
|---|---|---|
| Monthly ceiling | $60 month 1, then $0 unless pilot verdict is OPT_IN_CANDIDATE, then $40/month | Provider side, set by the owner: claude.ai usage-credits monthly limit (Settings > Usage), or the Console workspace spend limit if calls use an API key. The billing route is only partly confirmed (see the 2026-11-17 reconciliation under "Known blocker": weekly figures moved, credits Off, still a coarse percentage). This is the only true hard stop; the agent cannot set it. |
| Per headless run | $4 Fable, $3 Opus comparison | `claude -p --max-budget-usd <cap>`; [UNVERIFIED] understood as a CLI-side list-price estimate, not billed money. |
| Per consult turns | 12 turns | `maxTurns: 12` in the agent file. [UNVERIFIED] no agent in this repo uses `maxTurns`; treat the cap as absent until a canary shows it is enforced. |
| Interactive subagent dollars | none locally | No local dollar cap exists, so do not install the agent file until the provider-side limit is set. |
| Reconciliation | after each batch | Read `/usage` and the provider dashboard. |

Fast mode stays off. Fable is never the default.

## Rollback

Delete `.claude/agents/fable-reasoner.md` (or set its `model` to `sonnet`). No settings.json change is needed. Lower or remove the provider-side limit if desired. Provider-side data from calls already made still follows the 30-day retention; local transcripts follow `cleanupPeriodDays`.
