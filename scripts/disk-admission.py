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
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
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


def write_reservation(state_path, label, nbytes, pid, token, expires, queue_id=None):
    """Create or replace this job's reservation file; call while holding state_lock."""
    directory = reservations_dir(state_path)
    _refuse_symlinks(directory / label)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    record = {'version': STATE_VERSION, 'label': label, 'bytes': nbytes, 'pid': pid,
              'start_token': token, 'expires': iso_utc(expires)}
    if queue_id:
        record['queue_id'] = queue_id
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

    A reservation whose process is gone or whose start token no longer matches
    (pid reuse) is deleted. An expired reservation whose holder is still the
    same live process keeps its lane (marked past_ttl): the time limit is not
    evidence of release, and freeing the lane would double-book a running job.
    Only well-formed files of this schema are ever deleted; anything else is
    reported as a problem and left alone. A process that cannot be inspected
    stays counted (fail closed).
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
            if record['expires_at'] <= now:
                record['past_ttl'] = True
            live.append(record)
            continue
        if current is None or current != record['start_token']:
            path.unlink()
            continue
        if record['expires_at'] <= now:
            record['past_ttl'] = True
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
            queued, queue_problems = _queue_entries(state_path)
            if queue_problems:
                return dict(result, admit=False, reason=queue_problems[0])
            orphans = _claimed_orphans(queued, live, token_fn)
            if not same:
                ahead = [e for e in queued if e['state'] in WAITING_STATES and not _entry_lapsed(e, now)]
                if ahead:
                    return dict(result, admit=False, reason='queue-ahead: %d queued entries wait for the heavy lane (first %s, %s); '
                                'enqueue instead of reserving directly' % (len(ahead), ahead[0]['id'], ahead[0]['label']))
            lanes = HEAVY_LANES[result['tier']]
            reserved = sum(r['bytes'] for r in others) + sum(e['bytes'] for e in orphans)
            if len(others) + len(orphans) >= lanes:
                return dict(result, admit=False, reserved_bytes=reserved,
                            reason='lane-cap: %d of %d heavy lanes in use (%s)' % (len(others), lanes, ', '.join(
                                '%s pid %d%s' % (r['label'], r['pid'], ' past its time limit' if r.get('past_ttl') else '')
                                for r in others)))
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


# ---------------------------------------------------------------------------------------------------------------------
# Heavy-lane queue (SPE-111). Non-preemptive and first-in-first-out: it never touches a holder, it only decides who is
# granted the lane after the current holder is gone. A grant is a durable record plus a marker file; resuming the owning
# session is an explicit handoff (queue status prints the commands and never runs them). Nothing here polls: state
# advances when a queue-aware command runs. Entries record only a digest of the job argv, never the argv itself.
QUEUE_DIR = 'queue'
QUEUE_VERSION = 1
QUEUE_WAIT_S = 24 * 3600
QUEUE_GRANT_S = 3600
QUEUE_MAX_GRANT_S = 4 * 3600
WAITING_STATES = ('QUEUED', 'ELIGIBLE', 'GRANTED')
LIVE_STATES = WAITING_STATES + ('CLAIMED',)
TERMINAL_STATES = ('RELEASED', 'EXPIRED', 'CANCELLED')
HARNESSES = ('claude', 'codex')
TICKET_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}')
SESSION_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{7,63}')
QUEUE_ID_RE = re.compile(r'q[0-9]{6}')
RECEIPT_ROTATE_BYTES = 1024 * 1024


def queue_dir(state_path):
    return state_path.parent / QUEUE_DIR


def _queue_prepare(state_path):
    directory = queue_dir(state_path)
    _refuse_symlinks(directory / 'x')
    os.makedirs(directory / 'granted', mode=0o700, exist_ok=True)
    for sub in (directory, directory / 'granted'):
        os.chmod(sub, 0o700)
    return directory


