#!/usr/bin/env python3
"""Guarded recovery library (SPE-176, ADR 0019 sections 3, 5 and 6). Wired only into `admission-monitor.py tick --act` (the monitor
supplies the real Env bindings; every default here still refuses), and importing it has no side effect.

Two narrow actions, each behind conclusive-evidence guards and an exclusive lock:

  1. cancel a conclusively abandoned, unstarted GRANTED queue entry, only through the supported `queue cancel --id` equivalent;
  2. SIGTERM a conclusively obsolete heavy-run / la-cargo WRAPPER or WAITER, never a workload, never any other signal.

Everything environmental is injected through Env (process table, kill, clock, scheduler cancel and status, receipts), so tests never touch a
real process. Process identity reuses disk-admission's helpers (`waiter_token`, `process_table`, `process_owner`, `_waiter_gone`).
Command lines are never read or recorded. Termination of anything beyond obsolete wrappers is a separate, unapproved decision: see
load_terminate_allowlist / terminate_allowed_for_workload, which are implemented, unwired and shipped disabled.
"""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import contextlib
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent

NOTICE_S = 300                 # a grant must be older than this (ADR 0019 notice threshold) before it can be called abandoned
GRACE_S = 10.0                 # bounded wait after SIGTERM
POLL_S = 1.0
VERIFY_WINDOW_S = 300          # how long verify_recovery reports `pending` while the next eligible entry has not started
RECEIPT_LIMIT = 1 << 20        # rotate recovery-receipts.jsonl at 1 MiB (one previous generation kept: at most about 2 MiB)
RECEIPT_NAME = 'recovery-receipts.jsonl'
LOCK_NAME = 'recovery.lock'
ALLOWLIST_LIMIT = 65536

DECISION_ACTIONS = ('none', 'cancel')
REASONS = (
    # cancel
    'granted-unclaimed', 'no-waiter', 'waiter-gone', 'owner-gone', 'owner-refused', 'grant-older-than-notice', 'reread-agrees',
    # none
    'entry-missing', 'not-granted', 'no-grant', 'has-holder', 'has-release', 'has-workload', 'waiter-alive', 'waiter-uninspectable',
    'owner-live', 'owner-uninspectable', 'owner-refusal-unproven', 'grant-too-young', 'entry-changed', 'reread-failed',
)
OBSOLETE_REASONS = (
    'obsolete-entry-terminal', 'obsolete-entry-missing', 'pid-not-alive', 'table-unreadable', 'wrong-uid', 'wrapper-unregistered',
    'pid-reused', 'token-uninspectable', 'wrong-process-group', 'wrong-parent', 'entry-live', 'not-the-entry-waiter',
    'child-workload-running', 'detached-workload-present', 'invalid-pid', 'exe-unverified', 'exe-mismatch', 'workload-unverified', 'holds-workload',
)
TERMINAL = ('CANCELLED', 'EXPIRED', 'RELEASED')
RECEIPT_KEYS = ('ts', 'kind', 'id', 'action', 'result', 'reasons', 'pid', 'detail')
RECEIPT_KINDS = ('decision', 'cancel', 'terminate', 'verify', 'prune')
# Receipt `kind` values of a claim-refused line that mean the owner's executor cannot ever succeed with this grant. Every kind the scheduler emits
# today (memory-gate, floor, lane, budget, other) is transient, so no refusal counts yet; a kind is added here only by a reviewed change.
PERMANENT_REFUSAL_KINDS = ('malformed-grant', 'fingerprint-mismatch')
OWNER_STATES = ('gone', 'refused-permanently', 'live', 'unknown')
QID_RE = re.compile(r'q[0-9]{6}')
LABEL_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,63}')


def load(name):
    spec = spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_DA = None


def _disk_admission():
    global _DA
    if _DA is None:
        _DA = load('disk-admission')
    return _DA


