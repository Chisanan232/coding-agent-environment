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


PS_ENV = {'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'}


def _ps(args, pid_text):
    try:
        return subprocess.run(['/bin/ps'] + args, capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL, env=PS_ENV)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReservationError('cannot inspect process %s: %s' % (pid_text, exc))


def process_token(pid):
    """Start-time token of a process, None when it is gone.

    pid plus start time identifies one process; a reused pid has a new token. An exited-but-unreaped (zombie) process
    still has a token: whether its lane is free is decided from the whole workload by holder_status, never from the
    wrapper alone.
    """
    done = _ps(['-o', 'lstart=', '-p', str(pid)], str(pid))
    token = done.stdout.strip()
    if done.returncode == 0 and token:
        return token
    if done.returncode == 1 and not token:
        return None
    raise ReservationError('cannot inspect process %d: ps exit %d' % (pid, done.returncode))


def process_table():
    """{pid: (ppid, pgid, stat, start token)} for every process; raises ReservationError when it cannot be read.

    Only these fields are read, never a command line. An unparseable line fails the whole read (fail closed).
    """
    done = _ps(['-axo', 'pid=,ppid=,pgid=,stat=,lstart='], 'table')
    if done.returncode != 0:
        raise ReservationError('cannot read the process table: ps exit %d' % done.returncode)
    table = {}
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 4)
        if len(fields) < 5 or not all(f.isdigit() for f in fields[:3]):
            raise ReservationError('cannot parse the process table')
        table[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3], fields[4].strip())
    if not table:
        raise ReservationError('the process table is empty')
    return table


# Workload identity (SPE-111). A wrapper that exited (even one nobody reaped) says nothing about the work it started, which
# can outlive it through setsid, a double fork or plain reparenting. The reservation therefore also records the wrapper's
# process group (when it led one) and a bounded set of descendant pid + start-token pairs, refreshed whenever the holder is
# inspected while its wrapper is alive. A lane is freed only when none of those identities is running any more.
MAX_WORKLOAD_MEMBERS = 256
WITNESSES_REQUIRED = 1  # inspections of the running wrapper, after the capture, needed before an exit can be certified
MAX_PID = 2 ** 31


def _running(table, pid, token):
    entry = table.get(pid)
    return entry is not None and entry[3] == token and not entry[2].startswith('Z')


def capture_workload(pid, token, table_fn=None):
    """Identity snapshot of a holder, taken while it is alive; `observed` is False when the table cannot be read."""
    try:
        table = (table_fn or process_table)()
    except ReservationError:
        return {'pgid': None, 'leader': False, 'members': [], 'observed': False, 'truncated': False, 'refreshes': 0}
    entry = table.get(pid)
    pgid = entry[1] if entry is not None and entry[3] == token else None
    workload = {'pgid': pgid, 'leader': pgid == pid, 'members': [], 'observed': True, 'truncated': False, 'refreshes': 0}
    return _grown(workload, pid, token, table, wrapper_alive=entry is not None and entry[3] == token)[0]


def merge_workload(old, new):
    """Fold a stored workload (same wrapper pid and token) into a fresh capture of the same wrapper, so a re-reserve cannot
    forget a member that was reparented since. Old members are kept only if they are still in `new` or the caller already
    filtered them as running; the group and witness history is kept."""
    if old is None:
        return new
    members = {(m['pid'], m['token']): m for m in old['members'] + new['members']}
    kept = sorted(members.values(), key=lambda m: m['pid'])
    return dict(new, members=kept[:MAX_WORKLOAD_MEMBERS], truncated=old['truncated'] or new['truncated'] or len(kept) > MAX_WORKLOAD_MEMBERS,
                pgid=old['pgid'] if old['pgid'] is not None else new['pgid'], leader=old['leader'] or new['leader'],
                observed=old['observed'] or new['observed'], refreshes=max(old.get('refreshes', 0), new.get('refreshes', 0)))


def wrapper_is_zombie(pid, token, table_fn=None):
    """True when pid is the process with this start token and has exited without being reaped. Unreadable table: False
    (the caller's own checks and capture_workload's `observed` flag cover it)."""
    try:
        entry = (table_fn or process_table)().get(pid)
    except ReservationError:
        return False
    return entry is not None and entry[3] == token and entry[2].startswith('Z')


def _grown(workload, pid, token, table, wrapper_alive):
    """(workload with its running members refreshed, set of running member pids). Dead members are dropped."""
    children = {}
    for other, (ppid, _, _, _) in table.items():
        children.setdefault(ppid, []).append(other)
    running = {m['pid']: m['token'] for m in workload['members'] if m['pid'] != pid and _running(table, m['pid'], m['token'])}
    if workload['leader'] and workload['pgid'] is not None:
        leader = table.get(workload['pgid'])
        # Group members count only while the group's leader pid is still the recorded wrapper (or already gone): a
        # different process holding that pid is a reused pid, not our group.
        if leader is None or leader[3] == token:
            for other, (_, group, stat_, start) in table.items():
                if group == workload['pgid'] and other != pid and not stat_.startswith('Z'):
                    running[other] = start
    queue = list(running) + ([pid] if wrapper_alive else [])
    seen = set(queue)
    while queue:
        for child in children.get(queue.pop(), []):
            entry = table[child]
            if child not in seen and child != 1 and not entry[2].startswith('Z'):
                seen.add(child)
                running[child] = entry[3]
                queue.append(child)
    running.pop(pid, None)
    truncated = workload['truncated'] or len(running) > MAX_WORKLOAD_MEMBERS
    kept = sorted(running.items())[:MAX_WORKLOAD_MEMBERS]
    grown = dict(workload, members=[{'pid': p, 'token': t} for p, t in kept], truncated=truncated)
    return grown, set(running)


class HolderStatus:
    """state is alive, unresolved or exited. evidence explains an exit; reason explains an unresolved state."""
    __slots__ = ('state', 'evidence', 'reason', 'workload', 'failed')

    def __init__(self, state, evidence='', reason='', workload=None, failed=False):
        self.state, self.evidence, self.reason, self.workload, self.failed = state, evidence, reason, workload, failed


def holder_status(pid, token, workload, token_fn=None, table_fn=None):
    """Classify one lane holder from its wrapper identity and its recorded workload.

    alive: the wrapper is the same running process. unresolved: the lane must be kept and the reason is reported, because
    either the holder or the process table could not be inspected, or the wrapper is gone (reaped, a zombie or its pid
    reused) while a recorded workload process still runs, or the workload was never fully recorded. exited: the wrapper is
    gone and every recorded identity (descendants, and the wrapper's own process group when it led one) is gone. A reused
    pid is compared by start token, so it never counts as the wrapper nor as a member. A record without a workload
    (written before SPE-111) is judged by its wrapper alone.
    """
    try:
        current = (token_fn or process_token)(pid)
    except ReservationError as exc:
        return HolderStatus('unresolved', reason='holder-uninspectable: %s' % exc, workload=workload, failed=True)
    same = current is not None and current == token
    gone = 'pid-reused' if current is not None and not same else 'holder-exited'
    if workload is None:
        return HolderStatus('alive', workload=None) if same else HolderStatus('exited', evidence=gone)
    try:
        table = (table_fn or process_table)()
    except ReservationError as exc:
        return HolderStatus('unresolved', reason='process-table-unreadable: %s' % exc, workload=workload, failed=True)
    entry = table.get(pid)
    zombie = same and entry is not None and entry[3] == token and entry[2].startswith('Z')
    if same and not zombie:
        grown = _grown(workload, pid, token, table, wrapper_alive=True)[0]
        # A witness: the wrapper was inspected while running, after the capture. Capped so a steady holder stops writing.
        grown['observed'], grown['refreshes'] = True, min(workload.get('refreshes', 0) + 1, WITNESSES_REQUIRED + 1)
        return HolderStatus('alive', workload=grown)
    grown, running = _grown(workload, pid, token, table, wrapper_alive=False)
    if running:
        return HolderStatus('unresolved', reason='workload-outlives-wrapper: wrapper pid %d is %s but %d recorded process(es) '
                            'still run (%s)' % (pid, 'an unreaped zombie' if zombie else 'gone', len(running),
                                                ', '.join(str(p) for p in sorted(running)[:8])), workload=grown)
    if grown['truncated']:
        return HolderStatus('unresolved', reason='workload-not-fully-recorded: more than %d descendants were seen, so an '
                            'empty remainder cannot prove the workload ended' % MAX_WORKLOAD_MEMBERS, workload=grown)
    if workload.get('refreshes', 0) < WITNESSES_REQUIRED:
        return HolderStatus('unresolved', reason='workload-not-witnessed: the wrapper ended before any inspection saw it '
                            'running, so detached work cannot be ruled out; release it explicitly once the job is over',
                            workload=grown)
    return HolderStatus('exited', evidence=gone, workload=grown)


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


# ---------------------------------------------------------------------------------------------------------------------
# Declared workload classes and estimates (SPE-150 PR3). RECORD-ONLY: these fields are validated, stored and shown, and no
# admission decision (lane cap, byte floor, grant order) reads them yet. A job declares what it expects to need; an
# estimate can be raised by the caller but never lowered below the class default.
#
# THE NUMBERS BELOW ARE GUESSES, deliberately conservative. They may be lowered only by a reviewed constants change
# backed by evidence from `disk-admission.py measurements`, never by a caller and never in passing.
# mem = mem_base_mib + mem_per_job_mib * jobs; jobs/min_jobs are the default and the lowest degraded parallelism
# (min_jobs is documented for the later admission change and is not read by this one); disk_gib is the default for
# --bytes; excl names what two jobs of the class must not share ('target' = the Cargo target directory, 'docker' = the
# Docker daemon). A class with no entry values (light, remote) has no defaults. No class means the legacy meaning: the
# whole budget and all cores, recorded as nothing.
WORKLOAD_CLASSES = {
    'light': {},
    'remote': {},
    'build-crate': {'mem_base_mib': 2048, 'mem_per_job_mib': 1024, 'jobs': 4, 'min_jobs': 2, 'disk_gib': 10, 'excl': 'target'},
    'build-workspace': {'mem_base_mib': 4096, 'mem_per_job_mib': 1536, 'jobs': 8, 'min_jobs': 4, 'disk_gib': 40, 'excl': 'target'},
    'build-cold-isolated': {'mem_base_mib': 4096, 'mem_per_job_mib': 1536, 'jobs': 8, 'min_jobs': 4, 'disk_gib': 60,
                            'excl': 'target'},
    'container-suite': {'mem_base_mib': 12288, 'mem_per_job_mib': 0, 'jobs': 4, 'min_jobs': 2, 'disk_gib': 20, 'excl': 'docker'},
}
CLASS_RE = re.compile(r'[a-z][a-z0-9-]{0,31}')
MAX_MEM_BYTES = 2 ** 44      # 16 TiB: far above any real host, small enough to reject absurd or hostile values
MAX_CPU_SLOTS = 1024
MAX_EXCL_KEYS = 8
MAX_EXCL_KEY_LEN = 512
WORKLOAD_FIELDS = ('class', 'mem_bytes', 'cpu_slots', 'test_threads', 'excl_keys', 'created', 'owner')
DECLARED_FIELDS = ('class', 'mem_bytes', 'cpu_slots', 'test_threads')   # compared at claim; the rest is provenance
MIB = 1024 ** 2


