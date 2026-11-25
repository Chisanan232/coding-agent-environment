# Fable escalation: automatic consult inside a shared monthly budget (SPE-171)

Owner decision, 2026-11-25 (SPE-171): Fable (`claude-fable-5-2`) is a fourth routing tier for the owner's **personal, non-sensitive** engineering consults on the approved personal host. A consult runs **automatically** when a trigger is met and every gate passes; there is no per-consult approval. Company-managed sessions are out of scope. Default routes are unchanged (Sonnet main, `opus-architect`, Opus advisor). Fable is never a default model: no agent file, hook, scheduler or settings default names it, and `tests/test-fable-escalation.py` fails if one does.

Tool: `scripts/fable-escalation.py` (the only entry point). Tests: `tests/test-fable-escalation.py` and `tests/test-fable-cost-estimate.py` (a fake `claude` binary; no real model call).

## What an owner must still do

The tool is inert until the owner does these; the agent never does them.

1. Run `authorization template --cwd <an owned repo>` (read-only). It prints a candidate file, this host's hash and the file's sha256.
2. Confirm in the claude.ai account settings that "Help improve Claude" (model improvement) is **off** (see Retention below), then set `retention.model_improvement_setting` to `"off"` and `confirmed_on` to that date in the candidate.
3. Install the file yourself at `~/.config/coding-agent-environment/local/fable-authorization.json`, mode 0600, in a directory only you can write. The script never writes it.
4. `authorization check` (read-only) shows the verdicts: authorization, retention, host, repo owner, managed session, caps, isolation and the month's remaining budget.
5. Isolation verification (below): until `isolation.verified_on` is set, `attempt` never returns `CONSULT`. When one blocker is READY, the owner sets `isolation.live_verification_blocker` to its id in the authorization file (the agent cannot choose the case); the owner or coordinator then runs `consult --id <id> --execute --live-verification` once, reads the printed verdict and the saved stream, and only if it passes sets `isolation.verified_on` to that date.
6. Recommended, owner-managed (this change edits no settings): add a Claude permission rule that denies or asks before any write to `~/.config/coding-agent-environment/local/**`. The authorization file is a trust anchor that is owner-authored only by convention, so a session able to write it could widen its own scope, caps or retention state.
7. Apply the skill text to the live `~/.claude` with `scripts/profile-install.sh --claude-skills` (see `docs/ROLLOUT-USER-CLAUDE-MD.md`). Until then a live session does not carry the instruction and nothing consults.

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
| `retention` | `model_improvement_setting` `off`, `on` or `unconfirmed`; `confirmed_on` date or null; `off` requires a `confirmed_on` date |
| `isolation` | `verified_on` date or null (null, the default: the CLI isolation flags are not yet verified, so no consult is offered), and `live_verification_blocker` (16-hex blocker id or null: the one blocker the owner allows to run the live case) |
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
| `scope_repo_not_owned` | the working tree's `origin` remote (first remote if there is no `origin`) is not exactly `github.com/<owner>/<repo>` (https, `ssh://git@` or `git@github.com:` forms, parsed with a full match, so lookalike hosts, userinfo tricks and path-embedded `github.com` do not parse), or that owner/repo is not the target's `owner/repo`, or the owner is not in `repo_owners`. The git probe runs with every `GIT_*` variable removed |
| `managed_session` | `/Library/Application Support/ClaudeCode/managed-settings.json` exists (presence only), or `bin/coding-agent-profile resolve --cwd` does not answer with exactly `status=none name= root= overlay=` or `status=ok name=<allowed profile> ...` and exit 0. Garbage, extra tokens or lines, a non-zero exit, an unresolved or malformed profile all count as managed (fail closed). A missing resolver binary also counts as managed: without it the session cannot be shown to be personal |
| `api_env_present` | an API credential or alternate-provider variable is set (presence only, never printed); `consult` only |
| `ineligible_payload:<rule>` | see Payload |
| `monthly_budget_exhausted`, `blocker_already_reserved_this_month`, `consult_in_flight`, `ledger_unreadable` | see Budget |
| `isolation_unverified` | `isolation.verified_on` is null: `attempt` records the blocker READY but returns `CONTINUE`, and `consult` is refused unless it is the one `--live-verification` case |
| `live_verification_not_needed` | `--live-verification` after isolation was already verified |
| `live_verification_not_authorized` | `--live-verification` for a blocker the owner has not named in `isolation.live_verification_blocker` |
| `live_verification_already_used` | the host-wide one-shot marker (`<ledger_dir>/live-verification.json`) or an earlier verification record exists |
| `trigger_not_met` | the stored facts of a READY record no longer meet a trigger (re-evaluated at `consult`, so a hand-forged record cannot skip it) |
| `isolation_export_failed`, `isolation_secret_in_referenced_file` | see Isolation |
| `breaker_open` | the circuit breaker is open |

