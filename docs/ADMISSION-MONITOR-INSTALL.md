# Admission stall monitor: persistent installation packet (SPE-176, ADR 0018)

Nothing here is installed by the repository, a test or an agent session. Installing the agent is an owner-run step that needs the owner's
explicit authorization: a LaunchAgent created from an agent session was classifier-denied before ("Unauthorized Persistence",
2026-11-08) and is not worked around. This is a user-level agent only: no root, no LaunchDaemon, no sudo, no system service.

## What gets installed

| Item | Value |
|---|---|
| File | `~/Library/LaunchAgents/com.coding-agent-environment.admission-monitor.plist` (mode 0644, owner: you) |
| Label | `com.coding-agent-environment.admission-monitor` |
| Program | `/usr/bin/python3 <CHECKOUT>/scripts/admission-monitor.py tick --act --if-due` (Python 3.9 verified), where `<CHECKOUT>` is the canonical checkout `~/Bryant-Developments/coding-agent-environment-main-view`, fast-forward only |
| Schedule | every 60 s (`StartInterval`) and once at load; `ThrottleInterval` 30; no `KeepAlive`, so it never restarts in a loop |
| Priority | `ProcessType Background`, `Nice 10`, `LowPriorityIO`; one tick takes about the 10 s memory sampling window plus a short `ps`/queue read (not measured end to end) and holds a non-blocking lock, so a slow tick makes the next one exit at once |
| Environment | `PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin`; no secrets, no credentials |
| Writes | only `~/.cache/coding-agent-environment/admission-monitor/` (0700: `state.json`, `status.json`, `receipts.jsonl` and `recovery-receipts.jsonl` rotated at 1 MiB, `owner-outbox.jsonl`, `monitor.lock`, `recovery.lock`) and, through its recovery verbs, what `disk-admission.py queue advance` and `queue cancel` already write |
| Logs | launchd stdout and stderr go to `/dev/null`; the durable record is `status.json` and `receipts.jsonl` (`admission-monitor.py status`) |
| Alerts | through `session-notify.py emit-resource` (enum phrases only). With every session-notify backend off it is recorded and no banner shows, and the monitor still labels it delivered; `status.json` holds the same facts. When the blockage ends the monitor sends `emit-resource --clear` once so a recurrence is announced again |
| Reads | the queue entry files, the memory latch file, `ps -axo pid=,ppid=,time=` and, for registered wrappers only, the process table and an open-file list (command lines are reduced in memory to interpreter and script basenames and never kept), the wrapper registry (`~/.cache/coding-agent-environment/admission/wrappers/`), free space on the data volume |
| Recovery verb | `disk-admission.py queue advance` from the canonical checkout, only with `--act`, after the same cause held for two checks at least 30 s apart, never for disk floor, memory pressure or unreadable state, at most one per 120 s and five per rolling 30 minutes |
| Guarded recovery (`--act` only) | at most one action per tick and three per rolling 30 minutes: cancel a conclusively abandoned, unstarted GRANTED entry through `queue cancel`, or send one SIGTERM to a conclusively obsolete heavy-run / la-cargo wrapper whose entry is terminal for two ticks. See "What `--act` does now" below |
| Never | send any signal other than that one SIGTERM (never SIGKILL, never a process group), touch a healthy workload, an arbitrary application, a system service or an unidentified process, run `launchctl`, reset the latch, change a threshold, delete anything, or release an entry itself. `queue advance`, which any caller runs, may release a claim whose holder provably exited and cancel an unclaimed entry whose declared waiter provably exited; never on doubt |

Optional ZeroClaw hand-off (credential-free, no Claude Code sign-in needed): enabling `backends.zeroclaw-spool` in the session-notify config also writes each unclaimed-handoff, queue-wait and over-deadline incident to the supervisor spool; see `SESSION-NOTIFICATIONS.md` (ZeroClaw incident spool). It cannot be enabled together with `telegram` for the same notice kind.

Prerequisite: the canonical checkout contains the merged monitor (and the merged `session-notify emit-resource` if you want the alert
recorded through session-notify). Verify before installing:

```bash
cd ~/Bryant-Developments/coding-agent-environment-main-view && git status -sb && ls scripts/admission-monitor.py
python3 scripts/admission-monitor.py callers | tail -3
```

## Install (observe-only first, then act)

1. Print and lint the agent without installing it. Start observe-only (no `--act`): it reports and alerts but never advances.

```bash
cd ~/Bryant-Developments/coding-agent-environment-main-view
python3 scripts/admission-monitor.py plist --checkout "$PWD" --observe-only > /tmp/admission-monitor.plist
plutil -lint /tmp/admission-monitor.plist
cat /tmp/admission-monitor.plist        # read it: program path, StartInterval, /dev/null logs, no KeepAlive
```

2. Install and start it:

```bash
install -m 0644 /tmp/admission-monitor.plist ~/Library/LaunchAgents/com.coding-agent-environment.admission-monitor.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.coding-agent-environment.admission-monitor.plist
```

3. Verify after two minutes:

```bash
launchctl print gui/$(id -u)/com.coding-agent-environment.admission-monitor | grep -E 'state|last exit|run interval'
python3 ~/Bryant-Developments/coding-agent-environment-main-view/scripts/admission-monitor.py status --json
ls -l ~/.cache/coding-agent-environment/admission-monitor/
```

