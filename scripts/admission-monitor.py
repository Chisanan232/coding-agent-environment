#!/usr/bin/env python3
"""Deterministic stall monitor for the shared heavy-job queue (SPE-176, ADR 0018).

One `tick` reads the queue, the disk and memory gates' state and the process table, decides whether queued work is globally
stalled, names the cause, and (only with --act) runs the one recovery verb it owns: `disk-admission.py queue advance` from this
checkout. It needs no conversation and makes no model call.

It never signals a process, resets the memory latch, changes a threshold, writes disk/memory admission state itself, deletes
anything, or presents an entry's session id. GRANTED is never execution: only a claim, a release or CPU growth of a claimed
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
import os
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
INTERVAL_S = 60
STALL_S = 600                 # waiting work with no claim, release or CPU growth for this long is a global stall
GRANTED_ALERT_S = 2700        # above the observed p99 grant->claim (29 min), below the 60 min grant window
CPU_STALL_S = 900             # a claimed, alive workload with no CPU growth this long is reported, never touched
CONFIRM_SPACING_S = 30        # confirmations are counted at least this far apart
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
CAUSES = ('state-unreadable', 'disk-floor', 'memory-pressure', 'memory-hysteresis-static-compressor', 'memory-hysteresis',
          'stale-claim', 'frozen-queue', 'failed-continuation', 'outdated-caller', 'stalled-running', 'unobservable', 'healthy-busy', 'unknown')
OWNER_ACTION = {'disk-floor': 'reclaim-disk', 'memory-pressure': 'reduce-load', 'memory-hysteresis-static-compressor': 'approve-memory-policy',
                'memory-hysteresis': 'wait-or-approve-memory-policy', 'stale-claim': 'confirm-claim-ended',
                'failed-continuation': 'restart-waiting-session', 'outdated-caller': 'restart-outdated-caller',
                'state-unreadable': 'inspect-admission-state', 'unknown': 'inspect-admission-state',
                'frozen-queue': 'run-queue-advance', 'stalled-running': 'inspect-running-workload', 'unobservable': 'inspect-memory-admission'}


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


def safe_time(text):
    try:
        moment = parse_iso(text)
    except (ValueError, TypeError, AttributeError):
        return None
    return moment if moment.tzinfo is not None else None


def usable_entries(entries):
    """(entries whose times parse, problems). A damaged entry is reported and left alone, never allowed to crash a tick."""
    good, problems = [], []
    for e in entries:
        ok = safe_time(e.get('wait_expires')) is not None
        if e.get('state') == 'GRANTED':
            grant = e.get('grant')
            ok = ok and isinstance(grant, dict) and safe_time(grant.get('at')) is not None and safe_time(grant.get('expires')) is not None
        if e.get('state') == 'GRANTED' and ok and e['grant'].get('runner') is not None and not isinstance(e['grant']['runner'], dict):
            ok = False
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
        return {'causes': found, 'primary': next(iter(found), 'idle'), 'waiting': 0, 'oldest_grant_s': 0, 'notes': []}
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
    oldest = 0
    for entry in granted:
        age = int((now - safe_time(entry['grant']['at'])).total_seconds())
        oldest = max(oldest, age)
        if age >= GRANTED_ALERT_S:
            found['failed-continuation'] = '%s GRANTED %d min without a claim' % (entry['id'], age // 60)
    for entry in granted:
        runner = entry['grant'].get('runner')
        if not runner or runner.get('protocol', 0) < snap['protocol']:
            found['outdated-caller'] = '%s was granted by a runner without protocol %d' % (entry['id'], snap['protocol'])
    # Nothing blocks the head, nothing is claimed, nothing granted is waiting to be claimed: nobody is advancing the queue.
    if waiting and waiting[0]['state'] != 'ELIGIBLE' and not found and not granted and not any(e['state'] == 'CLAIMED' for e in entries):
        found['frozen-queue'] = 'waiting work, free lanes and no blocker, but nothing is being granted'
    primary = next((c for c in CAUSES if c in found), 'unknown')
    if not found:
        busy = any(e['state'] == 'CLAIMED' for e in entries) or any(e['state'] == 'ELIGIBLE' for e in waiting)   # ELIGIBLE: no lane free yet
        primary = 'healthy-busy' if busy else 'unknown'
    # History for the owner, never a trigger: a grant that lapsed unclaimed shortly ago (a lost callback).
    notes = ['%s grant lapsed unclaimed' % e['id'] for e in entries if e['state'] == 'EXPIRED' and isinstance(e.get('grant'), dict)
             and not e.get('holder') and 0 <= (now - safe_time(e['grant']['expires'])).total_seconds() < 3 * 3600]
    return {'causes': found, 'primary': primary, 'waiting': len(waiting), 'oldest_grant_s': oldest, 'notes': notes}


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


def sanitize_state(state):
    """Field-by-field type check of a loaded state; a wrong type resets that field only (the counters that bound recovery are kept)."""
    def nums(value, limit):
        return [float(x) for x in value if isinstance(x, (int, float)) and not isinstance(x, bool)][-limit:] if isinstance(value, list) else []
    last = state.get('last_tick')
    if not (isinstance(last, dict) and all(isinstance(last.get(k), (int, float)) for k in ('wall', 'mono'))):
        last = {}
    blocker = state.get('blocker')
    if not (isinstance(blocker, dict) and isinstance(blocker.get('signature'), str) and type(blocker.get('confirmations')) is int):
        blocker = None
    alerts = state.get('alerts')
    alerts = {k: v for k, v in alerts.items() if isinstance(v, dict) and isinstance(v.get('sent_at'), (int, float))} if isinstance(alerts, dict) else {}
    cpu = state.get('cpu')
    cpu = {k: v for k, v in cpu.items() if isinstance(v, dict) and isinstance(v.get('cpu'), (int, float)) and isinstance(v.get('at'), (int, float))} if isinstance(cpu, dict) else {}
    states = state.get('states')
    states = {k: v for k, v in states.items() if isinstance(v, str)} if isinstance(states, dict) else {}
    window = [[float(w), float(r)] for w, r in (x for x in (state.get('window') or []) if isinstance(x, list) and len(x) == 2
                                                 and all(isinstance(y, (int, float)) for y in x))][-WINDOW_MAX:] if isinstance(state.get('window'), list) else []
    progress = state.get('progress_at')
    return dict(schema=1, last_tick=last, blocker=blocker, alerts=alerts, cpu=cpu, states=states, window=window,
                advances=nums(state.get('advances'), 64), progress_at=progress if isinstance(progress, (int, float)) else None,
                last_alert=state.get('last_alert') if isinstance(state.get('last_alert'), dict) else None)


# ---- tick --------------------------------------------------------------------------------------------------------------

class Env:
    """Everything a tick touches outside its own state, so tests substitute it."""

    def __init__(self, da, state_path, memory_state_path, limits, clock=None, mono=None, boot=None, table=None, memory=None,
                 free=None, advance=None, notify=None, clear=None):
        self.da, self.state_path, self.memory_state_path, self.limits = da, Path(state_path), Path(memory_state_path), limits
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.mono = mono or time.monotonic
        self.boot = boot or boot_time
        self.table = table or ps_table
        self.memory = memory or (lambda: memory_view(self.memory_state_path))
        self.free = free or (lambda: shutil.disk_usage('/System/Volumes/Data').free)
        self.advance = advance or self._advance
        self.notify = notify or session_notify
        self.clear = clear or session_clear

    def _advance(self):
        """Run one `queue advance`. A slow one is left to finish on its own (never signalled); the next tick's spacing covers it."""
        proc = subprocess.Popen([sys.executable, str(HERE / 'disk-admission.py'), 'queue', 'advance', '--json'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            return {'exit': None, 'out': 'still running after 180 s; left to finish'}
        return {'exit': proc.returncode, 'out': (out or '')[-400:]}


def recovery_phrase(actions, exhausted):
    if exhausted:
        return 'recovery-exhausted'
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
            if after in ('CLAIMED', 'RELEASED') and after != before:
                progress = True
    try:
        mem = env.memory()
    except (OSError, subprocess.SubprocessError, ValueError):
        mem = {'latched': False, 'metrics': {}}
    ratio = (mem.get('metrics') or {}).get('compressor_ratio')
    window = state['window']
    if ratio is not None:
        window = (window + [[wall, ratio]])[-WINDOW_MAX:]
    snap = {'now': now, 'wall': wall, 'entries': entries, 'problems': problems, 'free_bytes': env.free(), 'mem': mem,
            'window': window, 'claims': claims, 'protocol': env.da.RUNNER_PROTOCOL}
    verdict = classify(snap, env.limits)
    waiting = verdict['waiting']
    old_grant = verdict['oldest_grant_s'] >= GRANTED_ALERT_S
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
    stalled = (waiting > 0 or old_grant) and stalled_for >= (STALL_S if waiting else GRANTED_ALERT_S) and not quiet
    result = {'ran': True, 'at': iso(now), 'observation_gap': gap, 'primary': verdict['primary'], 'causes': verdict['causes'],
              'waiting': waiting, 'stalled': stalled, 'stalled_for_s': stalled_for, 'signature': sig, 'confirmed': confirmed,
              'owner_action': OWNER_ACTION.get(verdict['primary']), 'granted_is_execution': False, 'actions': [],
              'notes': verdict['notes']}
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
    escalate = stalled and confirmed and (verdict['primary'] not in RECOVERABLE or result.get('recovery_exhausted')
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
    if not verdict['causes'] and state.get('last_alert') and not state['last_alert'].get('cleared'):
        try:
            cleared = bool(env.clear())
        except Exception:
            cleared = False
        if cleared:
            state['last_alert']['cleared'] = True
            state['alerts'] = {}      # the next incident may announce again even with the same cause and signature
            result['cleared'] = True
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
        kind = ('current' if proto else 'memory-gated-advance-unstamped' if 'memory_fn' in text else 'outdated')
        found.append({'checkout': path.parent.parent.name, 'protocol': int(proto.group(1)) if proto else 0,
                      'stable_lanes': int(lanes.group(1)) if lanes else None, 'kind': kind})
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
    t.add_argument('--act', action='store_true', help='run the bounded advance recovery (default: observe only)')
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
            print('%-48s protocol=%d stable_lanes=%s %s' % (row['checkout'], row['protocol'], row['stable_lanes'], row['kind']))
        return 0
    if args.cmd == 'status':
        data = read_json(monitor_dir(state_path) / 'status.json', {'ran': False, 'why': 'no tick yet'})
        print(json.dumps(data, indent=1, sort_keys=True) if args.json else '%s primary=%s waiting=%s stalled=%s' % (
            data.get('at'), data.get('primary'), data.get('waiting'), data.get('stalled')))
        return 0
    env = Env(da, state_path, ma.DEFAULT_STATE, default_limits(da, ma))
    result = tick(env, act=args.act, if_due=args.if_due)
    print(json.dumps(result, indent=1, sort_keys=True) if args.json else '%s primary=%s waiting=%s stalled=%s actions=%s' % (
        result.get('at', '-'), result.get('primary', result.get('why')), result.get('waiting'), result.get('stalled'), result.get('actions')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
