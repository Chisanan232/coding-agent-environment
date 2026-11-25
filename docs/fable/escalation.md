# Fable escalation: automatic consult inside a shared monthly budget (SPE-171)

Owner decision, 2026-11-25 (SPE-171): Fable (`claude-fable-5-2`) is a fourth routing tier for the owner's **personal, non-sensitive** engineering consults on the approved personal host. A consult runs **automatically** when a trigger is met and every gate passes; there is no per-consult approval. Company-managed sessions are out of scope. Default routes are unchanged (Sonnet main, `opus-architect`, Opus advisor). Fable is never a default model: no agent file, hook, scheduler or settings default names it, and `tests/test-fable-escalation.py` fails if one does.

Tool: `scripts/fable-escalation.py` (the only entry point). Tests: `tests/test-fable-escalation.py` and `tests/test-fable-cost-estimate.py` (a fake `claude` binary; no real model call).

## What an owner must still do

The tool is inert until the owner does these; the agent never does them.

1. Run `authorization template --cwd <an owned repo>` (read-only). It prints a candidate file, this host's hash and the file's sha256.
2. Confirm in the claude.ai account settings that "Help improve Claude" (model improvement) is **off** (see Retention below), then set `retention.model_improvement_setting` to `"off"` and `confirmed_on` to that date in the candidate.
3. Install the file yourself at `~/.config/coding-agent-environment/local/fable-authorization.json`, mode 0600, in a directory only you can write. The script never writes it.
4. `authorization check` (read-only) shows the verdicts: authorization, retention, host, repo owner, managed session, caps and the month's remaining budget.
5. Apply the skill text to the live `~/.claude` with `scripts/profile-install.sh --claude-skills` (see `docs/ROLLOUT-USER-CLAUDE-MD.md`). Until then a live session does not carry the instruction and nothing consults.

## How the workflow reaches it

The development loop calls one command after every failed attempt, and the command decides:

`python3 "$HOME/Bryant-Developments/coding-agent-environment-main-view/scripts/fable-escalation.py" attempt --target OWNER/REPO#N --check <id> --session <id> --model <m> --approach <text> --failure <signature> --question "<one question?>" --non-sensitive`

It returns at once with one `action`:

- `CONTINUE`: keep working. It carries a `reason` and the `fallback` (Sonnet main plus `opus-architect` and the existing circuit breaker). Every refusal is `CONTINUE`.
- `CONSULT`: the blocker is READY. Run `consult --id <id> --execute` once, implement and verify the advice in the original session, then record `verdict`. Repeat `attempt` calls for a READY blocker return the same `CONSULT`; once it is CONSULTING, CONSULTED, FAILED or CLOSED they return `CONTINUE`. The agent never asks the owner.

State machine: `READY` -> `CONSULTING` -> `CONSULTED` | `FAILED` -> `CLOSED` (after `verdict`). Only READY can consult, once.

The call sites are the failure steps of `dev-impl-loop` and the "Blocker escalation to Fable" section of `engineering-workflow`; both are `.claude/skills` sources inside `claude-only` spans, so the Codex install drops them. The script is called by absolute path from the maintained checkout. Trouble that is not a reasoning failure (`--cause`, or failure text naming CI, queue, permission or outage) is not recorded, so it neither counts nor lingers. Evaluate and the READY transition run under one lock, so concurrent calls produce one READY blocker and no error. `--review-resolved` clears a settled disagreement. Detection is in the tool, not in a hook: a hook cannot tell a materially different approach from a retry.

## When a blocker is eligible

One trigger must hold, and no exclusion:

| Trigger | Rule |
|---|---|
| `two_distinct_attempts_same_failure` | Two Opus attempts with materially different approaches (distinct `approach` text) end in the same acceptance failure |
| `unresolved_review_disagreement` | Independent review leaves a substantive architecture disagreement with at least two stated positions |