def _cargo_target_dir(env, home, cwd):
    """The Cargo target directory this job would use, resolved from CARGO_TARGET_DIR, else the user's Cargo config
    (`build.target-dir`), else ./target. Reads one small file; executes nothing."""
    def absolute(value, base):
        return os.path.normpath(value if os.path.isabs(value) else os.path.join(base, value))

    value = env.get('CARGO_TARGET_DIR')
    if isinstance(value, str) and value:
        return absolute(value, cwd)
    try:
        import tomllib
        descriptor = os.open(Path(home) / '.cargo/config.toml', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as handle:
            if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                configured = tomllib.loads(handle.read(65536).decode()).get('build', {}).get('target-dir')
                if isinstance(configured, str) and configured and '\0' not in configured:
                    return absolute(configured, str(home))
    except Exception:
        pass
    return absolute('target', cwd)


def resolve_workload(name, mem_gib, cpus, nbytes, env=None, home=None, cwd=None, session_id=None, ticket=None, now=None):
    """(fields, error). fields is {} when nothing was declared (the legacy meaning), else the optional record fields.

    Unknown class names fail closed. --mem-gib, --cpus and --bytes may raise an estimate and never lower it below the class
    default. Nothing is executed and nothing outside the Cargo config is read.
    """
    if name is None and mem_gib is None and cpus is None:
        return {}, None
    spec = None
    if name is not None:
        if not (isinstance(name, str) and name in WORKLOAD_CLASSES):
            return None, 'invalid-request: unknown workload class (known: %s)' % ', '.join(sorted(WORKLOAD_CLASSES))
        spec = WORKLOAD_CLASSES[name]
    if mem_gib is not None and not (type(mem_gib) in (int, float) and 0 < mem_gib <= MAX_MEM_BYTES / GIB and mem_gib == mem_gib):
        return None, 'invalid-request: --mem-gib is out of range'
    if cpus is not None and not (type(cpus) is int and 0 < cpus <= MAX_CPU_SLOTS):
        return None, 'invalid-request: --cpus is out of range'
    default_mem = default_cpus = None
    if spec:
        default_cpus = spec['jobs']
        default_mem = (spec['mem_base_mib'] + spec['mem_per_job_mib'] * spec['jobs']) * MIB
        if nbytes is not None and nbytes < spec['disk_gib'] * GIB:
            return None, 'invalid-request: --bytes is below the %s default of %d GiB (an estimate can be raised, never lowered)' % (
                name, spec['disk_gib'])
    mem_bytes = int(mem_gib * GIB) if mem_gib is not None else None
    if default_mem is not None and mem_bytes is not None and mem_bytes < default_mem:
        return None, 'invalid-request: --mem-gib is below the %s default of %.2f GiB (an estimate can be raised, never lowered)' % (
            name, default_mem / GIB)
    if default_cpus is not None and cpus is not None and cpus < default_cpus:
        return None, 'invalid-request: --cpus is below the %s default of %d (an estimate can be raised, never lowered)' % (
            name, default_cpus)
    mem_bytes = mem_bytes if mem_bytes is not None else default_mem
    slots = cpus if cpus is not None else default_cpus
    fields = {}
    if name is not None:
        fields['class'] = name
    if mem_bytes is not None:
        fields['mem_bytes'] = mem_bytes
    if slots is not None:
        fields['cpu_slots'] = slots
        fields['test_threads'] = slots
    if spec:
        env = os.environ if env is None else env
        if spec['excl'] == 'target':
            key = 'cargo-target:' + _cargo_target_dir(env, home if home is not None else account_home(), cwd or os.getcwd())
        else:
            key = spec['excl']
        fields['excl_keys'] = [key]
    elif name is not None:
        fields['excl_keys'] = []
    fields['created'] = iso_utc(now or datetime.now(timezone.utc))
    fields['owner'] = {'uid': os.getuid(), 'session_id': session_id, 'ticket': ticket}
    return fields, None


def _valid_declared(record):
    """Schema check of the optional SPE-150 fields on a reservation or queue entry. Absent fields are fine (legacy);
    present ones must be well typed and in range, with booleans never accepted as numbers."""
    def number(key, upper):
        return key not in record or (type(record[key]) is int and 0 < record[key] <= upper)

    owner = record.get('owner')
    return (('class' not in record or (isinstance(record['class'], str) and CLASS_RE.fullmatch(record['class']) is not None))
            and number('mem_bytes', MAX_MEM_BYTES) and number('cpu_slots', MAX_CPU_SLOTS) and number('test_threads', MAX_CPU_SLOTS)
            and ('excl_keys' not in record or (isinstance(record['excl_keys'], list) and len(record['excl_keys']) <= MAX_EXCL_KEYS
                                               and all(isinstance(k, str) and 0 < len(k) <= MAX_EXCL_KEY_LEN
                                                       and not any(ord(c) < 32 or ord(c) == 127 for c in k) for k in record['excl_keys'])))
            and ('created' not in record or _valid_time_text(record['created']))
            and ('owner' not in record or (isinstance(owner, dict) and type(owner.get('uid')) is int and 0 <= owner['uid'] < 2 ** 32
                                           and (owner.get('session_id') is None or (isinstance(owner['session_id'], str)
                                                and SESSION_RE.fullmatch(owner['session_id']) is not None))
                                           and (owner.get('ticket') is None or (isinstance(owner['ticket'], str)
                                                and TICKET_RE.fullmatch(owner['ticket']) is not None)))))


def _valid_time_text(text):
    try:
        return isinstance(text, str) and len(text) <= 64 and datetime.fromisoformat(text.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def _declared(record):
    return tuple(record.get(key) for key in DECLARED_FIELDS)


def _declared_fields(record):
    return {key: record[key] for key in WORKLOAD_FIELDS if key in record}


def reservations_dir(state_path):
    return state_path.parent / RESERVATIONS_DIR


def write_reservation(state_path, label, nbytes, pid, token, expires, queue_id=None, workload=None, declared=None):
    """Create or replace this job's reservation file; call while holding state_lock."""
    directory = reservations_dir(state_path)
    _refuse_symlinks(directory / label)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    record = {'version': STATE_VERSION, 'label': label, 'bytes': nbytes, 'pid': pid,
              'start_token': token, 'expires': iso_utc(expires)}
    if declared:
        record.update(declared)
    if queue_id:
        record['queue_id'] = queue_id
    if workload is not None:
        record['workload'] = workload
    write_json_atomic(directory / (label + '.json'), record)
    return record


def _valid_pid(value):
    return type(value) is int and 1 < value < MAX_PID


def _valid_workload(workload, wrapper_pid=None):
    """Schema check. A leader's group is its own pid (never init's or anyone else's group), so a hand-edited record cannot
    attribute an unrelated group to a holder."""
    return (isinstance(workload, dict) and type(workload.get('leader')) is bool and type(workload.get('observed')) is bool
            and type(workload.get('truncated')) is bool and type(workload.get('refreshes', 0)) is int
            and 0 <= workload.get('refreshes', 0) <= 1000
            and (workload.get('pgid') is None or _valid_pid(workload['pgid']))
            and (not workload['leader'] or (workload.get('pgid') is not None and (wrapper_pid is None or workload['pgid'] == wrapper_pid)))
            and isinstance(workload.get('members'), list) and len(workload['members']) <= MAX_WORKLOAD_MEMBERS
            and all(isinstance(m, dict) and _valid_pid(m.get('pid')) and isinstance(m.get('token'), str)
                    and m['pid'] != wrapper_pid for m in workload['members']))


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
    if (not valid or ('workload' in record and not _valid_workload(record['workload'], record['pid']))
            or not _valid_declared(record)):
        raise ValueError('wrong schema')
    expires_at = datetime.fromisoformat(record['expires'].replace('Z', '+00:00'))
    if expires_at.tzinfo is None:
        # A naive time cannot be compared with now; treat it as a damaged record.
        raise ValueError('expires has no timezone')
    record['expires_at'] = expires_at
    return record


def _persist_workload(state_path, path, record, workload):
    """Best effort: keep the refreshed workload on the reservation and on its queue entry. A failed write only means the
    next inspection starts from the older snapshot."""
    if workload is None or workload == record.get('workload'):
        return
    try:
        fresh = {k: v for k, v in record.items() if k not in ('expires_at', 'past_ttl', 'unresolved')}
        fresh['workload'] = workload
        write_json_atomic(path, fresh)
        record['workload'] = workload
        qid = record.get('queue_id')
        if qid and QUEUE_ID_RE.fullmatch(qid):
            entry = _read_queue_entry(queue_dir(state_path) / (qid + '.json'))
            if entry['state'] == 'CLAIMED' and entry['holder']['pid'] == record['pid']:
                _persist_holder_workload(state_path, entry, workload)
    except (OSError, ValueError, StateError):
        pass


def _persist_holder_workload(state_path, entry, workload):
    """Best effort: keep the refreshed workload on a CLAIMED entry, also after an explicit release removed its reservation."""
    if workload is None or workload == entry['holder'].get('workload') or state_path is None:
        return
    try:
        entry['holder']['workload'] = workload
        _write_queue_entry(state_path, entry)
    except OSError:
        pass


def live_reservations(state_path, now=None, token_fn=process_token, table_fn=None):
    """Return (live records, problems); call while holding state_lock.

    A reservation is deleted only on positive evidence that its holder is finished: the wrapper is gone (or its pid was
    reused, compared by start token) and every recorded workload identity is gone too. A wrapper that is gone or an
    unreaped zombie while recorded workload processes still run, or a holder or process table that cannot be inspected,
    keeps its lane and bytes and is returned with `unresolved` set to the reason (an inspection failure is also a
    problem). An expired reservation whose holder is still running keeps its lane too (marked past_ttl): the time limit is
    not evidence of release, and freeing the lane would double-book a running job. Only well-formed files of this schema
    are ever deleted; anything else is reported as a problem and left alone.
    """
    now = now or datetime.now(timezone.utc)
    directory = reservations_dir(state_path)
    live, problems = [], []
    if not directory.exists():
        return live, problems
    _refuse_symlinks(directory / 'x')
    cache = {}

    def table():
        # One process-table read per pass; a failure is cached too so every record reports the same inspection failure.
        if not cache:
            try:
                cache['table'] = (table_fn or process_table)()
            except ReservationError as exc:
                cache['error'] = exc
        if 'error' in cache:
            raise cache['error']
        return cache['table']

    for path in sorted(directory.glob('*.json')):
        try:
            record = _read_reservation(path)
        except (OSError, ValueError) as exc:
            problems.append('reservation-unreadable: %s: %s' % (path.name, exc))
            continue
        status = holder_status(record['pid'], record['start_token'], record.get('workload'), token_fn, table)
        if status.state == 'exited':
            path.unlink()
            continue
        if status.state == 'unresolved':
            record['unresolved'] = status.reason
            if status.failed:
                problems.append('reservation-unverifiable: %s: %s' % (path.name, status.reason))
        _persist_workload(state_path, path, record, status.workload)
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
            token_fn=process_token, ttl=DEFAULT_TTL, owner_fn=process_owner, allow_long=False, table_fn=None, declared=None,
            host_fn=None):
    """Admit or refuse via _reserve, then record a measurement sample outside the lock for an admitted reservation.

    host_fn (optional) supplies the host sample for the SHADOW budget, taken here before the lock; it never changes the result."""
    now = now or datetime.now(timezone.utc)
    result = _reserve(label, nbytes, pid, state_path, free_fn, now, token_fn, ttl, owner_fn, allow_long, table_fn, declared,
                      _host_or_error(host_fn))
    if result.get('admit') and str(result.get('reason', '')).startswith('reserved '):
        _measure(state_path, 'reserve', result.get('free_bytes'), now)
    return result


def _reserve(label, nbytes, pid, state_path, free_fn, now, token_fn, ttl, owner_fn, allow_long, table_fn=None, declared=None,
             host=None):
    """Admit one heavy job and record its byte reservation, or refuse.

    Admission needs: heavy admitted by tier and latch, a free heavy lane, and
    free bytes minus live reservations minus this request still at or above
    the 100 GiB floor. Everything runs under the state lock, so concurrent
    callers cannot jointly overcommit.
    """
    if not (isinstance(label, str) and LABEL_RE.fullmatch(label) and type(nbytes) is int and nbytes > 0
            and type(pid) is int and pid > 0 and type(ttl) is int and 0 < ttl <= (MAX_TTL if allow_long else DEFAULT_TTL)):
        return _refusal('invalid-request: label, bytes, pid or ttl is out of range')
    if declared and not (isinstance(declared, dict) and set(declared) <= set(WORKLOAD_FIELDS) and _valid_declared(declared)):
        return _refusal('invalid-request: workload class or estimates are malformed')
    now = now or datetime.now(timezone.utc)
    try:
        with state_lock(state_path):
            result = _evaluate_locked('heavy', state_path, free_fn, now)
            if not result['admit']:
                return result
            live, problems = live_reservations(state_path, now, token_fn, table_fn)
            if problems:
                return dict(result, admit=False, reason=problems[0])
            token = token_fn(pid)
            if token is None:
                return dict(result, admit=False, reason='process-not-running: pid %d' % pid)
            if wrapper_is_zombie(pid, token, table_fn):
                return dict(result, admit=False, reason='process-not-running: pid %d has exited' % pid)
            workload = capture_workload(pid, token, table_fn)
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
            if queue_problems and (not same or nbytes > same[0]['bytes']):
                # With a damaged queue only a same-process refresh that does not grow its bytes can pass.
                return dict(result, admit=False, reason=queue_problems[0])
            # Only the claiming process itself (same pid and start token) is excused from the orphan count; an orphan that
            # holds this label under another process keeps its lane and the label.
            orphans = [] if queue_problems else [e for e in _claimed_orphans(queued, live, token_fn, table_fn, state_path)
                                                  if not (e['label'] == label and e['holder']['pid'] == pid
                                                          and e['holder']['start_token'] == token)]
            if not same and any(e['label'] == label for e in orphans):
                return dict(result, admit=False, reason='label-in-use: %s belongs to a running claimed job' % label)
            if not same:
                ahead = [e for e in queued if e['state'] in WAITING_STATES and not _entry_lapsed(e, now)]
                if ahead:
                    return dict(result, admit=False, reason='queue-ahead: %d queued entries wait for the heavy lane (first %s, %s); '
                                'enqueue instead of reserving directly' % (len(ahead), ahead[0]['id'], ahead[0]['label']))
            if same and not declared and any(_declared(same[0])):
                declared = _declared_fields(same[0])   # a refresh without a declaration keeps what the job declared before
            if same and same[0].get('workload') is not None:
                workload = merge_workload(same[0]['workload'], workload)   # a refresh must not forget reparented members
            lanes = HEAVY_LANES[result['tier']]
            reserved = sum(r['bytes'] for r in others) + sum(e['bytes'] for e in orphans)
            lane_full = len(others) + len(orphans) >= lanes
            budget = None
            if host is not None:
                budget = _shadow_site(host, 'reserve', label, lambda: (budget_holders(others, orphans, queued, now), budget_request(
                    declared or {}, nbytes, label)), result['tier'], result['free_bytes'], reserved,
                    not lane_full and result['free_bytes'] - reserved - nbytes >= HEAVY_FLOOR)
                if queue_dir(state_path).exists():
                    _budget_receipt(state_path, {'id': '-', 'label': label, 'tickets': []}, now, budget)
            if lane_full:
                return _with_budget(dict(result, admit=False, reserved_bytes=reserved,
                                         reason='lane-cap: %d of %d heavy lanes in use (%s)' % (len(others), lanes, ', '.join(
                                             '%s pid %d%s%s' % (r['label'], r['pid'], ' past its time limit' if r.get('past_ttl') else '',
                                                                ' UNRESOLVED: ' + r['unresolved'] if r.get('unresolved') else '')
                                             for r in others))), budget)
            if result['free_bytes'] - reserved - nbytes < HEAVY_FLOOR:
                return _with_budget(dict(result, admit=False, reserved_bytes=reserved,
                                         reason='over-capacity: %.1f GiB free, %.1f GiB reserved, %.1f GiB requested, floor 100 GiB' % (
                                             result['free_bytes'] / GIB, reserved / GIB, nbytes / GIB)), budget)
            write_reservation(state_path, label, nbytes, pid, token, now + timedelta(seconds=ttl), workload=workload, declared=declared)
            return _with_budget(dict(result, reserved_bytes=reserved + nbytes,
                                     reason='reserved %.1f GiB for %s (lane %d of %d)' % (nbytes / GIB, label, len(others) + 1, lanes)),
                                budget)
    except StateError as exc:
        return _refusal('%s: %s' % (exc.kind, exc))
    except OSError as exc:
        return _refusal('state-unwritable: %s' % exc)


def release(label, state_path, free_fn=read_free_bytes):
    """Delete this tool's reservation file for label (a missing one is success); log an end line when one was deleted."""
    held = None
    if isinstance(label, str) and LABEL_RE.fullmatch(label):
        try:
            held = _read_reservation(reservations_dir(state_path) / (label + '.json'))
        except (OSError, ValueError):
            held = None
    result = _release(label, state_path)
    if held is not None and result.get('admit') and str(result.get('reason', '')).startswith('released '):
        _end_measurement(state_path, held, _free_or_none(free_fn), datetime.now(timezone.utc))
    return result


def _release(label, state_path):
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
# Measurement log (SPE-150 PR2). Observational only: it records how much memory a reservation holder's process tree
# really used so a later change can size lanes from evidence. It never feeds an admission decision, never runs under the
# admission lock, and every failure is swallowed (counted in MEASUREMENT_FAILURES). Lines hold numbers and the label,
# pid and start token only: never argv, a command line or the environment.
MEASUREMENTS_NAME = 'measurements.jsonl'
MEASUREMENT_VERSION = 1
MEASUREMENT_FAILURES = 0
SAMPLE_INTERVAL_S = 60  # the snapshot refresher's cadence, so figures are lower bounds
MIN_SOLO_RUNS = 3
PS_ROW_RE = re.compile(r'\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s*$')


def _parse_cpu_time(text):
    """ps TIME ([dd-][hh:]mm:ss[.ff]) in seconds; None when unparsable."""
    try:
        days, _, rest = text.rpartition('-')
        total = 0.0
        for part in rest.split(':'):
            total = total * 60 + float(part)
        return total + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def _process_rows():
    """{pid: (ppid, rss_kib, cpu_s)} from one ps call (no args column); None when ps fails."""
    try:
        done = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,rss=,time='], capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    rows = {}
    for line in done.stdout.splitlines():
        match = PS_ROW_RE.fullmatch(line)
        cpu = _parse_cpu_time(match.group(4)) if match else None
        if match and cpu is not None:
            rows[int(match.group(1))] = (int(match.group(2)), int(match.group(3)), cpu)
    return rows


def _read_pressure():
    """Kernel memory pressure level (1 normal, 2 warn, 4 critical); None when unreadable."""
    try:
        done = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level'], capture_output=True,
                              text=True, timeout=5, stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/usr/sbin:/usr/bin'})
        level = int(done.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return level if level in (1, 2, 4) else None


def _subtree(rows, root):
    """(rss_bytes, cpu_s, process count) of root and every descendant; None when root is not running."""
    if root not in rows:
        return None
    children = {}
    for pid, (ppid, _, _) in rows.items():
        children.setdefault(ppid, []).append(pid)
    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return (sum(rows[p][1] for p in seen) * 1024, sum(rows[p][2] for p in seen), len(seen))


def _reservation_holders(state_path):
    """Reservation records as currently on disk, read-only and without verification; unreadable ones are skipped."""
    holders = []
    directory = reservations_dir(state_path)
    for path in sorted(directory.glob('*.json')):
        try:
            holders.append(_read_reservation(path))
        except (OSError, ValueError):
            continue
    return holders


def _measurements_path(state_path):
    return state_path.parent / MEASUREMENTS_NAME


def _append_measurement(state_path, line):
    path = _measurements_path(state_path)
    _rotate_once(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(line, sort_keys=True) + '\n')


def _run_key(record):
    return {'label': record['label'], 'pid': record['pid'], 'start_token': record['start_token']}


def _measure(state_path, source, free_bytes=None, now=None):
    """Append one sample line per live reservation holder; never raises and never alters the caller's result."""
    global MEASUREMENT_FAILURES
    try:
        now = now or datetime.now(timezone.utc)
        holders = _reservation_holders(state_path)
        if not holders:
            return
        rows = _process_rows()
        if rows is None:
            MEASUREMENT_FAILURES += 1
            return
        pressure = _read_pressure()
        for record in holders:
            try:
                if process_token(record['pid']) != record['start_token']:
                    continue  # pid reused (or gone): another process's memory must not be attributed to this run
            except ReservationError:
                continue
            tree = _subtree(rows, record['pid'])
            if tree is None:
                continue  # the holder is gone: the lazy reaper owns that, and a dead process has nothing to measure
            line = dict(_run_key(record), v=MEASUREMENT_VERSION, ts=iso_utc(now), event=source, rss_bytes=tree[0],
                        cpu_s=round(tree[1], 2), procs=tree[2], pressure=pressure, concurrent=len(holders),
                        free_bytes=free_bytes)
            if record.get('queue_id'):
                line['queue_id'] = record['queue_id']
            if isinstance(record.get('class'), str):
                line['class'] = record['class']
            _append_measurement(state_path, line)
    except Exception:  # observation must never change an admission outcome
        MEASUREMENT_FAILURES += 1


def _read_measurement_lines(state_path):
    """Parsed lines from the rotated and current log, oldest first, plus the count of lines skipped as unreadable."""
    lines, skipped = [], 0
    base = _measurements_path(state_path)
    for path in (base.with_name(base.name + '.1'), base):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError:
            continue
        with os.fdopen(descriptor, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                continue
            size, cap = os.fstat(handle.fileno()).st_size, 4 * RECEIPT_ROTATE_BYTES
            handle.seek(max(0, size - cap))  # newest data wins if rotation ever fails to keep the file small
            data = handle.read(cap)
            if size > cap:
                data = data.partition(b'\n')[2]  # drop the cut-off first line
            for raw in data.splitlines():
                try:
                    line = json.loads(raw)
                    if not (isinstance(line, dict) and isinstance(line.get('label'), str) and isinstance(line.get('ts'), str)):
                        raise ValueError
                    lines.append(line)
                except ValueError:
                    skipped += 1
    return lines, skipped


def _end_measurement(state_path, record, free_bytes, now):
    """Close a run: duration and free-bytes change since its first line. Never raises."""
    global MEASUREMENT_FAILURES
    try:
        key = _run_key(record)
        mine = [ln for ln in _read_measurement_lines(state_path)[0]
                if all(ln.get(k) == v for k, v in key.items()) and ln.get('event') != 'end']
        line = dict(key, v=MEASUREMENT_VERSION, ts=iso_utc(now), event='end', duration_s=None, free_delta_bytes=None,
                    free_bytes=free_bytes)
        if mine:
            started = _parse_time(mine[0]['ts'])
            line['duration_s'] = max(0, int((now - started).total_seconds()))
            first = mine[0].get('free_bytes')
            if type(first) is int and type(free_bytes) is int:
                line['free_delta_bytes'] = free_bytes - first
        _append_measurement(state_path, line)
    except Exception:
        MEASUREMENT_FAILURES += 1


def _free_or_none(free_fn):
    try:
        return free_fn()
    except Exception:
        return None


def _percentile95(values):
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


def measurements_summary(state_path):
    """Per-class evidence from the log. Read-only. Peaks are lower bounds: a sample is taken about every 60 s."""
    lines, skipped = _read_measurement_lines(state_path)
    runs = {}
    for line in lines:
        if line.get('event') == 'end' or type(line.get('rss_bytes')) is not int:
            continue
        key = (line['label'], line.get('pid'), line.get('start_token'))
        run = runs.setdefault(key, {'class': line.get('class') or line['label'] or 'unclassified', 'peak': 0, 'concurrent': False,
                                    'pressured': 0, 'unknown_pressure': 0, 'samples': 0})
        run['samples'] += 1
        run['peak'] = max(run['peak'], line['rss_bytes'])
        run['concurrent'] = run['concurrent'] or (type(line.get('concurrent')) is int and line['concurrent'] > 1)
        pressure = line.get('pressure')
        if type(pressure) is not int:
            run['unknown_pressure'] += 1
        elif pressure > 1:
            run['pressured'] += 1
    classes = {}
    for run in runs.values():
        classes.setdefault(run['class'], []).append(run)
    out = {}
    for name, group in sorted(classes.items()):
        peaks = [r['peak'] for r in group]
        qualifying = [r['peak'] for r in group if not r['concurrent'] and not r['pressured'] and not r['unknown_pressure']]
        entry = {'runs': len(group), 'max_rss_bytes': max(peaks), 'p95_rss_bytes': _percentile95(peaks),
                 'solo_runs': sum(1 for r in group if not r['concurrent']),
                 'concurrent_runs': sum(1 for r in group if r['concurrent']),
                 'pressured_samples': sum(r['pressured'] for r in group),
                 'qualifying_solo_runs': len(qualifying)}
        entry['estimate_rss_bytes'] = max(qualifying) if len(qualifying) >= MIN_SOLO_RUNS else None
        entry['estimate'] = ('%.2f GiB' % (entry['estimate_rss_bytes'] / GIB) if len(qualifying) >= MIN_SOLO_RUNS
                             else 'insufficient evidence')
        out[name] = entry
    return {'classes': out, 'skipped_lines': skipped, 'failures_this_process': MEASUREMENT_FAILURES,
            'note': 'lower bounds: processes are sampled about every %d s, so a short peak can be missed; an estimate needs '
                    '>= %d solo runs with pressure 1 throughout' % (SAMPLE_INTERVAL_S, MIN_SOLO_RUNS)}


def format_measurements(summary):
    rows = ['%-32s runs=%d max=%.2f GiB p95=%.2f GiB solo=%d concurrent=%d pressured_samples=%d estimate=%s' % (
        name, e['runs'], e['max_rss_bytes'] / GIB, e['p95_rss_bytes'] / GIB, e['solo_runs'], e['concurrent_runs'],
        e['pressured_samples'], e['estimate']) for name, e in summary['classes'].items()]
    return '\n'.join(rows + ['no measurements recorded'] * (not rows) +
                     ['note: ' + summary['note'], 'unreadable lines skipped: %d' % summary['skipped_lines']])


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


def _rotate_once(path):
    """Move a private JSONL log aside to <name>.1 once it passes 1 MiB; one backup, never a symlink."""
    # Unlocked writers can race here: the size is re-read right before the move and the same file must still be
    # over the limit, so a concurrent rotation that already ran is never replayed over the backup. A line written
    # in the remaining microsecond window lands in the backup, not lost.
    try:
        first = path.lstat()
        if first.st_size > RECEIPT_ROTATE_BYTES and not path.is_symlink():
            again = path.lstat()
            if again.st_ino == first.st_ino and again.st_size > RECEIPT_ROTATE_BYTES:
                os.replace(path, path.with_name(path.name + '.1'))
    except OSError:
        pass


def _queue_receipt(state_path, event, entry, now, **extra):
    """Append one private JSONL line per transition; rotated once at 1 MiB. Call while holding state_lock."""
    directory = queue_dir(state_path)
    path = directory / 'receipts.jsonl'
    _rotate_once(path)
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
                                                  and isinstance(entry['holder'].get('start_token'), str)
                                                  and ('workload' not in entry['holder'] or _valid_workload(entry['holder']['workload'], entry['holder']['pid'])))))
    if not valid or not _valid_declared(entry):
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


