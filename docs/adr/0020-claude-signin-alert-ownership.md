# ADR 0020: Claude Code sign-in interruption alert, single owner (SPE-176)

Status: accepted for the repository (observe-first). **Hosting the watch from the engineering supervisor is an open owner decision**
(section 5). Installing the persistent trigger is an owner-run step (`docs/CLAUDE-AUTH-WATCH-INSTALL.md`).

## Context

On 2026-11-19 from 15:48Z to 19:40Z four Claude Code sessions stopped on `authentication_failed` and nobody was told until Remote Control
disconnect notices arrived hours later; the sessions resumed between 00:08Z and 00:55Z, after sign-in. No component owned "sign-in is needed":
the engineering supervisor's watchdog is read-only with no auth handling and its Telegram daemon only delivers fixed daily report slots, and the
admission monitor watches the queue, not the account.

## Decision

1. **One owner.** `scripts/claude-auth-watch.py` is the only detector and the only sender of Claude sign-in alerts. `session-notify` is its sender
   transport (merged Telegram backend, `emit-resource` dedup, `--clear`). The supervisor watchdog and the admission monitor must not detect or alert
   on authentication, and must not add a second sender, because two senders cannot be deduplicated against each other.
2. **Evidence is structured, never prose.** Supported signals (Claude Code 2.1.328):
   - `claude auth status` JSON key `loggedIn`. Only `False` means logged out; a failing, timed-out or unparseable command is UNKNOWN and never alerts.
     The logged-out exit code and shape are not verified. The command is run only by the real tick, never by a test (an isolated run of the real CLI
     leaked an env or keychain credential), and its output is parsed for that one key and discarded.
   - Session transcript records with `isApiErrorMessage: true` and `error: "authentication_failed"` (other error values are not auth). A later successful
     assistant record (no `error`, not an API-error message) means the session recovered.
   - Remote Control `system` records. "OAuth token unavailable" counts as auth only when corroborated by a native `authentication_failed` record or a
     logged-out status. "Session creation failed" is a separate, non-auth cause.
   - The session registry `~/.claude/sessions/*.json` (name, sessionId, pid only) supplies names and "gone".
3. **Verdict (recency first).** Only fresh evidence can open an incident: the newest unrecovered `authentication_failed` record of a session must be at most
   `FRESH_S` = 30 min old AND so must its transcript file's mtime. Older unrecovered failures are listed in `scan` as class `none`, evidence `idle`, and never open
   an incident; neither a registry entry with pid `null` nor an unreadable registry makes a stale transcript live. Given fresh failures, the auth status decides:
   - logged out (definite): any fresh failure, or a fresh RC "OAuth token unavailable" alone, opens the incident.
   - unknown (missing binary, timeout, garbled output, exit error): a fresh failure PLUS a fresh RC "OAuth token unavailable" record at or after it. Several failed
     sessions alone do not alert.
   - logged in (definite): vetoes, unless at least one session has a failure at most 10 min old with no later success AND (two or more sessions inside one 30-minute
     cluster, or an RC corroboration). **Risk, NOT_VERIFIED:** an expired-but-present token may still report `loggedIn: true`, which is why the override exists and
     why a single strong failure with no corroboration is not enough.
   Otherwise the verdict is `unknown` (fresh but uncorroborated) or `none`. The incident epoch is the first FRESH unrecovered failure (at most 30 min old when the incident opens), never an older failure
   of a session that has been failing continuously; it is stored when the incident opens, the key is `hash(host, epoch)`, and neither moves while the incident is open.
   Records or files dated later than now + 5 min are dropped and counted (`future_ts_dropped`), so a future-dated record cannot outrank real later evidence. The Remote Control disconnect verdict ("Session creation failed") has the same 30-minute recency
   rule, and its 6-hour cooldown stays.