Excluded, whatever else is true: queue delay, CI availability (`CI_UNAVAILABLE_EXTERNAL`, billing, quota), missing permissions or a host denial, external outages, and blockers that only an owner can clear (`owner_decision`). Those are authority problems, not reasoning bottlenecks. Two approaches that use the same words in a different order count as one. `--non-sensitive` is required but is not sufficient: the payload checks below also apply. `evaluate` is a pure check (exit 1 when ineligible, with reasons).

A blocker is the target plus the acceptance check, compared ignoring case, spacing and punctuation. More attempts or a reworded `blocker` summary do not make a new blocker; a reworded acceptance check does, so key it by its stable id (AC3).

## Standing authorization

Path (fixed, no environment or CLI override): `~/.config/coding-agent-environment/local/fable-authorization.json`. It is read only through `owner-merge-gate.py`'s `read_owner_file` (regular file, owned by the caller, not group/world-writable, not a symlink, every directory down from the owner-config root owned and private, no duplicate keys). Closed schema `fable-authorization/v1`: unknown keys at any level are refused.

| Field | Rule |
|---|---|
| `decision` | `ticket` `SPE-171`, `by` `owner`, a date and a short `ref` |
| `model` | `claude-fable-5-2` |
| `billing_route` | `claude.ai-max-usage-credits-off` |
| `retention` | `model_improvement_setting` `off`, `on` or `unconfirmed`; `confirmed_on` date or null |
| `scope` | `host_sha256` (sha256 of the macOS `IOPlatformUUID`), `repo_owners`, `allowed_profiles` |
| `caps` | `per_consult_usd`, `monthly_usd`, `timezone` `Asia/Taipei`; the effective caps are `min(file, script ceiling)`: $4 and $40, so a larger value in the file never raises them |
| `ledger_dir` | absolute, under the home directory; created 0700 if missing; must be a real directory owned by the caller with mode 0700 |

Gates, all fail closed and all return `CONTINUE` with the fallback (checked when `attempt` would make a blocker READY, and again at `consult`):

| Reason | Meaning |
|---|---|
| `no_authorization` | the file does not exist |
| `authorization_invalid` | unreadable, symlinked, group/world-writable, malformed, wrong model/ticket/route, unknown key |
| `retention_unconfirmed` | `model_improvement_setting` is not `off` (see Retention). `attempt` and `evaluate` still run; READY is withheld and `consult` is refused |
| `scope_host_mismatch` | this host's hash is not the authorized one, or it cannot be read |
| `scope_repo_not_owned` | the target `owner/repo#N` owner, or the owner of the working tree's `origin` remote (first remote if there is no `origin`), is not in `repo_owners` |
| `managed_session` | `/Library/Application Support/ClaudeCode/managed-settings.json` exists (presence only), or `bin/coding-agent-profile resolve --cwd` reports a profile not in `allowed_profiles` (unresolved or malformed also fails closed; a directory with no profile is fine) |
| `api_env_present` | an API credential or alternate-provider variable is set (presence only, never printed); `consult` only |
| `ineligible_payload:<rule>` | see Payload |
| `monthly_budget_exhausted`, `blocker_already_reserved_this_month`, `ledger_unreadable` | see Budget |
| `breaker_open` | the circuit breaker is open |

A refusal at `consult` leaves the blocker READY, spends nothing and can be retried (next month, after a fixed authorization, after a reset). `authorization template` and `authorization check` are read-only: they never write the authorization file, create the state directory or print a secret (`check` prints verdicts, not the host hash; `template` prints the hash because the owner needs it).

## Budget: one ledger for every session

Every dollar figure here is the client's list-price estimate (`costBasis`), not billed money. On the Max login with usage credits Off no money is billed per call. The ledger is an **enforceable local gate only for invocations that go through this tool**: it cannot see an interactive `/model` Fable session or any other client. The only provider-side stop is the owner's allowance meter (a coarse percentage in `/usage`) plus keeping usage credits Off.