def _fingerprint(salt, repo, worktree, tickets, argv, nbytes, declared=None):
    """Salted digest of the job. `declared` (class, mem_bytes, cpu_slots, test_threads) joins the digest only when something was
    declared, so a legacy entry's fingerprint is byte-identical to what it was before SPE-150 PR3."""
    material = {'repo': repo, 'worktree': worktree, 'tickets': sorted(tickets), 'argv': list(argv), 'bytes': nbytes}
    if declared and any(value is not None for value in declared):
        material['declared'] = list(declared)
    canonical = json.dumps(material, sort_keys=True, separators=(',', ':'))
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


def _claimed_orphans(entries, live, token_fn, table_fn=None, state_path=None):
    """CLAIMED entries whose reservation file is gone (explicit `release` while the job runs) but whose holder is not
    finished. They keep occupying a lane and their bytes: a deleted file is not evidence the job stopped. A holder is
    finished only by holder_status: unresolved (uninspectable, or workload still running behind a gone wrapper) stays counted."""
    held = {(r['label'], r['pid']) for r in live}
    orphans = []
    for entry in entries:
        if entry['state'] != 'CLAIMED' or (entry['label'], entry['holder']['pid']) in held:
            continue
        holder = entry['holder']
        status = holder_status(holder['pid'], holder['start_token'], holder.get('workload'), token_fn, table_fn)
        if status.state != 'exited':
            orphans.append(entry)
            _persist_holder_workload(state_path, entry, status.workload)
    return orphans


