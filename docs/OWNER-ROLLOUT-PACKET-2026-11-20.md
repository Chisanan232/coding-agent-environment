# Owner approval packet: stall monitor, sign-in watch, Telegram delivery, four lanes, routing (SPE-176)

Status: **prepared for the owner; nothing in this packet is installed, enabled or authorized.** Written 2026-11-20 against main `f1c7294` (sign-in watch #282 and `test-notice` #283 merged).
Each item below is **separately selectable**. Approving one does not approve another. Nothing here asks the owner to paste a secret into chat: no token or
chat id is typed, printed or logged by any step. LaunchAgents created from an agent session were classifier-denied as "Unauthorized Persistence" (2026-11-08),
so every install below is **owner-run** from the owner's own terminal (`! <command>` in the Claude prompt runs it in the session).

## 0. What is already true (evidence, not approval)

| Capability | Code merged | Checkout updated | Installed as a service | Notification delivered live | Recovery enabled live | Verified live |
|---|---|---|---|---|---|---|
| Stall monitor (queue wait, unclaimed grant, overdue workload) | yes (#268, #275, #277) | yes (canonical `62ec5a2`) | **no** | **no** | no (`tick --act` not running) | no |
| Guarded recovery (SIGTERM of obsolete wrappers, cancel of abandoned unstarted grants) | yes (#276, #281) | yes | no | no | no | isolated acceptance only (25 tests, real throwaway processes) |
| 20-minute hand-off grant window | yes (#274) | yes | n/a | n/a | yes (scheduler, live since the checkout update) | partly: from live receipts q000814 was granted 15:07:07Z and claimed 15:10:27Z inside the window (not otherwise sourced in docs); no lapse observed |
| Sign-in watch | yes (#282) | yes (canonical `42a258a`) | **no** | **no** | n/a (read-only) | no |
| Telegram backend (+ supervisor credential-file reuse) | yes (#273, #279) | yes | n/a | **no** (no config exists) | n/a | no |
| Four heavy lanes | yes (#278), **shipped OFF** | yes | n/a | n/a | off | no lane-4 run is known to exist (not independently checked; the flag has never been on) |
| Incremental-cache classifier correction | yes (#280) | yes | n/a | n/a | n/a | real-tree check True in 1.2-8.2 s |
| Four-tier routing (Haiku/Sonnet/Opus/Fable) | policy + agents installed 2026-11-19 | live | n/a | n/a | n/a | forced Haiku/Opus delegation verified; natural delegation 0 of 6 (documented pilot); my own live count 0 of 7 (unsourced) |

Live queue evidence: the queue ran continuously from 2026-11-19 11:33Z (q000812 through q000824 across AASM, Libra, LocalAgent and Circinus). The only
standing refusals were resource or exclusion-key waits (`disk-floor`, `excl_key overlap with p2576d`), never a latch.

## A. Telegram delivery for notices (credential-authority decision) — recommended FIRST, everything else notifies through it

**What you approve:** session-notify's `telegram` backend may read the existing supervisor credential file
`~/.config/engineering-supervisor/credentials.json` (the same file the supervisor's own Telegram daemon uses) and send **direct messages to your owner id**
(`TELEGRAM_OWNER_ID`) with the same bot. It never reads `TELEGRAM_SUPERGROUP_ID` and never posts to the forum.
**New authority:** a second consumer of the bot token (this is the credential-authority decision). The token is never copied into config, environment, logs,
state files or Jira; the file is read with the supervisor's own safety checks (absolute path, no symlink component, 0700 parent, exact 0600 file, owner uid).
**Risk delta:** enabling sends real messages for every notifiable transition (completion/ended sessions, stalls, sign-in alerts). Texts are fixed phrases plus
a bounded sanitized detail; no free text, paths, ids or command lines.

Enable (you run it; the config file holds only a path):

```bash
mkdir -p ~/.config/coding-agent-environment && chmod 700 ~/.config/coding-agent-environment
cd ~/Bryant-Developments/coding-agent-environment-main-view
python3 scripts/session-notify.py render-config > ~/.config/coding-agent-environment/session-notify.json   # everything off
chmod 600 ~/.config/coding-agent-environment/session-notify.json
# edit backends.telegram to: {"enabled": true, "approved_destination": "api.telegram.org", "credentials_json": "/Users/bryant/.config/engineering-supervisor/credentials.json"}
```

Verify delivery (sends ONE real message with fixed text "Claude Code: test notice, ignore"; its own record, shares no state with any real alert; repeat runs within 10 minutes are deduplicated).:

```bash
python3 scripts/session-notify.py test-notice --strict
# expect: test-notice: queued delivery=sent  and a Telegram message in your owner chat
```

Alternative (owner decision 2026-11-20, SPE-176): ZeroClaw alerts via the credential-free `zeroclaw-spool` backend instead of direct Telegram for the incident kinds. Enable `backends.zeroclaw-spool` and either leave `telegram` off or narrow `backends.telegram.kinds` to `["session"]`; both enabled for the same kind is rejected at config load. Details: `SESSION-NOTIFICATIONS.md`.

Rollback: set `backends.telegram.enabled` to `false` (or delete the file). No other state changes. Unverified until you run it: that the bot may message you
(you must have started a chat with the bot once) and that `delivery=sent` is returned.

## B. Admission stall monitor LaunchAgent (persistent detection; recovery in a second step)

**What you approve:** a user-level LaunchAgent `com.coding-agent-environment.admission-monitor` running
`/usr/bin/python3 ~/Bryant-Developments/coding-agent-environment-main-view/scripts/admission-monitor.py tick --if-due` every 60 s (observe-only first), logs to
`/dev/null`, no KeepAlive, no root, no secrets. Full table, install, verify, uninstall and rollback: `ADMISSION-MONITOR-INSTALL.md`.
**Step 1 (observe-only, recommended now):** it classifies and alerts (through A) but never acts. Run the printed `plist --observe-only` steps in that document.
**Step 2 (recovery, separate approval after a day of clean observe-only receipts):** reprint without `--observe-only` so the plist runs `tick --act --if-due`.
`--act` adds exactly two bounded actions, both reviewed and tested in isolation (not yet live): ONE SIGTERM (never SIGKILL, never a process group) to a
conclusively obsolete heavy-run/la-cargo wrapper or waiter after a 120 s confirmation, and the supported `queue cancel` of a conclusively abandoned unstarted
grant whose owner is provably gone. Healthy builds, workloads, other apps and unidentified processes are never touched (wrappers that share a process group with
a workload are never terminated). At most 1 action per tick and 3 per 30 min, recorded before acting; kill switch: `touch ~/.cache/coding-agent-environment/admission-monitor/recovery.disabled`.
**Controlled acceptance (what exists and what does not):** the isolated acceptance harness (`tests/test-admission-recovery-acceptance.py`, real throwaway processes and a
real temp scheduler) is the controlled test of abandoned-job recovery. Live recovery on the real queue stays **NOT_VERIFIED** until a real abandoned entry occurs
under Step 2; no synthetic job is ever enqueued on the live queue.

## C. Claude sign-in watch LaunchAgent (after PR #282 merges)

Second user LaunchAgent `com.coding-agent-environment.claude-auth-watch`, same shape as B: `claude-auth-watch.py tick --send --if-due` every 60 s. It is **read-only**
(transcripts, session registry, `claude auth status`); the only output is a notice through A. Install steps (observe-only first), verification, kill switch and
rollback: `CLAUDE-AUTH-WATCH-INSTALL.md`. Ownership: one component owns the alert (ADR 0020); **decision for you:** whether the supervisor should host it later
(default: no, this script stays the single detector and sender).
Unverified until it runs: the logged-out shape/exit code of `claude auth status`, the logged-in-but-expired-token case, and a real sign-in interruption. Observe-only
first exists to check them; after you sign in `claude-auth-watch.py reconcile --since <epoch>` enumerates survivors, completed results and duplicate submissions.

## D. Four heavy lanes: enable (separate reviewed one-line PR, NOT part of this packet's approvals unless you say so)

Shipped OFF (`FOURTH_LANE_ENABLED = False`). Enabling changes the live default for every product session sharing the scheduler. ADR 0017 rev 8 prerequisites, all
required: every active checkout current (a stale checkout's memory check arms the shared latch with four trees running: `admission-monitor.py callers` reported many unstamped and outdated checkouts on 2026-11-19 (116 and 55 by my count from that run; unverified, re-run it before deciding)), a fresh three-lane trial report PASS, measured headroom (lane 4 needs free space minus all reservations
minus the request >= 200 GiB, no unmanaged build trees, no past-TTL/unresolved holder), and the monitor from B running so a stall is detected. Safe refusal is
tested (`budget: lane 4 needs ...`, never latches); live lane-4 behaviour is NOT_VERIFIED. **Recommendation:** approve B and C first, then ask for D after a day of
observation.

## E. Routing and permissions: items only you can decide

1. **Codex thread cap 8 to 7:** live `~/.codex/config.toml` has 8, policy is 7. Review then apply: `cd ~/Bryant-Developments/coding-agent-environment-main-view && ./scripts/profile-install.sh --routing --dry-run`, then `./scripts/profile-install.sh --routing --apply` (reconciles only positively owned Codex routing; it does not serialize your user config).
2. **`autoMode.allow` lacks `$defaults`:** `scripts/permission-rollout.py verify` exits 1; the text lives in `~/.claude/settings.json`, which the host gates against agent edits (SPE-106/118).
3. **Haiku-alias plugin agents** (`caveman:cavecrew-investigator`, `-reviewer` declare bare `model: haiku`): either amend SPE-106 to allow one deny, add an ask rule, or admit them in the routing table (SPE-106 keeps `permissions.deny` empty).
4. **Natural delegation** to `haiku-worker` / `opus-architect` is 0 of 6 in the documented pilot and 0 of 6 with strict wording (docs/LIVE-ROLLOUT-2026-11-19.md); 0 of 7 qualifying live runs by my own count (not sourced in docs); wording is not the lever (measured). Forced delegation works. Decide whether to keep opt-in (default) or fund a routing experiment.
5. **Fable:** routed nowhere by design; the one validation consult is recorded (blocker ca7955486c2b5007, about $1.29 list estimate, closed, duplicates refused). No action unless you want another approved consult.

## F. What the agent will do next without approval

Merge PR #282, keep the canonical checkout current, run the isolated acceptance suites, keep the queue and the monitor's read-only `tick` under observation, and
report each of A to E as NOT_VERIFIED until you act. Completion of the rollout is **not** claimed before A to C are installed and a real notice has been delivered.