4. **Notices.** One notice on the false-to-true transition (strict delivery; recorded as handed-off, delivered and pending separately, never "delivered"
   on no-backend or an unknown outcome), one recovery notice then `--clear` only on affirmative evidence: the status is definitely logged in, at least one listed session produced a successful
   assistant record after the epoch, and NO live session has a fresh unrecovered failure (a session that resumed while another failed minutes ago does not close
   the incident). With no resumed turn, 15 quiet minutes (logged in, no fresh failure) close the incident SILENTLY: no "sessions resumed" claim, no recovery notice,
   no `--clear`, and the tick reports `closed-without-resume-evidence` (a logged-in status can still hold an expired token). An incident is aged from its own start and
   its last fresh failure, never from the epoch: a still-failing session keeps it open indefinitely, and one with no fresh failure for 48 h (`WINDOW_S`) closes silently.
   Every closed epoch is remembered (bounded, 16) so the same incident never reopens; only a new fresh failure, with a new epoch, opens a new incident. A silent close never calls
   `--clear`, so it cannot re-arm the same key. No periodic reminder is sent (one deduplicated `still-required` reminder per 24 h is a possible later addition). A recovery notice is sent only if the alert had been handed off.
   A Remote Control-only disconnect is a separate cause with its own phrase, never worded as sign-in, at most one per incident and one per 6 hours, and is
   suppressed while a sign-in incident is open. Text is enum phrases plus one closed `detail` (host short name and up to 6 sanitized session names:
   `[A-Za-z0-9 ._(),+=-]`, at most 160 characters, no URL, newline or markdown syntax). session-notify gives these causes their own synthetic session
   (`claude-auth-watch`) so their dedup and `--clear` do not interact with the admission monitor's.
5. **Supervisor hosting (owner decision, not taken here).** The supervisor could call `claude-auth-watch.py tick --send` from its own scheduler instead of
   a second LaunchAgent: the script is import-safe (`Env`, `Sources`, `Notifier` are injectable) and a tick is idempotent, lock-guarded and holds its own state, so
   the supervisor would only own the schedule. Until the owner decides, the standalone LaunchAgent is the single trigger. Running both would not double-send
   (same incident key, session-notify dedup) but is not supported.
6. **Reconcile.** `reconcile --since <epoch>` is read-only: surviving queue entries by state with live holder or waiter evidence, receipts released, expired
   or cancelled since the epoch, per-session first successful turn after the epoch, and duplicate-submission findings, each with a severity. Only hard evidence is
   FAIL: more than one LIVE entry (QUEUED, GRANTED, CLAIMED) with the same fingerprint (the fingerprint already covers label, worktree and argv). WARN, with the
   evidence listed and never FAIL: the same ticket and worktree with different jobs (a legitimate build plus test), a label shared by live entries with different
   fingerprints, a label enqueued again after its release (a legitimate retry) and a label enqueued more than once since the epoch. NOT_VERIFIED: a session shows no
   resumed work, or a live entry's `session_id` maps to no transcript session (never PASS). Overall: FAIL on any hard finding, else NOT_VERIFIED when mapping or
   evidence is missing, else WARN when only warnings exist, else PASS. Closed output keys: `schema since at verdict queue{by_state problems unmapped_live_entries}
   survivors survivors_more completed_during_outage completed_more sessions[{name resumed_at verdict findings}] findings[{kind severity label count}]`.

   **Flap control and notice policy.** A recovery is announced, or an incident closed, only after the account has been clean for `QUIET_RECOVER_S` = 15 min continuously
   (definitely logged in, no fresh failure in any live session; a resumed turn is needed for the notice). Re-failure handling: a failure within 30 min (`REFOLD_S`) of a close
   is folded into the same incident record (`recurred`, counted, no new alert) unless a recovery was delivered (or dropped by the cap) AND the failure has lasted 10 min
   (`REFAIL_NEW_S`); then it is a new incident with its own alert. After more than 30 min a failure opens a new incident.
   - **Alerts are never under the rolling cap.** Exactly one alert per incident, at most one NEW incident alert per 30 min (`ALERT_SPACING_S`) and at most 12 per 6 h. An alert
     held back by the spacing is deferred and sent at the first tick the spacing allows while the incident is open. An incident that ends before its alert could be sent is
     recorded as `outage-unannounced` (`outage_unannounced` in the tick result and `status`) and the next alert's `detail` carries `missed=N` (existing closed detail field, no new phrase).
   - **Recovery and Remote Control notices** share 3 per rolling 6 h (`RATE_CAP`); beyond it they are dropped and counted (`rate_limited`, `rate_limited_at`).
   - **Re-drive.** A pending, dropped or unknown-outcome alert is re-armed (`--clear --scope auth` for the auth session only, then a fresh strict emit) 7 times at 30 min,
     then hourly after a definite drop or failure, with no hard cap while the incident is open, so an alert survives a transport that is down for hours. While the outcome is
     ambiguous (`unknown` or `pending`) the slow rate is every 6 h (`AMBIG_S`). Recoveries and RC notices stop after 7 hand-offs. An ambiguous outcome can produce duplicate
     alerts after a sweep delivered one; accepted over a lost alert, bounded at 7 fast hand-offs plus 4 per day while the incident stays open. Every notice's `detail` ends with
     `inc=<6 hex>` (the incident key prefix, inside the existing closed charset) so a recipient can recognise a duplicate.
   - Persisted schedule times are clamped on load: a retry time at most 60 min ahead, any recorded past time at most now, so a corrupt future value cannot stall an unsent alert.
   - Every persisted number is range- and type-checked on load (finite, bounded, no bool or huge integer); a sent-log entry dated in the future or older than 6 h is ignored;
     an incident record with a phase outside `alerting`/`recovered` is reset. None of this can wedge the watch.