A refusal at `consult` leaves the blocker READY, spends nothing and can be retried (next month, after a fixed authorization, after a reset). `authorization template` and `authorization check` are read-only: they never write the authorization file, create the state directory or print a secret (`check` prints verdicts, not the host hash; `template` prints the hash because the owner needs it).

## Budget: one ledger for every session

Every dollar figure here is the client's list-price estimate (`costBasis`), not billed money. On the Max login with usage credits Off no money is billed per call. The ledger is an **enforceable local gate only for invocations that go through this tool**: it cannot see an interactive `/model` Fable session or any other client. The only provider-side stop is the owner's allowance meter (a coarse percentage in `/usage`) plus keeping usage credits Off.

Ledger: `<ledger_dir>/YYYY-MM.json` (`{month, entries[]}`) plus an append-only `journal.jsonl`, under `flock` on `<ledger_dir>/.lock`, atomic writes. The month is the reservation time in fixed UTC+8 (so `2026-11-30T16:00Z` is `2026-12`). Entry: `rid`, `blocker_id`, `session`, `reserved_usd`, `state` (`RESERVED` or `SETTLED`), `charged_usd`, `basis`, `reserved_at`, `settled_at`.

Inside `consult --execute`, after the gates and the version check:

1. Reserve under the ledger lock. `committed` = settled charges + the full `reserved_usd` of every RESERVED entry. Refuse `monthly_budget_exhausted` if `committed + per_consult > monthly`; refuse `blocker_already_reserved_this_month` if this blocker id already has an entry this month (this dedups across sessions even with different state directories); refuse `consult_in_flight` if any RESERVED entry is younger than the consult timeout plus a 60 s grace (600 s in all), which serializes consults host-wide under the ledger lock. A RESERVED entry dated more than 120 s ahead of that lock-time clock (clock stepped back, hand edit) is corrupt: it would look permanently fresh, so the reservation fails closed with `ledger_unreadable` and `month_budget` / `report` list it under `future_dated` for the owner to reconcile (an entry within the 120 s skew allowance still counts as fresh). The clock is sampled once inside that lock and used for the month key, the age check and `reserved_at`; when the window straddles a month boundary the previous month's file is read too (an unreadable one refuses `ledger_unreadable`); the shared breaker is rechecked there too (refusal `breaker_open`); otherwise record a RESERVED entry for the per-consult amount ($4). Budget and in-flight refusals do not trip the breaker: they are quota state (`consult_in_flight` clears when the first consult settles, or after the window).
2. Set the blocker CONSULTING (a crash after this leaves both spent).
3. Call `claude -p` with `--max-budget-usd 3.00`. The client checks that cap only between turns, so $1 of headroom sits inside the $4 reservation. The call's timeout is the 540 s timeout minus the time since the reservation timestamp, so it ends inside the in-flight window. The ledger lock is not held during the call. On a failed or overrunning call the breaker is opened before the entry is settled, so no waiter can reserve between the two. Lock order is always the blocker-state lock first, then the ledger lock.
4. Settle: a numeric `total_cost_usd` is charged (success or failure); missing or unparseable output, a timeout or any exception charges the full $4; a charge above $4 records `cap_overrun` and opens the breaker (the advice from an otherwise good answer is kept).

How interrupted calls are accounted: if the parent is killed or crashes after the reservation, the entry stays RESERVED at the full amount forever and is never auto-released. `report` lists a RESERVED entry older than the timeout (540 s) as `orphaned`; it is still counted. The owner can reconcile it by hand against the meter. An unreadable or corrupt ledger, or a ledger directory that is not private, refuses with `ledger_unreadable`.

`report` includes a `month_budget` section (committed, remaining, entries, reserved, orphaned) when the authorization is readable.

## Payload minimization

Minimum context only: no credentials, customer data or raw transcripts. Checked offline:

- At `attempt`, text that matches a rule is not stored and the call answers `note: not counted: <rule>`. A bad acceptance-check id is not tracked at all.
- At `consult`, the whole prompt and each stored text field are checked again, plus an 8 KB size cap. A hit refuses with `ineligible_payload:<rule>` before any reservation and does not trip the breaker.

Rules: `secret` (credential words, with quoted or suffixed keys, followed by `=` or `:`; `password is X` prose; Bearer tokens; `sk_live_`/`AIza` keys; `ghp_`/`sk-`/`sk-ant-`/`xox*-`/AWS key shapes, JWTs, PEM headers, `scheme://user:pass@`, any run of 40+ base64 or hex characters that contains a digit, so a full commit SHA is refused: cite it shortened), `transcript` (`Human:`/`Assistant:` line prefixes, `"role":`, `"type":"assistant|user|tool_use"`, `<system-reminder`, `.claude/projects/`, `.jsonl`), `email` (any address other than `noreply@`, including `user@localhost`), `size`. The secret patterns are a local equivalent of `owner-merge-local-ci.py`'s: that script is a hyphen-named module that loads the whole merge gate and runner on import, so it is not imported just for one regex.

