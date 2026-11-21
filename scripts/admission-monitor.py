#!/usr/bin/env python3
"""Deterministic stall monitor for the shared heavy-job queue (SPE-176, ADR 0018).

One `tick` reads the queue, the disk and memory gates' state and the process table, decides whether queued work is globally
stalled, names the cause, and (only with --act) runs the one recovery verb it owns: `disk-admission.py queue advance` from this
checkout. It needs no conversation and makes no model call.

With --act it also owns two guarded recoveries (ADR 0019, scripts/admission-recovery.py): cancel a conclusively abandoned, unstarted GRANTED entry
through the scheduler's own `queue cancel`, and SIGTERM (once, never a harder signal) a conclusively obsolete heavy-run / la-cargo wrapper whose queue entry is
terminal. Both are bounded (1 per tick, 3 per 30 min), off while `recovery.disabled` exists in the monitor directory, and never in observe-only mode.
It never signals anything else, resets the memory latch, changes a threshold, writes disk/memory admission state itself, deletes
anything, or presents an entry's session id (only `resolve_owner` reads it; no notice or outbox line carries it). GRANTED is never execution: only a claim, a release or CPU growth of a claimed
workload counts as progress. Memory-policy changes are a separate, owner-decided proposal (docs/adr/0018, option P1/P2).

  admission-monitor.py tick [--act] [--if-due] [--json]   one pass
  admission-monitor.py status [--json]                    last result, read-only
  admission-monitor.py callers                            admission runners found on disk, read-only
  admission-monitor.py plist                              print the launchd agent; installs nothing
"""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
INTERVAL_S = 60
STALL_S = 600                 # waiting work with no claim, release or CPU growth for this long is a global stall
# A grant nobody has claimed is noticed 5 min after it was made (the observed p90 grant->claim is 139 s) and escalated at 75% of that entry's own grant
# window, so the owner hears BEFORE it lapses: 45 min of a waiter entry's 60 min window (above the observed p99 grant->claim of 29 min), 15 min of a
# hand-off entry's 20 min window (ADR 0019). A hand-off grant lapses to EXPIRED and is then only history, so an alert on a fixed 45 min could never fire.
GRANT_NOTICE_S = 300
GRANT_ESCALATE_FRACTION = 0.75
HANDOFF_GRANT_S = 1200        # mirror of disk-admission.HANDOFF_GRANT_S (a test pins the equality): the window of an entry with no `waiter`
LAPSE_WINDOW_S = 6 * 3600     # an unclaimed grant that lapsed this recently and was never raised is still raised once
INCIDENT_LIMIT = 24           # unclaimed-grant incidents kept in the state file; the state read cap is 64 KiB
UNVERIFIED_AFTER_DEDUPS = 5   # `deduped` for a notice this record never handed off: after this many answers, stop asking and record it unverified
INCIDENT_HARD_LIMIT = 64      # an incident still inside its lapse window is never evicted for the soft limit; this bound protects the read cap
GRANTED_STALL_S = 2700        # a granted-only queue with a waiter entry is a stall from this age, as before the hand-off window existed
INCIDENT_KEEP_S = 7 * 3600    # past the lapse window plus an hour, an incident can no longer be re-raised, so it is dropped
INCIDENT_ACK_KEEP_S = 3600
# Queue wait and execution clocks (ADR 0019, observed p90 4.0 h and p99 12.5 h enqueue->grant, p99 5.4 h claim->release). Neither takes any action.
WAIT_NOTICE_S = 4 * 3600      # status notice only, no page
WAIT_ESCALATE_S = 12 * 3600 + 1800
WAIT_NEAR_S = 20 * 3600       # second and last notice of a wait incident: the 24 h wait window is about to end
RUN_DEFAULT_DEADLINE_S = 6 * 3600
RUN_DEADLINE_MAX_S = 48 * 3600
RUN_ESCALATE_FACTOR = 2
WAIT_INCIDENT_MAX = 12        # open wait incidents; the oldest waits get them, the rest are counted in status (`wait_overflow`), never paged one by one
RUN_INCIDENT_MAX = 6
NOTICE_LIST_MAX = 8
OUTBOX_LIMIT = 1 << 20
GAP_MAX_S = 48 * 3600         # one remembered gap never discounts more than this (and a stored one outside 0..this is dropped)
GAP_LEDGER_MAX = 32           # observation gaps remembered for discounting wait and run ages
OUTBOX_NAME = 'owner-outbox.jsonl'
CPU_STALL_S = 900             # a claimed, alive workload with no CPU growth this long is reported, never touched
CONFIRM_SPACING_S = 30        # confirmations are counted at least this far apart
CLEAR_AFTER_TICKS = 3         # clean ticks in a row before the alert channel is told the blockage ended
CONFIRM_TICKS = 2             # consecutive ticks a cause must hold before it is acted on or alerted
LIVE = ('QUEUED', 'ELIGIBLE', 'GRANTED', 'CLAIMED')
GAP_FACTOR = 3                # a gap over this many intervals, or sleep, is an observation gap
ADVANCE_MIN_GAP_S = 120
ADVANCE_MAX_PER_WINDOW = 5
ADVANCE_WINDOW_S = 1800
ALERT_COOLDOWN_S = 6 * 3600
STATIC_WINDOW_S = 600         # compressor must have been flat this long to be called static
STATIC_SPAN = 0.01            # ...within one percentage point of RAM
WINDOW_MAX = 16
STATE_LIMIT = 65536
RECOVERY_KILL_SWITCH = 'recovery.disabled'   # a file of any kind in the monitor directory turns act-mode recovery into a no-op
RECOVERY_MAX_PER_WINDOW = 3
RECOVERY_WINDOW_S = 1800
RECOVERY_CONFIRM_S = 120      # a terminal entry must have had a live registered wrapper this long (two ticks): a wrapper that is just exiting is never signalled
RECOVERY_GRACE_S = 10.0       # bounded wait after the one SIGTERM
RECOVERY_POLL_S = 1.0
RECOVERY_LIMIT = 16           # recovery incidents kept in the state file
RECOVERY_KEEP_S = 24 * 3600
RECOVERY_REGISTRY_MAX = 64
RECOVERY_OUTCOMES = ('terminated', 'failed-to-terminate', 'cancelled', 'failed')
RECOVERY_NOTICE = {'terminated': ('obsolete-wrapper-terminated', 'wrapper-terminated'), 'cancelled': ('abandoned-unstarted-cancelled', 'grant-cancelled'),
                   'failed-to-terminate': ('failed-to-terminate', 'termination-failed')}
RECOVERY_EVIDENCE = {'terminated': ['wrapper-registered', 'entry-terminal'], 'failed-to-terminate': ['wrapper-registered', 'entry-terminal'],
                     'cancelled': ['grant-unclaimed-escalate', 'owner-gone']}
CAUSES = ('state-unreadable', 'disk-floor', 'memory-pressure', 'memory-hysteresis-static-compressor', 'memory-hysteresis',
          'stale-claim', 'frozen-queue', 'unclaimed-handoff', 'failed-continuation', 'outdated-caller', 'stalled-running', 'unobservable', 'healthy-busy', 'unknown')
# Causes of the per-entry wait and execution incidents. They are never part of a verdict (they cannot make the queue "stalled" or start an advance).
INCIDENT_CAUSES = ('queue-wait-excessive', 'workload-over-deadline')
PHASES = ('escalated', 'lapsed', 'near')
CLOCKS = ('grant', 'wait', 'run')
GLOBAL_BLOCKERS = ('disk-floor', 'memory-pressure', 'memory-hysteresis-static-compressor', 'memory-hysteresis')
BLOCKERS = GLOBAL_BLOCKERS + ('higher-entry-running', 'none')
OWNER_KINDS = ('claude', 'codex', 'heavy-run', 'la-cargo', 'unknown')
OWNER_EVIDENCE = ('no-session-id', 'session-live', 'session-ended', 'session-unknown', 'process-alive', 'process-gone', 'process-uninspectable')
SUPPRESSED = 'suppressed-higher-entry'
EVIDENCE = OWNER_EVIDENCE + ('grant-unclaimed-escalate', 'grant-lapsed-unclaimed', 'no-waiter', 'queued-over-escalate', 'queued-expiry-near', 'blocked-by-global-cause', 'behind-running-entry', 'holder-alive',
                             'over-deadline', 'deadline-declared', 'deadline-default', 'wrapper-registered', 'entry-terminal', 'owner-gone')
OWNER_ACTION = {'disk-floor': 'reclaim-disk', 'memory-pressure': 'reduce-load', 'memory-hysteresis-static-compressor': 'approve-memory-policy',
                'memory-hysteresis': 'wait-or-approve-memory-policy', 'stale-claim': 'confirm-claim-ended',
                'failed-continuation': 'restart-waiting-session', 'outdated-caller': 'restart-outdated-caller',
                'state-unreadable': 'inspect-admission-state', 'unknown': 'inspect-admission-state',
                'frozen-queue': 'run-queue-advance', 'unclaimed-handoff': 'claim-or-reenqueue-handoff', 'stalled-running': 'inspect-running-workload', 'unobservable': 'inspect-memory-admission',
                'queue-wait-excessive': 'check-queued-wait', 'workload-over-deadline': 'check-over-deadline-workload',
                'obsolete-wrapper-terminated': 'check-recovery-result', 'abandoned-unstarted-cancelled': 'check-recovery-result',
                'failed-to-terminate': 'inspect-stuck-wrapper'}