## Safety properties

No login or logout, no model call, no network of its own (session-notify sends), no signal, no deletion, no queue or admission mutation, no email, account
id, token or command line is read, stored or printed. State lives in `CLAUDE_AUTH_WATCH_DIR` (default `~/.cache/coding-agent-environment/claude-auth-watch/`,
0700, files 0600, atomic, bounded to 64 KiB, flock). Transcripts are read as tail windows only (256 KiB per file, newest 400 files within 48 h, lines over 64 KiB
skipped, symlinks and non-regular files never followed, a 20 s budget). Kill switch: `CLAUDE_AUTH_WATCH_DISABLED=1` or a `DISABLED` file in the state directory.
A sleep, reboot or clock step is recorded as a gap and never counts as evidence.

## Status unknown never alerts alone

When `claude auth status` cannot answer (binary missing from the LaunchAgent's PATH, timeout, garbled output, non-zero exit) the watch alerts only for a fresh native failure PLUS a
fresh Remote Control "OAuth token unavailable" record. Several failed sessions alone do not alert, because without the status the same evidence also fits a transient API
error burst. The consequence: if the `claude` binary is not found (set `CLAUDE_AUTH_WATCH_CLAUDE_BIN` or install it in `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`), sign-in
interruptions without an RC disconnect are not alerted; `status` shows `status: unknown`.

## Unverified assumptions

- The logged-out output and exit code of `claude auth status` (only `loggedIn is False` is treated as logged out).
- Transcript record shapes (`isApiErrorMessage`, `error`, `type`, `timestamp`, `sessionId`) and the Remote Control `system` record wording, observed on Claude Code 2.1.328 only.
- That a failure record is written when a session hits an authentication error, and a later assistant record means the account works.
- That a definite `loggedIn: true` can coexist with an expired token (hence the veto override and the silent no-evidence close).
- The 30 min, 10 min, 15 min, 6 h and 48 h thresholds are chosen, not measured; live observe-only operation should confirm them before `--send` is enabled.

## Known limits

- Evidence lives in transcripts the sessions write; a session that is idle and never writes again shows no failure until it does (the registry cannot say why it stopped).
- The logged-out output shape of `claude auth status` is unverified; if it fails or changes, only a fresh failure plus an RC "OAuth token unavailable" record can alert (unknown status) and a logged-out status cannot be seen.
- A session that fails once and then stays silent for more than 30 minutes is never alerted on (it is listed as idle); the watch trades that miss for no stale or false alerts.
- Definite logged-in with an expired token is only partly covered (see the risk in section 3).
