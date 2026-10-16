#!/usr/bin/env python3
"""Host disk-pressure admission core (SPE-109 stage S1).

Answers one question for a caller: may this unit of work start given the free
space on the host data volume right now. Nothing calls it yet, so it protects
nothing host-wide until a later stage wires it into an entry point.

Tiers by free space (GiB = 1024**3): emergency < 20, low 20..<100,
stable 100..<200, wide >= 200. Heavy work needs stable or wide. Light work
is admitted in low, stable and wide and refused in emergency.

A latch is set when free space drops below 20 GiB and is released only once
free space reaches 50 GiB. While latched every weight is refused, light
included. "Held until 50 GiB" means the 20..<50 GiB range stays refused during
recovery; that is the architect's reading, PENDING FOUNDER CONFIRMATION.
Cleanup work is meant to stay admissible while latched but has no weight
here yet; that is a later stage.

Fail closed: an unreadable volume refuses, and a corrupt, unparsable or
wrong-schema state file counts as latched and refuses with its own reason.

The only host write is the state file, default
~/.cache/coding-agent-environment/admission/state.json. Free space and the state path cannot be
overridden from the command line; tests inject them through arguments.

Exit codes for `check`: 0 admit, 77 refuse. Usage errors exit 2.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
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


def write_state(path, free_bytes, latch, now=None):
    """Atomically replace the state file; call while holding state_lock."""
    _refuse_symlinks(path)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record = {'version': STATE_VERSION, 'timestamp': stamp.isoformat(timespec='seconds').replace('+00:00', 'Z'),
              'free_bytes': free_bytes, 'tier': tier(free_bytes), 'latch': bool(latch)}
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


def measure_free_bytes(glomeris=read_glomeris_free, statvfs=read_free_bytes):
    """Smaller of the readable sources; refuses only when none is readable."""
    values, errors = [], []
    for source in (glomeris, statvfs):
        try:
            values.append(source())
        except VolumeError as exc:
            errors.append(str(exc))
    if not values:
        raise VolumeError('; '.join(errors))
    return min(values)


def evaluate(weight, state_path, free_fn=measure_free_bytes, now=None):
    """Measure, decide and persist under one lock; weight None only refreshes state.

    Returns a result dict. admit is False whenever anything is untrustworthy.
    """
    refused = {'admit': False, 'tier': None, 'free_bytes': None, 'latch': None, 'timestamp': None}
    try:
        with state_lock(state_path):
            try:
                free = free_fn()
            except VolumeError as exc:
                return dict(refused, reason='volume-unreadable: %s' % exc)
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
    except StateError as exc:
        return dict(refused, reason='%s: %s' % (exc.kind, exc))
    except OSError as exc:
        return dict(refused, reason='state-unwritable: %s' % exc)
    return dict(admit=admit, reason=reason, tier=record['tier'], free_bytes=free,
                latch=record['latch'], timestamp=record['timestamp'])


REFUSED_EXIT = 77


def build_parser():
    # No free-space or state-path flags: the measurement and its record must not be forgeable from argv.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', allow_abbrev=False, help='exit 0 to admit, 77 to refuse')
    check.add_argument('--weight', choices=WEIGHTS, required=True)
    status = commands.add_parser('status', allow_abbrev=False, help='report the current tier and latch')
    status.add_argument('--json', action='store_true', required=True)
    return parser


def main(argv=None, free_fn=measure_free_bytes, state_path=None):
    state_path = state_path or default_state_path()
    args = build_parser().parse_args(argv)
    result = evaluate(args.weight if args.command == 'check' else None, state_path, free_fn)
    if args.command == 'status':
        keys = ('tier', 'free_bytes', 'latch', 'timestamp')
        print(json.dumps(dict({key: result[key] for key in keys}, **({} if result['tier'] else {'error': result['reason']})), sort_keys=True))
        return 0 if result['tier'] else REFUSED_EXIT
    free = 'unknown' if result['free_bytes'] is None else '%.1f GiB' % (result['free_bytes'] / GIB)
    print('%s; tier=%s free=%s' % (result['reason'], result['tier'] or 'unknown', free))
    return 0 if result['admit'] else REFUSED_EXIT


if __name__ == '__main__':
    raise SystemExit(main())
