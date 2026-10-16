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
~/.cache/aa-qa/admission/state.json. Free space and the state path cannot be
overridden from the command line; tests inject them through arguments.

Exit codes for `check`: 0 admit, 77 refuse. Usage errors exit 2.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

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
DEFAULT_STATE_PATH = Path.home() / '.cache/aa-qa/admission/state.json'


class StateError(Exception):
    """The state file or its location cannot be trusted."""


def _refuse_symlinks(path):
    # lstat semantics: a redirected file or directory could point the write elsewhere.
    for candidate in (path, path.parent):
        if candidate.is_symlink():
            raise StateError('state path is a symlink: %s' % candidate)


@contextmanager
def state_lock(path):
    """Serialize every read-modify-write of the state file across processes."""
    _refuse_symlinks(path)
    if not path.parent.exists():
        os.makedirs(path.parent, mode=0o700)
        os.chmod(path.parent, 0o700)
    descriptor = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def write_state(path, free_bytes, latch, now=None):
    """Atomically replace the state file; call while holding state_lock."""
    _refuse_symlinks(path)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record = {'version': STATE_VERSION, 'timestamp': stamp.isoformat(timespec='seconds').replace('+00:00', 'Z'),
              'free_bytes': free_bytes, 'tier': tier(free_bytes), 'latch': bool(latch)}
    temporary = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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
        with open(path, 'rb') as handle:
            record = json.loads(handle.read(65536))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise StateError('state unreadable: %s' % exc)
    valid = (isinstance(record, dict) and record.get('version') == STATE_VERSION
             and type(record.get('latch')) is bool and type(record.get('free_bytes')) is int
             and record.get('tier') in ('emergency', 'low', 'stable', 'wide')
             and isinstance(record.get('timestamp'), str))
    if not valid:
        raise StateError('state has the wrong schema')
    return record