def iso(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class Env(object):
    """Injected environment. Every callable has a safe unknown-returning default except kill, which defaults to refusing.

    table_fn()        -> {pid: (ppid, pgid, stat, start token)}; raises on failure (fail closed)
    token_fn(pid)     -> waiter start token (UTC) or None when gone; raises on failure
    uid_fn(pid)       -> owning uid or None when gone
    uid               -> our uid
    kill_fn(pid, sig) -> sends one signal
    clock()           -> aware datetime; sleep_fn(seconds) advances it
    cancel_fn(qid)    -> scheduler `queue cancel --id qid` result {'ok', 'reason'}; the only way an entry is ever cancelled
    read_entry_fn(qid)-> fresh entry dict or None
    status_fn()       -> {'entries': [entry...], 'lane_ids': [qid holding a lane/reservation], 'workloads': {qid: [pid...]}}
    receipts_fn(qid)  -> scheduler receipt dicts for the entry (oldest first)
    owner_exec_fn(entry)    -> 'gone' | 'refused-permanently' | 'live' | 'unknown': the owner's execution mechanism (executor process, waiter session)
    registered_fn(qid)      -> {'pid', 'start_token', 'pgid', 'ppid', 'exe'} registered wrapper for the entry, or None
    exe_fn(pid)             -> executable identity (interpreter + script) of the live process, or None when unknown
    holds_workload_fn(pid)  -> True/False: the process holds a workload by evidence (open cargo/target lock); None when unknown
    """

    def __init__(self, monitor_dir, **fns):
        self.monitor_dir = Path(monitor_dir)
        self.uid = os.getuid()
        self.table_fn = _unavailable
        self.token_fn = _unavailable
        self.uid_fn = _unavailable
        self.kill_fn = _no_kill
        self.clock = lambda: datetime.now(timezone.utc)
        self.sleep_fn = lambda seconds: None
        self.cancel_fn = lambda qid: {'ok': False, 'reason': 'cancel-unavailable'}
        self.read_entry_fn = lambda qid: None
        self.status_fn = lambda: {'entries': [], 'lane_ids': [], 'workloads': {}}
        self.receipts_fn = lambda qid: []
        self.owner_exec_fn = lambda entry: 'unknown'
        self.registered_fn = lambda qid: None
        self.exe_fn = lambda pid: None
        self.holds_workload_fn = lambda pid: None
        for name, fn in fns.items():
            if not hasattr(self, name):
                raise TypeError('unknown Env field %s' % name)
            setattr(self, name, fn)


def _unavailable(*args):
    raise OSError('not available')


def _no_kill(pid, sig):
    raise OSError('kill is not wired')


def default_env(state_path, monitor_dir):
    """Real scheduler bindings. The owner-execution, wrapper-registry, executable and workload readers stay at their uninspectable defaults
    here: the consumer (the monitor's act mode) supplies them, so without it no entry is conclusively abandoned and no wrapper obsolete."""
    da = _disk_admission()
    state_path = Path(state_path)

    def entries():
        found, problems = da._queue_entries(state_path)
        if problems:
            raise OSError('queue unreadable')
        return found

    def read_entry(qid):
        return next((e for e in entries() if e['id'] == qid), None)

    def status():
        found = entries()
        holders = [e for e in found if e['state'] == 'CLAIMED' or (e['state'] == 'GRANTED' and e.get('grant'))]
        return {'entries': found, 'lane_ids': [e['id'] for e in holders], 'workloads': {}}

    def receipts(qid):
        path = da.queue_dir(state_path) / 'receipts.jsonl'
        lines = []
        try:
            with open(str(path), 'rb') as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - RECEIPT_LIMIT))
                data = handle.read().decode('utf-8', 'replace')
        except OSError:
            return lines
        for text in data.splitlines():
            try:
                line = json.loads(text)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get('id') == qid:
                lines.append(line)
        return lines

    return Env(monitor_dir, table_fn=da.process_table, token_fn=da.waiter_token, uid_fn=da.process_owner,
               cancel_fn=lambda qid: da.queue_cancel(state_path, qid),   # the queue id alone: no session scoping, no end confirmation, no file edit
               read_entry_fn=read_entry, status_fn=status, receipts_fn=receipts)


class UnsafeDir(OSError):
    pass


def safe_monitor_dir(env):
    """Create (0700) and verify the monitor dir: a real directory, not a symlink, ours, no group/other access. Raises UnsafeDir."""
    path = env.monitor_dir
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(str(path))
    except OSError:
        raise UnsafeDir('monitor dir unavailable')
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != env.uid or info.st_mode & 0o077:
        raise UnsafeDir('monitor dir is a symlink, not ours or not private')


# ---- wrapper registry (ADR 0019 section 3) -----------------------------------------------------------------------------
# heavy-run and la-cargo record one closed-schema file per queued entry at enqueue and remove it on normal exit. The monitor trusts a record only
# after the live process still matches it (start token, process group, parent, interpreter+script basenames). No argv is ever stored.

WRAPPER_DIR_NAME = 'wrappers'
WRAPPER_KINDS = ('heavy-run', 'la-cargo')
WRAPPER_KEYS = ('qid', 'pid', 'start_token', 'pgid', 'ppid', 'exe_id', 'kind', 'created')
WRAPPER_FILE_LIMIT = 4096
EXE_RE = re.compile(r'[A-Za-z0-9._-]{1,64} [A-Za-z0-9._-]{1,64}')
BASENAME_RE = re.compile(r'[A-Za-z0-9._-]{1,64}')


def wrappers_dir(state_path=None):
    """The registry directory: ADMISSION_WRAPPERS_DIR (experiments and tests) else `wrappers/` beside the admission state file."""
    override = os.environ.get('ADMISSION_WRAPPERS_DIR')
    if override:
        return Path(override)
    base = Path(state_path) if state_path is not None else _disk_admission().default_state_path()
    return base.parent / WRAPPER_DIR_NAME