def queue_enqueue(state_path, label, tickets, repo, worktree, harness, session_id, nbytes, argv, again=False, now=None,
                  wait_s=QUEUE_WAIT_S, declared=None):
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
    elif declared and not (isinstance(declared, dict) and set(declared) <= set(WORKLOAD_FIELDS) and _valid_declared(declared)):
        bad = 'workload'
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
            fingerprint = _fingerprint(salt, repo, real_worktree, tickets, argv, nbytes, _declared(declared or {}))
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
            entry.update({k: v for k, v in (declared or {}).items() if k != 'created'})  # the entry already has `created`
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


def _has_waiting(state_path):
    """Read-only, unlocked: is any entry waiting for a lane? Only then is a host sample worth taking for the shadow budget."""
    try:
        return any(e['state'] in ('QUEUED', 'ELIGIBLE') for e in _queue_entries(state_path)[0])
    except (StateError, OSError):
        return False


def queue_advance(state_path, free_fn=measure_free_bytes, now=None, token_fn=process_token, grant_s=QUEUE_GRANT_S, table_fn=None,
                  host_fn=None):
    """_queue_advance, then a measurement sample per reservation holder outside the lock. host_fn feeds the SHADOW budget."""
    now = now or datetime.now(timezone.utc)
    host = _host_or_error(host_fn) if host_fn is not None and _has_waiting(state_path) else None
    seen = {}

    def free():
        seen['free'] = free_fn()
        return seen['free']

    out = _queue_advance(state_path, free, now, token_fn, grant_s, table_fn, host)
    _measure(state_path, 'advance', seen.get('free'), now)
    return out