def load(name):
    spec = spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def iso(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def monitor_dir(state_path):
    override = os.environ.get('ADMISSION_MONITOR_DIR')   # experiments and tests keep their state apart from production
    return Path(override) if override else Path(state_path).parent.parent / 'admission-monitor'


def read_json(path, default):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            return json.loads(handle.read(STATE_LIMIT))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(json.dumps(data, sort_keys=True))
    os.replace(tmp, path)


# ---- observation (injectable) ---------------------------------------------------------------------------------------

def boot_time():
    try:
        out = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
        return int(re.search(r'sec = (\d+)', out).group(1))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return 0


def cpu_seconds(table_text):
    """{pid: (ppid, cpu seconds)} from `ps -axo pid=,ppid=,time=`; command lines are never read."""
    out = {}
    for line in table_text.splitlines():
        fields = line.split()
        if len(fields) != 3 or not (fields[0].isdigit() and fields[1].isdigit()):
            continue
        clock = fields[2].replace('-', ':').split(':')
        try:
            parts = [float(x) for x in clock]
        except ValueError:
            continue
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        if len(parts) == 4:   # d-hh:mm:ss
            seconds = ((parts[0] * 24 + parts[1]) * 60 + parts[2]) * 60 + parts[3]
        out[int(fields[0])] = (int(fields[1]), seconds)
    return out


def tree_cpu(table, root):
    """CPU seconds of a process and all its descendants."""
    kids = {}
    for pid, (ppid, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    total, stack, seen = 0.0, [root], set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in table:
            continue
        seen.add(pid)
        total += table[pid][1]
        stack.extend(kids.get(pid, ()))
    return total


def ps_table():
    done = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,time='], capture_output=True, text=True, timeout=20,
                          stdin=subprocess.DEVNULL)
    return cpu_seconds(done.stdout) if done.returncode == 0 else {}


def memory_view(memory_state_path):
    """Latch state straight from the file (never `check`, which would write it) and the light-weight metrics."""
    state = read_json(memory_state_path, {})
    latch = {'latched': bool(state.get('latched')), 'ok_streak': state.get('ok_streak', 0)}
    try:
        done = subprocess.run([sys.executable, str(HERE / 'memory-admission.py'), 'status', '--json', '--state', str(memory_state_path)],
                              capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
        metrics = json.loads(done.stdout)['memory']
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        metrics = {}
    return dict(latch, metrics=metrics)


# ---- pure core -------------------------------------------------------------------------------------------------------

# Causes that a `queue advance` from this checkout can clear (it releases proven-exited claims, expires lapsed grants and probes the
# memory gate, which is also what counts the recovery streak). Everything else is reported, never advanced.
ADVANCE_CAUSES = {'stale-claim', 'failed-continuation', 'memory-hysteresis', 'memory-hysteresis-static-compressor', 'frozen-queue'}
# Causes the owner can usually leave alone for a while because the monitor's own recovery may clear them first.
RECOVERABLE = {'stale-claim', 'failed-continuation', 'memory-hysteresis', 'frozen-queue'}


def finite(value):
    """A real, finite number read from a state file: not a bool, not NaN or infinity (json.loads accepts both), not an int too large for a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def safe_time(text):
    try:
        moment = parse_iso(text)
    except (ValueError, TypeError, AttributeError):
        return None
    return moment if moment.tzinfo is not None else None


def is_handoff(entry):
    """An entry with no declared waiter is a manual hand-off: its owner must claim the grant by hand."""
    return 'waiter' not in entry


def grant_window_s(entry):
    """This entry's own grant window: the granted span, capped at the hand-off window when it has no waiter."""
    grant = entry['grant']
    span = int((safe_time(grant['expires']) - safe_time(grant['at'])).total_seconds())
    return min(span, HANDOFF_GRANT_S) if is_handoff(entry) else span


def escalate_s(entry):
    return int(grant_window_s(entry) * GRANT_ESCALATE_FRACTION)


def incident_key(entry):
    return '%s@%s' % (entry['id'], entry['grant']['at'])


def incident_signature(key, phase):
    """16 hex chars, stable per incident and phase: session-notify dedups on cause plus this, so restarts and two monitors send once per phase."""
    return hashlib.sha256(('%s:%s' % (key, phase)).encode()).hexdigest()[:16]


def incident_ref(key):
    """Stable across every phase of one incident (unlike the signature): the id the ZeroClaw spool files of its transitions share."""
    return 'inc:' + hashlib.sha256(key.encode()).hexdigest()[:16]


def lapsed_handoffs(entries, events, now, recorded_only=False):
    """Hand-off entries whose grant lapsed with no claim or holder, within LAPSE_WINDOW_S. Includes a GRANTED entry whose window ended but
    which no queue-aware command has yet recorded as EXPIRED (unless `recorded_only`: after a clock step only the scheduler's own EXPIRED counts).
    `events`: {id: set of receipt events}."""
    out = []
    for e in entries:
        grant = e.get('grant')
        if not (isinstance(grant, dict) and is_handoff(e)) or e.get('holder') or e['state'] not in (('EXPIRED',) if recorded_only else ('GRANTED', 'EXPIRED')):
            continue
        gone = (now - safe_time(grant['expires'])).total_seconds()
        if 0 <= gone < LAPSE_WINDOW_S and not ({'claimed', 'released'} & set(events.get(e['id'], ()))):
            out.append(e)
    return out


def usable_entries(entries):
    """(entries whose times parse, problems). A damaged entry is reported and left alone, never allowed to crash a tick."""
    good, problems = [], []
    for e in entries:
        ok = safe_time(e.get('wait_expires')) is not None
        if e.get('state') == 'GRANTED':
            grant = e.get('grant')
            ok = ok and isinstance(grant, dict) and safe_time(grant.get('at')) is not None and safe_time(grant.get('expires')) is not None
        if e.get('state') == 'EXPIRED' and isinstance(e.get('grant'), dict):
            ok = ok and safe_time(e['grant'].get('expires')) is not None
        if ok:
            good.append(e)
        else:
            problems.append('queue-entry-unusable: %s has an unreadable time field' % e.get('id'))
    return good, problems


def enter_violations(m, enter):
    out = []
    if (m.get('pressure_level') or 1) >= 2:
        out.append('pressure level %s' % m.get('pressure_level'))
    if (m.get('swap_io_mib_s') or 0) >= enter['swap_io_mib_s_heavy']:
        out.append('swap I/O')
    if (m.get('compressor_ratio') or 0) >= enter['compressor_ratio_heavy']:
        out.append('compressor above enter')
    if (m.get('load_per_core') or 0) >= enter['load_per_core_heavy']:
        out.append('load')
    return out


def exit_blocker_keys(m, exit_):
    out = []
    if m.get('pressure_level') != 1:
        out.append('pressure')
    if (m.get('swap_io_mib_s') or 0) > exit_['swap_io_mib_s']:
        out.append('swap')
    if (m.get('compressor_ratio') or 0) > exit_['compressor_ratio']:
        out.append('compressor')
    if (m.get('load_per_core') or 0) > exit_['load_per_core']:
        out.append('load')
    return out


def compressor_static(window, now_wall):
    old = [r for w, r in window if now_wall - w >= STATIC_WINDOW_S]
    recent = [r for w, r in window if now_wall - w <= STATIC_WINDOW_S * 2] or []
    if not old or len(recent) < 2:
        return False
    return max(recent) - min(recent) <= STATIC_SPAN


def classify(snap, limits):
    """Every cause that holds, in precedence order. snap: now, wall, entries, problems, free_bytes, mem, window, claims, protocol.

    claims: {qid: {'status': alive|exited|unresolved, 'idle_s': seconds without CPU change}}. Entries must already be usable."""
    now = snap['now']
    entries = snap['entries']
    waiting = [e for e in entries if e['state'] in ('QUEUED', 'ELIGIBLE') and safe_time(e['wait_expires']) > now]
    granted = [e for e in entries if e['state'] == 'GRANTED' and safe_time(e['grant']['expires']) > now]
    found = {}
    if snap['problems']:
        found['state-unreadable'] = snap['problems'][0]
    if not waiting and not granted:
        return {'causes': found, 'primary': next(iter(found), 'idle'), 'waiting': 0, 'oldest_grant_s': 0, 'notes': [], 'notices': [],
                'unclaimed': [], 'grant_escalated': False, 'grant_stall_s': None}
    head = waiting[0] if waiting else None
    mem = snap['mem']
    metrics = mem.get('metrics') or {}
    if head is not None:
        claimed = sum(e['bytes'] for e in entries if e['state'] == 'CLAIMED')
        parked = sum(e['bytes'] for e in granted)   # reserved by a grant nobody has claimed: capacity held, nothing running
        if snap['free_bytes'] - claimed - head['bytes'] < limits['floor']:
            found['disk-floor'] = '%.0f GiB free, %.0f GiB held by running work, %.0f GiB requested' % (
                snap['free_bytes'] / GIB, claimed / GIB, head['bytes'] / GIB)
        elif snap['free_bytes'] - claimed - parked - head['bytes'] < limits['floor']:
            found['failed-continuation'] = 'unclaimed grants hold %.0f GiB; %.0f GiB free, %.0f GiB requested by %s' % (
                parked / GIB, snap['free_bytes'] / GIB, head['bytes'] / GIB, head['id'])
    if not metrics and not mem.get('latched'):
        found['unobservable'] = 'memory metrics could not be read'
    enter = enter_violations(metrics, limits['enter'])
    if enter:
        found['memory-pressure'] = ', '.join(enter)
    elif mem.get('latched'):
        blockers = exit_blocker_keys(metrics, limits['exit'])
        kind = 'memory-hysteresis'
        if blockers == ['compressor'] and compressor_static(snap['window'], snap['wall']):
            kind = 'memory-hysteresis-static-compressor'
        found[kind] = 'latched, %s/%s good checks, exit blocked by %s' % (mem.get('ok_streak'), limits['exit_samples'],
                                                                         ','.join(blockers) or 'nothing now (waiting for checks)')
    for entry in entries:
        if entry['state'] != 'CLAIMED':
            continue
        claim = snap['claims'].get(entry['id'], {})
        if claim.get('status') == 'exited':
            found['stale-claim'] = '%s holder exited, claim not released' % entry['id']
        elif claim.get('status') == 'alive' and claim.get('idle_s', 0) >= CPU_STALL_S:
            found['stalled-running'] = '%s alive but its CPU time has not changed for %d min (report only)' % (entry['id'], claim['idle_s'] // 60)
    oldest, notices, unclaimed, escalated, stall_s = 0, [], [], False, None
    for entry in granted:
        age = int((now - safe_time(entry['grant']['at'])).total_seconds())
        oldest = max(oldest, age)
        if age >= escalate_s(entry):
            escalated = True
            stall_s = min(stall_s if stall_s is not None else GRANTED_STALL_S, 0 if is_handoff(entry) else GRANTED_STALL_S)
            if is_handoff(entry):
                found['unclaimed-handoff'] = '%s hand-off GRANTED %d min without a claim' % (entry['id'], age // 60)
                unclaimed.append(entry['id'])
            else:
                found['failed-continuation'] = '%s GRANTED %d min without a claim' % (entry['id'], age // 60)
        elif is_handoff(entry) and age >= GRANT_NOTICE_S:
            notices.append('%s hand-off GRANTED %d min, not yet claimed' % (entry['id'], age // 60))
    for entry in granted:
        runner = (snap.get('stamps') or {}).get(entry['id'])      # from the granted receipt: the grant object itself carries no stamp
        if not isinstance(runner, dict) or runner.get('protocol', 0) < snap['protocol']:
            found['outdated-caller'] = '%s was granted by a runner without protocol %d' % (entry['id'], snap['protocol'])
    # Nothing blocks the head, nothing is claimed, nothing granted is waiting to be claimed: nobody is advancing the queue.
    held_elsewhere = waiting and waiting[0]['state'] == 'ELIGIBLE' and snap.get('reservations', 0) > 0    # a lane held by a non-queue reservation
    if waiting and not found and not granted and not held_elsewhere and not any(e['state'] == 'CLAIMED' for e in entries):
        found['frozen-queue'] = 'waiting work, free lanes and no blocker, but nothing is being granted'
    primary = next((c for c in CAUSES if c in found), 'unknown')
    if not found:
        busy = any(e['state'] == 'CLAIMED' for e in entries) or (any(e['state'] == 'ELIGIBLE' for e in waiting) and (bool(granted) or snap.get('reservations', 0) > 0))   # an ELIGIBLE head waits on a lane someone holds
        primary = 'healthy-busy' if busy else 'unknown'
    # History for the owner, never a trigger: a grant that lapsed unclaimed shortly ago (a lost callback).
    notes = ['%s grant lapsed unclaimed' % e['id'] for e in entries if e['state'] == 'EXPIRED' and isinstance(e.get('grant'), dict)
             and not e.get('holder') and 0 <= (now - safe_time(e['grant']['expires'])).total_seconds() < 3 * 3600]
    return {'causes': found, 'primary': primary, 'waiting': len(waiting), 'oldest_grant_s': oldest, 'notes': notes, 'notices': notices,
            'unclaimed': unclaimed, 'grant_escalated': escalated, 'grant_stall_s': stall_s}


def gap_discount_s(gaps, start_wall):
    """Seconds of observed gaps (sleep, clock step, a stopped monitor, a reboot) that fall after `start_wall`; a gap that began before it counts in proportion.
    A gap is [wall at the last tick, wall now, seconds not to be trusted as elapsed time]."""
    total = 0.0
    for g_start, g_end, seconds in gaps:
        if seconds <= 0 or g_end <= start_wall:
            continue
        total += seconds if start_wall <= g_start or g_end <= g_start else seconds * (g_end - start_wall) / (g_end - g_start)
    return total


def discounted_age(start, now, gaps):
    """Wall age minus the gaps the monitor observed since `start`; None for a missing or future start (a clock step is never an age)."""
    raw = None if start is None else (now - start).total_seconds()
    if raw is None or raw < 0:
        return None
    return int(max(0.0, raw - gap_discount_s(gaps, start.timestamp())))


def queued_age_s(entry, now, gaps=()):
    """Seconds the entry has waited since `created`, not counting observed gaps (a host asleep is not an excessive wait)."""
    return discounted_age(safe_time(entry.get('created')), now, gaps)


def run_deadline(entry):
    """(seconds, source): the job's declared `deadline_s` when the entry carries a well-typed one (no producer writes it yet), else the 6 h default."""
    declared = entry.get('deadline_s')
    if type(declared) is int and 0 < declared <= RUN_DEADLINE_MAX_S:
        return declared, 'declared'
    return RUN_DEFAULT_DEADLINE_S, 'default'


def run_elapsed_s(entry, now, gaps=()):
    holder = entry.get('holder')
    return discounted_age(safe_time(holder.get('claimed_at')) if isinstance(holder, dict) else None, now, gaps)


def blocker_for(verdict, entries, now):
    """The already-classified global cause holding the queue (attribution only), else whether a higher entry is running or granted, else none."""
    for cause in GLOBAL_BLOCKERS:
        if cause in verdict['causes']:
            return cause
    if any(e['state'] == 'CLAIMED' or (e['state'] == 'GRANTED' and safe_time(e['grant']['expires']) > now) for e in entries):
        return 'higher-entry-running'
    return 'none'


def higher_entries_within_deadline(entries, claims, now, gaps=()):
    """True when something is running and EVERY running entry has a live holder, is not CPU-stalled and is still inside its own deadline. A dead, stale,
    stalled or unknown holder, or a claim time that cannot be read, never counts as healthy work."""
    running = [e for e in entries if e['state'] == 'CLAIMED']

    def healthy(e):
        claim, elapsed = claims.get(e['id'], {}), run_elapsed_s(e, now, gaps)
        return claim.get('status') == 'alive' and claim.get('idle_s', 0) < CPU_STALL_S and elapsed is not None and elapsed < run_deadline(e)[0]
    return bool(running) and all(healthy(e) for e in running)


def waiting_entries(entries, now, gaps=()):
    """Entries still waiting inside their window, oldest first; only those with a trustworthy age."""
    out = [(queued_age_s(e, now, gaps), e) for e in entries if e['state'] in ('QUEUED', 'ELIGIBLE') and safe_time(e['wait_expires']) > now]
    return sorted(((a, e) for a, e in out if a is not None), key=lambda x: -x[0])


def wait_notices(entries, now, gaps=()):
    """Status-only lines for entries waiting past the 4 h notice and short of the escalate point. Bounded; never a page."""
    rows = [(e['id'], age) for age, e in waiting_entries(entries, now, gaps) if WAIT_NOTICE_S <= age < WAIT_ESCALATE_S]
    lines = ['%s queued %d h, not yet granted' % (qid, age // 3600) for qid, age in rows[:NOTICE_LIST_MAX]]
    if len(rows) > NOTICE_LIST_MAX:
        lines.append('%d more entries queued over %d h' % (len(rows) - NOTICE_LIST_MAX, WAIT_NOTICE_S // 3600))
    return lines


def resolve_owner(entry, sessions, process=None, launcher=None):
    """Who owns this entry, as a closed record {kind, session_id, reachable, evidence}. Pure: all observation comes through the arguments.

    `sessions`: {session id: {'kind': 'claude'|'codex', 'live': True|False|None}} (local session records, may be empty). `process(pid, token)` and
    `launcher(session_id)` answer True (alive), False (proven gone) or None (could not be inspected). `reachable` is False only when every signal that
    exists says gone; anything uninspectable, absent or contradictory is None or True, never gone. No model call, no message."""
    def ask(fn, *args):
        try:
            answer = fn(*args) if fn else None
        except Exception:
            return None
        return answer if isinstance(answer, bool) else None
    owner = entry.get('owner') if isinstance(entry.get('owner'), dict) else {}
    sid = owner.get('session_id', entry.get('session_id'))
    sid = sid if isinstance(sid, str) and 0 < len(sid) <= 64 else None
    record = (sessions or {}).get(sid) if sid else None
    record = record if isinstance(record, dict) else {}
    holder, waiter = entry.get('holder'), entry.get('waiter')
    if record.get('kind') in ('claude', 'codex'):
        kind = record['kind']
    elif entry.get('harness') in ('claude', 'codex'):
        kind = entry['harness']
    elif str(entry.get('label') or '').startswith('la-cargo'):
        kind = 'la-cargo'
    elif isinstance(holder, dict) or isinstance(waiter, dict) or entry.get('class'):
        kind = 'heavy-run'
    else:
        kind = 'unknown'
    evidence, signals = [], []
    if sid is None:
        evidence.append('no-session-id')
    else:
        live = record.get('live') if isinstance(record.get('live'), bool) else ask(launcher, sid)
        signals.append(live)
        evidence.append({True: 'session-live', False: 'session-ended', None: 'session-unknown'}[live])
    proc = holder if isinstance(holder, dict) and entry.get('state') == 'CLAIMED' else waiter
    if isinstance(proc, dict) and type(proc.get('pid')) is int and isinstance(proc.get('start_token'), str):
        alive = ask(process, proc['pid'], proc['start_token'])
        signals.append(alive)
        evidence.append({True: 'process-alive', False: 'process-gone', None: 'process-uninspectable'}[alive])
    reachable = True if True in signals else (False if signals and all(x is False for x in signals) else None)
    return {'kind': kind, 'session_id': sid, 'reachable': reachable, 'evidence': [x for x in OWNER_EVIDENCE if x in evidence]}


def outbox_record(rec, key, owner, now, extra):
    """One owner-outbox line: enum values, the incident key and the job id only; never a path, command line or session id."""
    cause = {'wait': 'queue-wait-excessive', 'run': 'workload-over-deadline', 'grant': 'unclaimed-handoff'}[rec['clock']]
    wanted = list(extra) + list(owner['evidence'])
    return {'v': 1, 'at': iso(now), 'key': key, 'job': rec['id'], 'clock': rec['clock'], 'phase': rec['phase'], 'cause': cause, 'blocker': rec['blocker'],
            'evidence': [x for x in EVIDENCE if x in wanted], 'owner_action': OWNER_ACTION[cause],
            'recovery': 'wait-expiry-near' if rec['phase'] == 'near' else 'lapsed' if rec['phase'] == 'lapsed' else 'none', 'owner': {'kind': owner['kind'], 'reachable': owner['reachable']}}


def outbox_has(directory, key, phase):
    """True when the outbox (current file or its one backup) already holds a line for this incident key and phase: a crash between the append and the
    state write must not produce a second line."""
    for name in (OUTBOX_NAME, OUTBOX_NAME + '.1'):
        path = directory / name
        try:
            text = '' if path.is_symlink() else path.read_text(errors='replace')
        except OSError:
            continue
        for line in text.splitlines():
            if key in line:
                try:
                    found = json.loads(line)
                except ValueError:
                    continue
                if isinstance(found, dict) and found.get('key') == key and found.get('phase') == phase:
                    return True
    return False


def outbox_append(directory, record):
    """Append one line to the bounded owner outbox (rotated to one backup at 1 MiB, never deleted). The monitor cannot message a peer session: the
    coordinating session or a future adapter forwards these facts."""
    path = directory / OUTBOX_NAME
    with contextlib.suppress(OSError):
        if path.stat().st_size >= OUTBOX_LIMIT:
            os.replace(path, directory / (OUTBOX_NAME + '.1'))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        handle.write(json.dumps(record, sort_keys=True) + '\n')


def signature(verdict, entries):
    """Stable while the same blockage lasts: the cause set plus the head, claimed and granted ids; a new enqueue behind them changes nothing."""
    live = [e for e in entries if e['state'] in ('GRANTED', 'CLAIMED')]
    waiting = [e['id'] for e in entries if e['state'] in ('QUEUED', 'ELIGIBLE')][:1]
    raw = json.dumps([verdict['primary'], sorted(verdict['causes']), sorted(e['id'] for e in live)[:8], waiting], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def alert_due(state, key, now_wall):
    sent = (state.get('alerts') or {}).get(key)
    if sent is None:
        return True
    if not sent.get('delivered'):
        return now_wall >= sent.get('next_try', 0)
    return now_wall - sent['sent_at'] >= ALERT_COOLDOWN_S


def advance_allowed(state, now_wall):
    """At most one advance per ADVANCE_MIN_GAP_S and ADVANCE_MAX_PER_WINDOW per rolling window, whatever the blocker's signature."""
    log = [t for t in state.get('advances') or [] if now_wall - t < ADVANCE_WINDOW_S]
    if log and now_wall - max(log) < ADVANCE_MIN_GAP_S:
        return False, 'min-gap'
    if len(log) >= ADVANCE_MAX_PER_WINDOW:
        return False, 'recovery-exhausted'
    return True, 'ok'


def clean_incident(rec):
    """A well-typed copy of a stored unclaimed-grant incident, or None when its identity fields are damaged."""
    def num(key):
        value = rec.get(key)
        return value if finite(value) else None
    if not (isinstance(rec, dict) and isinstance(rec.get('id'), str) and len(rec['id']) <= 16 and num('grant_wall') is not None
            and num('expires_wall') is not None and rec.get('phase') in PHASES):
        return None
    verify = rec.get('verify')
    verify = {k: verify[k] for k in ('at', 'lanes_in_use', 'next', 'next_started') if k in verify and isinstance(verify[k], (str, int, bool, type(None)))
              } if isinstance(verify, dict) else None
    def count(key):
        value = rec.get(key)
        return value if type(value) is int and 0 <= value < 10 ** 9 else None
    token = rec.get('holder_token')
    return {'clock': rec['clock'] if rec.get('clock') in CLOCKS else 'grant', 'blocker': rec['blocker'] if rec.get('blocker') in BLOCKERS else 'none',
            'holder_pid': count('holder_pid'), 'holder_token': token if isinstance(token, str) and len(token) <= 64 else None, 'elapsed_s': count('elapsed_s'),
            'deadline_s': count('deadline_s'), 'deadline_source': rec['deadline_source'] if rec.get('deadline_source') in ('declared', 'default') else None,
            'outbox_phase': rec['outbox_phase'] if rec.get('outbox_phase') in PHASES else None, 'status': SUPPRESSED if rec.get('status') == SUPPRESSED else None,
            'resolved_at': num('resolved_at'), 'discounted_s': count('discounted_s'),
            'id': rec['id'], 'phase': rec['phase'], 'grant_wall': num('grant_wall'), 'expires_wall': num('expires_wall'), 'raised_at': num('raised_at'),
            'ack_deadline': num('ack_deadline'), 'delivered_at': num('delivered_at'), 'acknowledged_at': num('acknowledged_at'),
            'notified_phase': rec['notified_phase'] if rec.get('notified_phase') in PHASES else None,
            'owner_notice_state': rec['owner_notice_state'] if rec.get('owner_notice_state') in ('acknowledged', 'delivered', 'unknown', 'undelivered', 'pending', 'resolved') else 'pending',
            'handed_off_at': num('handed_off_at'), 'claim_seen': rec.get('claim_seen') is True, 'handed_off_unverified': rec.get('handed_off_unverified') is True,
            'dedups': rec['dedups'] if type(rec.get('dedups')) is int and 0 <= rec['dedups'] < 1000 else 0,
            'delivery': rec['delivery'] if rec.get('delivery') in ('sent', 'pending', 'dropped', 'unknown', 'unconfirmed') else None,
            'attempts': rec['attempts'] if type(rec.get('attempts')) is int and 0 <= rec['attempts'] < 1000 else 0, 'next_try': num('next_try') or 0,
            'waiter': False, 'action': 'none', 'verify': verify}


def clean_recovery(rec):
    """A well-typed copy of a stored recovery incident (a terminated wrapper or a cancelled grant), or None when its identity fields are damaged."""
    if not (isinstance(rec, dict) and rec.get('kind') in ('wrapper', 'grant') and isinstance(rec.get('id'), str) and len(rec['id']) <= 16
            and finite(rec.get('at')) and rec.get('outcome') in RECOVERY_OUTCOMES):
        return None

    def count(key, high=10 ** 9):
        value = rec.get(key)
        return value if type(value) is int and 0 <= value < high else None

    def number(key):
        return float(rec[key]) if finite(rec.get(key)) else None
    token, cancelled = rec.get('token'), rec.get('cancelled_at')
    return {'kind': rec['kind'], 'id': rec['id'], 'at': float(rec['at']), 'outcome': rec['outcome'], 'pid': count('pid', 2 ** 31),
            'token': token if isinstance(token, str) and len(token) <= 64 else None,
            'cancelled_at': cancelled if isinstance(cancelled, str) and len(cancelled) <= 32 and safe_time(cancelled) is not None else None,
            'verify': rec['verify'] if rec.get('verify') in ('pending', 'verified', 'failed') else None, 'verified_at': number('verified_at'),
            'next': rec['next'] if isinstance(rec.get('next'), str) and len(rec['next']) <= 16 else None,
            'attempts': count('attempts', 1000) or 0, 'next_try': number('next_try') or 0.0, 'handed': rec.get('handed') is True,
            'delivered_at': number('delivered_at'), 'delivery': rec['delivery'] if rec.get('delivery') in ('sent', 'pending', 'dropped', 'unknown', 'unconfirmed') else None,
            'dedups': count('dedups', 1000) or 0, 'outbox': rec.get('outbox') is True}


def bound_incidents(incidents):
    """At most INCIDENT_HARD_LIMIT kept: closed (acknowledged or resolved) incidents go first, oldest first; an open incident is never evicted."""
    excess = len(incidents) - INCIDENT_HARD_LIMIT
    for key, rec in sorted(incidents.items(), key=lambda kv: kv[1]['grant_wall']):
        if excess <= 0:
            break
        if rec['acknowledged_at'] is not None or rec['resolved_at'] is not None:
            del incidents[key]
            excess -= 1
    return dict(sorted(incidents.items(), key=lambda kv: kv[1]['grant_wall']))


def sanitize_state(state):
    """Field-by-field type check of a loaded state; a wrong type resets that field only (the counters that bound recovery are kept)."""
    def nums(value, limit):
        return [float(x) for x in value if finite(x)][-limit:] if isinstance(value, list) else []
    last = state.get('last_tick')
    if not (isinstance(last, dict) and all(finite(last.get(k)) for k in ('wall', 'mono'))):
        last = {}
    blocker = state.get('blocker')
    if not (isinstance(blocker, dict) and isinstance(blocker.get('signature'), str) and type(blocker.get('confirmations')) is int
            and finite(blocker.get('last', 0))):
        blocker = None
    alerts = state.get('alerts')
    alerts = {k: v for k, v in alerts.items() if isinstance(v, dict) and finite(v.get('sent_at')) and finite(v.get('next_try', 0)) and type(v.get('attempts', 0)) is int} if isinstance(alerts, dict) else {}
    cpu = state.get('cpu')
    cpu = {k: v for k, v in cpu.items() if isinstance(v, dict) and finite(v.get('cpu')) and finite(v.get('at'))} if isinstance(cpu, dict) else {}
    states = state.get('states')
    states = {k: v for k, v in states.items() if isinstance(v, str)} if isinstance(states, dict) else {}
    window = [[float(w), float(r)] for w, r in (x for x in (state.get('window') or []) if isinstance(x, list) and len(x) == 2
                                                 and all(finite(y) for y in x))][-WINDOW_MAX:] if isinstance(state.get('window'), list) else []
    progress = state.get('progress_at')
    incidents = state.get('incidents')
    incidents = {k: c for k, c in ((k, clean_incident(v)) for k, v in incidents.items() if isinstance(k, str) and len(k) <= 64)
                 if c} if isinstance(incidents, dict) else {}
    gaps = [[float(g[0]), float(g[1]), float(g[2])] for g in (state.get('gaps') if isinstance(state.get('gaps'), list) else [])
            if isinstance(g, list) and len(g) == 3 and all(finite(x) for x in g) and 0 <= g[2] <= GAP_MAX_S and g[0] <= g[1]][-GAP_LEDGER_MAX:]
    log = nums(state.get('recovery_log'), 64)
    candidates = state.get('candidates')
    candidates = dict(sorted(((k, float(v)) for k, v in candidates.items() if isinstance(k, str) and len(k) <= 64 and finite(v)), key=lambda kv: kv[1])[:RECOVERY_LIMIT]
                      ) if isinstance(candidates, dict) else {}
    recoveries = state.get('recoveries')
    recoveries = {k: c for k, c in ((k, clean_recovery(v)) for k, v in recoveries.items() if isinstance(k, str) and len(k) <= 64) if c} if isinstance(recoveries, dict) else {}
    return dict(schema=1, gaps=gaps, incidents=bound_incidents(incidents), recovery_log=log, candidates=candidates, recoveries=recoveries, last_tick=last, blocker=blocker, alerts=alerts, cpu=cpu, states=states, window=window,
                advances=nums(state.get('advances'), 64), progress_at=progress if finite(progress) else None,
                last_alert=state.get('last_alert') if isinstance(state.get('last_alert'), dict) and finite(state['last_alert'].get('clear_next', 0))
                and type(state['last_alert'].get('clear_tries', 0)) is int else None,
                clean_ticks=state.get('clean_ticks') if type(state.get('clean_ticks')) is int and 0 <= state['clean_ticks'] < 10 ** 6 else 0)


# ---- tick --------------------------------------------------------------------------------------------------------------

class Env:
    """Everything a tick touches outside its own state, so tests substitute it."""

    def __init__(self, da, state_path, memory_state_path, limits, clock=None, mono=None, boot=None, table=None, memory=None,
                 free=None, advance=None, notify=None, clear=None, reservations=None, stamps=None, events=None, emit_incident=None,
                 owner_sessions=None, owner_process=None, owner_launcher=None, recovery_env=None):
        self.da, self.state_path, self.memory_state_path, self.limits = da, Path(state_path), Path(memory_state_path), limits
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.mono = mono or time.monotonic
        self.boot = boot or boot_time
        self.table = table or ps_table
        self.memory = memory or (lambda: memory_view(self.memory_state_path))
        self.free = free or (lambda: shutil.disk_usage('/System/Volumes/Data').free)
        self.advance = advance or self._advance
        self.reservations = reservations or self._reservations
        self.stamps = stamps or self._stamps
        self.events = events or self._events
        self.emit_incident = emit_incident or session_notify_incident
        self.owner_sessions = owner_sessions or (lambda: {})      # local session records: none are read yet, so the harness field decides the kind
        self.owner_process = owner_process or self._owner_process
        self.owner_launcher = owner_launcher or (lambda session: None)      # launcher metadata is not readable here: uninspectable, never gone
        self.recovery_env = recovery_env or self._recovery_env
        self.notify = notify or session_notify
        self.clear = clear or session_clear

    def _owner_process(self, pid, token):
        """True when `pid` is still the process with that start token, False when proven gone or reused, None when it could not be inspected."""
        try:
            current = self.da.process_token(pid)
        except Exception:
            return None
        return False if current is None else current == token

    def owner(self, entry):
        try:
            return resolve_owner(entry, self.owner_sessions(), self.owner_process, self.owner_launcher)
        except Exception:
            return resolve_owner({}, {})

    def _recovery_env(self, directory):
        """The real bindings of the recovery library, built only when a candidate exists. Every answer that cannot be inspected is unknown or
        `holds`, which the library turns into a refusal."""
        lib = recovery_lib()
        renv = lib.default_env(self.state_path, directory)
        registry, uid = lib.wrappers_dir(self.state_path), renv.uid
        renv.registered_fn = lambda qid: lib.read_registry(qid, registry, uid, token_fn=renv.token_fn)
        renv.exe_fn = lib.exe_identity
        renv.holds_workload_fn = lambda pid: holds_workload(renv.table_fn, pid)
        renv.owner_exec_fn = self.owner_exec
        renv.kill_fn = sigterm_only
        renv.sleep_fn = time.sleep
        return renv

    def owner_exec(self, entry):
        """'gone' | 'live' | 'unknown' for the entry's owner. A declared waiter is the executor: its own process answers. A hand-off entry has no waiter, so
        only a session record that says the owning session ended can prove it gone (none is read yet: such an entry stays unknown)."""
        waiter = entry.get('waiter')
        if isinstance(waiter, dict) and type(waiter.get('pid')) is int and isinstance(waiter.get('start_token'), str):
            try:
                token = self.da.waiter_token(waiter['pid'])
            except Exception:
                return 'unknown'
            return 'gone' if token is None or token != waiter['start_token'] else 'live'
        return {True: 'live', False: 'gone'}.get(self.owner(entry)['reachable'], 'unknown')

    def _reservations(self):
        """Number of reservation files (lane holders outside the queue too); read-only, 0 when unreadable."""
        try:
            return sum(1 for _ in (self.state_path.parent / 'reservations').glob('*.json'))
        except OSError:
            return 0

    def _stamps(self, granted):
        """{entry id: runner stamp} for the given GRANTED entries, from the newest matching 'granted' receipt; absent means unstamped."""
        want = {e['id']: e['grant']['at'] for e in granted}
        found = {}
        for name in ('receipts.jsonl.1', 'receipts.jsonl'):
            try:
                text = (self.state_path.parent / 'queue' / name).read_text(errors='replace')
            except OSError:
                continue
            for line in text.splitlines():
                if '"granted"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('event') == 'granted' and rec.get('id') in want and rec.get('ts') == want[rec['id']]:
                    found[rec['id']] = rec.get('runner')
        return found

    def _events(self, ids):
        """{entry id: set of receipt events} for the given ids, read-only; an unreadable receipt file leaves them empty."""
        want, found = set(ids), {}
        for name in ('receipts.jsonl.1', 'receipts.jsonl'):
            try:
                text = (self.state_path.parent / 'queue' / name).read_text(errors='replace')
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('id') in want and isinstance(rec.get('event'), str):
                    found.setdefault(rec['id'], set()).add(rec['event'])
        return found

    def _advance(self):
        """Run one `queue advance`. A slow one is left to finish on its own (never signalled); the next tick's spacing covers it."""
        proc = subprocess.Popen([sys.executable, str(HERE / 'disk-admission.py'), 'queue', 'advance', '--json'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            return {'exit': None, 'out': 'still running after 180 s; left to finish'}
        return {'exit': proc.returncode, 'out': (out or '')[-400:]}


_RECOVERY = None


def recovery_lib():
    global _RECOVERY
    if _RECOVERY is None:
        _RECOVERY = load('admission-recovery')
    return _RECOVERY


def sigterm_only(pid, sig):
    """The only signal path of the monitor: one SIGTERM to one positive pid. Any other signal, process groups and pid 0/1 are refused here as well as in the library."""
    if sig != signal.SIGTERM or type(pid) is not int or pid <= 1:
        raise OSError('only SIGTERM to one process is allowed')
    os.kill(pid, sig)


def holds_workload(table_fn, pid, lsof=None):
    """True when the wrapper may be holding a workload: any other live process in its group or below it, an open cargo lock, or anything
    that could not be inspected (uncertain is True). False only when the table and the open-file list were both read and show neither."""
    try:
        table = table_fn()
        row = table[pid]
    except Exception:
        return True
    below, frontier = set(), [pid]
    while frontier:
        parent = frontier.pop()
        for other, r in table.items():
            if r[0] == parent and other not in below and other != pid:
                below.add(other)
                frontier.append(other)
    if any(other != pid and 'Z' not in r[2] and (r[1] == row[1] or other in below) for other, r in table.items()):
        return True
    try:
        if lsof is not None:
            out = lsof(pid)
        else:
            tool = next((path for path in ('/usr/sbin/lsof', '/usr/bin/lsof') if os.path.exists(path)), None)
            if tool is None:
                return True      # no way to look at open files: uncertain is holding
            done = subprocess.run([tool, '-nP', '-Fn', '-p', str(pid)], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
            out = done.stdout if done.returncode == 0 else ''
    except Exception:
        return True
    names = [line[1:] for line in (out or '').splitlines() if line.startswith('n')]
    if not names:
        return True
    return any(os.path.basename(n) in ('.cargo-lock', '.package-cache') for n in names)


def recovery_phrase(actions, exhausted):
    if exhausted:
        return 'recovery-exhausted'
    for action in actions:
        if action.get('recovery') in ('wrapper-terminated', 'grant-cancelled', 'termination-failed'):
            return action['recovery']
    if any(a.get('handoff') == 'lapsed' for a in actions):
        return 'lapsed'
    if any(a.get('wait') == 'expiry-near' for a in actions):
        return 'wait-expiry-near'
    if any(a.get('advance') == 'ran' for a in actions):
        return 'advance-tried'
    if any(a.get('advance') == 'would-run' for a in actions):
        return 'observe-only'
    return 'none'


def session_notify(alert, runner=None):
    """Raise the alert through the existing session-notify pipeline (enum phrases only; its own dedup, retry and backends apply).

    True when that pipeline accepted the request (exit 0). With every backend off that records the alert and shows no banner;
    `status.json` keeps the same facts either way."""
    actions = alert.get('recovery') or []
    exhausted = any(a.get('advance') == 'recovery-exhausted' for a in actions)
    argv = [sys.executable, str(HERE / 'session-notify.py'), 'emit-resource', '--cause', alert['cause'], '--recovery',
            recovery_phrase(actions, exhausted), '--owner-action', alert.get('owner_action') or 'inspect-admission-state',
            '--signature', alert['signature']]
    try:
        done = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def session_notify_incident(alert, runner=None):
    """Raise an unclaimed-grant notice with `--strict` and report what session-notify says: {'result': queued|deduped|no-backend|failed,
    'delivery': sent|pending|dropped|unknown|None}. `queued` is a hand-off to the pipeline, not delivery; `sent` means a backend accepted it."""
    actions = alert.get('recovery') or []
    argv = [sys.executable, str(HERE / 'session-notify.py'), 'emit-resource', '--cause', alert['cause'], '--recovery', recovery_phrase(actions, False),
            '--owner-action', alert.get('owner_action') or 'inspect-admission-state', '--signature', alert['signature'], '--strict']
    if alert.get('incident_ref'):
        argv += ['--incident-ref', alert['incident_ref']]
    try:
        done = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {'result': 'failed', 'delivery': None}
    seen = re.search(r'emit-resource: (queued|deduped|no-backend)(?: delivery=(sent|pending|dropped|unknown))?', getattr(done, 'stdout', '') or '')
    if done.returncode != 0 or not seen:
        return {'result': 'failed', 'delivery': None}
    return {'result': seen.group(1), 'delivery': seen.group(2)}


def session_clear(runner=None):
    """Tell session-notify the blockage ended, so a later recurrence of the same cause is announced again."""
    argv = [sys.executable, str(HERE / 'session-notify.py'), 'emit-resource', '--clear']
    try:
        done = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def default_limits(da, ma):
    return {'floor': da.HEAVY_FLOOR, 'enter': ma.ENTER, 'exit': ma.EXIT, 'exit_samples': ma.EXIT_SAMPLES}


def tick(env, act=False, if_due=False):
    """One idempotent pass. Returns the result dict (also persisted as status.json). Never raises on bad input: a failed observation
    is itself reported as `state-unreadable`."""
    directory = monitor_dir(env.state_path)
    if directory.is_symlink():
        return {'ran': False, 'why': 'monitor directory is a symlink'}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(directory / 'monitor.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        return {'ran': False, 'why': 'another monitor holds the lock'}
    try:
        try:
            return _tick_locked(env, directory, act, if_due)
        except Exception as exc:   # a tick must leave a status behind, not a traceback launchd sends to /dev/null
            result = {'ran': True, 'at': iso(env.clock()), 'primary': 'state-unreadable', 'causes': {'state-unreadable': '%s: %s' % (
                type(exc).__name__, str(exc)[:160])}, 'waiting': None, 'stalled': False, 'confirmed': False, 'actions': [],
                'granted_is_execution': False, 'owner_action': OWNER_ACTION['state-unreadable']}
            with contextlib.suppress(OSError):
                write_json(directory / 'status.json', result)
            return result
    finally:
        os.close(lock_fd)


def _tick_locked(env, directory, act, if_due):
    now = env.clock()
    wall, mono, boot = now.timestamp(), env.mono(), env.boot()
    path = directory / 'state.json'
    prev = read_json(path, None)
    fresh_start = not isinstance(prev, dict) or prev.get('schema') != 1
    state = sanitize_state({} if fresh_start else prev)
    last = state['last_tick']
    if if_due and last and wall - last['wall'] < INTERVAL_S:
        return {'ran': False, 'why': 'not due'}
    gap, rebooted = fresh_start, False    # nothing is known yet on a first tick: observe once before acting
    if last:
        if boot and prev.get('last_tick', {}).get('boot') and boot != prev['last_tick']['boot']:
            gap = rebooted = True
        elif wall - last['wall'] > INTERVAL_S * GAP_FACTOR or (wall - last['wall']) - (mono - last['mono']) > 60 or wall < last['wall']:
            gap = True
    if gap:    # a gap's duration is not stall time: every clock restarts, and a flat-compressor window never spans one
        if last and wall > last['wall']:      # remember how much of this span cannot be trusted as elapsed time (sleep, clock step, reboot, a stopped monitor)
            state['gaps'] = (state['gaps'] + [[last['wall'], wall, min(GAP_MAX_S, max(0.0, wall - last['wall'] - INTERVAL_S))]])[-GAP_LEDGER_MAX:]
        state['progress_at'] = wall
        state['window'] = []
        state['cpu'] = {k: {'cpu': v['cpu'], 'at': wall} for k, v in state['cpu'].items()}
        state['blocker'] = None
        if rebooted:
            state['cpu'], state['states'] = {}, {}
    try:
        entries, problems = env.da._queue_entries(env.state_path)
    except Exception as exc:    # StateError from a symlinked queue directory, or an unreadable store
        entries, problems = [], ['queue-unreadable: %s' % str(exc)[:160]]
    entries, bad = usable_entries(entries)
    problems = problems + bad
    try:
        table = env.table()
    except (OSError, subprocess.SubprocessError):
        table = {}
    cpu_prev = state['cpu']
    claims, cpu_now, progress = {}, {}, False
    for entry in entries:
        if entry['state'] != 'CLAIMED':
            continue
        holder = entry['holder']
        status = env.da.holder_status(holder['pid'], holder['start_token'], holder.get('workload'), None, None)
        cpu = tree_cpu(table, holder['pid']) if table else 0.0
        before = cpu_prev.get(entry['id'])
        if before is None or cpu != before['cpu']:      # any change counts: reaped children make a tree's total fall as well as rise
            cpu_now[entry['id']] = {'cpu': cpu, 'at': wall}
            progress = progress or (before is not None)
        else:
            cpu_now[entry['id']] = before
        claims[entry['id']] = {'status': status.state, 'idle_s': int(wall - cpu_now[entry['id']]['at'])}
    states_now = {e['id']: e['state'] for e in entries if e['state'] in LIVE}      # terminal entries never accumulate in the state file
    if not gap:
        now_by_id = {e['id']: e['state'] for e in entries}
        for qid, before in state['states'].items():
            after = now_by_id.get(qid)
            if (after in ('CLAIMED', 'RELEASED') and after != before) or (after is None and before == 'CLAIMED' and not problems):
                progress = True      # a claim started, finished, or finished and was garbage-collected between two ticks
    try:
        mem = env.memory()
    except (OSError, subprocess.SubprocessError, ValueError):
        mem = {'latched': False, 'metrics': {}}
    ratio = (mem.get('metrics') or {}).get('compressor_ratio')
    window = state['window']
    if ratio is not None:
        window = (window + [[wall, ratio]])[-WINDOW_MAX:]
    snap = {'now': now, 'wall': wall, 'entries': entries, 'problems': problems, 'free_bytes': env.free(), 'mem': mem,
            'window': window, 'claims': claims, 'protocol': env.da.RUNNER_PROTOCOL, 'reservations': env.reservations(),
            'stamps': env.stamps([e for e in entries if e['state'] == 'GRANTED'])}
    verdict = classify(snap, env.limits)
    waiting = verdict['waiting']
    old_grant = verdict['grant_escalated']
    progress_at = state['progress_at'] if state['progress_at'] is not None else wall
    if progress or not (waiting or old_grant):
        progress_at = wall
    stalled_for = int(wall - progress_at) if (waiting or old_grant) else 0
    if old_grant:
        stalled_for = max(stalled_for, verdict['oldest_grant_s'])    # a grant nobody claimed has been idle since it was made
    sig = signature(verdict, entries)
    blocker = state['blocker']
    if verdict['causes'] and blocker and blocker['signature'] == sig and not gap:
        if wall - blocker.get('last', 0) >= CONFIRM_SPACING_S:      # two quick manual ticks are one observation
            blocker['confirmations'] += 1
            blocker['last'] = wall
    elif verdict['causes']:
        blocker = {'signature': sig, 'since': iso(now), 'confirmations': 0 if gap else 1, 'last': wall}
    else:
        blocker = None
    confirmed = bool(blocker) and blocker['confirmations'] >= CONFIRM_TICKS
    quiet = verdict['primary'] in ('idle', 'healthy-busy')
    stalled = (waiting > 0 or old_grant) and stalled_for >= (STALL_S if waiting else verdict['grant_stall_s'] or 0) and not quiet
    result = {'ran': True, 'at': iso(now), 'observation_gap': gap, 'primary': verdict['primary'], 'causes': verdict['causes'],
              'waiting': waiting, 'stalled': stalled, 'stalled_for_s': stalled_for, 'signature': sig, 'confirmed': confirmed,
              'owner_action': OWNER_ACTION.get(verdict['primary']), 'granted_is_execution': False, 'actions': [],
              'notes': verdict['notes'], 'notices': verdict['notices'] + wait_notices(entries, now, state['gaps'])}
    state['advances'] = [t for t in state['advances'] if wall - t < ADVANCE_WINDOW_S]
    wants_advance = waiting > 0 and not gap and confirmed and any(c in ADVANCE_CAUSES for c in verdict['causes']) and not (
        {'disk-floor', 'memory-pressure', 'state-unreadable'} & set(verdict['causes']))
    if wants_advance:
        ok, why = advance_allowed(state, wall)
        if not act:
            result['actions'].append({'advance': 'would-run' if ok else why})
        elif ok:
            state['advances'].append(wall)      # recorded first: a crash or timeout after this point still counts against the bound
            persist(directory, state, cpu_now, states_now, window, blocker, progress_at, wall, mono, boot)
            try:
                outcome = env.advance()
            except Exception as exc:
                outcome = {'exit': None, 'out': '%s' % type(exc).__name__}
            result['actions'].append({'advance': 'ran', 'exit': outcome.get('exit')})
        else:
            result['actions'].append({'advance': why})
            if why == 'recovery-exhausted':
                result['recovery_exhausted'] = True
    alert_key = '%s:%s' % (verdict['primary'], sig)
    state['alerts'] = {k: v for k, v in state['alerts'].items() if wall - v['sent_at'] < 2 * ALERT_COOLDOWN_S}
    escalate = stalled and confirmed and verdict['primary'] != 'unclaimed-handoff' and (verdict['primary'] not in RECOVERABLE or result.get('recovery_exhausted')
                                          or stalled_for >= 2 * STALL_S)
    if escalate and alert_due(state, alert_key, wall):
        alert = {'cause': verdict['primary'], 'causes': sorted(verdict['causes']), 'signature': sig, 'detail': verdict['causes'],
                 'recovery': result['actions'], 'owner_action': result['owner_action'], 'blocked_for_s': stalled_for}
        try:
            delivered = bool(env.notify(alert))
        except Exception:
            delivered = False
        previous = state['alerts'].get(alert_key, {})
        attempts = previous.get('attempts', 0) + 1
        state['alerts'][alert_key] = {'sent_at': wall, 'delivered': delivered, 'attempts': attempts,
                                      'next_try': wall + min(300 * 2 ** (attempts - 1), ALERT_COOLDOWN_S)}
        state['last_alert'] = {'at': iso(now), 'cause': verdict['primary'], 'signature': sig, 'delivered': delivered, 'attempts': attempts}
        result['alert'] = dict(alert, delivered=delivered)
    handoff_incidents(env, state, verdict, entries, now, wall, result, gap and not fresh_start, claims=claims, directory=directory)
    if act:
        recovery_pass(env, state, entries, now, wall, gap, directory, result,
                      lambda: persist(directory, state, cpu_now, states_now, window, blocker, progress_at, wall, mono, boot))
    result['registry_pruned'] = prune_wrappers(env, directory)
    state['clean_ticks'] = 0 if verdict['causes'] or gap else state.get('clean_ticks', 0) + 1
    last_alert = state.get('last_alert')
    # A clear bumps session-notify's cycle and forgets its dedup records: never while an incident's notice is still being handed off or polled.
    open_notice = any(not incident_settled(r) for r in state['incidents'].values())
    if state['clean_ticks'] >= CLEAR_AFTER_TICKS and last_alert and not last_alert.get('cleared') and wall >= last_alert.get('clear_next', 0) and not open_notice:
        try:
            cleared = bool(env.clear())
        except Exception:
            cleared = False
        if cleared:
            last_alert['cleared'] = True
            state['alerts'] = {}      # the next incident may announce again even with the same cause and signature
            result['cleared'] = True
        else:
            tries = last_alert.get('clear_tries', 0) + 1
            last_alert.update(clear_tries=tries, clear_next=wall + min(300 * 2 ** (tries - 1), 3600))
    result['last_alert'] = state.get('last_alert')
    persist(directory, state, cpu_now, states_now, window, blocker, progress_at, wall, mono, boot)
    write_json(directory / 'status.json', result)
    receipts = directory / 'receipts.jsonl'
    with contextlib.suppress(OSError):
        if receipts.exists() and receipts.stat().st_size >= 1 << 20:
            os.replace(receipts, directory / 'receipts.jsonl.1')      # one backup, so history never silently stops
    with open(receipts, 'a') as handle:
        handle.write(json.dumps({k: result[k] for k in ('at', 'primary', 'waiting', 'stalled', 'confirmed', 'actions', 'observation_gap')},
                                sort_keys=True) + '\n')
    return result


def prune_wrappers(env, directory):
    """Drop wrapper-registry files that can no longer matter (dead pid, replaced start token, older than 7 days): unlink only, at most 50 per tick, in every
    tick mode and independent of the recovery kill switch (it removes records, it never acts on a process). Returns the count; never raises."""
    try:
        lib = recovery_lib()
        return lib.prune_registry(lib.wrappers_dir(env.state_path), lib.Env(directory, clock=env.clock))
    except Exception:
        return 0


def kill_switch_on(directory):
    """True when `recovery.disabled` exists (any file type) or cannot be examined: an unreadable switch is treated as pulled."""
    try:
        os.lstat(str(directory / RECOVERY_KILL_SWITCH))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def recovery_key(kind, qid, *identity):
    return '%s-%s' % (kind, hashlib.sha256(':'.join((kind, qid) + tuple(str(x) for x in identity)).encode()).hexdigest()[:12])


def recovery_allowed(state, wall):
    """At most RECOVERY_MAX_PER_WINDOW actions in a rolling window; the one-per-tick limit is the caller's. A clock step back never frees a slot."""
    log = [t for t in state['recovery_log'] if wall - t < RECOVERY_WINDOW_S]
    return len(log) < RECOVERY_MAX_PER_WINDOW


def new_recovery(kind, qid, wall, outcome, pid=None, token=None, cancelled_at=None):
    return {'kind': kind, 'id': qid, 'at': wall, 'outcome': outcome, 'pid': pid, 'token': token, 'cancelled_at': cancelled_at, 'verify': None,
            'verified_at': None, 'next': None, 'attempts': 0, 'next_try': 0.0, 'handed': False, 'delivered_at': None, 'delivery': None, 'dedups': 0,
            'outbox': False}


def recovery_pass(env, state, entries, now, wall, gap, directory, result, save):
    """Act-mode recovery (ADR 0019). Never called by observe-only or plain ticks. Order: verify earlier recoveries, then at most ONE new action
    (terminate a conclusively obsolete wrapper, else cancel a conclusively abandoned unstarted grant), then notices. The bound is recorded and persisted
    BEFORE the action, so a crash or timeout still counts. Everything uncertain is a refusal that the library reports; a survivor is reported, never escalated."""
    info = result['recovery'] = {'state': 'ok', 'acted': [], 'reported': [], 'verified': []}
    if kill_switch_on(directory):
        info['state'] = 'disabled'
        return
    lib = recovery_lib()
    try:
        lib.safe_monitor_dir(lib.Env(directory))
    except lib.UnsafeDir:
        info['state'] = 'unsafe-monitor-dir'
        return
    by_id, recs = {e['id']: e for e in entries}, state['recoveries']
    holder = []

    def get_env():
        if not holder:
            holder.append(env.recovery_env(directory))
        return holder[0]

    for key, rec in sorted(recs.items(), key=lambda kv: kv[1]['at']):
        if rec['outcome'] not in ('terminated', 'cancelled') or rec['verify'] in ('verified', 'failed') or wall <= rec['at']:
            continue      # verified on a LATER tick, exactly once to a final state
        checked = lib.verify_recovery(rec['id'], get_env(), cancelled_at=rec['cancelled_at'])
        rec['verify'] = checked['state']
        rec['next'] = str(checked['checks'].get('next_entry'))[:16] if checked['checks'].get('next_entry') else rec['next']
        if checked['state'] != 'pending':
            rec['verified_at'] = wall
        info['verified'].append({'id': rec['id'], 'kind': rec['kind'], 'state': checked['state']})
    acted = False

    def spend():
        state['recovery_log'] = [t for t in state['recovery_log'] if wall - t < RECOVERY_WINDOW_S] + [wall]
        save()

    wdir = lib.wrappers_dir(env.state_path)
    seen = {}
    registered = []
    for qid in lib.list_registered(wdir)[:RECOVERY_REGISTRY_MAX]:
        entry = by_id.get(qid)
        if entry is None or entry['state'] not in lib.TERMINAL:
            continue
        reg = get_env().registered_fn(qid)
        if not reg:
            continue
        ckey = recovery_key('wrapper', qid, reg['pid'], reg['start_token'])
        seen[ckey] = state['candidates'].get(ckey, wall)
        registered.append((qid, reg, ckey))
    state['candidates'] = dict(sorted(seen.items(), key=lambda kv: kv[1])[:RECOVERY_LIMIT])
    for qid, reg, ckey in registered:
        if gap or acted or ckey in recs or wall - state['candidates'].get(ckey, wall) < RECOVERY_CONFIRM_S:
            continue
        verdict = lib.obsolete_wrapper(qid, reg['pid'], get_env())
        if not verdict['obsolete']:
            info['reported'].append({'id': qid, 'kind': 'wrapper', 'refused': verdict['reasons'], 'children': len(verdict['children']), 'detached': len(verdict['detached'])})
            continue
        if not recovery_allowed(state, wall):
            info['state'] = 'recovery-bound'
            continue
        spend()
        acted = True
        out = lib.terminate_wrapper(qid, reg['pid'], get_env(), grace_s=RECOVERY_GRACE_S, poll_s=RECOVERY_POLL_S)
        if out['result'] in ('terminated', 'failed-to-terminate'):
            recs[ckey] = new_recovery('wrapper', qid, wall, out['result'], reg['pid'], reg['start_token'], iso(now))
            info['acted'].append({'id': qid, 'action': 'wrapper', 'result': out['result']})
        else:
            info['reported'].append({'id': qid, 'kind': 'wrapper', 'refused': [out['result']]})
        save()
    for entry in entries:
        if gap or acted or entry['state'] != 'GRANTED' or entry.get('holder'):
            continue
        age = (now - safe_time(entry['grant']['at'])).total_seconds()
        ckey = recovery_key('grant', entry['id'], entry['grant']['at'])
        if age <= lib.NOTICE_S or ckey in recs:
            continue
        if lib.abandoned_unstarted(entry, get_env())['action'] == 'none':   # the library's only other decision is a conclusive cancel
            continue
        if not recovery_allowed(state, wall):
            info['state'] = 'recovery-bound'
            continue
        spend()
        acted = True
        out = lib.do_cancel(entry, get_env())
        if out['result'] in ('cancelled', 'failed'):
            recs[ckey] = new_recovery('grant', entry['id'], wall, out['result'], cancelled_at=(out['receipt'] or {}).get('cancelled_at') or iso(now))
            info['acted'].append({'id': entry['id'], 'action': 'grant', 'result': out['result']})
        else:
            info['reported'].append({'id': entry['id'], 'kind': 'grant', 'refused': [out['result']]})
        save()
    for item in info['acted']:
        result['actions'].append({'recovery_action': '%s-%s' % (item['action'], item['result'])})
    recovery_notices(env, recs, by_id, now, wall, directory)
    state['recoveries'] = prune_recoveries(recs, wall)
    info['pending_verification'] = sum(1 for r in state['recoveries'].values() if r['outcome'] in ('terminated', 'cancelled') and r['verify'] in (None, 'pending'))
    result['recovery_incidents'] = [{'id': r['id'], 'kind': r['kind'], 'outcome': r['outcome'], 'verify': r['verify'], 'delivery': r['delivery'],
                                     'delivered': r['delivered_at'] is not None} for r in state['recoveries'].values()]


def recovery_settled(rec):
    return rec['outcome'] not in RECOVERY_NOTICE or rec['delivered_at'] is not None or rec['delivery'] in ('unknown', 'dropped', 'unconfirmed') or rec['dedups'] >= UNVERIFIED_AFTER_DEDUPS


def prune_recoveries(recs, wall):
    """Finished (verified or failed, notice settled) incidents older than a day go first; never an open one for the soft limit."""
    def done(r):
        return r['verify'] in ('verified', 'failed') or r['outcome'] in ('failed-to-terminate', 'failed')
    kept = {k: r for k, r in recs.items() if not (done(r) and recovery_settled(r) and wall - r['at'] > RECOVERY_KEEP_S)}
    for key, rec in sorted(kept.items(), key=lambda kv: kv[1]['at']):
        if len(kept) <= RECOVERY_LIMIT:
            break
        if done(rec) and recovery_settled(rec):
            del kept[key]
    return kept


def recovery_notices(env, recs, by_id, now, wall, directory):
    """One owner-outbox line and one strict session-notify request per recovery (enum phrases only), deduped by entry id, pid and start token (the key)."""
    for key, rec in sorted(recs.items(), key=lambda kv: kv[1]['at']):
        if rec['outcome'] not in RECOVERY_NOTICE or recovery_settled(rec) or wall < rec['next_try']:
            continue
        cause, phrase = RECOVERY_NOTICE[rec['outcome']]
        if not rec['outbox']:
            entry = by_id.get(rec['id'])
            owner = env.owner(entry) if entry else {'kind': 'unknown', 'reachable': None, 'evidence': []}
            line = {'v': 1, 'at': iso(now), 'key': key, 'job': rec['id'], 'clock': 'recovery', 'phase': rec['outcome'], 'cause': cause, 'blocker': 'none',
                    'evidence': [x for x in EVIDENCE if x in RECOVERY_EVIDENCE[rec['outcome']]], 'owner_action': OWNER_ACTION[cause], 'recovery': phrase,
                    'owner': {'kind': owner['kind'], 'reachable': owner['reachable']}}
            try:
                if not outbox_has(directory, key, rec['outcome']):
                    outbox_append(directory, line)
                rec['outbox'] = True
            except OSError:
                pass
        alert = {'cause': cause, 'causes': [cause], 'signature': incident_signature(key, rec['outcome']), 'detail': {'phase': rec['outcome']},
                 'recovery': [{'recovery': phrase}], 'owner_action': OWNER_ACTION[cause], 'blocked_for_s': int(max(0, wall - rec['at']))}
        try:
            outcome = env.emit_incident(alert)
        except Exception:
            outcome = {}
        rec['attempts'] += 1
        if outcome.get('result') == 'queued':
            rec['handed'] = True
        elif outcome.get('result') == 'deduped' and not rec['handed']:
            rec['dedups'] += 1      # an earlier call (a crash before the state was saved, another monitor) queued it; stop asking after a few
        if outcome.get('result') in ('queued', 'deduped'):
            rec['delivery'] = outcome.get('delivery')
            if outcome.get('delivery') == 'sent':
                rec['delivered_at'] = wall
            elif outcome.get('delivery') == 'pending' and rec['attempts'] >= 20:
                rec['delivery'] = 'unconfirmed'
        if not recovery_settled(rec):
            rec['next_try'] = wall + min(60 * 2 ** (min(rec['attempts'], 10) - 1), 900)


def incident_settled(rec):
    """Nothing more to ask session-notify about this incident: acknowledged, delivered, finished elsewhere, or unverifiable."""
    handed = rec['notified_phase'] == rec['phase']
    return rec['status'] == SUPPRESSED or rec['acknowledged_at'] is not None or rec['resolved_at'] is not None or rec['handed_off_unverified'] or (handed and (
        rec['delivered_at'] is not None or rec['delivery'] in ('unknown', 'dropped', 'unconfirmed')))


def owner_notice_state(rec):
    """What is known about the owner notice: acknowledged, delivered, unknown (finished by an earlier call), undelivered (retries ended, or the hand-off keeps
    failing: no backend), else pending. `undelivered` does not stop a clear, so it is surfaced in status instead; status.json is NOT delivery."""
    if rec['acknowledged_at'] is not None:
        return 'acknowledged'
    if rec['delivered_at'] is not None:
        return 'delivered'
    if rec['resolved_at'] is not None and (rec['status'] == SUPPRESSED or (rec['notified_phase'] is None and rec['attempts'] == 0)):
        return 'resolved'      # the wait or run ended and no notice was ever attempted: nothing is owed. A notice that was tried and never arrived stays undelivered.
    if rec['delivery'] == 'unknown':
        return 'unknown'
    if rec['handed_off_unverified'] or rec['delivery'] in ('dropped', 'unconfirmed') or (rec['notified_phase'] != rec['phase'] and rec['attempts'] >= 1):
        return 'undelivered'
    return 'pending'


def prune_incidents(incidents, wall):
    """Drop incidents that can no longer be re-raised; over the soft limit drop closed ones first, then ones past the lapse window. An incident still inside
    its lapse window is never evicted for the soft limit (it could be raised twice). A closed incident whose notice never arrived stays listed (undelivered)."""
    def closed_at(r):
        return r['acknowledged_at'] if r['acknowledged_at'] is not None else r['resolved_at']

    def done(r):      # closed, and nothing is owed to the owner
        return closed_at(r) is not None and r['owner_notice_state'] != 'undelivered'
    kept = {k: r for k, r in incidents.items() if not (wall - r['expires_wall'] > INCIDENT_KEEP_S or (done(r) and wall - closed_at(r) > INCIDENT_ACK_KEEP_S))}
    for old_first in (done, lambda r: wall - r['expires_wall'] > LAPSE_WINDOW_S):
        for key, rec in sorted(kept.items(), key=lambda kv: kv[1]['grant_wall']):
            if len(kept) <= INCIDENT_LIMIT:
                break
            if old_first(rec):
                del kept[key]
    return bound_incidents(kept)


def handoff_incidents(env, state, verdict, entries, now, wall, result, gap=False, claims=None, directory=None):
    """Unclaimed-grant incidents (ADR 0019): raised at the escalate point before the grant lapses, or once on lapse if it never was.

    Key = entry id plus grant time, so repeated ticks, restarts and a second monitor are one incident; a lapse after a raise upgrades it (at most two
    notices per incident: escalate and lapse). `handed_off_at` is set only when session-notify queued the notice; `delivered_at` only when its own
    result shows a backend accepted it; acknowledgement (a claim or release by a holder; never inferred from silence) and the ack deadline are separate
    fields. On a tick after an observation gap (sleep, reboot, clock step) wall-clock age is not trusted: only the scheduler's own EXPIRED state may
    raise. The monitor takes no action on the entry: the scheduler's own expiry frees the lane, which is only verified here."""
    incidents, by_id = state['incidents'], {e['id']: e for e in entries}
    for rec in incidents.values():
        entry = by_id.get(rec['id'])
        if rec['clock'] == 'grant' and entry and entry['state'] in ('CLAIMED', 'RELEASED'):
            rec['claim_seen'] = True
            if rec['acknowledged_at'] is None:
                rec['acknowledged_at'] = wall
    unseen = [e['id'] for e in entries if e['state'] in ('GRANTED', 'EXPIRED') and isinstance(e.get('grant'), dict) and is_handoff(e)
              and not e.get('holder') and incident_key(e) not in incidents and safe_time(e['grant']['expires']) <= now]
    try:
        events = env.events(unseen) if unseen else {}      # receipts are read only for a lapse that has no incident yet
    except Exception:
        events = {}
    wanted = [] if gap else [(e, 'escalated') for e in entries if e['id'] in verdict['unclaimed']]
    wanted += [(e, 'lapsed') for e in lapsed_handoffs(entries, events, now, recorded_only=gap)]
    for entry, phase in wanted:
        key, rec = incident_key(entry), incidents.get(incident_key(entry))
        if rec is None:
            granted_at, expires = safe_time(entry['grant']['at']).timestamp(), safe_time(entry['grant']['expires']).timestamp()
            incidents[key] = new_incident(entry['id'], phase, granted_at, expires, wall, granted_at + escalate_s(entry), 'grant', 'none')
        elif phase == 'lapsed' and rec['phase'] == 'escalated' and not rec['claim_seen']:
            rec.update(phase='lapsed', attempts=0, next_try=0, handed_off_at=None, delivered_at=None, delivery=None, handed_off_unverified=False, dedups=0)
    wait_run_incidents(env, state, verdict, entries, claims or {}, now, wall, gap, directory, result)
    in_use = sum(1 for e in entries if e['state'] == 'CLAIMED' or (e['state'] == 'GRANTED' and safe_time(e['grant']['expires']) > now))
    waiting = [e['id'] for e in entries if e['state'] in ('QUEUED', 'ELIGIBLE') and safe_time(e['wait_expires']) > now]
    for key, rec in sorted(incidents.items(), key=lambda kv: kv[1]['grant_wall']):
        if rec['phase'] == 'lapsed':     # the lane must be free again, and an eligible entry should start (existing continuation checks decide blame)
            before = rec['verify'] or {}
            nxt = before.get('next') or (waiting[0] if waiting else None)
            started = bool(before.get('next_started')) or (nxt is not None and by_id.get(nxt, {}).get('state') in ('GRANTED', 'CLAIMED', 'RELEASED'))
            rec['verify'] = {'at': iso(now), 'lanes_in_use': in_use, 'next': nxt, 'next_started': started}
        handed = rec['notified_phase'] == rec['phase']
        if wall < rec['next_try'] or incident_settled(rec):
            continue
        alert = incident_alert(key, rec, wall)
        try:
            outcome = env.emit_incident(alert)
        except Exception:
            outcome = {}
        rec['attempts'] += 1
        # `deduped` counts only for a notice this record already handed off (a poll); a first-time `deduped` or `no-backend` is not a hand-off.
        if not handed and outcome.get('result') == 'deduped':
            rec['dedups'] += 1     # an earlier call (a crash before the state was saved, another monitor) queued it; stop asking after a few
            rec['handed_off_unverified'] = rec['dedups'] >= UNVERIFIED_AFTER_DEDUPS
        if not handed and outcome.get('result') == 'queued':
            rec.update(handed_off_at=wall, notified_phase=rec['phase'], attempts=0, next_try=0)
            handed = True
        if handed and outcome.get('result') in ('queued', 'deduped'):
            rec['delivery'] = outcome.get('delivery')
            if outcome.get('delivery') == 'sent':
                rec.update(delivered_at=wall, next_try=0)
            elif outcome.get('delivery') == 'pending' and rec['attempts'] >= 20:
                rec['delivery'] = 'unconfirmed'
        if not incident_settled(rec):
            rec['next_try'] = wall + min(60 * 2 ** (min(rec['attempts'], 10) - 1), 900)     # not handed off, or not yet delivered: retried or polled
        state['last_alert'] = {'at': iso(now), 'cause': alert['cause'], 'signature': alert['signature'], 'delivered': rec['delivered_at'] is not None,
                               'attempts': rec['attempts']}
    for rec in incidents.values():
        rec['owner_notice_state'] = owner_notice_state(rec)
    state['incidents'] = prune_incidents(incidents, wall)
    result['owner_notice_undelivered'] = [{'incident': k, 'id': r['id'], 'phase': r['phase'], 'since': iso(datetime.fromtimestamp(r['raised_at'] or wall, timezone.utc))}
                                          for k, r in state['incidents'].items() if r['owner_notice_state'] == 'undelivered']
    result['handoff_incidents'] = [{'status': r['status'], 'clock': r['clock'], 'blocker': r['blocker'], 'id': r['id'], 'phase': r['phase'], 'owner_notice_state': r['owner_notice_state'], 'handed_off_at': r['handed_off_at'], 'delivered_at': r['delivered_at'],
                                    'delivery': r['delivery'], 'acknowledged_at': r['acknowledged_at'], 'resolved_at': r['resolved_at'], 'ack_deadline': r['ack_deadline'], 'waiter': False,
                                    'action': 'none', 'verify': r['verify']} for r in state['incidents'].values()]


def incident_alert(key, rec, wall):
    """The enum-only notice request for one incident phase (the cause, recovery phrase and owner action all come from fixed tables)."""
    if rec['clock'] == 'wait':
        cause, recovery = 'queue-wait-excessive', [{'wait': 'expiry-near'}] if rec['phase'] == 'near' else []
    elif rec['clock'] == 'run':
        cause, recovery = 'workload-over-deadline', []
    else:
        cause, recovery = 'unclaimed-handoff', [{'handoff': 'lapsed'}] if rec['phase'] == 'lapsed' else []
    return {'cause': cause, 'causes': [cause], 'signature': incident_signature(key, rec['phase']), 'incident_ref': incident_ref(key), 'detail': {'phase': rec['phase']},
            'recovery': recovery, 'owner_action': OWNER_ACTION[cause], 'blocked_for_s': int(wall - rec['grant_wall'])}


def new_incident(entry_id, phase, started, expires, wall, deadline, clock, blocker):
    return {'status': None, 'resolved_at': None, 'discounted_s': None, 'clock': clock, 'blocker': blocker, 'holder_pid': None, 'holder_token': None, 'elapsed_s': None, 'deadline_s': None, 'deadline_source': None,
            'outbox_phase': None, 'id': entry_id, 'phase': phase, 'grant_wall': started, 'expires_wall': expires, 'raised_at': wall, 'ack_deadline': deadline,
            'handed_off_at': None, 'delivered_at': None, 'delivery': None, 'acknowledged_at': None, 'owner_notice_state': 'pending', 'claim_seen': False,
            'handed_off_unverified': False, 'dedups': 0, 'notified_phase': None, 'attempts': 0, 'next_try': 0, 'waiter': False, 'action': 'none', 'verify': None}


def wait_run_incidents(env, state, verdict, entries, claims, now, wall, gap, directory, result):
    """Per-entry QUEUED wait and execution incidents (ADR 0019 clocks 1 and 3). They take no action and never signal anything.

    wait: raised once entry age since `created` passes 12.5 h while the entry is still QUEUED/ELIGIBLE, upgraded once at 20 h (`near`: the 24 h window is
    ending); at most two notices. Key = entry id plus `wait`. A global cause already holding the queue is attributed in `blocker`, but the entry still gets its
    one incident. At most WAIT_INCIDENT_MAX are open; older waits get them first and the rest are counted in `wait_overflow`.
    run: a CLAIMED entry whose holder is alive, past twice its deadline (declared, else 6 h). CPU and log quietness are never inputs here. The incident keeps
    the holder pid, its start token as stored, the elapsed time and where the deadline came from.
    After an observation gap nothing new is raised (wall-clock age is not trusted), as for hand-off grants. The owner outbox gets one line per phase."""
    incidents, by_id = state['incidents'], {e['id']: e for e in entries}
    trusted = 'state-unreadable' not in verdict['causes']
    for rec in incidents.values():
        entry = by_id.get(rec['id'])
        if rec['clock'] == 'wait':
            over = entry is None or entry['state'] not in ('QUEUED', 'ELIGIBLE') or safe_time(entry['wait_expires']) <= now
        elif rec['clock'] == 'run':
            over = entry is None or entry['state'] != 'CLAIMED'
            if not over:
                rec['expires_wall'] = wall
        else:
            continue
        if over and trusted and rec['resolved_at'] is None:
            rec['resolved_at'] = wall      # the wait or the run ended (granted, expired, cancelled, released): resolved, never an owner acknowledgement
    overflow, gaps = 0, state['gaps']
    if not gap:
        blocker = blocker_for(verdict, entries, now)
        open_wait = sum(1 for r in incidents.values() if r['clock'] == 'wait' and r['resolved_at'] is None)
        hold = blocker == 'higher-entry-running' and higher_entries_within_deadline(entries, claims, now, gaps)      # the only blocker is healthy running work: no page
        for r in incidents.values():
            if r['clock'] == 'wait' and r['status'] == SUPPRESSED and r['resolved_at'] is None and not hold:
                r.update(status=None, blocker=blocker, attempts=0, next_try=0)      # the blocker changed (global cause, over its deadline, gone): page now
        for age, entry in waiting_entries(entries, now, gaps):
            phase = 'near' if age >= WAIT_NEAR_S else 'escalated' if age >= WAIT_ESCALATE_S else None
            if phase is None:
                break      # oldest first: nothing younger is due either
            key, rec = entry['id'] + ':wait', incidents.get(entry['id'] + ':wait')
            created = safe_time(entry['created']).timestamp()
            if rec is None:
                if open_wait >= WAIT_INCIDENT_MAX:
                    overflow += 1
                    continue
                open_wait += 1
                expires = safe_time(entry['wait_expires']).timestamp()
                incidents[key] = new_incident(entry['id'], phase, created, expires, wall, created + WAIT_NEAR_S if phase == 'escalated' else expires, 'wait', blocker)
                incidents[key].update(status=SUPPRESSED if hold and phase == 'escalated' else None, discounted_s=int(gap_discount_s(gaps, created)))
            elif phase == 'near' and rec['phase'] == 'escalated' and rec['resolved_at'] is None:
                rec.update(phase='near', attempts=0, next_try=0, handed_off_at=None, delivered_at=None, delivery=None, handed_off_unverified=False, dedups=0,
                           blocker=blocker, ack_deadline=safe_time(entry['wait_expires']).timestamp(), status=None)      # wait-expiry-near always pages
        open_run = sum(1 for r in incidents.values() if r['clock'] == 'run' and r['resolved_at'] is None)
        for entry in entries:
            if entry['state'] != 'CLAIMED' or claims.get(entry['id'], {}).get('status') != 'alive' or entry['id'] + ':run' in incidents:
                continue
            deadline, source = run_deadline(entry)
            elapsed = run_elapsed_s(entry, now, gaps)
            if elapsed is None or elapsed < RUN_ESCALATE_FACTOR * deadline or open_run >= RUN_INCIDENT_MAX:
                continue
            open_run += 1
            holder = entry['holder']
            rec = new_incident(entry['id'], 'escalated', safe_time(holder['claimed_at']).timestamp(), wall, wall, None, 'run', 'none')
            rec.update(holder_pid=holder['pid'] if type(holder.get('pid')) is int else None, holder_token=str(holder.get('start_token'))[:64],
                       elapsed_s=elapsed, deadline_s=deadline, deadline_source=source, discounted_s=int(gap_discount_s(gaps, rec['grant_wall'])))
            incidents[entry['id'] + ':run'] = rec
    result['wait_overflow'] = overflow
    held = [r for r in incidents.values() if r['status'] == SUPPRESSED and r['resolved_at'] is None]
    result['notices'] = list(result.get('notices') or []) + ['%s queued over %d h, waiting behind running work within its deadline' % (r['id'], WAIT_ESCALATE_S // 3600)
                                                              for r in held[:NOTICE_LIST_MAX]]
    pending = 0
    for key, rec in incidents.items():
        entry = by_id.get(rec['id'])
        if rec['status'] == SUPPRESSED or rec['acknowledged_at'] is not None or rec['resolved_at'] is not None or rec['outbox_phase'] == rec['phase'] or entry is None or directory is None:
            continue
        if rec['clock'] == 'grant':
            facts = ['grant-lapsed-unclaimed' if rec['phase'] == 'lapsed' else 'grant-unclaimed-escalate', 'no-waiter']
        elif rec['clock'] == 'wait':
            facts = ['queued-expiry-near' if rec['phase'] == 'near' else 'queued-over-escalate']
            facts.append('blocked-by-global-cause' if rec['blocker'] in GLOBAL_BLOCKERS else 'behind-running-entry' if rec['blocker'] == 'higher-entry-running' else '')
        else:
            facts = ['holder-alive', 'over-deadline', 'deadline-declared' if rec['deadline_source'] == 'declared' else 'deadline-default']
        try:
            if not outbox_has(directory, key, rec['phase']):
                outbox_append(directory, outbox_record(rec, key, env.owner(entry), now, facts))
            rec['outbox_phase'] = rec['phase']
        except (OSError, KeyError):
            pending += 1
    result['owner_outbox_pending'] = pending


def persist(directory, state, cpu_now, states_now, window, blocker, progress_at, wall, mono, boot):
    state.update({'last_tick': {'wall': wall, 'mono': mono, 'boot': boot}, 'cpu': cpu_now, 'states': states_now, 'window': window,
                  'blocker': blocker, 'progress_at': progress_at})
    write_json(directory / 'state.json', state)


# ---- callers / plist -----------------------------------------------------------------------------------------------------

def inventory(root):
    """Admission runners on disk: protocol and lane cap read as text from each checkout's disk-admission.py; nothing is executed."""
    found = []
    for path in sorted(Path(root).glob('*/scripts/disk-admission.py')):
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        proto = re.search(r'^RUNNER_PROTOCOL = (\d+)', text, re.M)
        lanes = re.search(r"^HEAVY_LANES = \{[^}]*'stable': (\d+)", text, re.M)
        # The effective stable cap, as heavy_lane_cap() computes it: the table is only the two-lane base, and a copy gains the third
        # lane there when it enables it and lists 'stable' in THIRD_LANE_TIERS (ADR 0017 rev 12). A copy without that constant never
        # gave the stable tier a third lane.
        third = re.search(r'^THIRD_LANE_ENABLED = True\b', text, re.M)
        tiers = re.search(r'^THIRD_LANE_TIERS = \(([^)]*)\)', text, re.M)
        stable_cap = (int(lanes.group(1)) + (1 if lanes and third and tiers and re.search(r"'stable'", tiers.group(1)) else 0)
                      if lanes else None)
        kind = ('current' if proto else 'memory-gated-advance-unstamped' if 'memory_fn' in text else 'outdated')
        # Bounded backfill (ADR 0017 rev 9): every poller copy must carry the same revision before it is enabled. `hardened` is the
        # SPE-176 follow-up (claim-time head re-check, BACKFILL_GRANT_S window); `absent` predates backfill entirely.
        enabled = re.search(r'^BACKFILL_ENABLED = (True|False)\b', text, re.M)
        backfill = ('absent' if not enabled else ('on' if enabled.group(1) == 'True' else 'off')
                    + ('-hardened' if re.search(r'^BACKFILL_GRANT_S = \d+', text, re.M) else ''))
        found.append({'checkout': path.parent.parent.name, 'protocol': int(proto.group(1)) if proto else 0,
                      'stable_lanes': stable_cap, 'kind': kind, 'backfill': backfill})
    return found


def plist(script, act=True):
    """The launchd agent as text. `script` must be the canonical checkout's copy: a path inside a feature worktree would vanish with it."""
    flags = '<string>tick</string>' + ('<string>--act</string>' if act else '') + '<string>--if-due</string>'
    return '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.coding-agent-environment.admission-monitor</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>%s</string>%s</array>
<key>StartInterval</key><integer>%d</integer>
<key>RunAtLoad</key><true/>
<key>ProcessType</key><string>Background</string>
<key>Nice</key><integer>10</integer>
<key>LowPriorityIO</key><true/>
<key>ThrottleInterval</key><integer>30</integer>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
<key>StandardOutPath</key><string>/dev/null</string>
<key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
''' % (script, flags, INTERVAL_S)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('tick')
    t.add_argument('--act', action='store_true', help='run the bounded recoveries: queue advance, abandoned-grant cancel, obsolete-wrapper SIGTERM (default: observe only)')
    t.add_argument('--if-due', action='store_true')
    t.add_argument('--json', action='store_true')
    s = sub.add_parser('status')
    s.add_argument('--json', action='store_true')
    sub.add_parser('callers')
    pl = sub.add_parser('plist')
    pl.add_argument('--checkout', required=True, help='the canonical checkout directory (its scripts/admission-monitor.py is the program)')
    pl.add_argument('--observe-only', action='store_true', help='omit --act: the agent only observes and reports')
    args = parser.parse_args(argv)
    da, ma = load('disk-admission'), load('memory-admission')
    state_path = da.default_state_path()
    if args.cmd == 'plist':
        script = Path(args.checkout).expanduser().resolve() / 'scripts/admission-monitor.py'
        if not script.is_file():
            print('no scripts/admission-monitor.py under %s' % args.checkout, file=sys.stderr)
            return 2
        print(plist(script, act=not args.observe_only), end='')
        return 0
    if args.cmd == 'callers':
        rows = inventory(Path.home() / 'Bryant-Developments')
        for row in rows:
            print('%-48s protocol=%d stable_lanes=%s %s backfill=%s' % (row['checkout'], row['protocol'], row['stable_lanes'], row['kind'],
                                                                      row['backfill']))
        return 0
    if args.cmd == 'status':
        data = read_json(monitor_dir(state_path) / 'status.json', {'ran': False, 'why': 'no tick yet'})
        print(json.dumps(data, indent=1, sort_keys=True) if args.json else '%s primary=%s waiting=%s stalled=%s owner-notice-undelivered=%d' % (
            data.get('at'), data.get('primary'), data.get('waiting'), data.get('stalled'), len(data.get('owner_notice_undelivered') or [])))
        return 0
    env = Env(da, state_path, ma.DEFAULT_STATE, default_limits(da, ma))
    result = tick(env, act=args.act, if_due=args.if_due)
    print(json.dumps(result, indent=1, sort_keys=True) if args.json else '%s primary=%s waiting=%s stalled=%s actions=%s' % (
        result.get('at', '-'), result.get('primary', result.get('why')), result.get('waiting'), result.get('stalled'), result.get('actions')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
