#!/usr/bin/env python3
"""Host disk-pressure admission adapter (SPE-109 stage S1).

Answers one question for a caller: may this unit of work start given the free
space on the host data volume right now. This is a thin admission adapter over
Glomeris, the single storage authority. It is NOT a cleanup engine, registry
or daemon: it deletes nothing, tracks no resources or leases, and runs only
when called. Nothing calls it yet, so it protects nothing host-wide until a
later stage wires it into an entry point.

Free space comes from the pinned Glomeris core `status --json` (binary
resolved through the installed storage-core receipt, verified by
storage-integration.py). A statvfs reading of /System/Volumes/Data (else `/`)
is a cross-check and fallback: the smaller reading wins, either source alone
suffices, and only when both fail does the check refuse. Glomeris pressure
levels (WARN/PRESSURED/CRITICAL/EMERGENCY, by percent or bytes) are advisory
input only and are ignored here; the admission tier is computed from free bytes.

Tiers by free space (GiB = 1024**3): emergency < 20, low 20..<100,
stable 100..<200, wide >= 200. Heavy work needs stable or wide. Light work
is admitted in low, stable and wide and refused in emergency.

A latch is set when free space drops below 20 GiB and is released only once
fresh free space reaches 50 GiB. While latched every weight is refused, light
included. "Held until 50 GiB" means the 20..<50 GiB range stays refused during
recovery; that is the architect's reading, PENDING FOUNDER CONFIRMATION.
The latch comes from the fresh measurement, not only a stored flag: a missing
state file below 50 GiB is latched, and a record that contradicts itself (for
example latch=false with free_bytes under 20 GiB) is treated as corrupt. A
same-user forger who writes a self-consistent record cannot be detected; the
state file is advisory, not a security boundary.
Cleanup work is meant to stay admissible while latched but has no weight
here yet; that is a later stage.

Fail closed: an unreadable volume, a symlinked or non-regular state path, an
unwritable state file, a lock held for over 5 seconds, and a corrupt,
unparsable or wrong-schema state file (counted as latched) all refuse, each
with its own reason.

The only host write is the state file, default
~/.cache/coding-agent-environment/admission/state.json (home from the account
database, not $HOME), plus its .lock. Free space and the state path cannot be
overridden from the command line; tests inject them through arguments.

Exit codes for `check`: 0 admit, 77 refuse, 2 usage error. Callers MUST treat
every non-zero exit, not only 77, as REFUSE; unexpected errors exit 77.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import time

GIB = 1024 ** 3
EMERGENCY_BELOW = 20 * GIB
LOW_BELOW = 100 * GIB
STABLE_BELOW = 200 * GIB
RELEASE_AT = 50 * GIB


def tier(free_bytes):
    if free_bytes < EMERGENCY_BELOW:
        return 'emergency'
    if free_bytes < LOW_BELOW:
        return 'low'
    if free_bytes < STABLE_BELOW:
        return 'stable'
    return 'wide'


WEIGHTS = ('heavy', 'light')
ADMITTED_TIERS = {'heavy': ('stable', 'wide'), 'light': ('low', 'stable', 'wide')}


def next_latch(free_bytes, state):
    """Set below 20 GiB; once set, released only at 50 GiB or more."""
    if free_bytes < EMERGENCY_BELOW:
        return True
    if state.get('latch') and free_bytes < RELEASE_AT:
        return True
    return False


def decide(weight, free_bytes, state):
    """Return (admit, new_state, reason) for one unit of work."""
    if weight not in WEIGHTS:
        raise ValueError('weight must be heavy or light')
    current = tier(free_bytes)
    new_state = dict(state, latch=next_latch(free_bytes, state))
    if new_state['latch']:
        return False, new_state, '%s refused: latched until free space reaches 50 GiB (tier %s)' % (weight, current)
    if current in ADMITTED_TIERS[weight]:
        return True, new_state, '%s admitted in tier %s' % (weight, current)
    return False, new_state, '%s refused in tier %s' % (weight, current)


STATE_VERSION = 1
LOCK_TIMEOUT = 5.0
STATE_RELATIVE = '.cache/coding-agent-environment/admission/state.json'


def account_home():
    # The account database, not $HOME: the environment is caller-controlled.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def default_state_path():
    return account_home() / STATE_RELATIVE


class StateError(Exception):
    """The state file or its location cannot be trusted; kind names the refusal."""

    def __init__(self, message, kind='state-untrusted'):
        super().__init__(message)
        self.kind = kind


def _refuse_symlinks(path):
    # lstat semantics: a redirected file or directory could point the write elsewhere.
    for candidate in (path, path.parent):
        if candidate.is_symlink():
            raise StateError('state path is a symlink: %s' % candidate)
    # Any redirected ancestor between the account home and the file counts too.
    root = account_home()
    if path.parent.is_relative_to(root):
        expected = os.path.join(os.path.realpath(root), path.parent.relative_to(root))
        if os.path.realpath(path.parent) != expected:
            raise StateError('state directory has a symlinked ancestor: %s' % path.parent)


@contextmanager
def state_lock(path):
    """Serialize every read-modify-write of the state file across processes."""
    _refuse_symlinks(path)
    # exist_ok: two first-time callers may race to create the directory.
    os.makedirs(path.parent, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        # Bounded: a stuck holder must produce a refusal, never a hung caller.
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError('lock held for more than %gs' % LOCK_TIMEOUT, 'state-lock-timeout')
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def write_json_atomic(path, record):
    """Atomically replace a private JSON file; call while holding state_lock."""
    _refuse_symlinks(path)
    # Random per call: a leftover from a crashed writer must never block later writes.
    temporary = path.with_name('.%s.%s.tmp' % (path.name, secrets.token_hex(8)))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Persist the rename itself, not only the file contents.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def iso_utc(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def write_state(path, free_bytes, latch, now=None):
    """Atomically replace the state file; call while holding state_lock."""
    record = {'version': STATE_VERSION, 'timestamp': iso_utc(now or datetime.now(timezone.utc)),
              'free_bytes': free_bytes, 'tier': tier(free_bytes), 'latch': bool(latch)}
    write_json_atomic(path, record)
    return record


def read_state(path):
    """Return the stored state, {} when none exists; StateError when untrustworthy."""
    _refuse_symlinks(path)
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StateError('state unreadable: %s' % exc, 'state-corrupt')
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise StateError('state is not a regular file', 'state-not-regular')
        try:
            record = json.loads(handle.read(65536))
        except (OSError, ValueError) as exc:
            raise StateError('state unreadable: %s' % exc, 'state-corrupt')
    valid = (isinstance(record, dict) and record.get('version') == STATE_VERSION
             and type(record.get('latch')) is bool and type(record.get('free_bytes')) is int
             and record.get('tier') in ('emergency', 'low', 'stable', 'wide')
             and isinstance(record.get('timestamp'), str))
    if not valid:
        raise StateError('state has the wrong schema', 'state-corrupt')
    # A record cannot honestly show an unlatched host below the latch line.
    if record['tier'] != tier(record['free_bytes']) or (not record['latch'] and record['free_bytes'] < EMERGENCY_BELOW):
        raise StateError('state contradicts itself', 'state-corrupt')
    return record


DATA_VOLUME = '/System/Volumes/Data'


class VolumeError(Exception):
    """Free space could not be measured, so nothing can be admitted."""


def read_free_bytes():
    # On Darwin `/` is the sealed system volume and sits near 5% forever; the
    # data volume is the one that fills up.
    mount = DATA_VOLUME if os.path.exists(DATA_VOLUME) else '/'
    try:
        stats = os.statvfs(mount)
    except OSError as exc:
        raise VolumeError('cannot read free space on %s: %s' % (mount, exc))
    return stats.f_bavail * stats.f_frsize


GLOMERIS_TIMEOUT = 10


def read_glomeris_free():
    """Free bytes from the pinned Glomeris core `status --json`.

    The binary comes from the installed storage-core receipt, only when
    storage-integration.py verifies it as installed. Glomeris pressure
    levels in the output are advisory and deliberately ignored.
    """
    try:
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location('storage_integration', Path(__file__).with_name('storage-integration.py'))
        storage = module_from_spec(spec)
        spec.loader.exec_module(storage)
        receipt = account_home() / '.local/share/coding-agent-environment/storage/storage-core.json'
        if storage.receipt_state(receipt) != 'installed':
            raise VolumeError('storage core is not installed')
        binary = storage.read_selection(receipt)[0]['path']
        done = subprocess.run([binary, 'status', '--json'], capture_output=True, text=True,
                              timeout=GLOMERIS_TIMEOUT, stdin=subprocess.DEVNULL, check=True)
        free = json.loads(done.stdout)['free_bytes']
    except VolumeError:
        raise
    except Exception as exc:
        raise VolumeError('glomeris status unavailable: %s' % exc)
    if type(free) is not int or free < 0:
        raise VolumeError('glomeris reported an invalid free_bytes')
    return free


def measure_with_source(glomeris=read_glomeris_free, statvfs=read_free_bytes):
    """(free bytes, source name) of the smaller readable source; VolumeError if none reads."""
    values, errors = [], []
    for name, source in (('glomeris', glomeris), ('statvfs', statvfs)):
        try:
            values.append((source(), name))
        except VolumeError as exc:
            errors.append(str(exc))
    if not values:
        raise VolumeError('; '.join(errors))
    return min(values)


def measure_free_bytes(glomeris=read_glomeris_free, statvfs=read_free_bytes):
    """Smaller of the readable sources; refuses only when none is readable."""
    return measure_with_source(glomeris, statvfs)[0]


RESERVATIONS_DIR = 'reservations'
LABEL_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,63}')
DEFAULT_TTL = 4 * 3600
MAX_TTL = 24 * 3600
HEAVY_FLOOR = LOW_BELOW
# One heavy lane in stable and wide until a second-lane analysis is approved.
HEAVY_LANES = {'emergency': 0, 'low': 0, 'stable': 1, 'wide': 1}


class ReservationError(Exception):
    """A reservation or its owning process cannot be verified."""


def process_token(pid):
    """Start-time token of a live process, None when it is gone.

    pid plus start time identifies one process; a reused pid has a new token.
    """
    try:
        done = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True,
                              timeout=5, stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'})
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReservationError('cannot inspect process %d: %s' % (pid, exc))
    token = done.stdout.strip()
    if done.returncode == 0 and token:
        return token
    if done.returncode == 1 and not token:
        return None
    raise ReservationError('cannot inspect process %d: ps exit %d' % (pid, done.returncode))


def process_owner(pid):
    """Owning uid of a live process, None when it is gone."""
    try:
        done = subprocess.run(['/bin/ps', '-o', 'uid=', '-p', str(pid)], capture_output=True, text=True,
                              timeout=5, stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'})
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReservationError('cannot inspect process %d: %s' % (pid, exc))
    value = done.stdout.strip()
    if done.returncode == 0 and value.isdigit():
        return int(value)
    if done.returncode == 1 and not value:
        return None
    raise ReservationError('cannot inspect process %d: ps exit %d' % (pid, done.returncode))


def reservations_dir(state_path):
    return state_path.parent / RESERVATIONS_DIR


def write_reservation(state_path, label, nbytes, pid, token, expires):
    """Create or replace this job's reservation file; call while holding state_lock."""
    directory = reservations_dir(state_path)
    _refuse_symlinks(directory / label)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    record = {'version': STATE_VERSION, 'label': label, 'bytes': nbytes, 'pid': pid,
              'start_token': token, 'expires': iso_utc(expires)}
    write_json_atomic(directory / (label + '.json'), record)
    return record


