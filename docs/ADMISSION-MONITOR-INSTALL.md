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
| Writes | only `~/.cache/coding-agent-environment/admission-monitor/` (0700: `state.json`, `status.json`, `receipts.jsonl` rotated to `.1` at 1 MiB, `monitor.lock`) and, through its one recovery verb, what `disk-admission.py queue advance` already writes |
| Logs | launchd stdout and stderr go to `/dev/null`; the durable record is `status.json` and `receipts.jsonl` (`admission-monitor.py status`) |
| Alerts | through `session-notify.py emit-resource` (enum phrases only). With every session-notify backend off it is recorded and no banner shows, and the monitor still labels it delivered; `status.json` holds the same facts. When the blockage ends the monitor sends `emit-resource --clear` once so a recurrence is announced again |
| Reads | the queue entry files, the memory latch file, `ps -axo pid=,ppid=,time=` (never command lines), free space on the data volume |
| Recovery verb | `disk-admission.py queue advance` from the canonical checkout, only with `--act`, after the same cause held for two checks at least 30 s apart, never for disk floor, memory pressure or unreadable state, at most one per 120 s and five per rolling 30 minutes |
| Never | signal or kill a process, run `launchctl`, reset the latch, change a threshold, delete anything, or cancel or release an entry itself. `queue advance`, which any caller runs, may release a claim whose holder provably exited and cancel an unclaimed entry whose declared waiter provably exited; never on doubt |

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

## Uninstall and rollback

```bash
launchctl bootout gui/$(id -u)/com.coding-agent-environment.admission-monitor
rm ~/Library/LaunchAgents/com.coding-agent-environment.admission-monitor.plist
# optional: forget its state (queue and admission state are untouched)
rm -r ~/.cache/coding-agent-environment/admission-monitor
```

Rollback to observe-only keeps the agent but removes the only action: reprint with `--observe-only`, `bootout`, replace the file, `bootstrap`.
Removing the agent changes no admission behaviour: the queue, the gates, the limits and the merge gates are the same with or without it.

## Fallback without the agent

Nothing needs the agent to keep working. Run `admission-monitor.py tick` by hand (observe-only) or `tick --act`; `heavy-run` and `la-cargo`
are unchanged.