Expected: `last exit code = 0`, `status.json` less than 2 minutes old, `granted_is_execution: false`.

4. After at least a day of observe-only receipts with no false alert, switch on recovery: print the plist again without `--observe-only`, replace the file, and reload (`launchctl bootout` then `bootstrap`, as below).

## What `--act` does now (SPE-176 wiring, ADR 0019)

Beyond `queue advance`, each `--act` tick first verifies earlier recoveries, then takes at most one new action:

1. **Obsolete wrapper.** A `heavy-run` or `la-cargo` that registered itself when it enqueued (`wrappers/<qid>.json`) whose queue entry is CANCELLED, EXPIRED
   or RELEASED, with the same live wrapper seen that way on two ticks at least 120 s apart, and whose entry names that very pid and start token as its waiter, and whose pid, start token, process group, parent, uid and
   interpreter+script identity still match, with no child, group mate, detached process or open cargo lock, gets one SIGTERM. If it survives 10 s it is reported
   as `failed-to-terminate` (owner action: inspect the stuck wrapper) and never signalled again.
2. **Abandoned unstarted grant.** A GRANTED entry older than 5 min with no holder, release or workload, whose declared waiter is provably gone (or that has none and
   whose owning session is recorded as ended) and that a fresh re-read still agrees with, is cancelled with `queue cancel`. A live or unknown owner is never cancelled.
3. **Reports, not actions.** A wrapper with a live child workload or a detached process is listed under `recovery.reported` in `status.json`; nothing is signalled.

Each action writes `recovery-receipts.jsonl`, an owner-outbox line and one session-notify notice, and the incident is verified on a later tick (`recovery_incidents`
in `status.json`: `verify` is `pending`, `verified` or `failed`). `--observe-only` and a plain `tick` never do any of this.

**Kill switch.** `touch ~/.cache/coding-agent-environment/admission-monitor/recovery.disabled` makes act-mode recovery a no-op at the next tick (verification of
earlier recoveries included; `queue advance` is a separate verb and keeps working). Remove the file to re-enable; a wrapper that was terminal while the switch was on
is confirmed again for two ticks first. A monitor directory that is not a private (0700) real directory owned by you also disables it (`recovery.state` shows
`unsafe-monitor-dir`).

**Verify it.**

```bash
python3 ~/Bryant-Developments/coding-agent-environment-main-view/scripts/admission-monitor.py status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("recovery"), d.get("recovery_incidents"))'
tail -5 ~/.cache/coding-agent-environment/admission-monitor/recovery-receipts.jsonl      # decision / terminate / cancel / verify lines, no command lines
ls -l ~/.cache/coding-agent-environment/admission/wrappers/                               # one 0600 file per queued, live wrapper
```

Expected while healthy: `recovery.state` is `ok`, `acted` and `reported` are empty, and the registry holds only wrappers of live entries. A stale file left by `kill -9`
is never trusted and is removed by the next tick (unlink only; dead pid, changed start token or older than 7 days; at most 50 per tick; a count-only `prune` receipt).

**Roll back.** Fastest: create `recovery.disabled`. Complete: reprint the agent with `--observe-only` as below, which removes every action including `queue advance`.
The registry files and receipts can stay; removing `wrappers/` is safe because `heavy-run` and `la-cargo` write it best effort and never depend on it.

## Closing incidents the supervisor still lists (SPE-188)

The monitor announces an incident with `opened` and `escalated` spool files. Once the scheduler resolves it (the grant lapsed or was cancelled, the wait or run ended) or a holder claims it, the monitor sends one closing `recovered` file with the same `incident_ref` (`incident-resolved` cause, see `docs/SESSION-NOTIFICATIONS.md`). It is a notice, not an action, so it also runs in observe-only mode. Incidents resolved before this existed were never closed; list and close them once:

```bash
python3 scripts/admission-monitor.py reconcile-incidents              # dry run: lists each supervisor ref as closable, keep or unmapped; writes nothing
python3 scripts/admission-monitor.py reconcile-incidents --apply      # sends one closing notice per closable ref
```

A ref is closable only when its scheduler entry (the ref is `inc:` plus the first 16 hex of sha256 of `<queue id>@<grant.at>`) shows the incident over. Whether the supervisor consumer drops an incident on `recovered` is its owner's side.

## Uninstall and rollback

```bash
launchctl bootout gui/$(id -u)/com.coding-agent-environment.admission-monitor
rm ~/Library/LaunchAgents/com.coding-agent-environment.admission-monitor.plist
# optional: forget its state (queue and admission state are untouched)
rm -r ~/.cache/coding-agent-environment/admission-monitor
```

Rollback to observe-only keeps the agent but removes every action: reprint with `--observe-only`, `bootout`, replace the file, `bootstrap`.
Removing the agent changes no admission behaviour: the queue, the gates, the limits and the merge gates are the same with or without it.

## Fallback without the agent

Nothing needs the agent to keep working. Run `admission-monitor.py tick` by hand (observe-only) or `tick --act`; `heavy-run` and `la-cargo`
are unchanged.