def _queue_receipt(state_path, event, entry, now, **extra):
    """Append one private JSONL line per transition; rotated once at 1 MiB. Call while holding state_lock."""
    directory = queue_dir(state_path)
    path = directory / 'receipts.jsonl'
    try:
        if path.lstat().st_size > RECEIPT_ROTATE_BYTES and not path.is_symlink():
            os.replace(path, directory / 'receipts.jsonl.1')
    except OSError:
        pass
    line = dict({'ts': iso_utc(now), 'event': event, 'id': entry['id'], 'label': entry['label'],
                 'tickets': entry['tickets']}, **extra)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as handle:
            handle.write(json.dumps(line, sort_keys=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return False  # the state change already happened; the caller reports it with receipt_ok=False
    return True


def _read_queue_entry(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('not a regular file')
        entry = json.loads(handle.read(65536))
    valid = (isinstance(entry, dict) and entry.get('version') == QUEUE_VERSION and entry.get('id') == path.stem
             and QUEUE_ID_RE.fullmatch(path.stem) and entry.get('state') in LIVE_STATES + TERMINAL_STATES
             and type(entry.get('seq')) is int and type(entry.get('bytes')) is int and entry['bytes'] > 0
             and isinstance(entry.get('label'), str) and LABEL_RE.fullmatch(entry['label'])
             and isinstance(entry.get('tickets'), list) and entry.get('harness') in HARNESSES
             and isinstance(entry.get('fingerprint'), str) and len(entry['fingerprint']) == 64
             and isinstance(entry.get('wait_expires'), str)
             and (entry.get('grant') is None or (isinstance(entry['grant'], dict) and isinstance(entry['grant'].get('expires'), str)))
             and (entry['state'] != 'CLAIMED' or (isinstance(entry.get('holder'), dict) and type(entry['holder'].get('pid')) is int
                                                  and isinstance(entry['holder'].get('start_token'), str))))
    if not valid:
        raise ValueError('wrong schema')
    return entry


def _queue_entries(state_path):
    """Return (entries sorted by seq, problems). Unreadable files are reported and never deleted."""
    directory = queue_dir(state_path)
    entries, problems = [], []
    if not directory.exists():
        return entries, problems
    _refuse_symlinks(directory / 'x')
    for path in sorted(directory.glob('q*.json')):
        try:
            entries.append(_read_queue_entry(path))
        except (OSError, ValueError) as exc:
            problems.append('queue-entry-unreadable: %s: %s' % (path.name, exc))
    return sorted(entries, key=lambda e: e['seq']), problems


def _write_queue_entry(state_path, entry):
    write_json_atomic(queue_dir(state_path) / (entry['id'] + '.json'), entry)


def _parse_time(text):
    moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('time has no timezone')
    return moment


def _read_small_json(path, limit=4096):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('not a regular file')
        return json.loads(handle.read(limit))


def _queue_salt(state_path):
    """Per-store random salt so a digest of a low-entropy argv cannot be brute-forced from an entry file. Under the lock."""
    path = queue_dir(state_path) / 'fingerprint.salt'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(secrets.token_hex(32))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        pass
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('salt is not a regular file')
        salt = handle.read(256)
    if len(salt) < 32:
        raise ValueError('salt is too short')
    return salt


def _fingerprint(salt, repo, worktree, tickets, argv, nbytes):
    canonical = json.dumps({'repo': repo, 'worktree': worktree, 'tickets': sorted(tickets), 'argv': list(argv), 'bytes': nbytes},
                           sort_keys=True, separators=(',', ':'))
    return hmac.new(salt, canonical.encode(), hashlib.sha256).hexdigest()


def _entry_lapsed(entry, now):
    """True when a waiting entry's wait limit or a grant's window has passed (read-only; advance records the expiry)."""
    try:
        if entry['state'] == 'GRANTED':
            return _parse_time(entry['grant']['expires']) <= now
        if entry['state'] in ('QUEUED', 'ELIGIBLE'):
            return _parse_time(entry['wait_expires']) <= now
    except (KeyError, ValueError, TypeError):
        return False  # unparseable stays counted (fail closed)
    return False


def _claimed_orphans(entries, live, token_fn):
    """CLAIMED entries whose holder is still the same live process but whose reservation file is gone (explicit `release`
    while the job runs). They keep occupying a lane and their bytes: a deleted file is not evidence the job stopped."""
    held = {(r['label'], r['pid']) for r in live}
    orphans = []
    for entry in entries:
        if entry['state'] != 'CLAIMED' or (entry['label'], entry['holder']['pid']) in held:
            continue
        try:
            current = token_fn(entry['holder']['pid'])
        except ReservationError:
            orphans.append(entry)  # unverifiable stays counted
            continue
        if current is not None and current == entry['holder']['start_token']:
            orphans.append(entry)
    return orphans


def queue_enqueue(state_path, label, tickets, repo, worktree, harness, session_id, nbytes, argv, again=False, now=None,
                  wait_s=QUEUE_WAIT_S):
    """Add one durable entry, or return the live one with the same fingerprint. Never stores argv."""
    now = now or datetime.now(timezone.utc)
    bad = None
    if not (isinstance(label, str) and LABEL_RE.fullmatch(label)):
        bad = 'label'
    elif not (isinstance(tickets, list) and 1 <= len(tickets) <= 8
              and all(isinstance(t, str) and TICKET_RE.fullmatch(t) for t in tickets)):
        bad = 'tickets'
    elif harness not in HARNESSES:
        bad = 'harness'
    elif not (isinstance(session_id, str) and SESSION_RE.fullmatch(session_id)):
        bad = 'session-id'
    elif not (type(nbytes) is int and nbytes > 0):
        bad = 'bytes'
    elif not (isinstance(argv, list) and 1 <= len(argv) <= 64 and all(isinstance(a, str) and '\0' not in a for a in argv) and argv[0]):
        bad = 'argv'
    elif not (isinstance(repo, str) and 0 < len(repo) <= 200):
        bad = 'repo'
    elif not (type(wait_s) is int and 0 < wait_s <= QUEUE_WAIT_S):
        bad = 'wait'
    elif not (isinstance(worktree, str) and os.path.isabs(worktree) and os.path.isdir(worktree)):
        bad = 'worktree'
    if bad:
        return {'ok': False, 'reason': 'invalid-request: %s is missing or out of range' % bad}
    real_worktree = os.path.realpath(worktree)
    try:
        with state_lock(state_path):
            _queue_prepare(state_path)
            entries, problems = _queue_entries(state_path)
            if problems:
                return {'ok': False, 'reason': problems[0]}
            try:
                salt = _queue_salt(state_path)
            except ValueError as exc:
                return {'ok': False, 'reason': 'queue-salt-unreadable: %s; remove queue/fingerprint.salt only when no entry is live '
                                               '(a new salt stops dedupe of existing entries)' % exc}
            fingerprint = _fingerprint(salt, repo, real_worktree, tickets, argv, nbytes)
            for entry in entries:
                if entry['fingerprint'] != fingerprint:
                    continue
                if entry['state'] in LIVE_STATES and not _entry_lapsed(entry, now):
                    if entry.get('session_id') != session_id:
                        return {'ok': False, 'reason': 'this job is already queued as %s by another session (%s)' % (
                            entry['id'], entry['label'])}
                    return {'ok': True, 'id': entry['id'], 'reason': 'already queued as %s' % entry['id'], 'existing': True}
                if entry['state'] in LIVE_STATES:
                    continue  # lapsed: advance will record the expiry; a fresh entry may be queued
                if entry['state'] == 'RELEASED' and not again:
                    return {'ok': False, 'reason': '%s already ran this job (RELEASED); pass --again to queue it again' % entry['id']}
                if entry['state'] in ('CANCELLED', 'EXPIRED') and not again:
                    return {'ok': False, 'reason': '%s was %s; pass --again to queue it again' % (entry['id'], entry['state'])}
            seq_path = queue_dir(state_path) / 'seq.json'
            floor = 1 + max([e['seq'] for e in entries] or [0])
            try:
                seq = _read_small_json(seq_path)['next']
                if type(seq) is not int or seq < 1:
                    raise ValueError('bad counter')
            except FileNotFoundError:
                seq = floor
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return {'ok': False, 'reason': 'queue-entry-unreadable: seq.json: %s' % exc}
            seq = max(seq, floor)  # a rewound counter must never reuse an id
            if seq > 999999:
                return {'ok': False, 'reason': 'queue-full: sequence exhausted'}
            entry = {'version': QUEUE_VERSION, 'id': 'q%06d' % seq, 'seq': seq, 'label': label, 'state': 'QUEUED',
                     'tickets': list(tickets), 'repo': repo, 'worktree': real_worktree, 'harness': harness,
                     'session_id': session_id, 'owner_uid': os.getuid(), 'bytes': nbytes, 'fingerprint': fingerprint,
                     'created': iso_utc(now), 'wait_expires': iso_utc(now + timedelta(seconds=wait_s)), 'grant': None}
            if (queue_dir(state_path) / (entry['id'] + '.json')).exists():
                return {'ok': False, 'reason': 'queue-entry-unreadable: %s already exists' % entry['id']}
            write_json_atomic(seq_path, {'next': seq + 1})  # counter first: a crash leaves a gap, never a reused id
            _write_queue_entry(state_path, entry)
            receipt_ok = _queue_receipt(state_path, 'enqueued', entry, now)
            return {'ok': True, 'id': entry['id'], 'receipt_ok': receipt_ok, 'reason': 'queued as %s (position %d)' % (
                entry['id'], 1 + sum(1 for e in entries if e['state'] in WAITING_STATES and not _entry_lapsed(e, now)))}
    except StateError as exc:
        return {'ok': False, 'reason': '%s: %s' % (exc.kind, exc)}
    except OSError as exc:
        return {'ok': False, 'reason': 'state-unwritable: %s' % exc}


def _drop_marker(state_path, qid):
    try:
        (queue_dir(state_path) / 'granted' / qid).unlink()
    except FileNotFoundError:
        pass


def queue_advance(state_path, free_fn=measure_free_bytes, now=None, token_fn=process_token, grant_s=QUEUE_GRANT_S):
    """One advance step under the lock: expire lapsed grants and waits, then grant the first in line if a lane is free.

    A lane is occupied by a live reservation (including one past its time limit or unverifiable) and by an unclaimed,
    unlapsed grant. Nothing here ever signals or releases a holder.
    """
    now = now or datetime.now(timezone.utc)
    out = {'granted': [], 'expired': [], 'eligible': [], 'released': [], 'reason': 'ok'}
    if not (type(grant_s) is int and 0 < grant_s <= QUEUE_MAX_GRANT_S):
        out['reason'] = 'invalid-request: grant window out of range'
        return out
    try:
        with state_lock(state_path):
            if not queue_dir(state_path).exists():
                return out
            _queue_prepare(state_path)
            entries, problems = _queue_entries(state_path)
            if problems:
                out['reason'] = problems[0]
                return out
            result = _evaluate_locked(None, state_path, free_fn, now)
            if result['tier'] is None:
                out['reason'] = result['reason']
                return out
            live, res_problems = live_reservations(state_path, now, token_fn)
            if res_problems:
                out['reason'] = res_problems[0]
                return out
            for entry in entries:
                if entry['state'] != 'CLAIMED':
                    continue
                holder = entry['holder']
                try:
                    current = token_fn(holder['pid'])
                except ReservationError:
                    continue  # unverifiable: the lane stays occupied (fail closed)
                if current is None:
                    evidence, alive = 'holder-exited', False
                elif current != holder['start_token']:
                    evidence, alive = 'pid-reused', False
                else:
                    continue  # still the same live process: the lane stays occupied, with or without its reservation file
                entry['state'] = 'RELEASED'
                entry['release'] = {'at': iso_utc(now), 'evidence': evidence, 'holder_alive': alive}
                _write_queue_entry(state_path, entry)
                _queue_receipt(state_path, 'released', entry, now, evidence=evidence, holder_alive=alive)
                out['released'].append(entry['id'])
            for entry in entries:
                try:
                    lapsed = (entry['state'] == 'GRANTED' and _parse_time(entry['grant']['expires']) <= now) or (
                        entry['state'] in ('QUEUED', 'ELIGIBLE') and _parse_time(entry['wait_expires']) <= now)
                except ValueError as exc:
                    out['reason'] = 'queue-entry-unreadable: %s: %s' % (entry['id'], exc)
                    return out
                if lapsed:
                    was = entry['state']
                    entry['state'] = 'EXPIRED'
                    _write_queue_entry(state_path, entry)
                    _drop_marker(state_path, entry['id'])
                    _queue_receipt(state_path, 'expired', entry, now, was=was)
                    out['expired'].append(entry['id'])
            waiting = [e for e in entries if e['state'] in ('QUEUED', 'ELIGIBLE')]
            granted = [e for e in entries if e['state'] == 'GRANTED']
            if not waiting:
                return out
            lanes = 0 if result['latch'] else HEAVY_LANES[result['tier']]
            first = waiting[0]
            if lanes == 0:
                if first['state'] != 'ELIGIBLE':
                    first['state'] = 'ELIGIBLE'
                    _write_queue_entry(state_path, first)
                    _queue_receipt(state_path, 'eligible', first, now, tier=result['tier'])
                out['eligible'].append(first['id'])
                out['reason'] = 'no heavy lane at tier %s' % result['tier']
                return out
            orphans = _claimed_orphans(entries, live, token_fn)
            if len(live) + len(granted) + len(orphans) >= lanes:
                out['reason'] = 'lane occupied'
                return out
            reserved = sum(r['bytes'] for r in live) + sum(e['bytes'] for e in orphans)
            if result['free_bytes'] - reserved - first['bytes'] < HEAVY_FLOOR:
                out['reason'] = 'over-capacity: %.1f GiB free, %.1f GiB reserved, %.1f GiB requested for %s, floor 100 GiB' % (
                    result['free_bytes'] / GIB, reserved / GIB, first['bytes'] / GIB, first['id'])
                return out
            window = min(grant_s, QUEUE_MAX_GRANT_S)
            first['state'] = 'GRANTED'
            first['grant'] = {'at': iso_utc(now), 'expires': iso_utc(now + timedelta(seconds=window))}
            _write_queue_entry(state_path, first)
            marker = queue_dir(state_path) / 'granted' / first['id']
            write_json_atomic(marker, {'id': first['id'], 'label': first['label'], 'tickets': first['tickets'],
                                       'expires': first['grant']['expires']})
            _queue_receipt(state_path, 'granted', first, now, expires=first['grant']['expires'], tier=result['tier'])
            out['granted'].append(first['id'])
            return out
    except StateError as exc:
        out['reason'] = '%s: %s' % (exc.kind, exc)
        return out
    except OSError as exc:
        out['reason'] = 'state-unwritable: %s' % exc
        return out


def _memory_precondition():
    """Fresh memory, CPU and heavy-process check via the existing SPE-117 gate (memory only: disk is judged under our lock)."""
    script = Path(__file__).resolve().with_name('memory-admission.py')
    try:
        done = subprocess.run([sys.executable, str(script), 'check', '--weight', 'heavy', '--memory-only'], capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, 'memory-gate-unavailable: %s' % exc
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    return done.returncode == 0, (lines[0] if lines else 'memory gate exit %d' % done.returncode)


def queue_claim(state_path, qid, label, nbytes, argv, cwd, free_fn=measure_free_bytes, pid=None, now=None, token_fn=process_token,
                owner_fn=process_owner, ttl=DEFAULT_TTL, memory_fn=_memory_precondition, exec_fn=None, allow_long=False):
    """Claim a granted lane for this very process, then exec the job; every precondition is revalidated first.

    Outside the lock: the entry must be GRANTED and unlapsed, owned by this uid, and match label, bytes, worktree and job
    fingerprint; then fresh memory/CPU/heavy-process admission. Under the lock: the entry again, then tier, latch, lane
    count (the grant itself does not count), disk floor and our own process; the reservation and the holder are written in
    one critical section. A refusal keeps the grant. Returns a dict with ok, reason and exit (77 refused, 127 exec failed).
    """
    fresh_clock = now is None
    now = now or datetime.now(timezone.utc)
    pid = pid or os.getpid()
    exec_fn = exec_fn or _exec_job

    def refuse(reason):
        return {'ok': False, 'reason': reason, 'exit': REFUSED_EXIT}

    if not (isinstance(qid, str) and QUEUE_ID_RE.fullmatch(qid) and isinstance(label, str) and LABEL_RE.fullmatch(label)
            and type(nbytes) is int and nbytes > 0 and type(ttl) is int and 0 < ttl <= (MAX_TTL if allow_long else DEFAULT_TTL)
            and isinstance(argv, list) and argv and all(isinstance(a, str) and '\0' not in a for a in argv) and argv[0]):
        return refuse('invalid-request: queue id, label, bytes, ttl or command is out of range')

    def check_entry(entry):
        if entry is None:
            return 'no queue entry %s' % qid
        if entry['state'] != 'GRANTED':
            return '%s is not granted (state %s)' % (qid, entry['state'])
        try:
            if _parse_time(entry['grant']['expires']) <= now:
                return 'grant for %s lapsed; run queue advance and wait for the next grant' % qid
        except (KeyError, ValueError, TypeError):
            return 'queue-entry-unreadable: %s grant time' % qid
        if entry['owner_uid'] != os.getuid():
            return '%s belongs to another user' % qid
        if entry['label'] != label or entry['bytes'] != nbytes:
            return '%s was granted for label %s and %d bytes' % (qid, entry['label'], entry['bytes'])
        if os.path.realpath(cwd) != entry['worktree']:
            return '%s was granted for worktree %s' % (qid, entry['worktree'])
        try:
            salt = _queue_salt(state_path)
        except (OSError, ValueError) as exc:
            return 'queue-salt-unreadable: %s' % exc
        if _fingerprint(salt, entry['repo'], entry['worktree'], entry['tickets'], argv, nbytes) != entry['fingerprint']:
            return '%s was granted for a different job (fingerprint mismatch)' % qid
        return None

    def find(entries):
        return next((e for e in entries if e['id'] == qid), None)

    try:
        with state_lock(state_path):
            entries, problems = _queue_entries(state_path)
            if problems:
                return refuse(problems[0])
            why = check_entry(find(entries))
        if why:
            return refuse(why)
        admitted, memory_reason = memory_fn()
        if not admitted:
            return refuse('memory-gate: %s' % memory_reason)
        if fresh_clock:
            now = datetime.now(timezone.utc)  # the memory gate takes 10 s or more; judge lapse and expiry at the real time
        with state_lock(state_path):
            entries, problems = _queue_entries(state_path)
            if problems:
                return refuse(problems[0])
            entry = find(entries)
            why = check_entry(entry)
            if why:
                return refuse(why)
            result = _evaluate_locked('heavy', state_path, free_fn, now)
            if not result['admit']:
                return refuse(result['reason'])
            live, res_problems = live_reservations(state_path, now, token_fn)
            if res_problems:
                return refuse(res_problems[0])
            token = token_fn(pid)
            if token is None:
                return refuse('process-not-running: pid %d' % pid)
            try:
                owner = owner_fn(pid)
            except ReservationError as exc:
                return refuse('process-unverifiable: %s' % exc)
            if owner != os.getuid():
                return refuse('process-not-owned: pid %d is not run by this user' % pid)
            if any(r['label'] == label for r in live):
                return refuse('label-in-use: %s already holds a reservation' % label)
            orphans = _claimed_orphans(entries, live, token_fn)
            lanes = HEAVY_LANES[result['tier']]
            if len(live) + len(orphans) >= lanes:
                return refuse('lane-cap: %d of %d heavy lanes in use (%s)' % (len(live) + len(orphans), lanes, ', '.join(
                    ['%s pid %d%s' % (r['label'], r['pid'], ' past its time limit' if r.get('past_ttl') else '') for r in live] +
                    ['%s pid %d (reservation removed, job still running)' % (e['label'], e['holder']['pid']) for e in orphans])))
            reserved = sum(r['bytes'] for r in live) + sum(e['bytes'] for e in orphans)
            if result['free_bytes'] - reserved - nbytes < HEAVY_FLOOR:
                return refuse('over-capacity: %.1f GiB free, %.1f GiB reserved, %.1f GiB requested, floor 100 GiB' % (
                    result['free_bytes'] / GIB, reserved / GIB, nbytes / GIB))
            write_reservation(state_path, label, nbytes, pid, token, now + timedelta(seconds=ttl), queue_id=qid)
            try:
                entry['state'] = 'CLAIMED'
                entry['holder'] = {'pid': pid, 'start_token': token, 'claimed_at': iso_utc(now)}
                _write_queue_entry(state_path, entry)
            except OSError:
                try:
                    (reservations_dir(state_path) / (label + '.json')).unlink()  # no half claim: the job is not exec'd
                except OSError:
                    pass
                raise
            try:
                _drop_marker(state_path, qid)
            except OSError:
                pass  # a stale marker is harmless: the entry is CLAIMED and the grant cannot be claimed again
            _queue_receipt(state_path, 'claimed', entry, now, pid=pid)
    except StateError as exc:
        return refuse('%s: %s' % (exc.kind, exc))
    except OSError as exc:
        return refuse('state-unwritable: %s' % exc)
    try:
        exec_fn(argv[0], argv)
    except OSError as exc:
        release(label, state_path)
        _queue_mark_released(state_path, qid, now, 'exec-failed', False)
        return {'ok': False, 'reason': 'exec failed: %s' % exc, 'exit': 127}
    return {'ok': True, 'reason': 'claimed %s for %s' % (qid, label), 'exit': 0}


def _queue_mark_released(state_path, qid, now, evidence, alive):
    try:
        with state_lock(state_path):
            entries, _ = _queue_entries(state_path)
            for entry in entries:
                if entry['id'] == qid and entry['state'] == 'CLAIMED':
                    entry['state'] = 'RELEASED'
                    entry['release'] = {'at': iso_utc(now), 'evidence': evidence, 'holder_alive': alive}
                    _write_queue_entry(state_path, entry)
                    _queue_receipt(state_path, 'released', entry, now, evidence=evidence, holder_alive=alive)
    except (StateError, OSError):
        pass


def _handoff(entry):
    """Commands for the owner to run. They are printed, never executed; resuming a session is explicit."""
    if entry['harness'] == 'claude':
        resume = 'cd %s && claude --resume %s' % (shlex.quote(entry['worktree']), entry['session_id'])
    else:
        resume = ('cd %s && codex exec resume %s  # codex exec resume is unverified in this environment' %
                  (shlex.quote(entry['worktree']), entry['session_id']))
    claim = ('cd %s && disk-admission.py run --queue-id %s --label %s --bytes %d -- <the exact job>' % (
        shlex.quote(entry['worktree']), entry['id'], entry['label'], entry['bytes']))
    return {'resume': resume, 'claim': claim}


def queue_status(state_path, now=None):
    """Read-only: entries (no argv, digest only) with handoff text for granted ones. Writes and deletes nothing."""
    now = now or datetime.now(timezone.utc)
    try:
        entries, problems = _queue_entries(state_path)
    except (StateError, OSError) as exc:
        return {'entries': [], 'problems': ['queue-unreadable: %s' % exc]}
    shown = []
    for entry in entries:
        item = {k: v for k, v in entry.items()}
        if entry['state'] == 'GRANTED' and not _entry_lapsed(entry, now):
            item['handoff'] = _handoff(entry)
        shown.append(item)
    return {'entries': shown, 'problems': problems}


def queue_cancel(state_path, qid, now=None, session_id=None):
    """Cancel a waiting or unclaimed-granted entry. Never signals a process."""
    now = now or datetime.now(timezone.utc)
    if not (isinstance(qid, str) and QUEUE_ID_RE.fullmatch(qid)):
        return {'ok': False, 'reason': 'invalid-request: bad queue id'}
    try:
        with state_lock(state_path):
            entries, problems = _queue_entries(state_path)
            if problems:
                return {'ok': False, 'reason': problems[0]}
            for entry in entries:
                if entry['id'] != qid:
                    continue
                if entry['state'] not in LIVE_STATES:
                    return {'ok': False, 'reason': '%s is already %s' % (qid, entry['state'])}
                if session_id is not None and entry.get('session_id') != session_id:
                    return {'ok': False, 'reason': '%s belongs to another session' % qid}
                was = entry['state']
                entry['state'] = 'CANCELLED'
                _write_queue_entry(state_path, entry)
                _drop_marker(state_path, qid)
                receipt_ok = _queue_receipt(state_path, 'cancelled', entry, now, was=was)
                return {'ok': True, 'receipt_ok': receipt_ok, 'reason': 'cancelled %s' % qid}
            return {'ok': False, 'reason': 'no queue entry %s' % qid}
    except StateError as exc:
        return {'ok': False, 'reason': '%s: %s' % (exc.kind, exc)}
    except OSError as exc:
        return {'ok': False, 'reason': 'state-unwritable: %s' % exc}



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
            blockers += ['reservation-past-ttl-holder-alive: %s (pid %d) is past its time limit but still running; '
                         'its lane stays occupied until it exits or is released' % (r['label'], r['pid'])
                         for r in live if r.get('past_ttl')]
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
REFRESH_LABEL = 'com.coding-agent-environment.admission-snapshot'
REFRESH_INTERVAL_S = 60  # well inside STALE_AFTER_S, so a missed run does not make consumers refuse at once


def build_refresher_plist(python, script):
    """launchd user agent that republishes the snapshot. No KeepAlive; output goes nowhere because the published
    record is the product (a stale snapshot is what `snapshot --check-fresh` reports), so no log can grow."""
    return plistlib.dumps({
        'Label': REFRESH_LABEL,
        'ProgramArguments': [python, script, 'snapshot', '--json'],
        'StartInterval': REFRESH_INTERVAL_S,
        'RunAtLoad': True,
        'EnvironmentVariables': {'HOME': str(account_home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': '/dev/null',
    }).decode()


def _exec_job(program, args):
    """exec the job with default signal dispositions: Python ignores SIGPIPE and SIGXFSZ and exec inherits that,
    which would make `sh -c 'yes | head -1'` behave differently under the wrapper than without it."""
    for name in ('SIGPIPE', 'SIGXFSZ'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    os.execvp(program, args)


def run_reserved(label, nbytes, argv, state_path, free_fn=measure_free_bytes, ttl=DEFAULT_TTL, allow_long=False,
                 exec_fn=_exec_job):
    """Reserve for this very process, then exec the job: nothing runs before admission.

    execv keeps the pid and the start time, so the reservation is held by the job itself and ends with it
    (lazy reaping handles exit and pid reuse; a live holder keeps its lane past its TTL). A refusal never execs and returns 77; a failed exec
    releases the reservation and returns 127. Every message goes to stderr; the exit code is the contract,
    and it is authoritative only until the job starts (after exec the exit code is the job's own).
    """
    # Only the program must be non-empty: `git commit -m ''` and `sh -c ''` are legitimate jobs.
    if not argv or not all(isinstance(part, str) and '\0' not in part for part in argv) or not argv[0]:
        print('refused: invalid-request: a command is required after --', file=sys.stderr)
        return REFUSED_EXIT
    result = reserve(label, nbytes, os.getpid(), state_path, free_fn, ttl=ttl, allow_long=allow_long)
    if not result['admit']:
        print(result['reason'], file=sys.stderr)
        return REFUSED_EXIT
    if sys.stderr.isatty():
        print(result['reason'], file=sys.stderr)  # never injected into a captured job stderr
    try:
        exec_fn(argv[0], argv)
    except OSError as exc:
        release(label, state_path)
        print('exec failed: %s' % exc, file=sys.stderr)
        return 127
    return 0  # only reachable with an injected exec_fn


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
    commands.add_parser('refresher-plist', allow_abbrev=False, help='print the snapshot-refresher launchd agent; installs nothing')
    go = commands.add_parser('run', allow_abbrev=False, help='reserve bytes for this process, then exec the job')
    go.add_argument('--bytes', type=int, required=True, dest='nbytes')
    go.add_argument('--label', required=True)
    go.add_argument('--ttl-s', type=int, default=DEFAULT_TTL, dest='ttl', help='seconds, at most 4 h unless --max-hold')
    go.add_argument('--max-hold', action='store_true', dest='allow_long')
    go.add_argument('--queue-id', dest='queue_id', help='claim a granted heavy-lane queue entry (revalidated first)')
    go.add_argument('job', nargs=argparse.REMAINDER, help='-- COMMAND [ARG...]')
    drop = commands.add_parser('release', allow_abbrev=False, help='release a reservation by label')
    drop.add_argument('--label', required=True)
    queue = commands.add_parser('queue', allow_abbrev=False, help='heavy-lane queue: enqueue, advance, status, cancel')
    qcmds = queue.add_subparsers(dest='queue_command', required=True)
    enq = qcmds.add_parser('enqueue', allow_abbrev=False, help='queue a heavy job; argv is hashed, never stored')
    enq.add_argument('--label', required=True)
    enq.add_argument('--ticket', action='append', required=True, dest='tickets')
    enq.add_argument('--repo', required=True)
    enq.add_argument('--worktree', required=True)
    enq.add_argument('--harness', choices=HARNESSES, required=True)
    enq.add_argument('--session-id', required=True, dest='session_id')
    enq.add_argument('--bytes', type=int, required=True, dest='nbytes')
    enq.add_argument('--wait-s', type=int, default=QUEUE_WAIT_S, dest='wait_s')
    enq.add_argument('--again', action='store_true')
    enq.add_argument('job', nargs=argparse.REMAINDER, help='-- COMMAND [ARG...]')
    adv = qcmds.add_parser('advance', allow_abbrev=False, help='expire lapsed entries and grant the first in line if a lane is free')
    adv.add_argument('--grant-s', type=int, default=QUEUE_GRANT_S, dest='grant_s')
    qst = qcmds.add_parser('status', allow_abbrev=False, help='read-only listing with the handoff commands')
    qst.add_argument('--json', action='store_true', required=True)
    qcx = qcmds.add_parser('cancel', allow_abbrev=False, help='cancel a waiting or unclaimed entry')
    qcx.add_argument('--id', required=True, dest='qid')
    qcx.add_argument('--session-id', dest='session_id', help='refuse unless the entry belongs to this session')
    return parser


def _queue_cli(args, state_path, free_fn):
    command = args.queue_command
    if command == 'enqueue':
        if args.job[:1] != ['--']:
            print('refused: invalid-request: put -- between the options and the command', file=sys.stderr)
            return REFUSED_EXIT
        out = queue_enqueue(state_path, args.label, args.tickets, args.repo, args.worktree, args.harness, args.session_id,
                            args.nbytes, args.job[1:], again=args.again, wait_s=args.wait_s)
    elif command == 'advance':
        out = queue_advance(state_path, free_fn, grant_s=args.grant_s)
        out['ok'] = out['reason'] in ('ok',) or bool(out['granted'] or out['expired'] or out['eligible']) or out['reason'] in (
            'lane occupied',) or out['reason'].startswith('no heavy lane')
    elif command == 'cancel':
        out = queue_cancel(state_path, args.qid, session_id=args.session_id)
    else:
        out = dict(queue_status(state_path), ok=True)
    print(json.dumps(out, sort_keys=True))
    return 0 if out.get('ok') else REFUSED_EXIT


def main(argv=None, free_fn=measure_free_bytes, state_path=None):
    state_path = state_path or default_state_path()
    args = build_parser().parse_args(argv)
    if args.command == 'refresher-plist':
        script = Path(__file__).resolve()
        if str(script).startswith(('/tmp/', '/private/tmp/', '/var/folders/', '/private/var/folders/')):
            print('warning: %s is scratch space; run refresher-plist from the stable merged checkout' % script,
                  file=sys.stderr)
        print(build_refresher_plist(sys.executable, str(script)), end='')
        return 0
    if args.command == 'run':
        if args.job[:1] != ['--']:
            print('refused: invalid-request: put -- between the options and the command', file=sys.stderr)
            return REFUSED_EXIT
        job = args.job[1:]
        if args.queue_id:
            out = queue_claim(state_path, args.queue_id, args.label, args.nbytes, job, os.getcwd(), free_fn, ttl=args.ttl,
                              allow_long=args.allow_long)
            if not out['ok']:
                print(out['reason'], file=sys.stderr)
            return out['exit'] if not out['ok'] else 0
        return run_reserved(args.label, args.nbytes, job, state_path, free_fn, args.ttl, args.allow_long)
    if args.command == 'queue':
        return _queue_cli(args, state_path, free_fn)
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
