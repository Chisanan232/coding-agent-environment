# Session status and notifications (SPE-120)

Shared, product-neutral capability: a short-lived hook process records each Claude Code session's state durably and sends a notification only when the state changes in a way the owner should act on. Implemented in `scripts/session-notify.py` (stdlib only, no daemon). Status: **version-controlled and unit-tested; not deployed, not live-validated.** Mobile push is implemented but has never been contacted and is untested pending owner approval of a service.

## Truth model

- `Stop` and `SessionEnd` are **not** project completion. A turn ending is `IDLE`.
- `COMPLETED` and `BLOCKED_*` come **only** from a structured checkpoint file written with `session-notify.py checkpoint`; `COMPLETED` also needs a `verified` record (who verified and a durable reference) and a checkpoint written *after the current run started* and not already acted on. Every `SessionStart` (resume included) and every `UserPromptSubmit` (even while already `RUNNING`) opens a new run, so a file left by an earlier run or task never counts; a checkpoint is also used at most once.
- The hook payload's text fields (`message`, `last_assistant_message`, `transcript_path`) are never read. Free-form model prose is never evidence of QA sign-off or release readiness.
- The checkpoint is written by the agent, so it is treated as untrusted input: every field is rebuilt from an allowlist before it can reach any backend (below). It records a claim; it does not prove one. Verification evidence (CI run, review verdict, merge-gate PASS) still lives in the systems that produced it.

## States and transitions

| State | Meaning | Reached by | Notifies |
|---|---|---|---|
| `RUNNING` | A run is in progress | `SessionStart`, `UserPromptSubmit`, or any `PostToolUse` after a block/idle/finish/failure | no |
| `IDLE` | Turn ended, nothing declared | `Stop` with no (valid, fresh) checkpoint | no (`notify.idle` reserved, off) |
| `COMPLETED` | Verified completion declared for this run | `Stop` + fresh checkpoint `COMPLETED` with `verified` | yes |
| `BLOCKED_OWNER` | Waiting for the owner | `Notification` of `permission_prompt`, `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input`, or `idle_prompt` from RUNNING/IDLE; or checkpoint | yes |
| `BLOCKED_DEPENDENCY` / `BLOCKED_RESOURCE` | Waiting on another change / on a resource | checkpoint at `Stop` (needs a resume trigger) | yes |
| `FAILED` | API failure, or no heartbeat | `StopFailure`; checkpoint `FAILED`; `sweep` finds a silent RUNNING session | yes |
| `SESSION_ENDED` | Session closed | `SessionEnd` | only if it ended while RUNNING for reason `other`/`logout` |

Notification key = session + task + transition + PR head, remembered together with the *cycle* it was sent in. A cycle starts each time the state enters RUNNING from something else, so approving a prompt and hitting a second prompt notifies again; a **resume does not open a new cycle**.

**Delivery contract: best-effort delivery with bounded retries; after the retries are exhausted the notification is dropped and counted (`dropped` in `status`); deduplication is best-effort inside a bounded window.** A notification can therefore be delivered zero times (every attempt failed, or its record was untrusted) or, outside the window, more than once. A repeated hook call or a restart does not repeat a notification as long as the delivered key is still remembered: memory is kept for 24 hours (`DEDUP_WINDOW`) and at most 500 keys per session (`NOTIFIED_CAP`, oldest forgotten first, silently), and a whole finished-session record is removed after 14 days. Outside that window or past that cap the same transition may notify again. A tombstone only ever moves to a newer cycle, so a late retry of an older cycle cannot reopen an already-delivered one. No delivery guarantee is made, in either direction.

Checkpoint freshness is two floors, nothing more: `run_started` (set by every `SessionStart` and `UserPromptSubmit`) and `cp_floor` (the mtime of the last checkpoint a `Stop` acted on). A checkpoint counts only if its mtime is newer than both. Tool progress (`PostToolUse`) after a block, idle, finish or stale failure changes the state but never touches either floor, so a completion written by that very tool call stays valid, and a checkpoint already used cannot complete or block a second time.

## Hook events (verified against Claude Code 2.1.292)