Ledger: `<ledger_dir>/YYYY-MM.json` (`{month, entries[]}`) plus an append-only `journal.jsonl`, under `flock` on `<ledger_dir>/.lock`, atomic writes. The month is the reservation time in fixed UTC+8 (so `2026-11-30T16:00Z` is `2026-12`). Entry: `rid`, `blocker_id`, `session`, `reserved_usd`, `state` (`RESERVED` or `SETTLED`), `charged_usd`, `basis`, `reserved_at`, `settled_at`.

Inside `consult --execute`, after the gates and the version check:

1. Reserve under the ledger lock. `committed` = settled charges + the full `reserved_usd` of every RESERVED entry. Refuse `monthly_budget_exhausted` if `committed + per_consult > monthly`; refuse `blocker_already_reserved_this_month` if this blocker id already has an entry this month (this dedups across sessions even with different state directories); otherwise record a RESERVED entry for the per-consult amount ($4). Budget refusals do not trip the breaker: they are quota state and clear at the month rollover.
2. Set the blocker CONSULTING (a crash after this leaves both spent).
3. Call `claude -p` with `--max-budget-usd 3.00`. The client checks that cap only between turns, so $1 of headroom sits inside the $4 reservation. The ledger lock is not held during the call. Lock order is always the blocker-state lock first, then the ledger lock.
4. Settle: a numeric `total_cost_usd` is charged (success or failure); missing or unparseable output, a timeout or any exception charges the full $4; a charge above $4 records `cap_overrun` and opens the breaker (the advice from an otherwise good answer is kept).

How interrupted calls are accounted: if the parent is killed or crashes after the reservation, the entry stays RESERVED at the full amount forever and is never auto-released. `report` lists a RESERVED entry older than the timeout (540 s) as `orphaned`; it is still counted. The owner can reconcile it by hand against the meter. An unreadable or corrupt ledger, or a ledger directory that is not private, refuses with `ledger_unreadable`.

`report` includes a `month_budget` section (committed, remaining, entries, reserved, orphaned) when the authorization is readable.

## Payload minimization

Minimum context only: no credentials, customer data or raw transcripts. Checked offline:

- At `attempt`, text that matches a rule is not stored and the call answers `note: not counted: <rule>`. A bad acceptance-check id is not tracked at all.
- At `consult`, the whole prompt and each stored text field are checked again, plus an 8 KB size cap. A hit refuses with `ineligible_payload:<rule>` before any reservation and does not trip the breaker.

Rules: `secret` (credential words followed by `=` or `:`, `ghp_`/`sk-`/`sk-ant-`/`xox*-`/AWS key shapes, JWTs, PEM headers, `scheme://user:pass@`, any run of 40+ base64 or hex characters that contains a digit, so a full commit SHA is refused: cite it shortened), `transcript` (`Human:`/`Assistant:` line prefixes, `"role":`, `"type":"assistant|user|tool_use"`, `<system-reminder`, `.claude/projects/`, `.jsonl`), `email` (any address other than `noreply@`), `size`. The secret patterns are a local equivalent of `owner-merge-local-ci.py`'s: that script is a hyphen-named module that loads the whole merge gate and runner on import, so it is not imported just for one regex.

The prompt carries only the blocker, the check, truncated attempts, review positions and the question, plus "Read only files in this repository; do not open .env*, credentials, ~/.ssh, ~/.config, ~/.claude". The argv also passes `--disallowedTools` denies for `Read(**/.env*)`, `Read(~/.ssh/**)`, `Read(~/.config/**)` and `Read(~/.claude/**)`. [UNVERIFIED: whether `--disallowedTools` is honoured in `-p` mode was not tested, because that needs a paid call; the prompt rule and the payload checks do not depend on it.]

## Retention (Max-login route)

The consult runs on the claude.ai Max login, a consumer account. What is retained depends on the account's model-improvement setting:

| Setting | Retention |
|---|---|
| off | 30 days |
| on | 5 years, and the content is used for training |
| any | flagged content (safety classifiers) up to 2 years regardless |

