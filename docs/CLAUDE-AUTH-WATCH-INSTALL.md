# Claude sign-in watch: persistent installation packet (SPE-176, ADR 0020)

Nothing here is installed by the repository, a test or an agent session. Installing the agent is an owner-run step that needs the owner's explicit
authorization (agent-created LaunchAgents are classifier-denied as "Unauthorized Persistence"). User-level only: no root, no LaunchDaemon, no sudo.
This is part of the single owner approval packet with the admission monitor (`docs/ADMISSION-MONITOR-INSTALL.md`).

## What gets installed

| Item | Value |
|---|---|
| File | `~/Library/LaunchAgents/com.coding-agent-environment.claude-auth-watch.plist` (mode 0644) |
| Label | `com.coding-agent-environment.claude-auth-watch` |
| Program | `/usr/bin/python3 <CHECKOUT>/scripts/claude-auth-watch.py tick --send --if-due` (Python 3.9 verified); `<CHECKOUT>` is the canonical checkout `~/Bryant-Developments/coding-agent-environment-main-view` |
| Schedule | every 60 s (`StartInterval`) and at load; no `KeepAlive`; `ThrottleInterval` 30; logs to `/dev/null` |
| State | `~/.cache/coding-agent-environment/claude-auth-watch/` (0700, `state.json` 0600, bounded 64 KiB); `CLAUDE_AUTH_WATCH_DIR` overrides |
| Reads | `~/.claude/projects/*/*.jsonl` tail windows, `~/.claude/sessions/*.json` (name, sessionId, pid), `claude auth status` (only when a session shows auth evidence or an incident is open) |
| Sends | through `session-notify.py emit-resource` only (Telegram backend must already be configured there); enum phrases plus the closed `detail` |

The watch can also hand sign-in alerts and the recovery to ZeroClaw through the credential-free `zeroclaw-spool` backend (works while signed out; see `SESSION-NOTIFICATIONS.md`); enable it instead of, not together with, `telegram` for those kinds.

Only failures at most 30 minutes old (record and transcript file) can open an incident; see ADR 0020 section 3 for the status rules. It does not log in or out, call a model, signal a process, delete anything or touch the queue.

## Install (observe-only first)

```bash
cd ~/Bryant-Developments/coding-agent-environment-main-view
python3 scripts/claude-auth-watch.py plist --checkout "$PWD" --observe-only > /tmp/claude-auth-watch.plist
plutil -lint /tmp/claude-auth-watch.plist && cat /tmp/claude-auth-watch.plist      # program path, 60 s, /dev/null logs, no KeepAlive
install -m 0644 /tmp/claude-auth-watch.plist ~/Library/LaunchAgents/com.coding-agent-environment.claude-auth-watch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.coding-agent-environment.claude-auth-watch.plist
```

Observe-only runs `tick --if-due` without `--send`: it classifies and records the last result but changes no incident state and sends nothing.
Verify after two minutes:

```bash
launchctl print gui/$(id -u)/com.coding-agent-environment.claude-auth-watch | grep -E 'state|last exit|run interval'
python3 scripts/claude-auth-watch.py status
python3 scripts/claude-auth-watch.py scan            # read-only, closed schema
```

After a day with no false verdict, print the plist again without `--observe-only`, then `launchctl bootout` and `bootstrap` the new file.

## Limits to know before enabling `--send`

- If `claude auth status` cannot answer (for example the `claude` binary is not on the agent's PATH; set `CLAUDE_AUTH_WATCH_CLAUDE_BIN` or install it in `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`), status is `unknown` and the watch alerts only with a fresh failure plus a Remote Control "OAuth token unavailable" record, never on several failed sessions alone. Check `status` shows `logged-in` or `logged-out` while observing.
- Alerts: one per incident and at most one new incident alert per 30 min (a deferred alert is sent as soon as allowed; an outage that ended first shows as `outage_unannounced` in `status` and as `missed=N` in the next alert). Recovery and Remote Control notices: at most 3 per rolling 6 h, extras counted as `rate_limited`. Recoveries need 15 clean minutes. An alert the transport does not confirm is re-driven (7 times at 30 min, then hourly) while the incident is open.
- Unverified assumptions (the logged-out output shape, transcript record shapes, the thresholds) are listed in ADR 0020; observe-only first exists to check them.

## After a sign-in interruption

`python3 scripts/claude-auth-watch.py reconcile --since <incident epoch>` (the epoch is in `scan` as `incident_epoch`). Read-only; PASS, FAIL or NOT_VERIFIED per session.

## Kill switch, uninstall and rollback

```bash
touch ~/.cache/coding-agent-environment/claude-auth-watch/DISABLED      # or CLAUDE_AUTH_WATCH_DISABLED=1: ticks exit at once
launchctl bootout gui/$(id -u)/com.coding-agent-environment.claude-auth-watch
rm ~/Library/LaunchAgents/com.coding-agent-environment.claude-auth-watch.plist
rm -r ~/.cache/coding-agent-environment/claude-auth-watch                # optional; session-notify state is separate
```

If the supervisor is later chosen to host the watch (ADR 0020 section 5), boot this agent out first so only one trigger remains.