Checked in the installed binary and in the official hooks reference: `SessionStart` (`source`), `UserPromptSubmit`, `PostToolUse`, `Notification` (`notification_type`: `permission_prompt`, `idle_prompt`, `auth_success`, `elicitation_dialog`, `elicitation_url_dialog`, `elicitation_complete`, `elicitation_response`, `agent_needs_input`, `agent_completed`, `quota_auto_resume_*`), `Stop` (`stop_hook_active`), `StopFailure` (`error_type`), `SubagentStop`, `TaskCompleted` (`task_id`), `SessionEnd` (`reason`: `clear`, `resume`, `logout`, `prompt_input_exit`, `other`). Fields relied on: `session_id`, `cwd`, `hook_event_name`, `notification_type`, `error_type`, `reason`, `source`, `stop_hook_active`. `quota_auto_resume_*` semantics are not documented, so they are ignored. Unknown events and fields are ignored, never fatal. `StopFailure` output and exit code are ignored by Claude Code; the script prints nothing on every path so it cannot influence `Stop`.

| Event | Effect |
|---|---|
| `SessionStart`, `UserPromptSubmit` | RUNNING (new run) |
| `PostToolUse` | heartbeat; flips a blocked/idle/finished session back to RUNNING |
| `Notification` | owner-block types above; `agent_completed` is a heartbeat plus an optional notice (`notify.agent_completed`, default off) |
| `Stop` | IDLE, or the checkpoint's COMPLETED/BLOCKED_*/FAILED when valid and fresh |
| `StopFailure` | FAILED with an allowlisted kind (`rate_limit`, `overloaded`, ...) |
| `SubagentStop`, `TaskCompleted` | heartbeat and a history line; never a notification |
| `SessionEnd` | SESSION_ENDED |

## The checkpoint

The agent runs, from its worktree (nothing is hand-written):

```bash
python3 scripts/session-notify.py checkpoint --state BLOCKED_OWNER --task "SPE-120 notifier" --ticket SPE-120 --pr 101 --head <sha> \
  --blocker-owner owner --resume-trigger owner_decision
python3 scripts/session-notify.py checkpoint --state COMPLETED --ticket SPE-120 --pr 101 --head <sha> \
  --verified-by codex-review --evidence "PR#101@<sha>"
```

`--blocker-owner` takes one of `owner dependency resource ci reviewer other`; `--resume-trigger` takes one of `owner_approval owner_decision dependency_merged ci_green review_complete resource_available credentials_provided manual_restart other`. There is no free-text field that can reach a notification.

Written to `.claude/.session-status.json` (gitignored, 0600) or `SESSION_STATUS_FILE`. Schema: `{schema:1, state, task, repo, ticket, pr:{number,head}, workflow_state, blocker:{owner,resume_trigger}, verified:{by,evidence}, session_id?, updated}`. One active session per worktree is assumed (the existing one-worktree-per-ticket rule); an optional `session_id` pins a file to one session. The ticket also falls back to `CLAUDE_CURRENT_TICKET`.

## Delivery

Two independent backends, both **off** until `~/.config/coding-agent-environment/session-notify.json` enables them (`render-config` prints a template with everything off):

- `macos`: `osascript` `display notification`; title and body travel as separate argv items to an AppleScript `on run argv` handler, never through a shell or string-built script.
- `ntfy`, `pushover`: disabled by default and **never contacted by this change or its tests** (tests inject fakes). Each needs `enabled: true` **and** `approved_destination` equal to the server host. Credentials are environment-variable **names** (`topic_env`, `token_env`, `user_env`) or file paths (`topic_file`, `token_file`, `user_file`) in the config, never values. A file is used only if its path is absolute, it is a regular file (no symlink) owned by you and not group- or world-accessible (`chmod 600`); anything else counts as not configured. The environment variable wins when set and non-empty, otherwise the file is read. Neither the path's content nor the value is ever logged. Remote backends are refused for the `corporate` profile and for any session whose working path contains a configured `corporate_markers` entry.