def _read_reservation(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('not a regular file')
        record = json.loads(handle.read(65536))
    valid = (isinstance(record, dict) and record.get('version') == STATE_VERSION
             and record.get('label') == path.stem and LABEL_RE.fullmatch(path.stem)
             and type(record.get('bytes')) is int and record['bytes'] > 0
             and type(record.get('pid')) is int and record['pid'] > 0
             and isinstance(record.get('start_token'), str) and isinstance(record.get('expires'), str))
    if not valid:
        raise ValueError('wrong schema')
    expires_at = datetime.fromisoformat(record['expires'].replace('Z', '+00:00'))
    if expires_at.tzinfo is None:
        # A naive time cannot be compared with now; treat it as a damaged record.
        raise ValueError('expires has no timezone')
    record['expires_at'] = expires_at
    return record


def live_reservations(state_path, now=None, token_fn=process_token):
    """Return (live records, problems); call while holding state_lock.

    A reservation whose process is gone, whose start token no longer matches
    (pid reuse) or whose expiry passed is deleted. Only well-formed files of
    this schema are ever deleted; anything else is reported as a problem and
    left alone. A process that cannot be inspected stays counted (fail closed).
    """
    now = now or datetime.now(timezone.utc)
    directory = reservations_dir(state_path)
    live, problems = [], []
    if not directory.exists():
        return live, problems
    _refuse_symlinks(directory / 'x')
    for path in sorted(directory.glob('*.json')):
        try:
            record = _read_reservation(path)
        except (OSError, ValueError) as exc:
            problems.append('reservation-unreadable: %s: %s' % (path.name, exc))
            continue
        try:
            current = token_fn(record['pid'])
        except ReservationError as exc:
            problems.append('reservation-unverifiable: %s: %s' % (path.name, exc))
            live.append(record)
            continue
        if current is None or current != record['start_token'] or record['expires_at'] <= now:
            path.unlink()
            continue
        live.append(record)
    return live, problems


REFUSED = {'admit': False, 'tier': None, 'free_bytes': None, 'latch': None, 'timestamp': None}


def _evaluate_locked(weight, state_path, free_fn, now):
    """The measure-decide-persist step; the caller holds state_lock."""
    try:
        free = free_fn()
    except VolumeError as exc:
        return dict(REFUSED, reason='volume-unreadable: %s' % exc)
    try:
        state, corrupt = read_state(state_path), None
    except StateError as exc:
        if exc.kind != 'state-corrupt':
            raise
        # Unparsable state counts as latched until it is rewritten and free space recovers.
        state, corrupt = {'latch': True}, str(exc)
    # Lost history cannot prove the host never fell below 20 GiB, so below
    # 50 GiB a missing record is latched; only fresh free space releases it.
    missing = not state and free < RELEASE_AT and corrupt is None
    if missing:
        state = {'latch': True}
    admit, new_state, reason = decide(weight or 'light', free, state)
    if missing:
        reason = 'state-missing: %s' % reason
    if corrupt:
        admit, new_state = False, dict(new_state, latch=True)
        reason = 'state-corrupt: %s; treated as latched' % corrupt
    record = write_state(state_path, free, new_state['latch'], now)
    return dict(admit=admit, reason=reason, tier=record['tier'], free_bytes=free,
                latch=record['latch'], timestamp=record['timestamp'])


def evaluate(weight, state_path, free_fn=measure_free_bytes, now=None):
    """Measure, decide and persist under one lock; weight None only refreshes state.

    Returns a result dict. admit is False whenever anything is untrustworthy.
    """
    try:
        with state_lock(state_path):
            return _evaluate_locked(weight, state_path, free_fn, now)
    except StateError as exc:
        return dict(REFUSED, reason='%s: %s' % (exc.kind, exc))
    except OSError as exc:
        return dict(REFUSED, reason='state-unwritable: %s' % exc)


def _refusal(reason, **extra):
    return dict(REFUSED, reason=reason, **extra)


def reserve(label, nbytes, pid, state_path, free_fn=measure_free_bytes, now=None,
            token_fn=process_token, ttl=DEFAULT_TTL, owner_fn=process_owner, allow_long=False):
    """Admit one heavy job and record its byte reservation, or refuse.

    Admission needs: heavy admitted by tier and latch, a free heavy lane, and
    free bytes minus live reservations minus this request still at or above
    the 100 GiB floor. Everything runs under the state lock, so concurrent
    callers cannot jointly overcommit.
    """
    if not (isinstance(label, str) and LABEL_RE.fullmatch(label) and type(nbytes) is int and nbytes > 0
            and type(pid) is int and pid > 0 and type(ttl) is int and 0 < ttl <= (MAX_TTL if allow_long else DEFAULT_TTL)):
        return _refusal('invalid-request: label, bytes, pid or ttl is out of range')
    now = now or datetime.now(timezone.utc)
    try:
        with state_lock(state_path):
            result = _evaluate_locked('heavy', state_path, free_fn, now)
            if not result['admit']:
                return result
            live, problems = live_reservations(state_path, now, token_fn)
            if problems:
                return dict(result, admit=False, reason=problems[0])
            token = token_fn(pid)
            if token is None:
                return dict(result, admit=False, reason='process-not-running: pid %d' % pid)
            try:
                owner = owner_fn(pid)
            except ReservationError as exc:
                return dict(result, admit=False, reason='process-unverifiable: %s' % exc)
            if owner != os.getuid():
                return dict(result, admit=False, reason='process-not-owned: pid %d is not run by this user' % pid)
            same = [r for r in live if r['label'] == label]
            if same and (same[0]['pid'], same[0]['start_token']) != (pid, token):
                return dict(result, admit=False, reason='label-in-use: %s belongs to another live process' % label)
            others = [r for r in live if r['label'] != label]
            lanes = HEAVY_LANES[result['tier']]
            reserved = sum(r['bytes'] for r in others)
            if len(others) >= lanes:
                return dict(result, admit=False, reserved_bytes=reserved,
                            reason='lane-cap: %d of %d heavy lanes in use' % (len(others), lanes))
            if result['free_bytes'] - reserved - nbytes < HEAVY_FLOOR:
                return dict(result, admit=False, reserved_bytes=reserved,
                            reason='over-capacity: %.1f GiB free, %.1f GiB reserved, %.1f GiB requested, floor 100 GiB' % (
                                result['free_bytes'] / GIB, reserved / GIB, nbytes / GIB))
            write_reservation(state_path, label, nbytes, pid, token, now + timedelta(seconds=ttl))
            return dict(result, reserved_bytes=reserved + nbytes,
                        reason='reserved %.1f GiB for %s (lane %d of %d)' % (nbytes / GIB, label, len(others) + 1, lanes))
    except StateError as exc:
        return _refusal('%s: %s' % (exc.kind, exc))
    except OSError as exc:
        return _refusal('state-unwritable: %s' % exc)


def release(label, state_path):
    """Delete this tool's reservation file for label; a missing one is success."""
    if not (isinstance(label, str) and LABEL_RE.fullmatch(label)):
        return _refusal('invalid-request: bad label')
    try:
        with state_lock(state_path):
            path = reservations_dir(state_path) / (label + '.json')
            _refuse_symlinks(path)
            if not path.exists():
                return dict(REFUSED, admit=True, reason='no reservation named %s' % label)
            _read_reservation(path)
            path.unlink()
            return dict(REFUSED, admit=True, reason='released %s' % label)
    except StateError as exc:
        return _refusal('%s: %s' % (exc.kind, exc))
    except (OSError, ValueError) as exc:
        return _refusal('reservation-unreadable: %s' % exc)


SNAPSHOT_SCHEMA = 1
SNAPSHOT_NAME = 'snapshot.json'
STALE_AFTER_S = 120
# Flip only once an installed, operator-enabled gate has been proven end to end.
GATE_PROVEN_WIRED = False


def snapshot_age_s(record, now=None):
    """Seconds since the snapshot's ts; consumers must treat > 120 as stale."""
    stamp = datetime.fromisoformat(record['ts'].replace('Z', '+00:00'))
    return ((now or datetime.now(timezone.utc)) - stamp).total_seconds()


def snapshot_is_fresh(record, now=None, max_age=STALE_AFTER_S):
    """False for stale, future-dated (beyond 5s of skew) or malformed snapshots."""
    try:
        return -5 <= snapshot_age_s(record, now) <= max_age
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def read_snapshot(state_path):
    """The published snapshot; StateError when missing, unreadable or not this schema. Read-only."""
    path = state_path.with_name(SNAPSHOT_NAME)
    _refuse_symlinks(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise StateError('snapshot unreadable: %s' % exc, 'snapshot-unreadable')
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise StateError('snapshot is not a regular file', 'snapshot-unreadable')
        try:
            record = json.loads(handle.read(65536))
        except (OSError, ValueError) as exc:
            raise StateError('snapshot unreadable: %s' % exc, 'snapshot-unreadable')
    if not isinstance(record, dict) or record.get('schema') != SNAPSHOT_SCHEMA or not isinstance(record.get('ts'), str):
        raise StateError('snapshot has the wrong schema', 'snapshot-unreadable')
    return record


def snapshot(state_path, source_fn=measure_with_source, now=None, token_fn=process_token):
    """Refresh state and publish the host resource snapshot next to it."""
    now = now or datetime.now(timezone.utc)
    seen = {}

    def free():
        value, seen['source'] = source_fn()
        return value

    try:
        with state_lock(state_path):
            result = _evaluate_locked(None, state_path, free, now)
            live, problems = live_reservations(state_path, now, token_fn)
            blockers = list(problems)
            if result['tier'] is None:
                blockers.insert(0, result['reason'])
            elif result['latch']:
                blockers.insert(0, 'latched until free space reaches 50 GiB')
            elif HEAVY_LANES[result['tier']] == 0:
                blockers.insert(0, 'tier %s is below stable' % result['tier'])
            lanes = 0 if result['tier'] is None or result['latch'] else HEAVY_LANES[result['tier']]
            record = {'schema': SNAPSHOT_SCHEMA, 'ts': iso_utc(now),
                      'free_bytes': result['free_bytes'], 'tier': result['tier'], 'latch': result['latch'],
                      'heavy_lanes_allowed': lanes, 'active_heavy_jobs': len(live),
                      'reserved_bytes': sum(r['bytes'] for r in live),
                      'protection': 'wired' if GATE_PROVEN_WIRED else 'advisory-not-wired',
                      'blockers': blockers, 'source': seen.get('source')}
            write_json_atomic(state_path.with_name(SNAPSHOT_NAME), record)
            return record
    except StateError as exc:
        return {'error': '%s: %s' % (exc.kind, exc)}
    except OSError as exc:
        return {'error': 'state-unwritable: %s' % exc}


REFUSED_EXIT = 77


def build_parser():
    # No free-space or state-path flags: the measurement and its record must not be forgeable from argv.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', allow_abbrev=False, help='exit 0 to admit, 77 to refuse')
    check.add_argument('--weight', choices=WEIGHTS, required=True)
    status = commands.add_parser('status', allow_abbrev=False, help='report the current tier and latch')
    status.add_argument('--json', action='store_true', required=True)
    take = commands.add_parser('reserve', allow_abbrev=False, help='reserve bytes for one heavy job')
    take.add_argument('--bytes', type=int, required=True, dest='nbytes')
    take.add_argument('--pid', type=int, required=True)
    take.add_argument('--label', required=True)
    take.add_argument('--ttl-s', type=int, default=DEFAULT_TTL, dest='ttl', help='seconds, at most 4 h unless --max-hold')
    take.add_argument('--max-hold', action='store_true', dest='allow_long', help='allow a hold of up to 24 h')
    shot = commands.add_parser('snapshot', allow_abbrev=False, help='publish the host resource snapshot')
    mode = shot.add_mutually_exclusive_group(required=True)
    mode.add_argument('--json', action='store_true', help='write and print a fresh snapshot')
    mode.add_argument('--check-fresh', action='store_true', help='read-only: exit 0 when the snapshot is fresh, else 77')
    shot.add_argument('--max-age', type=int, default=STALE_AFTER_S, help='seconds, 1..120; with --check-fresh only')
    drop = commands.add_parser('release', allow_abbrev=False, help='release a reservation by label')
    drop.add_argument('--label', required=True)
    return parser


def main(argv=None, free_fn=measure_free_bytes, state_path=None):
    state_path = state_path or default_state_path()
    args = build_parser().parse_args(argv)
    if args.command in ('reserve', 'release'):
        result = (reserve(args.label, args.nbytes, args.pid, state_path, free_fn, ttl=args.ttl, allow_long=args.allow_long)
                  if args.command == 'reserve' else release(args.label, state_path))
        print(result['reason'])
        return 0 if result['admit'] else REFUSED_EXIT
    if args.command == 'snapshot' and args.check_fresh:
        if not 0 < args.max_age <= STALE_AFTER_S:
            # A caller may tighten the staleness rule but never relax it.
            print('refused: --max-age must be between 1 and %d seconds' % STALE_AFTER_S)
            return REFUSED_EXIT
        try:
            fresh = snapshot_is_fresh(read_snapshot(state_path), max_age=args.max_age)
            print('snapshot fresh' if fresh else 'snapshot stale: older than %d s or dated in the future' % args.max_age)
        except StateError as exc:
            fresh = False
            print('snapshot unusable: %s' % exc)
        return 0 if fresh else REFUSED_EXIT
    if args.command == 'snapshot':
        record = snapshot(state_path, (lambda: (free_fn(), 'injected')) if free_fn is not measure_free_bytes else measure_with_source)
        print(json.dumps(record, sort_keys=True))
        return REFUSED_EXIT if 'error' in record else 0
    result = evaluate(args.weight if args.command == 'check' else None, state_path, free_fn)
    if args.command == 'status':
        keys = ('tier', 'free_bytes', 'latch', 'timestamp')
        print(json.dumps(dict({key: result[key] for key in keys}, **({} if result['tier'] else {'error': result['reason']})), sort_keys=True))
        return 0 if result['tier'] else REFUSED_EXIT
    free = 'unknown' if result['free_bytes'] is None else '%.1f GiB' % (result['free_bytes'] / GIB)
    exact = 'unknown' if result['free_bytes'] is None else result['free_bytes']
    print('%s; tier=%s free=%s bytes=%s' % (result['reason'], result['tier'] or 'unknown', free, exact))
    return 0 if result['admit'] else REFUSED_EXIT


def run(argv=None, **options):
    """main() with every unexpected failure turned into a refusal; options inject state_path or free_fn."""
    try:
        return main(argv, **options)
    except Exception as exc:
        print('refused: internal error: %s' % exc)
        return REFUSED_EXIT


if __name__ == '__main__':
    raise SystemExit(run())