def exe_identity(pid, run=None, timeout=5):
    """'interpreter script' basenames of a live process, or None when unknown. The command line is reduced in memory to those two
    basenames and discarded: nothing else of it is kept, stored or printed."""
    try:
        done = (run or subprocess.run)(['/bin/ps', '-ww', '-o', 'args=', '-p', str(pid)], capture_output=True, text=True, timeout=timeout,
                                       stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'})
        parts = (done.stdout or '').split()
        returncode = done.returncode
    except (OSError, subprocess.SubprocessError, AttributeError):
        return None
    if returncode != 0 or len(parts) < 2:
        return None
    interpreter = os.path.basename(parts[0])
    script = None
    for part in parts[1:]:
        if part.startswith('-') and not part.startswith('--') and 'c' in part[1:]:
            return None      # `-c CODE` runs inline code: there is no script to name, and the code must not be mistaken for one
        if not part.startswith('-'):
            script = os.path.basename(part)
            break
    found = '%s %s' % (interpreter, script)
    return found if script and EXE_RE.fullmatch(found) else None


REGISTER_LOOKUP_TIMEOUT_S = 1      # registration must never delay a job: a slow `ps` means no record, and no record is never terminated


def _quick_token(pid, run=None):
    """The queue waiter start token (UTC, same format as disk-admission.waiter_token) with a 1 s bound; None when gone, slow or unreadable."""
    try:
        done = (run or subprocess.run)(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=REGISTER_LOOKUP_TIMEOUT_S,
                                       stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin', 'TZ': 'UTC'})
        token = (done.stdout or '').strip()
        return token if done.returncode == 0 and token else None
    except (OSError, subprocess.SubprocessError, AttributeError):
        return None


def _dir_private(directory, uid):
    try:
        info = os.lstat(str(directory))
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o077


def list_registered(directory, uid=None):
    """Queue ids that have a registry file, only from a real private directory of ours; [] otherwise."""
    uid = os.getuid() if uid is None else uid
    if not _dir_private(directory, uid):
        return []
    try:
        names = os.listdir(str(directory))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith('.json') and QID_RE.fullmatch(n[:-5]))


def read_registry(qid, directory, uid=None, token_fn=None):
    """The validated record of the wrapper registered for `qid` as {'pid', 'start_token', 'pgid', 'ppid', 'exe', 'kind', 'created'}, else None.

    Refuses (None) on: a directory or file that is a symlink, not ours, or readable by group/other; an oversized, non-JSON or off-schema record
    (exact key set, every type checked); a qid that differs from the file name; and, when `token_fn` is given, a pid whose live start token no
    longer equals the recorded one (a reused pid). Never raises."""
    uid = os.getuid() if uid is None else uid
    if not (isinstance(qid, str) and QID_RE.fullmatch(qid)) or not _dir_private(directory, uid):
        return None
    try:
        fd = os.open(str(Path(directory) / (qid + '.json')), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077 or info.st_size > WRAPPER_FILE_LIMIT:
                return None
            doc = json.loads(handle.read(WRAPPER_FILE_LIMIT + 1).decode('utf-8'))
    except (OSError, ValueError):
        return None
    rec = _valid_wrapper_record(doc, qid)
    if rec is None:
        return None
    if token_fn is not None:
        try:
            if token_fn(rec['pid']) != rec['start_token']:
                return None
        except Exception:
            return None
    return rec


def _valid_wrapper_record(doc, qid):
    if not isinstance(doc, dict) or set(doc) != set(WRAPPER_KEYS) or doc['qid'] != qid:
        return None
    for key, low in (('pid', 2), ('pgid', 1), ('ppid', 0)):
        if type(doc[key]) is not int or not low <= doc[key] < 2 ** 31:
            return None
    token, exe = doc['start_token'], doc['exe_id']
    if not (isinstance(token, str) and 0 < len(token) <= 64 and token.isprintable() and isinstance(exe, str) and EXE_RE.fullmatch(exe)):
        return None
    if doc['kind'] not in WRAPPER_KINDS or exe.split(' ')[1] != doc['kind'] or not isinstance(doc['created'], str):
        return None
    try:
        parse_iso(doc['created'])
    except ValueError:
        return None
    return {'pid': doc['pid'], 'start_token': token, 'pgid': doc['pgid'], 'ppid': doc['ppid'], 'exe': exe, 'kind': doc['kind'], 'created': doc['created']}


def register_wrapper(directory, qid, kind, uid=None, run=None):
    """Record the calling process (a heavy-run or la-cargo wrapper that just enqueued `qid`). Atomic, 0600, in a 0700 directory. Best effort:
    returns False on any failure and never raises, so a registry problem can never fail the job."""
    try:
        uid = os.getuid() if uid is None else uid
        pid = os.getpid()
        token = _quick_token(pid, run)
        exe = exe_identity(pid, run, timeout=REGISTER_LOOKUP_TIMEOUT_S) if run is not None else exe_identity(pid, timeout=REGISTER_LOOKUP_TIMEOUT_S)
        if token is None or exe is None or kind not in WRAPPER_KINDS or not QID_RE.fullmatch(qid):
            return False
        record = {'qid': qid, 'pid': pid, 'start_token': token, 'pgid': os.getpgid(0), 'ppid': os.getppid(), 'exe_id': exe, 'kind': kind,
                  'created': iso(datetime.now(timezone.utc))}
        if _valid_wrapper_record(record, qid) is None:
            return False
        directory = Path(directory)
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not _dir_private(directory, uid):
            return False
        tmp = directory / ('.%s.%d.tmp' % (qid, pid))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(json.dumps(record, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp), str(directory / (qid + '.json')))
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        return True
    except Exception:
        return False


def unregister_wrapper(directory, qid, uid=None):
    """Remove this process's own record for `qid` (normal exit). A record that names another pid is left alone. Never raises."""
    try:
        rec = read_registry(qid, directory, uid)
        if rec is not None and rec['pid'] == os.getpid():
            (Path(directory) / (qid + '.json')).unlink()
            return True
    except Exception:
        pass
    return False


PRUNE_MAX = 50                 # unlinks per pass
PRUNE_EXAMINE_MAX = 100        # records examined (one `ps` each) per pass; the start offset rotates so every record is reached in time
PRUNE_LIST_MAX = 1000          # registry names read per pass; the excess is ignored and counted
PRUNE_BUDGET_S = 5.0           # total wall time of one pass: a slow `ps` stops the pass, never the tick
PRUNE_MAX_AGE_S = 7 * 86400


def _bounded_token(pid):
    """Start token with a 1 s bound. None only when ps says there is no such process; a timeout or any other failure RAISES (uncertain is kept)."""
    done = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=REGISTER_LOOKUP_TIMEOUT_S,
                          stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin', 'TZ': 'UTC'})
    token = (done.stdout or '').strip()
    if done.returncode == 0 and token:
        return token
    if done.returncode == 1 and not token:
        return None
    raise OSError('cannot inspect process')


def _listed(directory, uid, limit):
    """(queue ids, ignored): at most `limit` registry names from a private real directory, and how many more were seen (scanning stops at 2 x limit)."""
    if not _dir_private(directory, uid):
        return [], 0
    names, ignored = [], 0
    try:
        with os.scandir(str(directory)) as it:
            for item in it:
                if item.name.endswith('.json') and QID_RE.fullmatch(item.name[:-5]):
                    if len(names) < limit:
                        names.append(item.name[:-5])
                    else:
                        ignored += 1
                        if ignored >= limit:
                            break
    except OSError:
        return [], 0
    return sorted(names), ignored


def prune_registry(directory, env, token_fn=None, now=None, limit=PRUNE_MAX, examine_max=PRUNE_EXAMINE_MAX, list_max=PRUNE_LIST_MAX,
                   budget_s=PRUNE_BUDGET_S, monotonic=time.monotonic, stats=None):
    """Unlink-only cleanup of registry files that can no longer matter: a valid record whose pid is gone, whose start token no longer matches, or that is
    older than 7 days. Only regular files of ours with a valid queue-id name in a private real directory; anything else, and anything uninspectable, is left.
    Bounded per call: `limit` unlinks, `examine_max` records examined (rotating start, so all are reached over successive ticks), `list_max` names read
    (the rest ignored and counted), `budget_s` seconds in total. One count-only receipt line when something was removed or ignored. Returns the number
    removed; `stats` (a dict) receives examined and ignored. Never raises."""
    count, examined, ignored = 0, 0, 0
    try:
        token_fn = token_fn or _bounded_token
        now = now or env.clock()
        deadline = monotonic() + budget_s
        names, ignored = _listed(directory, env.uid, list_max)
        start = ((int(now.timestamp()) // 60) * max(1, examine_max)) % len(names) if names else 0      # each minute starts one window further on
        for qid in names[start:] + names[:start]:
            if count >= limit or examined >= examine_max or monotonic() >= deadline:
                break
            rec = read_registry(qid, directory, env.uid)
            path = Path(directory) / (qid + '.json')
            if rec is None:
                continue
            examined += 1
            try:
                token = token_fn(rec['pid'])
                old = (now - parse_iso(rec['created'])).total_seconds() > PRUNE_MAX_AGE_S
            except Exception:
                continue
            if token is not None and token == rec['start_token'] and not old:
                continue
            try:
                info = os.lstat(str(path))
                if stat.S_ISREG(info.st_mode) and info.st_uid == env.uid:
                    path.unlink()
                    count += 1
            except OSError:
                continue
        if count or ignored:
            write_receipt(env, 'prune', 'registry', 'prune', 'pruned', detail='count=%d' % count + (' ignored=%d' % ignored if ignored else ''))
    except Exception:
        pass
    if stats is not None:
        stats.update(examined=examined, ignored=ignored)
    return count


# ---- receipts ---------------------------------------------------------------------------------------------------------

def write_receipt(env, kind, qid, action, result, reasons=(), pid=None, detail=None):
    """Append one closed-schema JSONL line; rotated once at 1 MiB. Returns False (never raises) when the file cannot be written."""
    if kind not in RECEIPT_KINDS:
        raise ValueError('bad receipt kind')
    line = {'ts': iso(env.clock()), 'kind': kind, 'id': qid, 'action': action, 'result': result, 'reasons': list(reasons)}
    if type(pid) is int:
        line['pid'] = pid
    if detail is not None:
        line['detail'] = str(detail)[:200]
    if not set(line) <= set(RECEIPT_KEYS):
        raise ValueError('receipt outside the closed schema')
    path = env.monitor_dir / RECEIPT_NAME
    try:
        safe_monitor_dir(env)
        try:
            if os.lstat(str(path)).st_size >= RECEIPT_LIMIT:
                os.replace(str(path), str(path) + '.1')
        except FileNotFoundError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != env.uid or info.st_mode & 0o077:
                return False
            handle.write(json.dumps(line, sort_keys=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return False
    return True


# ---- concurrency ------------------------------------------------------------------------------------------------------

class Busy(Exception):
    pass


@contextlib.contextmanager
def recovery_lock(env):
    """Exclusive non-blocking flock around check-then-act; a second concurrent recovery gets Busy."""
    safe_monitor_dir(env)
    try:
        fd = os.open(str(env.monitor_dir / LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)   # a symlinked lock file fails ELOOP
    except OSError:
        raise UnsafeDir('lock file unavailable')
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy()
        yield
    finally:
        os.close(fd)   # closing releases the flock


# ---- (1) abandoned, unstarted entry -----------------------------------------------------------------------------------

def _facts(entry):
    return {k: entry.get(k) for k in ('id', 'state', 'seq', 'grant', 'holder', 'release', 'waiter')}


def _evaluate(entry, env, now):
    """(cancel?, reasons) from one entry snapshot. Conservative: any doubt is `none`."""
    if not isinstance(entry, dict):
        return False, ['entry-missing']
    if entry.get('state') != 'GRANTED':
        return False, ['not-granted']
    block = []
    if entry.get('holder'):
        block.append('has-holder')
    if entry.get('release'):
        block.append('has-release')
    if isinstance(entry.get('holder'), dict) and entry['holder'].get('workload'):
        block.append('has-workload')
    grant = entry.get('grant')
    if not isinstance(grant, dict) or not isinstance(grant.get('at'), str):
        block.append('no-grant')
    if block:
        return False, block
    reasons = ['granted-unclaimed']
    if entry.get('waiter'):
        waiter = entry['waiter']
        if not (isinstance(waiter, dict) and type(waiter.get('pid')) is int and isinstance(waiter.get('start_token'), str)):
            return False, ['waiter-uninspectable']
        try:
            gone = _disk_admission()._waiter_gone(entry, env.token_fn)   # the scheduler's own rule: uninspectable is not gone
        except Exception:
            return False, ['waiter-uninspectable']
        if gone:
            reasons.append('waiter-gone')
        else:
            try:
                alive = env.token_fn(waiter['pid']) == waiter['start_token']
            except Exception:
                alive = None
            return False, ['waiter-uninspectable' if alive is None else 'waiter-alive']
    else:
        reasons.append('no-waiter')
    try:
        age = (now - parse_iso(grant['at'])).total_seconds()
    except (ValueError, TypeError):
        return False, ['no-grant']
    if age <= NOTICE_S:
        return False, ['grant-too-young']
    try:
        owner = env.owner_exec_fn(entry)
    except Exception:
        owner = 'unknown'
    if owner == 'live':
        return False, ['owner-live']
    if owner == 'gone':
        reasons.append('owner-gone')
    elif owner == 'refused-permanently':
        # The owner's word is not enough: the newest scheduler receipt for the entry must itself be a permanent claim refusal made after the
        # grant, so a later claim attempt, claim or any other event (a transient refusal and retry included) cancels the proof.
        try:
            latest = list(env.receipts_fn(entry['id']))[-1:]
            proven = bool(latest) and isinstance(latest[0], dict) and latest[0].get('event') == 'claim-refused' \
                and latest[0].get('kind') in PERMANENT_REFUSAL_KINDS and _after(latest[0].get('ts'), grant['at'])
        except Exception:
            proven = False   # an unreadable or non-dict receipt proves nothing
        if not proven:
            return False, ['owner-refusal-unproven']
        reasons.append('owner-refused')
    else:
        return False, ['owner-uninspectable']
    reasons.append('grant-older-than-notice')
    return True, reasons


def _after(ts, base):
    try:
        return parse_iso(ts) >= parse_iso(base)
    except (ValueError, TypeError, AttributeError):
        return False


def abandoned_unstarted(entry, env):
    """Closed-schema decision {'action': none|cancel, 'reasons': [enum]}. `cancel` only when every guard holds on the given snapshot AND
    a fresh re-read, taken now, still agrees on every fact and passes the same guards."""
    now = env.clock()
    ok, reasons = _evaluate(entry, env, now)
    if not ok:
        return {'action': 'none', 'reasons': reasons}
    try:
        fresh = env.read_entry_fn(entry['id'])
    except Exception:
        return {'action': 'none', 'reasons': ['reread-failed']}
    if fresh is None:
        return {'action': 'none', 'reasons': ['entry-missing']}
    if _facts(fresh) != _facts(entry):
        return {'action': 'none', 'reasons': ['entry-changed']}
    ok, again = _evaluate(fresh, env, env.clock())
    if not ok:
        return {'action': 'none', 'reasons': again}
    return {'action': 'cancel', 'reasons': reasons + ['reread-agrees']}


def _receipted(env, *args, **kwargs):
    """write_receipt that never raises; False means the audit line is not on disk and no action may follow."""
    try:
        return bool(write_receipt(env, *args, **kwargs))
    except Exception:
        return False


def do_cancel(entry, env):
    """Decide and, only on a conclusive `cancel`, call the injected `queue cancel --id` equivalent with the queue id alone, under the
    recovery lock. Returns {'result': busy|unsafe-monitor-dir|receipt-unwritable|none|cancelled|failed, 'decision', 'receipt'}. The scheduler owns the state change."""
    qid = entry.get('id') if isinstance(entry, dict) else None
    if not (isinstance(qid, str) and QID_RE.fullmatch(qid)):
        return {'result': 'none', 'decision': {'action': 'none', 'reasons': ['entry-missing']}, 'receipt': None}
    try:
        with recovery_lock(env):
            decision = abandoned_unstarted(entry, env)
            written = _receipted(env, 'decision', qid, decision['action'], 'decided', decision['reasons'])
            if decision['action'] != 'cancel':
                return {'result': 'none', 'decision': decision, 'receipt': None}
            if not written:   # no audit line, no action
                return {'result': 'receipt-unwritable', 'decision': decision, 'receipt': None}
            outcome = env.cancel_fn(qid)
            ok = bool(isinstance(outcome, dict) and outcome.get('ok'))
            receipt = {'id': qid, 'action': 'cancel', 'result': 'cancelled' if ok else 'failed', 'cancelled_at': iso(env.clock())}
            write_receipt(env, 'cancel', qid, 'cancel', receipt['result'], decision['reasons'],
                          detail=None if ok else (outcome or {}).get('reason'))
            return {'result': receipt['result'], 'decision': decision, 'receipt': receipt}
    except Busy:
        return {'result': 'busy', 'decision': None, 'receipt': None}
    except UnsafeDir:
        return {'result': 'unsafe-monitor-dir', 'decision': None, 'receipt': None}


def verify_recovery(qid, env, cancelled_at=None):
    """Post-cancel check from scheduler status, never assumed: pending | verified | failed with the checks that decided it.

    failed: the entry is not terminal, the lane/reservation is still held, or a detached workload for it remains.
    pending: all of that is clean but another eligible entry exists and has not yet started (CLAIMED) inside the verify window.
    """
    now = env.clock()
    try:
        status = env.status_fn()
        entries = {e['id']: e for e in status['entries']}
        table = env.table_fn()
    except Exception:
        return _verified(env, qid, 'pending', {'status': 'unreadable'})
    entry = entries.get(qid)
    checks = {'entry_terminal': bool(entry) and entry.get('state') in TERMINAL,
              'lane_released': qid not in status.get('lane_ids', []),
              'no_detached_workload': not any(p in table for p in status.get('workloads', {}).get(qid, []))}
    if not all(checks.values()):
        return _verified(env, qid, 'failed', checks)
    # "Eligible" here means ELIGIBLE or GRANTED (a head the scheduler has released to run); QUEUED entries may legitimately wait on caps.
    # A head that has been claimed leaves those states, so any such entry still present has not started.
    waiting = [e for e in entries.values() if e['id'] != qid and e.get('state') in ('ELIGIBLE', 'GRANTED')]
    checks['next_started'] = not waiting
    if waiting:
        head = min(waiting, key=lambda e: e.get('seq', 0))
        within = cancelled_at is None or (now - parse_iso(cancelled_at)).total_seconds() < VERIFY_WINDOW_S
        checks['next_entry'] = head['id']
        return _verified(env, qid, 'pending' if within else 'failed', checks)
    return _verified(env, qid, 'verified', checks)


def _verified(env, qid, state, checks):
    try:
        with recovery_lock(env):
            ok = write_receipt(env, 'verify', qid, 'verify', state, detail=json.dumps(checks, sort_keys=True))
    except (Busy, UnsafeDir):
        ok = False   # the state is still reported; only the receipt is skipped
    return {'state': state, 'checks': checks, 'receipt_ok': ok}


# ---- (2) obsolete wrapper / waiter -----------------------------------------------------------------------------------

def _descendants(table, root):
    kids, frontier = set(), [root]
    while frontier:
        parent = frontier.pop()
        for pid, row in table.items():
            if row[0] == parent and pid not in kids and pid != root:
                kids.add(pid)
                frontier.append(pid)
    return kids


def obsolete_wrapper(qid, pid, env):
    """{'obsolete': bool, 'reasons': [enum], 'children': [pid], 'detached': [pid]}. Children and detached processes are reported, never
    signalled; either one makes the wrapper not obsolete, because a workload may still be running."""
    out = {'obsolete': False, 'reasons': [], 'children': [], 'detached': []}

    def refuse(reason):
        out['reasons'] = [r for r in out['reasons'] if not r.startswith('obsolete-')] + [reason]   # a refusal carries no positive reason
        return out

    if not (type(pid) is int and pid > 1 and pid not in (os.getpid(), os.getppid())):
        return refuse('invalid-pid')
    try:
        table = env.table_fn()
    except Exception:
        return refuse('table-unreadable')
    row = table.get(pid)
    if row is None:
        return refuse('pid-not-alive')
    try:
        owner = env.uid_fn(pid)
    except Exception:
        owner = None
    if owner != env.uid:
        return refuse('wrong-uid')
    registered = env.registered_fn(qid)
    if not registered or registered.get('pid') != pid:
        return refuse('wrapper-unregistered')
    try:
        token = env.token_fn(pid)
    except Exception:
        return refuse('token-uninspectable')
    if token is None or token != registered.get('start_token'):
        return refuse('pid-reused')
    if not (type(row[1]) is int and row[1] > 0 and type(registered.get('pgid')) is int and row[1] == registered['pgid']):
        return refuse('wrong-process-group')
    if not (type(registered.get('ppid')) is int and row[0] == registered['ppid']):
        return refuse('wrong-parent')
    try:
        entry = env.read_entry_fn(qid)
    except Exception:
        return refuse('entry-live')
    if entry is None:
        out['reasons'].append('obsolete-entry-missing')
    elif entry.get('state') in TERMINAL:
        waiter = entry.get('waiter')      # positive proof only: the entry must name THIS pid and start token as its waiter
        if not (isinstance(waiter, dict) and waiter.get('pid') == pid and waiter.get('start_token') == token):
            return refuse('not-the-entry-waiter')
        out['reasons'].append('obsolete-entry-terminal')
    else:
        return refuse('entry-live')
    # A terminal entry proves the queue no longer needs this pid, not that the pid is still a wrapper: it may have exec'd the workload.
    expected_exe = registered.get('exe')
    try:
        exe = env.exe_fn(pid)
    except Exception:
        exe = None
    if not isinstance(expected_exe, str) or not expected_exe or exe is None:
        return refuse('exe-unverified')
    if exe != expected_exe:
        return refuse('exe-mismatch')
    try:
        holds = env.holds_workload_fn(pid)
    except Exception:
        holds = None
    if holds is None:
        return refuse('workload-unverified')
    if holds:
        return refuse('holds-workload')
    kids = _descendants(table, pid)
    live = sorted(p for p in kids if 'Z' not in table[p][2])
    detached = sorted(p for p, r in table.items() if r[1] == row[1] and p != pid and p not in kids and 'Z' not in r[2])
    out['children'], out['detached'] = live, detached
    if live:
        return refuse('child-workload-running')
    if detached:
        return refuse('detached-workload-present')
    out['obsolete'] = True
    return out


def terminate_wrapper(qid, pid, env, grace_s=GRACE_S, poll_s=POLL_S):
    """SIGTERM a conclusively obsolete wrapper or waiter: exactly one SIGTERM, never any other signal, no escalation. Returns
    {'result': busy|unsafe-monitor-dir|receipt-unwritable|refused|terminated|failed-to-terminate, 'verdict', 'evidence'}. A survivor is reported with its pid, never killed."""
    try:
        with recovery_lock(env):
            verdict = obsolete_wrapper(qid, pid, env)   # evaluated under the lock, immediately before the signal
            if not verdict['obsolete']:
                write_receipt(env, 'terminate', qid, 'none', 'refused', verdict['reasons'], pid=pid)
                return {'result': 'refused', 'verdict': verdict, 'evidence': None}
            if not _receipted(env, 'terminate', qid, 'sigterm', 'intent', verdict['reasons'], pid=pid):   # no audit line, no signal
                return {'result': 'receipt-unwritable', 'verdict': verdict, 'evidence': None}
            try:
                env.kill_fn(pid, signal.SIGTERM)
            except OSError as exc:
                write_receipt(env, 'terminate', qid, 'sigterm', 'failed-to-terminate', verdict['reasons'], pid=pid, detail='kill: %s' % exc.__class__.__name__)
                return {'result': 'failed-to-terminate', 'verdict': verdict, 'evidence': {'pid': pid, 'kill': 'error'}}
            deadline = env.clock() + timedelta(seconds=grace_s)
            while True:
                if not _alive_same(env, pid):
                    write_receipt(env, 'terminate', qid, 'sigterm', 'terminated', verdict['reasons'], pid=pid)
                    return {'result': 'terminated', 'verdict': verdict, 'evidence': None}
                if env.clock() >= deadline:
                    break
                env.sleep_fn(poll_s)
            evidence = {'pid': pid, 'alive_after_s': grace_s, 'signal': 'SIGTERM', 'request': 'owner-action'}
            write_receipt(env, 'terminate', qid, 'sigterm', 'failed-to-terminate', verdict['reasons'], pid=pid,
                          detail=json.dumps(evidence, sort_keys=True))
            return {'result': 'failed-to-terminate', 'verdict': verdict, 'evidence': evidence}
    except Busy:
        return {'result': 'busy', 'verdict': None, 'evidence': None}
    except UnsafeDir:
        return {'result': 'unsafe-monitor-dir', 'verdict': None, 'evidence': None}


def _alive_same(env, pid):
    """True while the same process (same start token) is still there; an unreadable table counts as still alive."""
    try:
        row = env.table_fn().get(pid)
    except Exception:
        return True
    if row is None:
        return False
    try:
        return env.token_fn(pid) is not None and 'Z' not in row[2]
    except Exception:
        return True


# ---- terminate allowlist (broader termination): implemented, UNWIRED, shipped absent ---------------------------------

class AllowlistError(ValueError):
    pass


ALLOWLIST_KEYS = ('version', 'enabled', 'entries')
ALLOWLIST_ENTRY_KEYS = ('kind', 'label', 'deadline_multiple')
DISABLED_POLICY = {'version': 1, 'enabled': False, 'entries': []}


def load_terminate_allowlist(path):
    """Closed-schema policy file. Absent file (the shipped default) -> disabled policy. Present but invalid, oversized, not a regular file
    or with an unknown key -> AllowlistError (never silently permissive). Nothing in (1) or (2) calls this."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return dict(DISABLED_POLICY, entries=[])
    except OSError as exc:
        raise AllowlistError('allowlist unreadable: %s' % exc.__class__.__name__)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise AllowlistError('allowlist is not a regular file')
    with os.fdopen(fd, 'rb') as handle:
        data = handle.read(ALLOWLIST_LIMIT + 1)
    if len(data) > ALLOWLIST_LIMIT:
        raise AllowlistError('allowlist too large')
    try:
        doc = json.loads(data.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise AllowlistError('allowlist is not valid JSON')
    if not isinstance(doc, dict) or set(doc) != set(ALLOWLIST_KEYS):
        raise AllowlistError('allowlist keys must be exactly %s' % (ALLOWLIST_KEYS,))
    if type(doc['version']) is not int or doc['version'] != 1 or type(doc['enabled']) is not bool or not isinstance(doc['entries'], list) or len(doc['entries']) > 64:
        raise AllowlistError('allowlist version, enabled or entries invalid')
    for item in doc['entries']:
        if not isinstance(item, dict) or set(item) != set(ALLOWLIST_ENTRY_KEYS):
            raise AllowlistError('allowlist entry keys must be exactly %s' % (ALLOWLIST_ENTRY_KEYS,))
        multiple = item['deadline_multiple']
        if not (isinstance(item['kind'], str) and LABEL_RE.fullmatch(item['kind']) and isinstance(item['label'], str)
                and LABEL_RE.fullmatch(item['label']) and type(multiple) in (int, float) and 1 <= multiple <= 10):
            raise AllowlistError('allowlist entry value invalid')
    return doc


def terminate_allowed_for_workload(policy, kind, label, deadline_multiple):
    """Always False unless the policy is enabled AND an explicit entry matches kind and label and the observed multiple of the job's
    declared deadline has reached the entry's. Unwired: terminating a running workload needs separate owner approval (ADR 0019 section 4)
    and a consumer that is not part of this slice."""
    if not isinstance(policy, dict) or policy.get('enabled') is not True:
        return False
    for item in policy.get('entries', []):
        if (isinstance(item, dict) and item.get('kind') == kind and item.get('label') == label
                and type(deadline_multiple) in (int, float) and deadline_multiple >= item.get('deadline_multiple', float('inf'))):
            return True
    return False