Whether `claude -p` (headless) is treated differently from interactive use is NOT_STATED in those sources. Because the setting decides whether prompts may be kept for years and trained on, `consult` refuses with `retention_unconfirmed` unless the authorization records `off`. Setting `confirmed_on` is the owner's attestation; the script cannot see the account setting. Sources (read 2026-11-25): code.claude.com/docs/en/data-usage; privacy.claude.com article 10023548 (dated July 1, 2026); privacy.claude.com article 10023580 (dated March 16, 2026). The older provider-side Covered Model statement in the enablement runbook is [UNVERIFIED]; these three pages are the current references.

The local transcript store (`~/.claude/projects`) holds the same content independently of Fable and is why transcripts are excluded from the payload.

## The consult

`consult --id <id>` prints the command shape (dry run: it performs the gates but reserves nothing). `--execute` runs one `claude -p --model claude-fable-5-2` with only `Read,Grep,Glob`, no MCP, no slash commands, no session persistence and `--max-budget-usd 3.00`. Included subscription only, as far as the environment shows: it refuses when `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, a Foundry key or a Bedrock/Vertex/Foundry switch is set. It cannot see an `apiKeyHelper` in settings or a stored console key, so the owner's usage-credits-off setting stays the real guard. The prompt goes on stdin, not argv. It requires Claude Code 2.1.321 or later. The result is accepted only if `is_error` is false, `modelUsage` is exactly `claude-fable-5-2` (a fallback model beside it is a mismatch) and the advice is non-empty. The advice goes to a 0600 file; the original session implements and verifies it.

## Cost estimate before invocation (SPE-176)

`estimate --case case.json` prints a list-price estimate. It calls no model and no network, neither creates nor reads the state directory, and does not check eligibility. Closed schema: `estimated_usd_low`, `estimated_usd_high`, `cap_usd`, `basis` (list of strings), `model` (`claude-fable-5-2`), `invoked` (always `false`). All figures are estimates of token use, not invoices.

- Low: a cold call writes the measured cold prefix (about 46k cache-creation tokens, $0.739) plus the consult packet, priced at the highest measured cache-write rate, counting the packet at 3 bytes per token (it leans high). A warm prefix cache can cost less; it is not used as a floor.
- High: the $4 per-consult reservation ceiling (`cap_usd`). The call itself runs with `--max-budget-usd 3.00`.
- Reference points in `basis`: the one completed real-route consult ($1.29, 2026-11-19) and the cold canary ($0.74, 2026-11-15), read from [consult-cost-reference.json](consult-cost-reference.json); `tests/test-fable-cost-estimate.py` fails if a figure no longer appears in its cited record.

## Circuit breaker

At most one consult per blocker, never retried. The breaker opens, and blocks every new READY transition and consult, on:

- `route_failure`: non-zero exit, `is_error`, unparseable or non-object output, any exception during the call, timeout, old client
- `model_mismatch`: the effective model is not exactly Fable 5.2
- `cap_overrun`: the reported cost exceeded the reservation
- `no_advance`: the original session records `verdict --result not_advanced`

Only `reset-breaker --note "<why resolved>"`, an owner action, closes it. Fallback on any failure or refusal is the existing baseline: Sonnet main plus `opus-architect`.

## Measuring benefit

`verdict --id <id> --result advanced|not_advanced --rework-rounds N --elapsed-s N --usd X` records the totals at acceptance next to the spend before the consult. `report` lists them. There is no control arm, so a single closed blocker cannot show a reduction; compare against an Opus-only blocker of similar kind before claiming one. The 2026-11-17 pilot (two tasks, equal quality, about twice the list cost) is the only evidence so far, and it showed no clear net benefit.

## Not done here

Installing the authorization file, confirming the retention setting and applying the skill text to `~/.claude` are owner steps (above). The real-route behaviour of the new argv (`--disallowedTools`, `--max-budget-usd 3.00`) is not verified: nothing here calls a real model.