The prompt carries only the blocker, the check, truncated attempts, review positions and the question, plus "Read only files in this repository; do not open .env*, credentials, ~/.ssh, ~/.config, ~/.claude".

Known limits of the rules (they fail closed, so a legitimate blocker can be held back): `git@github.com:owner/x` and `lodash@4.17.21` look like an email address; a long slash path with no `-` or `.` and a digit looks like a 40+ character base64 run; a prose line such as `token: the lexer ...` looks like a credential assignment. Reword the text or cite shortened identifiers.

## Isolation of the consult

What is certain: the consult does not run in the working tree. `consult --execute` exports the **committed** blobs of the repository (`git ls-tree` at `HEAD` plus `git cat-file`, so no working-tree or index read, no symlink to follow and no TOCTOU) into a private temporary directory, runs `claude -p` with that as its working directory and deletes it afterwards. That is a smaller working set, **not a confinement**: a tool given an absolute path can still read outside it unless a CLI control stops it. The controls are the CLI flags in the argv, and whether this CLI honours them in `-p` mode is **UNVERIFIED** until the live-verification case below:

- `--setting-sources project`: intended to keep user and local settings (allow rules, hooks, `~/.claude/CLAUDE.md` memory) out. [UNVERIFIED: whether it also stops `~/.claude/CLAUDE.md` from loading; check in the live case.]
- `--permission-mode dontAsk`: anything not allowed is denied.
- `--disallowedTools` denies for `Read(**/.env*)`, `Read(~/.ssh/**)`, `Read(~/.config/**)` and `Read(~/.claude/**)`.
- `--bare` is not used: it never reads OAuth, which is this route's login.

Export rules. Skipped: symlinks (mode 120000), submodules (160000), any path whose **component** matches a credential-looking name (case-insensitive: `.env*`, `*.env`, `*.env.*`, `.aws`, `.ssh`, `.gnupg`, `.config`, `.claude`, `.codex`, `.npmrc`/`.netrc`/`.pypirc`, `id_*` keys, `*.pem|key|p12|pfx|jks|keystore|kdbx`, and any name containing credential, secret, token, passwd, password or private key, so `tokenizer.py` is skipped too), files over 512 KB, and any file whose content has a NUL byte, is not strict UTF-8 (so UTF-16 or NUL-padded text cannot hide a secret; binary files are skipped too) or breaks any payload rule (secret, transcript marker, email address). Skipped-for-content files are counted as `export_content_skipped` in the consult receipt; if the prompt names one the consult refuses with `isolation_secret_in_referenced_file`. Uncommitted, staged, untracked and ignored files are never exported (commit first). A repository with no commit exports nothing. Content is scanned with a deadline check on every piece: each line, each window of three adjacent lines, and segments of the whitespace-collapsed file, so a secret split across lines or padded with whitespace is still seen and no pattern ever sees more than a few KB. A line over 4,096 characters counts as a hit. Credential-looking assignments are found by locating `identifier =` / `identifier :` once per word and deciding on that one word, so identifiers and whitespace runs of any length are covered in linear time; the URL-credential pattern accepts any character before the scheme. Fail closed with `isolation_export_failed`, before any reservation: a tree path that is absolute or has an empty, `.`, `..` or `.git` component, a `git ls-tree` listing that passes 50,000 entries (every entry counts) or 16 MB (streamed; git is killed and reaped as soon as a cap or the deadline is hit), more than 10,000 files or 16 MB (judged from the sizes in `git ls-tree -l` before any blob is read), a blob that differs from its listed size, any write failure (exclusive, no-follow creation under the real export root) and a 120 s overall deadline. The export runs while the blocker-state lock is held, so a slow export delays other sessions' attempts on the same state directory for up to that deadline; the ledger lock is never held during it.

### First-run verification (`isolation.verified_on`)

Until the owner sets `isolation.verified_on`, the first eligible blocker cannot consult: `attempt` records it READY but answers `CONTINUE isolation_unverified`. The one way forward is the single deliberate case `consult --id <id> --execute --live-verification`, which only runs for the blocker the owner named in `isolation.live_verification_blocker`. It is **one-shot and host-wide**: it exclusively creates `<ledger_dir>/live-verification.json` inside the same ledger lock as the reservation, after every reservation check has passed and before the call (so a `consult_in_flight`, `breaker_open`, budget or `ledger_unreadable` refusal never consumes the case), and any later run (any session, any state directory) is refused with `live_verification_already_used`, as is a run when an earlier verification record exists. Once the marker is claimed, a failure of the call does not give the case back (a failed write of the reservation itself removes the marker again); to run it again the owner deletes the marker by hand. It otherwise obeys every gate, reserves budget and honours the breaker like any consult. A verdict with `pass` false opens the breaker (`isolation_verification_failed`).