def _queue_advance(state_path, free_fn, now, token_fn, grant_s, table_fn=None, host=None):
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
            live, res_problems = live_reservations(state_path, now, token_fn, table_fn)
            if res_problems:
                out['reason'] = res_problems[0]
                return out
            for entry in entries:
                if entry['state'] != 'CLAIMED':
                    continue
                holder = entry['holder']
                status = holder_status(holder['pid'], holder['start_token'], holder.get('workload'), token_fn, table_fn)
                if status.state != 'exited':
                    _persist_holder_workload(state_path, entry, status.workload)
                    continue  # alive, or unresolved (uninspectable / workload still running): the lane stays occupied
                evidence, alive = status.evidence, False
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
            orphans = _claimed_orphans(entries, live, token_fn, table_fn, state_path)
            occupied = len(live) + len(granted) + len(orphans) >= lanes
            reserved = sum(r['bytes'] for r in live) + sum(e['bytes'] for e in orphans)
            if host is not None:
                out['budget'] = _shadow_site(host, 'advance', first['label'], lambda: (
                    budget_holders(live, orphans, granted, now), budget_request(first, first['bytes'], first['label'])), result['tier'],
                                             result['free_bytes'], reserved,
                                             not occupied and result['free_bytes'] - reserved - first['bytes'] >= HEAVY_FLOOR)
                _budget_receipt(state_path, first, now, out['budget'])
            if occupied:
                out['reason'] = 'lane occupied'
                return out
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
                owner_fn=process_owner, ttl=DEFAULT_TTL, memory_fn=_memory_precondition, exec_fn=None, allow_long=False,
                table_fn=None, declared=None, host_fn=None):
    """Claim a granted lane for this very process, then exec the job; every precondition is revalidated first.
    host_fn feeds the SHADOW budget (sampled after the memory gate, before the lock); it never changes the outcome.

    Outside the lock: the entry must be GRANTED and unlapsed, owned by this uid, and match label, bytes, worktree and job
    fingerprint; then fresh memory/CPU/heavy-process admission. Under the lock: the entry again, then tier, latch, lane
    count (the grant itself does not count), disk floor and our own process; the reservation and the holder are written in
    one critical section. A refusal keeps the grant. Returns a dict with ok, reason and exit (77 refused, 127 exec failed).
    """
    fresh_clock = now is None
    now = now or datetime.now(timezone.utc)
    pid = pid or os.getpid()
    exec_fn = exec_fn or _exec_job
    budget = None

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
        if _declared(entry) != _declared(declared or {}):
            return '%s was queued with a different workload class or estimate (class %s, mem_bytes %s, cpu_slots %s)' % (
                qid, entry.get('class'), entry.get('mem_bytes'), entry.get('cpu_slots'))
        if os.path.realpath(cwd) != entry['worktree']:
            return '%s was granted for worktree %s' % (qid, entry['worktree'])
        try:
            salt = _queue_salt(state_path)
        except (OSError, ValueError) as exc:
            return 'queue-salt-unreadable: %s' % exc
        if _fingerprint(salt, entry['repo'], entry['worktree'], entry['tickets'], argv, nbytes, _declared(entry)) != entry['fingerprint']:
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
        host = _host_or_error(host_fn)
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
            try:
                live, res_problems = live_reservations(state_path, now, token_fn, table_fn)
            except ReservationError as exc:
                return refuse('process-unverifiable: %s' % exc)
            if res_problems:
                return refuse(res_problems[0])
            try:
                token = token_fn(pid)
                owner = owner_fn(pid) if token is not None else None
            except ReservationError as exc:
                return refuse('process-unverifiable: %s' % exc)
            if token is None:
                return refuse('process-not-running: pid %d' % pid)
            if owner != os.getuid():
                return refuse('process-not-owned: pid %d is not run by this user' % pid)
            if any(r['label'] == label for r in live):
                return refuse('label-in-use: %s already holds a reservation' % label)
            orphans = _claimed_orphans(entries, live, token_fn, table_fn, state_path)
            lanes = HEAVY_LANES[result['tier']]
            reserved = sum(r['bytes'] for r in live) + sum(e['bytes'] for e in orphans)
            lane_full = len(live) + len(orphans) >= lanes
            if host is not None:
                budget = _shadow_site(host, 'claim', label, lambda: (budget_holders(live, orphans, entries, now, skip_id=qid),
                                                          budget_request(entry, nbytes, label)), result['tier'], result['free_bytes'], reserved,
                                      not lane_full and result['free_bytes'] - reserved - nbytes >= HEAVY_FLOOR)
                _budget_receipt(state_path, entry, now, budget)
            if lane_full:
                return _with_budget(refuse('lane-cap: %d of %d heavy lanes in use (%s)' % (len(live) + len(orphans), lanes, ', '.join(
                    ['%s pid %d%s%s' % (r['label'], r['pid'], ' past its time limit' if r.get('past_ttl') else '',
                                    ' UNRESOLVED: ' + r['unresolved'] if r.get('unresolved') else '') for r in live] +
                    ['%s pid %d (reservation removed, job still running)' % (e['label'], e['holder']['pid']) for e in orphans]))), budget)
            if result['free_bytes'] - reserved - nbytes < HEAVY_FLOOR:
                return _with_budget(refuse('over-capacity: %.1f GiB free, %.1f GiB reserved, %.1f GiB requested, floor 100 GiB' % (
                    result['free_bytes'] / GIB, reserved / GIB, nbytes / GIB)), budget)
            if wrapper_is_zombie(pid, token, table_fn):
                return refuse('process-not-running: pid %d has exited' % pid)
            workload = capture_workload(pid, token, table_fn)
            write_reservation(state_path, label, nbytes, pid, token, now + timedelta(seconds=ttl), queue_id=qid, workload=workload,
                              declared=dict(_declared_fields(entry), created=iso_utc(now)) if any(_declared(entry)) else None)
            try:
                entry['state'] = 'CLAIMED'
                entry['holder'] = {'pid': pid, 'start_token': token, 'claimed_at': iso_utc(now), 'workload': workload}
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
    _measure(state_path, 'claim', _free_or_none(free_fn), now)
    try:
        exec_fn(argv[0], argv)
    except OSError as exc:
        release(label, state_path)
        _queue_mark_released(state_path, qid, now, 'exec-failed', False)
        return _with_budget({'ok': False, 'reason': 'exec failed: %s' % exc, 'exit': 127}, budget)
    return _with_budget({'ok': True, 'reason': 'claimed %s for %s' % (qid, label), 'exit': 0}, budget)


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
    if any(_declared(entry)):
        # Always the recorded values: the claim re-resolves them and must match the entry exactly, raised estimates included.
        claim = claim.replace(' -- <the exact job>', ''.join(
            [' --class ' + entry['class']] * ('class' in entry) +
            [' --mem-gib %s' % (entry['mem_bytes'] / GIB)] * ('mem_bytes' in entry) +
            [' --cpus %d' % entry['cpu_slots']] * ('cpu_slots' in entry)) + ' -- <the exact job>')
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


def queue_release(state_path, qid, now=None, session_id=None, token_fn=process_token, table_fn=None, confirm_ended=False):
    """Owner's explicit release of a CLAIMED entry whose holder is unresolved (or already exited). Never signals a process.

    The only way out for a job that cannot be proved finished (an invisible detached workload, a job that ended before any
    inspection, a truncated record). Refused while the wrapper is alive and running (stop the job; advance records that),
    for another user's entry, when the holder or process table cannot be inspected (fail closed), and unless the caller
    proves ownership with the entry's own session id or states `confirm_ended` (the owner verified the workload ended).
    It also removes the entry's reservation file.
    """
    now = now or datetime.now(timezone.utc)
    if not (isinstance(qid, str) and QUEUE_ID_RE.fullmatch(qid)):
        return {'ok': False, 'reason': 'invalid-request: bad queue id'}
    if session_id is None and not confirm_ended:
        return {'ok': False, 'reason': 'refused: give --session-id (the entry\'s own session) or --confirm-ended (you verified '
                                       'the workload has ended); a same-user process alone is not enough'}
    try:
        with state_lock(state_path):
            entries, problems = _queue_entries(state_path)
            if problems:
                return {'ok': False, 'reason': problems[0]}
            entry = next((e for e in entries if e['id'] == qid), None)
            if entry is None:
                return {'ok': False, 'reason': 'no queue entry %s' % qid}
            if entry['state'] != 'CLAIMED':
                return {'ok': False, 'reason': '%s is %s; only a claimed entry is released here' % (qid, entry['state'])}
            if entry.get('owner_uid') != os.getuid():
                return {'ok': False, 'reason': '%s belongs to another user' % qid}
            if session_id is not None and entry.get('session_id') != session_id:
                return {'ok': False, 'reason': '%s belongs to another session' % qid}
            holder = entry['holder']
            status = holder_status(holder['pid'], holder['start_token'], holder.get('workload'), token_fn, table_fn)
            if status.failed:
                return {'ok': False, 'reason': '%s: cannot inspect the holder (%s); refusing to release what cannot be '
                                               'checked' % (qid, status.reason)}
            if status.state == 'alive':
                return {'ok': False, 'reason': '%s: its wrapper (pid %d) is still running; stop the job, then queue advance '
                                               'records the release' % (qid, holder['pid'])}
            path = reservations_dir(state_path) / (entry['label'] + '.json')
            _refuse_symlinks(path)
            try:
                record = _read_reservation(path)
            except FileNotFoundError:
                record = None
            except (OSError, ValueError) as exc:
                return {'ok': False, 'reason': 'reservation-unreadable: %s' % exc}
            if record is not None and (record.get('queue_id') != qid or record['pid'] != holder['pid']):
                return {'ok': False, 'reason': 'label %s is held by another reservation' % entry['label']}
            if record is not None:
                path.unlink()
            evidence = 'owner-release'
            entry['state'] = 'RELEASED'
            entry['release'] = {'at': iso_utc(now), 'evidence': evidence, 'holder_alive': status.state == 'unresolved',
                                'was': status.state, 'reason': status.reason[:200]}
            _write_queue_entry(state_path, entry)
            receipt_ok = _queue_receipt(state_path, 'released', entry, now, evidence=evidence, holder_alive=status.state == 'unresolved')
            return {'ok': True, 'receipt_ok': receipt_ok, 'label': entry['label'], 'holder_pid': holder['pid'],
                    'was': status.state, 'detail': status.reason[:200],
                    'reason': 'released %s (label %s, holder pid %d, was %s)' % (qid, entry['label'], holder['pid'], status.state)}
    except StateError as exc:
        return {'ok': False, 'reason': '%s: %s' % (exc.kind, exc)}
    except OSError as exc:
        return {'ok': False, 'reason': 'state-unwritable: %s' % exc}


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
                if entry['state'] not in WAITING_STATES:
                    return {'ok': False, 'reason': '%s is %s; cancel never stops a running job%s' % (
                        qid, entry['state'], ' (stop the job itself, then queue advance records the release)'
                        if entry['state'] == 'CLAIMED' else '')}
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


