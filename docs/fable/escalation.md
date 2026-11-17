# Fable escalation: offer once per blocker, consult only after approval (SPE-171)

Replaces "manual opt-in" with a proactive offer. Default routes are unchanged (Sonnet main, `opus-architect`, Opus advisor). Fable is never a default and no unattended Fable call is enabled: nothing in `.github`, `.claude`, a hook or a scheduler invokes `consult`, and `tests/test-fable-escalation.py` fails if one does.

Tool: `scripts/fable-escalation.py`. Tests: `tests/test-fable-escalation.py` (a fake `claude` binary; no real model call).

## When the agent offers

One trigger must hold, and no exclusion:

| Trigger | Rule |
|---|---|
| `two_distinct_attempts_same_failure` | Two Opus attempts with materially different approaches (distinct `approach` text) end in the same acceptance failure |
| `unresolved_review_disagreement` | Independent review leaves a substantive architecture disagreement with at least two stated positions |

Excluded, whatever else is true: queue delay, CI availability (`CI_UNAVAILABLE_EXTERNAL`, billing, quota), missing permissions or a host denial, external outages. The caller states `cause`, and a failure text that names one of these also excludes. The inputs must be attested non-sensitive and the repo owned; the question must be one concrete one-line question.

`evaluate` is a pure check (exit 1 when ineligible, with reasons). Ineligible means: continue independent work.

## Approval: one per blocker

1. `offer --case case.json --session <id>` records the blocker and writes one pending item in the pending-ASK ledger (`scripts/ask-pending.py`), so sessions sharing the ledger see one open request. It returns the packet: blocker, attempts made, the one question for Fable, the route.
2. The agent asks the owner once through the native prompt (AskUserQuestion: Approve / Decline) with that packet. This tool cannot obtain or verify the answer: the recorded approval is the agent's attestation of the owner's native answer, stored as a hash. A local script cannot stop a session from recording an approval nobody gave; `receipts.log` and the pending-ASK ledger are the audit trail, and the owner can check them.
3. `answer --id <id> --decision approve|decline --ref "<the owner's answer>"` records it (only a hash of the text is stored). Decline, or no reply, spends nothing and the blocker is never offered again; independent work continues.
4. An approval expires after 24 hours; a stale one needs a new offer after the blocker is revalidated.

A blocker is the target plus the acceptance check, compared ignoring case, spacing and punctuation. More attempts or a reworded `blocker` summary do not make a new blocker; a reworded acceptance check does, so key it by its stable id (AC3). The state directory is overridable by environment, so dedup and the breaker bind sessions that share it.

## The consult

`consult --id <id>` prints the command shape (dry run). `--execute` runs one `claude -p --model claude-fable-5-2` with only `Read,Grep,Glob`, no MCP, no slash commands, no session persistence and `--max-budget-usd 4`. Included subscription only, as far as the environment shows: it refuses when `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, a Foundry key or a Bedrock/Vertex/Foundry switch is set (presence check, never printed). It cannot see an `apiKeyHelper` in settings or a stored console key, so the owner's usage-credits-off setting stays the real guard. The prompt goes on stdin, not argv. It requires Claude Code 2.1.321 or later. Usage credits stay off, an owner setting this tool cannot see or change. The result is accepted only if `is_error` is false, `modelUsage` is exactly `claude-fable-5-2` (a fallback model beside it is a mismatch) and the advice is non-empty. The advice goes to a 0600 file; the original session implements and verifies it. The consult state is saved before the call, so a crash or kill still leaves the one consult spent.

## Circuit breaker

At most one consult per blocker, never retried. The breaker opens, and blocks every new offer and consult, on:

- `route_failure`: non-zero exit, `is_error`, unparseable or non-object output, any exception during the call, timeout, old client
- `model_mismatch`: the effective model is not exactly Fable 5.2
- `no_advance`: the original session records `verdict --result not_advanced`

Only `reset-breaker --note "<why resolved>"`, an owner action, closes it. Fallback on any failure is the existing baseline: Sonnet main plus `opus-architect`.

## Measuring benefit

`verdict --id <id> --result advanced|not_advanced --rework-rounds N --elapsed-s N --usd X` records the totals at acceptance next to the spend at offer time. `report` lists them. There is no control arm, so a single closed blocker cannot show a reduction; compare against an Opus-only blocker of similar kind before claiming one. The 2026-11-17 pilot (two tasks, equal quality, about twice the list cost) is the only evidence so far, and it showed no clear net benefit.

## Not done here

CI wiring of the new test (a workflow edit needs review) and any settings or `~/.claude/CLAUDE.md` routing text change (owner-managed) are separate steps. The real-case verification is pending the next blocker that meets a trigger.