Payload: **only** the state, a repository name, a ticket, a PR number, an enum blocker owner, an enum resume trigger rendered as a fixed phrase, a failure kind and a fixed phrase for stale/ended/agent events. The same body goes to every backend. Each field is rebuilt from a strict validator or fixed enum, never copied: the ticket must match `ABC-123`, the PR must be an integer, enums must be members, and the **repository name must appear in `known_repos` in your config and also pass the secret and corporate-marker checks**, otherwise the notice says `unlisted`; the ticket label passes the same two checks (a ticket like `ACME-123` is dropped when `ACME` is a marker). The checkpoint's `task` and `workflow_state` are short plain text kept for local identity and dedup and are never sent. Never sent: commands, environment values, keys, source, logs, transcripts, model text, paths, corporate content. The check runs when a notification is queued **and again at every delivery attempt** on whatever is on disk, and corporate status (the `corporate` profile, or a `corporate_markers` match on the session's last working directory, stored whole and never truncated, sticky once seen; a path too long to store, over 4096 characters, counts as corporate) is also re-evaluated at delivery, so a queued remote notice is revoked if the session turns out to be corporate or a marker is added later. Remote requests are plain `https` POSTs to the approved host with **no redirect following and no environment proxies**, so the credential is never forwarded elsewhere.

Reliability: state is persisted as `sending` before a transport runs; failures retry with backoff 30 s, 60 s, 120 s, 240 s (cap 900 s, 5 attempts) and are then dropped; retries run from the next hook of that session or from `sweep`. A send interrupted by a crash is recovered after 5 minutes and **counts toward the same attempt cap** (checked before sending), so it can repeat at most within that cap. A queue limited to 20 entries never discards silently: the oldest entry gets a terminal `dropped` (`queue-overflow`) outcome, is remembered for dedup, adds a history line and increments `dropped` (shown by `status`). A raising transport is a failed attempt, never a crash.

## Crash and silence handling

Hooks cannot run after `kill -9`, power loss or a reboot. `session-notify.py sweep` (cheap, run on a timer; not installed here) marks a `RUNNING` session whose last heartbeat is older than `stale_after_seconds` (default 2700) as `FAILED` once with the notice "no heartbeat". `PostToolUse` is the heartbeat, throttled to one write per 30 s, so a long build inside one tool call is covered by the threshold. Blocked, idle and ended sessions are never stale. Records finished for 14 days are removed. Example timer (a template, not installed):

```xml
<!-- ~/Library/LaunchAgents/dev.coding-agent-environment.session-sweep.plist -->
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>/ABS/PATH/scripts/session-notify.py</string><string>sweep</string></array>
<key>StartInterval</key><integer>300</integer>
```

## Safety properties

The hook always exits 0 with empty output, has a 10 s alarm, reads at most 256 KiB, and logs only event names and error classes (never payload content). State is opened with `O_NOFOLLOW`, must be a regular file owned by the user under 256 KiB whose structure conforms to one declarative schema (type, enum, bounds, required and closed key sets, nested; no booleans or NaN/infinity standing in for numbers), with a matching session id, otherwise it is *not trusted*: no change, no notification, shown as `UNTRUSTED` by `status`. The diagnostic log is written without ever blocking (`O_NONBLOCK`, regular files only, so a FIFO planted at its path is ignored) and the 10 s hook deadline ends the process outright (`os._exit`) at the first alarm after a best-effort log line, so nothing that catches exceptions can keep the hook alive; state writes are atomic and a delivery is persisted as `sending` first, so that exit loses nothing that a retry cannot recover. A session lock only counts if its path still names the inode that was locked, and garbage collection removes a lock only while holding it, so a waiter on a replaced lock file retries instead of running beside the new holder. Checkpoint contents and freshness come from the same opened file. **One validator guards every boundary** (hook payload, config, checkpoint, state file, pending deliveries): values are checked against a schema before anything indexes or iterates them. A payload with a wrongly typed consumed field is ignored; a checkpoint with a wrongly typed or unknown field anywhere (nested ones included) is absent; a config leaf of the wrong type or a subtree of the wrong container takes its default (an invalid `profile` or `corporate_markers` makes the profile `corporate`) and the problem is logged by key name; an unknown config key is reported, not applied; a state record that does not conform is untrusted as a whole. One unusable pending entry is dropped and counted without stopping the other deliveries. The checkpoint command validates what it writes against the same checkpoint schema (one schema, both directions), and a `BLOCKED_*` checkpoint must carry a `resume_trigger` from the fixed list or it is absent. `sweep --now` accepts only a finite epoch (0 to year 2100), and a record that does not conform is never written. A fuzz test mutates every field of each boundary to many wrong values and derives the expected verdict from the schema with an independent reference check; a lint test looks for external values used before validation, but it is **best effort only**: it is line-ordered, not a data-flow analysis (not path-sensitive, not interprocedural, does not follow container or call-result aliases), so the guarantee rests on the structure and the fuzz test, not on the lint. Session ids are restricted to `[A-Za-z0-9_-]{1,80}` so they cannot form a path. Concurrent hooks serialize on a per-session `flock`.