The run uses `--output-format stream-json --verbose` and a deterministic probe preamble ahead of the real prompt. The tool creates one readable canary inside the export and token canaries in a sibling temp directory outside it (a plain file and a `.env`), and asks for exactly these Read calls: the inside file, the two outside canaries, and three random, guaranteed-nonexistent names `~/.ssh/fable-probe-<hex>`, `~/.config/fable-probe-<hex>` and `~/.claude/fable-probe-<hex>`. No real credential or memory file (no `id_rsa`, no `CLAUDE.md`) is ever named, read or created, so a failed isolation cannot send one to the provider; memory/CLAUDE.md loading is judged from the init event alone. The tool creates nothing in the denied home directories. The full stream is saved (0600) under `<state dir>/verification/<id>.jsonl`. `pass` needs positive evidence for every line, and missing evidence is a failure, never a pass: the init event lists only Read/Grep/Glob, no MCP server and `permissionMode` `dontAsk`; the inside canary was read; every outside or denied probe was attempted by exactly one call (a duplicate call fails) and its result is an **error** whose whole text matches the anchored permission-denial message, without the canary content (a denied probe that returns normally, whatever it says, is a leak; "not found" is not a refusal); no canary content appears anywhere in the stream; no other tool was used and no other call touches a denied, `..` or outside path in ANY of its path-like inputs (`file_path`, `path`, `pattern`, `glob`, `notebook_path`; only a Read whose sole path input equals a probe is exempt); and the init event shows memory/CLAUDE.md loading is off (an init event that does not show it leaves it `unknown`, which fails). The exact CLI denial wording and whether the Read tool reports a permission refusal (rather than "not found") for a nonexistent file in a denied directory are UNVERIFIED; if the live case shows the CLI cannot tell them apart, the verdict fails closed and the fallback would be tool-created token canaries with random names in the denied directory (created only if absent, removed in a `finally`, never touching an existing file). That fallback is not implemented. Read the stream yourself before setting `verified_on`. This cannot be tested without a paid call, so the real behaviour is UNVERIFIED here: the tests use a fake `claude` that emits scripted streams.

### Budget is a soft cap

`--max-budget-usd 3.00` is checked by the client only between turns, so one turn can overrun it. The ledger caps *reservations* ($4 each, $40 a month); an overrun is only seen at settle time (charged as reported, `cap_overrun`, breaker opens). Only one consult can be in flight host-wide: a new reservation is refused (`consult_in_flight`) while any RESERVED entry is younger than 600 s (540 s timeout + 60 s grace). A RESERVED entry older than that is an orphan: it still counts toward the monthly total but no longer blocks, so a killed parent cannot wedge Fable. The worst-case overshoot is therefore one overrunning turn, not ten. The size of one overrun is bounded by one turn's input, which is bounded by the model's context, not by the export cap; as an order of magnitude, at the highest measured cache-write rate ($16 per million tokens, from the $0.739 / 46,190-token canary) a million-token turn would be about $16 beyond the $40 (an estimate, not a measurement; the ledger holds list-price estimates, not billed money). That remaining one-turn overrun is not removed. The export caps (16 MB, 512 KB per file) were lowered from 64 MB / 1 MB to keep what one consult can pull in smaller; they do not make the overshoot a hard bound. The only hard stops are the provider-side allowance meter and usage credits staying Off.

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

The breaker lives in the ledger directory (behind the ledger lock), so a fresh `FABLE_ESCALATION_DIR` cannot bypass an open one; a corrupt shared breaker file counts as open. `reset-breaker` clears the shared and the local one.

Only `reset-breaker --note "<why resolved>"`, an owner action, closes it. Fallback on any failure or refusal is the existing baseline: Sonnet main plus `opus-architect`.

## Measuring benefit

`verdict --id <id> --result advanced|not_advanced --rework-rounds N --elapsed-s N --usd X` records the totals at acceptance next to the spend before the consult. `report` lists them. There is no control arm, so a single closed blocker cannot show a reduction; compare against an Opus-only blocker of similar kind before claiming one. The 2026-11-17 pilot (two tasks, equal quality, about twice the list cost) is the only evidence so far, and it showed no clear net benefit.

## Not done here

Installing the authorization file, confirming the retention setting and applying the skill text to `~/.claude` are owner steps (above). The real-route behaviour of the new argv (`--disallowedTools`, `--max-budget-usd 3.00`) is not verified: nothing here calls a real model.