# ---------------------------------------------------------------------------------------------------------------------
# Shadow resource budget (SPE-150 PR4). SHADOW: NOT ENFORCED. At the three lane sites (reserve, queue advance, queue claim)
# the budget below is computed and recorded beside the decision (a `budget` key in the result, a receipts line, the snapshot
# and status `budget` sections) while the HEAVY_LANES check still decides alone. Nothing here changes an admit, a refusal,
# a grant order or a reason string. It persists nothing but receipts lines, and any failure to sample the host only marks the
# budget `unavailable`.
#
# Arithmetic, under the existing flock and over reservation records (host memory is never sampled inside the lock; a
# windowed host sample is taken BEFORE it):
#   projected = host_used + sum_r max(0, est_mem_r - measured_rss_r) + unmanaged_trees * UNMANAGED_ALLOWANCE + est_mem_new
# measured_rss_r (the holder's process subtree in the sample) counts only when the sample is newer than the record's
# `created`; an older or undated record is charged in full. A running heavy tree that no record accounts for is UNMANAGED: it
# adds the allowance (its own usage is already in host_used) and is never a refusal by itself. A legacy record (no class and
# no declared estimate) costs the whole memory budget and all cores; a legacy REQUEST needs the whole host, so it fits only
# beside no other workload. Who counts: live reservations (also past their TTL or unresolved), claimed orphans and unclaimed,
# unlapsed GRANTED queue entries, each at its recorded class cost.
# All numbers are guesses to be calibrated from `disk-admission.py measurements`; headroom is whole percent so a boundary
# comparison is exact.
SHADOW_NOTE = 'shadow: not enforced'
UNMANAGED_ALLOWANCE = 8 * GIB
HEADROOM_PERCENT = {'GREEN': 30, 'AMBER': 40}        # memory kept free: 0.30 / 0.40 of RAM
CPU_BUDGET_PERCENT = 100                              # cpu_slots may sum to cores x 1.0
MAX_CONCURRENT = {'emergency': 0, 'low': 0, 'stable': 1, 'wide': 1}   # constant 1 everywhere a heavy lane exists
PROBE_CAP = 2                                         # `fits_if_cap_were_2` asks what a second concurrent job would have met
SHADOW_DEADLINE_S = 3.0                              # overall wall-time cap on one host sample, ps rounds included
SHADOW_WINDOW_S = 2.0                                 # host sample window; memory-admission's own check uses 10 s
BAND_RANK = {'GREEN': 0, 'AMBER': 1, 'RED': 2}
REFERENCE_CLASS = 'build-crate'                       # the snapshot's "would a typical job fit" probe


def _legacy(record):
    return not any(key in record for key in ('class', 'mem_bytes', 'cpu_slots'))


def _created_time(text):
    try:
        return _parse_time(text) if isinstance(text, str) else None
    except ValueError:
        return None


def budget_holder(kind, record, created=None, pid=None, workload=None):
    """Normalised cost record of one thing that occupies the budget. `created` is when its reservation began (None: charge in
    full); pid is None for a grant nobody has claimed yet (nothing runs, nothing is measured)."""
    return {'kind': kind, 'label': record['label'], 'class': record.get('class'), 'legacy': _legacy(record),
            'mem_bytes': record.get('mem_bytes') or 0, 'cpu_slots': record.get('cpu_slots') or 0,
            'excl_keys': list(record.get('excl_keys') or []), 'created': _created_time(created), 'pid': pid,
            'members': [m['pid'] for m in (workload or {}).get('members', [])],
            'flags': [flag for flag in ('past_ttl', 'unresolved') if record.get(flag)]}


def budget_request(record, nbytes, label):
    return {'label': label, 'bytes': nbytes, 'class': record.get('class'), 'legacy': _legacy(record),
            'mem_bytes': record.get('mem_bytes') or 0, 'cpu_slots': record.get('cpu_slots') or 0,
            'test_threads': record.get('test_threads') or 0, 'excl_keys': list(record.get('excl_keys') or [])}


def budget_holders(live, orphans, entries, now, skip_id=None):
    """Holders for the budget from lane-site data: live reservations, claimed orphans (judged from their claim time) and the
    unlapsed GRANTED entries other than skip_id (the grant being claimed does not count against itself)."""
    holders = [budget_holder('reservation', r, r.get('created'), r['pid'], r.get('workload')) for r in live]
    holders += [budget_holder('orphan', e, e['holder'].get('claimed_at'), e['holder']['pid'], e['holder'].get('workload'))
                for e in orphans]
    holders += [budget_holder('granted', e, None) for e in entries
                if e['state'] == 'GRANTED' and e['id'] != skip_id and not _entry_lapsed(e, now)]
    return holders


def _subtree_pids(rows, roots):
    """Every pid at or below any of `roots` in {pid: (ppid, rss_kib, cpu_s)}."""
    children = {}
    for pid, (ppid, _, _) in rows.items():
        children.setdefault(ppid, []).append(pid)
    seen, stack = set(), [r for r in roots if r in rows]
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack.extend(children.get(pid, ()))
    return seen


def _gib(value):
    return '%.1f GiB' % (value / GIB)