## Local verification (reproducible smoke test)

Uses a throwaway state directory, config and checkpoint; it touches nothing under `~/.claude` and, with only `macos` enabled, shows one local banner.

```bash
T=$(mktemp -d)
export SESSION_NOTIFY_STATE_DIR=$T/state SESSION_NOTIFY_CONFIG=$T/config.json SESSION_STATUS_FILE=$T/checkpoint.json
python3 scripts/session-notify.py render-config > $T/config.json
python3 - <<PY
import json; p="$T/config.json"; c=json.load(open(p)); c["backends"]["macos"]["enabled"]=True; json.dump(c,open(p,"w"))
PY
hook() { echo "{\"hook_event_name\":\"$1\",\"session_id\":\"smoke\",\"cwd\":\"$T\"$2}" | python3 scripts/session-notify.py hook; }
hook SessionStart ',"source":"startup"'
hook UserPromptSubmit ''
sleep 1   # the checkpoint must be newer than the run start
python3 scripts/session-notify.py checkpoint --state COMPLETED --task smoke --verified-by qa-agent --evidence ci:1 --file $T/checkpoint.json
hook Stop ''             # one COMPLETED banner
python3 scripts/session-notify.py status     # state=COMPLETED; a CONFIG_PROBLEM row would show any rejected config key
hook Stop ''             # replay: no second banner (the checkpoint was already used, so the state falls back to IDLE)
python3 scripts/session-notify.py status     # state=IDLE
```

`--verified-by` and `--evidence` must be plain references such as `qa-agent` and `ci:1` (no spaces, paths or secret-like words). `render-config` output loads with no problem; an unknown key is reported (in `status` and `hook.log`) and ignored while every other setting still applies, and keys starting with `_` (such as `_note`) are comments and ignored silently.

## Deploy and rollback (owner-run; nothing here edits live settings)

1. `python3 scripts/session-notify.py render-config > ~/.config/coding-agent-environment/session-notify.json`, then set `macos.enabled` to `true` and list your repository names in `known_repos`. Leave `ntfy` and `pushover` disabled.
2. Back up `~/.claude/settings.json` (`cp -p`, record `shasum -a 256`), then merge the `hooks` block of `docs/examples/session-notify-hooks.example.json` by hand after replacing the script path. The existing hooks (`rtk`, disk admission, others) stay; add these entries alongside, not instead.
3. Start a **new** session; run the scenarios below. Existing sessions keep the hooks they loaded.
4. Rollback: remove the added hook entries (or restore the backup), set `macos.enabled` to `false`. State under `~/.cache/coding-agent-environment/session-status/` is safe to delete.

## Validation scenarios (live validation is pending the owner's deployment)

The unit tests replay each scenario against scratch state with fake transports (unit tests with a regression test for every review finding). The live check for each is below; none has been run live yet.