def budget_decision(host, reservations, request, caps):
    """Pure. What the resource budget WOULD decide for `request` beside `reservations` (shadow: not enforced).

    host: {'ram','used','cores','load1','time' (aware datetime),'window_s','rows','rows_prev','workloads','band' or
    'band_fn'(corrected load per core -> (band, reasons))} or None/{'error':..}. request: budget_request() or None for state
    only. caps: {'max_concurrent','free_bytes','reserved_bytes','floor'}. Returns a JSON-safe dict; `would_admit` honours the
    concurrency cap, `fits_if_cap_were_2` is the same verdict with the cap raised to 2, `resource_reasons` explains every
    non-cap refusal and `cap_reason` the cap.
    """
    out = {'shadow': SHADOW_NOTE, 'status': 'ok'}
    if not host or host.get('error') or any(host.get(k) is None for k in ('ram', 'used', 'cores')):
        return dict(out, status='unavailable', reason=(host or {}).get('error') or 'no host sample')
    ram, cores, rows, prev = host['ram'], host['cores'], host.get('rows') or {}, host.get('rows_prev') or {}
    sampled, window = host.get('time'), host.get('window_s') or 0
    managed, holders, residual, measured_cpu, load_credit, legacy = set(), [], 0, 0.0, 0.0, 0
    for h in reservations:
        pids = _subtree_pids(rows, ([h['pid']] if h['pid'] is not None else []) + h['members'])
        managed |= pids
        fresh = sampled is not None and h['created'] is not None and sampled > h['created']
        rss = sum(rows[p][1] for p in pids) * 1024 if fresh else 0
        rate = sum(max(0.0, rows[p][2] - prev[p][2]) for p in pids if p in prev) / window if fresh and window > 0 else 0.0
        slots = cores if h['legacy'] else h['cpu_slots']
        load_credit += min(slots, rate)
        measured_cpu += rate
        legacy += h['legacy']
        charge = None if h['legacy'] else max(0, h['mem_bytes'] - rss)   # a legacy record is charged the whole budget below
        residual += charge or 0
        holders.append({'label': h['label'], 'kind': h['kind'], 'class': h['class'], 'legacy': h['legacy'], 'est_mem_bytes': h['mem_bytes'],
                        'measured_rss_bytes': rss, 'charge_bytes': charge, 'cpu_slots': slots, 'measured_cpu': round(rate, 2),
                        'counted_measurement': bool(fresh), 'flags': h['flags']})
    unmanaged = [w for w in host.get('workloads') or []
                 if w.get('root_pid') is None or (w['root_pid'] not in managed and not managed & set(w.get('members') or []))]
    lpc = None
    if host.get('load1') is not None:
        lpc = max(0.0, host['load1'] - load_credit) / cores
    if host.get('band_fn'):
        band, band_reasons = host['band_fn'](lpc)
    else:
        band, band_reasons = host.get('band', 'GREEN'), host.get('band_reasons', [])
    headroom = HEADROOM_PERCENT['GREEN' if band == 'GREEN' else 'AMBER']
    limit = ram * (100 - headroom) // 100
    for entry in holders:
        if entry['legacy']:
            entry['charge_bytes'] = limit
    residual += limit * legacy
    allowance = len(unmanaged) * UNMANAGED_ALLOWANCE
    cpu_used = sum(e['cpu_slots'] for e in holders)
    cpu_limit = cores * CPU_BUDGET_PERCENT // 100
    count = len(reservations)
    out.update(band=band, band_reasons=list(band_reasons), headroom_percent=headroom, ram_bytes=ram, host_used_bytes=host['used'],
               limit_bytes=limit, residual_bytes=residual, unmanaged_trees=len(unmanaged), unmanaged_allowance_bytes=allowance,
               cpu_slots_used=cpu_used, cpu_limit=cpu_limit, corrected_load_per_core=None if lpc is None else round(lpc, 2),
               count=count, holders=holders)
    base = host['used'] + residual + allowance
    out['projected_bytes'] = base
    cap = caps.get('max_concurrent', 0)
    out['max_concurrent'] = cap
    if request is None:
        return out
    reasons = []
    if band == 'RED':
        reasons.append('band RED: %s' % ('; '.join(band_reasons) or 'memory-admission refuses'))
    if request['legacy']:
        # No declared estimate means the legacy meaning: the whole budget and all cores, so only a free host fits it.
        if count or unmanaged:
            reasons.append('legacy request needs the whole budget and all cores: %d workload(s) and %d unmanaged tree(s) hold some'
                           % (count, len(unmanaged)))
        out['projected_bytes'] = base + limit
        cpu_need = cores
    else:
        out['projected_bytes'] = base + request['mem_bytes']
        cpu_need = request['cpu_slots']
        if out['projected_bytes'] > limit:
            reasons.append('memory: projected %s > %s (RAM x (1 - %d%%))' % (_gib(out['projected_bytes']), _gib(limit), headroom))
        if cpu_used + cpu_need > cpu_limit:
            reasons.append('cpu: %d slots in use + %d requested > %d cores' % (cpu_used, cpu_need, cpu_limit))
    taken = set(request['excl_keys'])
    clash = sorted({h['label'] for h in reservations if taken & set(h['excl_keys'])})
    if clash:
        reasons.append('excl_key overlap with %s' % ', '.join(clash))
    if caps.get('free_bytes') is not None and caps['free_bytes'] - caps.get('reserved_bytes', 0) - request['bytes'] < caps.get('floor', 0):
        reasons.append('byte floor: %s free - %s reserved - %s requested < %s' % (
            _gib(caps['free_bytes']), _gib(caps.get('reserved_bytes', 0)), _gib(request['bytes']), _gib(caps['floor'])))

    def fits(limit_count):
        return not reasons and count + 1 <= limit_count

    out.update(resource_reasons=reasons, would_admit=fits(cap), fits_if_cap_were_2=fits(PROBE_CAP if cap > 0 else 0),
               cap_reason=None if count + 1 <= cap else 'cap: %d running + 1 > max concurrent %d' % (count, cap))
    jobs, threads = request['cpu_slots'], request['test_threads']
    floor_jobs = WORKLOAD_CLASSES.get(request['class'], {}).get('min_jobs') or 1
    if band == 'AMBER' and jobs:
        out['granted_jobs'], out['granted_test_threads'] = min(jobs, max(floor_jobs, jobs // 2)), max(1, threads // 2)
    elif band == 'GREEN':
        out['granted_jobs'], out['granted_test_threads'] = jobs, threads
    else:
        out['granted_jobs'] = out['granted_test_threads'] = 0
    return out


def budget_line(budget):
    """One human line; always carries the shadow note."""
    if budget.get('status') != 'ok':
        return 'budget (%s): unavailable: %s' % (SHADOW_NOTE, budget.get('reason'))
    if 'would_admit' not in budget:
        return 'budget (%s): band %s, projected %s of %s, %d/%d cpu slots' % (
            SHADOW_NOTE, budget['band'], _gib(budget['projected_bytes']), _gib(budget['limit_bytes']), budget['cpu_slots_used'],
            budget['cpu_limit'])
    why = budget['resource_reasons'] + ([budget['cap_reason']] if budget['cap_reason'] else [])
    return 'budget (%s): would %s%s; fits_if_cap_were_2=%s; band %s; projected %s of %s; %d/%d cpu slots; lane check decided %s' % (
        SHADOW_NOTE, 'admit' if budget['would_admit'] else 'refuse', ' (%s)' % '; '.join(why) if why else '',
        str(budget['fits_if_cap_were_2']).lower(), budget['band'], _gib(budget['projected_bytes']), _gib(budget['limit_bytes']),
        budget['cpu_slots_used'] + budget.get('request_cpu_slots', 0), budget['cpu_limit'], budget.get('enforced', 'n/a'))


def _memory_module():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('memory_admission', Path(__file__).with_name('memory-admission.py'))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_host_sample(window_s=SHADOW_WINDOW_S, sleep=time.sleep, sample_fn=None, rows_fn=None, memory=None,
                     deadline_s=SHADOW_DEADLINE_S):
    """Windowed host sample for the shadow budget, taken OUTSIDE the admission lock, within an overall wall-time deadline
    (the window plus every ps/vm_stat/lsof round). Never raises: a failure or a missed deadline returns {'error': ...}; the
    abandoned worker thread is a daemon that only reads and cannot affect any decision."""
    import threading
    box = {}

    def work():
        box['sample'] = _read_host_sample(window_s, sleep, sample_fn, rows_fn, memory)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(deadline_s)
    return box.get('sample') or {'error': 'host-sample-timeout: no sample within %g s' % deadline_s}


def _read_host_sample(window_s, sleep, sample_fn, rows_fn, memory):
    try:
        memory = memory or _memory_module()
        sample_fn, rows_fn = sample_fn or memory.take_sample, rows_fn or _process_rows
        first, rows_prev = sample_fn(), rows_fn()
        sleep(window_s)
        second, rows = sample_fn(), rows_fn()
        view = memory.budget_view(first, second)
        if rows is None or rows_prev is None:
            return {'error': 'process table unreadable'}
        return {'ram': view['ram'], 'used': view['used_bytes'], 'cores': view['cores'], 'load1': view['load1'],
                'time': datetime.fromtimestamp(second['time'], timezone.utc), 'window_s': max(0.0, second['time'] - first['time']),
                'rows': rows, 'rows_prev': rows_prev, 'workloads': view['workloads'],
                'band_fn': lambda load_per_core: memory.budget_band(view['m'], load_per_core)}
    except Exception as exc:   # observation must never change an admission outcome
        return {'error': 'host-sample-unavailable: %s' % type(exc).__name__}


def _host_or_error(host_fn):
    if host_fn is None:
        return None
    try:
        return host_fn() or {'error': 'no host sample'}
    except Exception as exc:
        return {'error': 'host-sample-unavailable: %s' % type(exc).__name__}


def _shadow_site(host, site, label, build, tier, free_bytes, reserved, enforced_admit):
    """The budget record for one lane site. The lane check has already decided (`enforced_admit`); this only reports.

    `build()` returns (holders, request) and runs here, inside the guard: it runs under the admission lock on records that
    may be malformed, and a failure must only mark the budget unavailable, never change or abort the lane decision."""
    try:
        holders, request = build()
        caps = {'max_concurrent': MAX_CONCURRENT.get(tier, 0), 'free_bytes': free_bytes, 'reserved_bytes': reserved, 'floor': HEAVY_FLOOR}
        budget = budget_decision(host, holders, request, caps)
        budget.update(site=site, label=label, enforced='admit' if enforced_admit else 'refuse')
        if request is not None:
            budget['request_cpu_slots'] = request['cpu_slots']
        return budget
    except Exception as exc:   # never BaseException: Ctrl-C and exit must still work
        return {'shadow': SHADOW_NOTE, 'status': 'unavailable', 'site': site, 'label': label,
                'reason': 'budget-computation-failed: %s' % type(exc).__name__}


def _compact_budget(budget):
    keys = ('shadow', 'status', 'reason', 'site', 'enforced', 'band', 'would_admit', 'fits_if_cap_were_2', 'resource_reasons',
            'cap_reason', 'projected_bytes', 'limit_bytes', 'cpu_slots_used', 'cpu_limit', 'count', 'max_concurrent',
            'unmanaged_trees', 'granted_jobs', 'granted_test_threads')
    return {k: budget[k] for k in keys if k in budget}


def _budget_receipt(state_path, entry, now, budget):
    """Append the shadow verdict to the existing receipts log (the only thing the shadow budget persists). Under the lock."""
    try:
        if budget.get('status') == 'ok':
            _queue_receipt(state_path, 'shadow-budget', entry, now, budget=_compact_budget(budget))
    except Exception:   # a receipt is observation; it must never abort the lane decision
        pass


def _with_budget(result, budget):
    return result if budget is None else dict(result, budget=budget)


def _state_budget(host, state_path, now, tier, free_bytes, live, entries, orphans):
    """Budget section for snapshot and status: the occupied budget plus a probe with the reference class."""
    try:
        reserved = sum(r['bytes'] for r in live) + sum(e['bytes'] for e in orphans)
    except Exception:
        reserved = 0
    holders = []

    def build(request=None):
        holders[:] = budget_holders(live, orphans, entries, now)
        return holders, request

    state = _shadow_site(host, 'state', None, build, tier, free_bytes, reserved, True)
    if state['status'] == 'ok':
        spec = WORKLOAD_CLASSES[REFERENCE_CLASS]
        probe = budget_request({'class': REFERENCE_CLASS, 'mem_bytes': (spec['mem_base_mib'] + spec['mem_per_job_mib'] * spec['jobs']) * MIB,
                                'cpu_slots': spec['jobs'], 'test_threads': spec['jobs']}, spec['disk_gib'] * GIB, 'reference')
        verdict = _shadow_site(host, 'state', None, lambda: (holders, probe), tier, free_bytes, reserved, True)
        if verdict['status'] == 'ok':
            state['reference'] = dict({'class': REFERENCE_CLASS}, **{k: verdict[k] for k in (
                'would_admit', 'fits_if_cap_were_2', 'resource_reasons', 'cap_reason', 'granted_jobs', 'granted_test_threads')})
    state.pop('enforced', None)
    return state


def declared_summary(records):
    """One row per reservation that declared a class or estimate; empty for legacy reservations. Read-only display."""
    return [dict({'label': r['label'], 'pid': r['pid']}, **_declared_fields(r)) for r in records if any(_declared(r))]


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


def snapshot(state_path, source_fn=measure_with_source, now=None, token_fn=process_token, table_fn=None, host_fn=None):
    """Publish the host resource snapshot, then sample each reservation holder outside the lock. host_fn adds the SHADOW
    `budget` section (sampled before the lock)."""
    now = now or datetime.now(timezone.utc)
    record = _snapshot(state_path, source_fn, now, token_fn, table_fn, _host_or_error(host_fn))
    if 'error' not in record:
        _measure(state_path, 'snapshot', record.get('free_bytes'), now)
    return record


def _snapshot(state_path, source_fn, now, token_fn, table_fn=None, host=None):
    """Refresh state and publish the host resource snapshot next to it."""
    seen = {}

    def free():
        value, seen['source'] = source_fn()
        return value

    try:
        with state_lock(state_path):
            result = _evaluate_locked(None, state_path, free, now)
            live, problems = live_reservations(state_path, now, token_fn, table_fn)
            blockers = list(problems)
            blockers += ['reservation-unresolved: %s (pid %d) keeps its lane and %d bytes: %s' % (
                r['label'], r['pid'], r['bytes'], r['unresolved']) for r in live
                if r.get('unresolved') and not r['unresolved'].startswith(('holder-uninspectable', 'process-table-unreadable'))]
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
            if declared_summary(live):
                record['workloads'] = declared_summary(live)   # display only: no decision reads it
            if host is not None and result['tier'] is not None:
                try:
                    entries, queue_problems = _queue_entries(state_path)
                    entries = [] if queue_problems else entries
                    record['budget'] = _state_budget(host, state_path, now, result['tier'], result['free_bytes'], live, entries,
                                                     _claimed_orphans(entries, live, token_fn, table_fn, state_path))
                except (StateError, OSError, ReservationError, ValueError) as exc:   # the shadow section never fails a snapshot
                    record['budget'] = {'shadow': SHADOW_NOTE, 'status': 'unavailable', 'reason': 'records unreadable: %s' % exc}
            write_json_atomic(state_path.with_name(SNAPSHOT_NAME), record)
            return record
    except StateError as exc:
        return {'error': '%s: %s' % (exc.kind, exc)}
    except OSError as exc:
        return {'error': 'state-unwritable: %s' % exc}


def status_budget(state_path, result, host_fn, now=None):
    """Read-only `budget` section for `status`: unlocked reads of the records, no verification writes. Shadow: not enforced."""
    now = now or datetime.now(timezone.utc)
    host = _host_or_error(host_fn)
    try:
        live = _reservation_holders(state_path)
        entries, problems = _queue_entries(state_path)
        entries = [] if problems else entries
        orphans = _claimed_orphans(entries, live, process_token, None, None)
        return _state_budget(host, state_path, now, result['tier'], result['free_bytes'], live, entries, orphans)
    except (StateError, OSError, ReservationError) as exc:
        return {'shadow': SHADOW_NOTE, 'status': 'unavailable', 'reason': 'records unreadable: %s' % exc}


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
                 exec_fn=_exec_job, declared=None, host_fn=None):
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
    result = reserve(label, nbytes, os.getpid(), state_path, free_fn, ttl=ttl, allow_long=allow_long, declared=declared, host_fn=host_fn)
    if not result['admit']:
        print(result['reason'], file=sys.stderr)
        if result.get('budget'):
            print(budget_line(result['budget']), file=sys.stderr)
        return REFUSED_EXIT
    if sys.stderr.isatty():
        print(result['reason'], file=sys.stderr)  # never injected into a captured job stderr
        if result.get('budget'):
            print(budget_line(result['budget']), file=sys.stderr)
    try:
        exec_fn(argv[0], argv)
    except OSError as exc:
        release(label, state_path)
        print('exec failed: %s' % exc, file=sys.stderr)
        return 127
    return 0  # only reachable with an injected exec_fn


def _add_workload_args(parser):
    parser.add_argument('--class', dest='workload_class', help='declared workload class (%s); recorded, not yet used to admit' %
                        ', '.join(sorted(WORKLOAD_CLASSES)))
    parser.add_argument('--mem-gib', type=float, dest='mem_gib', help='declared memory estimate; may raise the class default, never lower it')
    parser.add_argument('--cpus', type=int, dest='cpus', help='declared CPU slots; may raise the class default, never lower it')


def _declared_from_args(args, nbytes, session_id=None, ticket=None):
    """(fields, error) for the CLI: resolves the declared class and estimates without executing anything."""
    return resolve_workload(args.workload_class, args.mem_gib, args.cpus, nbytes, session_id=session_id, ticket=ticket)


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
    _add_workload_args(take)
    shot = commands.add_parser('snapshot', allow_abbrev=False, help='publish the host resource snapshot')
    mode = shot.add_mutually_exclusive_group(required=True)
    mode.add_argument('--json', action='store_true', help='write and print a fresh snapshot')
    mode.add_argument('--check-fresh', action='store_true', help='read-only: exit 0 when the snapshot is fresh, else 77')
    shot.add_argument('--max-age', type=int, default=STALE_AFTER_S, help='seconds, 1..120; with --check-fresh only')
    meas = commands.add_parser('measurements', allow_abbrev=False, help='read-only summary of the measured workload peaks')
    meas.add_argument('--json', action='store_true')
    commands.add_parser('refresher-plist', allow_abbrev=False, help='print the snapshot-refresher launchd agent; installs nothing')
    go = commands.add_parser('run', allow_abbrev=False, help='reserve bytes for this process, then exec the job')
    go.add_argument('--bytes', type=int, required=True, dest='nbytes')
    go.add_argument('--label', required=True)
    go.add_argument('--ttl-s', type=int, default=DEFAULT_TTL, dest='ttl', help='seconds, at most 4 h unless --max-hold')
    go.add_argument('--max-hold', action='store_true', dest='allow_long')
    _add_workload_args(go)
    go.add_argument('--queue-id', dest='queue_id', help='claim a granted heavy-lane queue entry (revalidated first)')
    go.add_argument('job', nargs=argparse.REMAINDER, help='-- COMMAND [ARG...]')
    drop = commands.add_parser('release', allow_abbrev=False, help='release a reservation by label')
    drop.add_argument('--label', required=True)
    queue = commands.add_parser('queue', allow_abbrev=False, help='heavy-lane queue: enqueue, advance, status, cancel, release')
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
    _add_workload_args(enq)
    enq.add_argument('job', nargs=argparse.REMAINDER, help='-- COMMAND [ARG...]')
    adv = qcmds.add_parser('advance', allow_abbrev=False, help='expire lapsed entries and grant the first in line if a lane is free')
    adv.add_argument('--grant-s', type=int, default=QUEUE_GRANT_S, dest='grant_s')
    qst = qcmds.add_parser('status', allow_abbrev=False, help='read-only listing with the handoff commands')
    qst.add_argument('--json', action='store_true', required=True)
    clm = qcmds.add_parser('claim', allow_abbrev=False, help='claim a granted entry for a supervising --pid; does not exec')
    clm.add_argument('--id', required=True, dest='qid')
    clm.add_argument('--label', required=True)
    clm.add_argument('--bytes', type=int, required=True, dest='nbytes')
    clm.add_argument('--pid', type=int, required=True, help='the live process of this user that holds the lane until it exits')
    clm.add_argument('--ttl-s', type=int, default=DEFAULT_TTL, dest='ttl')
    clm.add_argument('--max-hold', action='store_true', dest='allow_long')
    _add_workload_args(clm)
    clm.add_argument('job', nargs=argparse.REMAINDER, help='-- COMMAND [ARG...] (the exact job the grant was queued for)')
    qcx = qcmds.add_parser('cancel', allow_abbrev=False, help='cancel a waiting or unclaimed entry')
    qcx.add_argument('--id', required=True, dest='qid')
    qcx.add_argument('--session-id', dest='session_id', help='refuse unless the entry belongs to this session')
    qrl = qcmds.add_parser('release', allow_abbrev=False, help='owner release of a claimed entry that cannot be proved finished')
    qrl.add_argument('--id', required=True, dest='qid')
    qrl.add_argument('--session-id', dest='session_id', help='the entry\'s own session id (proves ownership)')
    qrl.add_argument('--confirm-ended', action='store_true', dest='confirm_ended',
                     help='state that you verified the claimed job and everything it started has ended')
    return parser


def _is_strict_ancestor(pid, ppid_fn=None, limit=64):
    """True when pid is a strict ancestor of this process (this command exits right after claiming, so itself would
    consume the grant for nothing); unknown means False (fail closed). Name kept for the callers' readability."""
    def ppid_of(p):
        done = subprocess.run(['/bin/ps', '-o', 'ppid=', '-p', str(p)], capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'})
        return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else None

    ppid_fn = ppid_fn or ppid_of
    current = os.getppid()
    for _ in range(limit):
        if current == pid:
            return True
        try:
            current = ppid_fn(current)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        if current is None or current <= 1:
            return False
    return False


def _queue_cli(args, state_path, free_fn, host_fn=None):
    command = args.queue_command
    if command == 'enqueue':
        if args.job[:1] != ['--']:
            print('refused: invalid-request: put -- between the options and the command', file=sys.stderr)
            return REFUSED_EXIT
        declared, error = _declared_from_args(args, args.nbytes, args.session_id, args.tickets[0] if args.tickets else None)
        if error:
            print(json.dumps({'ok': False, 'reason': error}))
            return REFUSED_EXIT
        out = queue_enqueue(state_path, args.label, args.tickets, args.repo, args.worktree, args.harness, args.session_id,
                            args.nbytes, args.job[1:], again=args.again, wait_s=args.wait_s, declared=declared)
    elif command == 'advance':
        out = queue_advance(state_path, free_fn, grant_s=args.grant_s, host_fn=host_fn)
        out['ok'] = out['reason'] in ('ok',) or bool(out['granted'] or out['expired'] or out['eligible']) or out['reason'] in (
            'lane occupied',) or out['reason'].startswith('no heavy lane')
    elif command == 'claim':
        if args.pid <= 0 or not _is_strict_ancestor(args.pid):
            print(json.dumps({'ok': False, 'reason': 'invalid-request: --pid must be a live ancestor of this command '
                                                     '(the supervising process that runs it), not an arbitrary process'}))
            return REFUSED_EXIT
        if args.job[:1] != ['--']:
            print(json.dumps({'ok': False, 'reason': 'invalid-request: put -- between the options and the command'}))
            return REFUSED_EXIT
        declared, error = _declared_from_args(args, args.nbytes)
        if error:
            print(json.dumps({'ok': False, 'reason': error}))
            return REFUSED_EXIT
        out = queue_claim(state_path, args.qid, args.label, args.nbytes, args.job[1:], os.getcwd(), free_fn, pid=args.pid,
                          ttl=args.ttl, allow_long=args.allow_long, memory_fn=lambda: _memory_precondition(),
                          exec_fn=lambda program, argv: None, declared=declared, host_fn=host_fn)
        out.pop('exit', None)
    elif command == 'cancel':
        out = queue_cancel(state_path, args.qid, session_id=args.session_id)
    elif command == 'release':
        out = queue_release(state_path, args.qid, session_id=args.session_id, confirm_ended=args.confirm_ended)
    else:
        out = dict(queue_status(state_path), ok=True)
    print(json.dumps(out, sort_keys=True))
    return 0 if out.get('ok') else REFUSED_EXIT


def main(argv=None, free_fn=measure_free_bytes, state_path=None, host_fn=None):
    state_path = state_path or default_state_path()
    if host_fn is None and free_fn is measure_free_bytes:
        host_fn = read_host_sample   # an injected free-space reading means a test or a caller that wants no host sampling
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
        declared, error = _declared_from_args(args, args.nbytes)
        if error:
            print('refused: ' + error, file=sys.stderr)
            return REFUSED_EXIT
        if args.queue_id:
            out = queue_claim(state_path, args.queue_id, args.label, args.nbytes, job, os.getcwd(), free_fn, ttl=args.ttl,
                              allow_long=args.allow_long, declared=declared, host_fn=host_fn)
            if not out['ok']:
                print(out['reason'], file=sys.stderr)
            return out['exit'] if not out['ok'] else 0
        return run_reserved(args.label, args.nbytes, job, state_path, free_fn, args.ttl, args.allow_long, declared=declared,
                            host_fn=host_fn)
    if args.command == 'measurements':
        summary = measurements_summary(state_path)
        print(json.dumps(summary, sort_keys=True) if args.json else format_measurements(summary))
        return 0
    if args.command == 'queue':
        return _queue_cli(args, state_path, free_fn, host_fn)
    if args.command in ('reserve', 'release'):
        declared, error = _declared_from_args(args, args.nbytes) if args.command == 'reserve' else ({}, None)
        if error:
            print('refused: ' + error)
            return REFUSED_EXIT
        result = (reserve(args.label, args.nbytes, args.pid, state_path, free_fn, ttl=args.ttl, allow_long=args.allow_long,
                          declared=declared, host_fn=host_fn)
                  if args.command == 'reserve' else release(args.label, state_path))
        print(result['reason'])
        if result.get('budget') and sys.stderr.isatty():
            print(budget_line(result['budget']), file=sys.stderr)
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
        record = snapshot(state_path, (lambda: (free_fn(), 'injected')) if free_fn is not measure_free_bytes else measure_with_source,
                          host_fn=host_fn)
        print(json.dumps(record, sort_keys=True))
        return REFUSED_EXIT if 'error' in record else 0
    result = evaluate(args.weight if args.command == 'check' else None, state_path, free_fn)
    if args.command == 'status':
        keys = ('tier', 'free_bytes', 'latch', 'timestamp')
        shown = dict({key: result[key] for key in keys}, **({} if result['tier'] else {'error': result['reason']}))
        try:
            workloads = declared_summary(_reservation_holders(state_path))
        except OSError:
            workloads = []
        if workloads:
            shown['workloads'] = workloads
        if host_fn is not None and result['tier']:
            shown['budget'] = status_budget(state_path, result, host_fn)   # shadow: not enforced
        print(json.dumps(shown, sort_keys=True))
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