| # | Scenario | Run | Observe |
|---|---|---|---|
| 1 | Ordinary completion | In a throwaway session write a `COMPLETED` checkpoint with `--verified-by`/`--evidence`, then end the turn | one `COMPLETED` banner; `status` shows COMPLETED; a second turn with no new checkpoint stays quiet |
| 2 | Blocked owner | Trigger a permission prompt (a command with an `ask` rule on a throwaway branch) | one `BLOCKED_OWNER` banner; after approving, the next prompt notifies again |
| 3 | Background agent completion | Run a background agent; then set `notify.agent_completed` to `true` | default: no banner; enabled: one "background agent finished" notice, state stays RUNNING |
| 4 | API failure | Real failures cannot be forced safely: replay `echo '{"hook_event_name":"StopFailure","session_id":"t1","error_type":"rate_limit"}' \| python3 scripts/session-notify.py hook` with `SESSION_NOTIFY_STATE_DIR` set to a scratch directory | one `FAILED` banner with `rate_limit`; live StopFailure unverified until one occurs naturally |
| 5 | Session exit | `/exit` at the prompt, then exit via a terminal close | `prompt_input_exit`: no banner, state SESSION_ENDED; ending while RUNNING with reason `other`: one `SESSION_ENDED` banner |
| 6 | Abrupt termination and recovery | `kill -9` **only a throwaway session you started**, wait past `stale_after_seconds` (or pass `sweep --now`), run `sweep` | exactly one "no heartbeat" `FAILED` banner; a second sweep adds none |
| 7 | Concurrent sessions | Two sessions in two worktrees | `status` lists both with their own repo and ticket; each notifies independently, no cross-talk |
| 8 | Resume after reboot | After a restart run `claude --resume <id>` | state record survives, `SessionStart(resume)` starts a new run (earlier checkpoints stop counting); an old notification does not repeat while its key is inside the dedup window |

## Owner decisions still open

Whether and which mobile service to approve (ntfy or Pushover; no choice and no contact has been made); whether to wire `sweep` to launchd; the `stale_after_seconds` value for 10-30 hour runs; which paths count as `corporate_markers` on a corporate machine; the repository names to list in `known_repos` (an empty list means every notice says `unlisted`).

## Evidence tokens and approved contract

Founder approved the contract on 2026-10-22: best-effort delivery with bounded retries, best-effort dedup inside a 24 h / 500-key window, a new prompt or resume discards earlier checkpoints, a working path over 4096 characters counts as corporate, state schema v3. Mobile push remains unapproved and uncontacted.

Delivery and what the owner sees are separate evidence and are never merged into one claim:

| Token | Meaning | Source | Status |
|---|---|---|---|
| `LOCAL_NOTIFICATION_DELIVERED` | the macOS backend ran (`osascript` exit 0) and the state file records the notice as delivered, replay produced no second one | `scripts/session-notify.py status`, the smoke test above | reproducible by the agent; recorded once on 2026-10-22 |
| `LOCAL_BANNER_CONFIRMED` | a person saw the banner on screen | the owner, by eye, after running the smoke test | **not yet confirmed**; notification settings, Focus mode or a locked screen can hide a delivered banner |

A third, objective level sits between them: `LOCAL_BANNER_PRESENTED_BY_OS`. After the smoke test, the macOS unified log shows the notification subsystem presenting it. It is not the owner's eyes, and Focus or a locked screen are still possible, but it shows macOS accepted and displayed the banner (attributed to Script Editor, the `osascript` notification sender; the body is `<private>` in the log and not read):

```bash
/usr/bin/log show --last 2m --style compact --predicate '(process == "usernoted" OR process == "NotificationCenter") AND (eventMessage CONTAINS[c] "Presenting" OR eventMessage CONTAINS[c] "visible banner" OR eventMessage CONTAINS[c] "Clearing banner")'
```

Expected lines: `Presenting <NotificationRecord app:"com.apple.ScriptEditor2" ...> as banner`, `Setting visible banner`, then `Clearing banner after expiration` about 5 s later. Use `/usr/bin/log`, since `log` is a zsh builtin. Recorded once on 2026-10-22 (no Focus assertion active). `LOCAL_BANNER_PRESENTED_BY_OS` is claimed; `LOCAL_BANNER_CONFIRMED` stays the owner's own sighting.

A delivered record alone never upgrades to a confirmed banner. Until the owner reports seeing it, only `LOCAL_NOTIFICATION_DELIVERED` is claimed.
