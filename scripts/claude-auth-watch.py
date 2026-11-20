#!/usr/bin/env python3
"""Claude Code sign-in interruption detector, single-owner alert and post-sign-in reconciliation (SPE-176, ADR 0020).

OWNERSHIP CONTRACT. This script is the ONLY detector and the ONLY sender of Claude Code sign-in alerts. session-notify is its
sender transport (the merged Telegram backend plus `emit-resource` dedup and `--clear`). The engineering-supervisor watchdog and
the admission monitor must NOT detect or alert on authentication; hosting this script from the supervisor is an owner decision
(docs/adr/0020-claude-signin-alert-ownership.md).

  claude-auth-watch.py scan [--json]                 read-only, closed-schema classification of every affected session
  claude-auth-watch.py tick [--send] [--if-due]      scan; with --send, ONE deduplicated notice per incident, ONE on recovery
  claude-auth-watch.py status                        last tick result, read-only
  claude-auth-watch.py reconcile --since EPOCH       read-only post-sign-in report: survivors, results, resumed work, duplicates
  claude-auth-watch.py plist --checkout DIR          print the launchd agent; installs nothing

It never logs in or out, never calls a model, never signals a process, never deletes, never mutates the queue, and never prints an
email, account id, token or command line. Evidence comes only from structured fields: the `claude auth status` JSON key `loggedIn`,
transcript records with `isApiErrorMessage` and `error == "authentication_failed"`, successful assistant records, and the Remote
Control system records. A Remote Control "OAuth token unavailable" line is auth evidence only when corroborated by a native
authentication_failed record or by a logged-out status.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import argparse
import calendar
import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
INTERVAL_S = 60
GAP_FACTOR = 3
WINDOW_S = 48 * 3600          # a transcript untouched for longer is not read; an incident still open after this long closes silently
FRESH_S = 1800                # a failure, and its transcript file, must be at most this old to open an incident (or to be one cluster)
STRONG_FRESH_S = 600          # the only failures that can override a definite logged-in status
SKEW_S = 300                  # a record or file newer than now + this is dropped as future-dated, never sorted after real later evidence
REDRIVE_S = 1800              # a notice that is pending or was dropped by the transport is re-driven at this spacing
MAX_SENDS = 7                 # hand-offs per notice: the first plus at most 6 re-drives
REFOLD_S = 1800               # a failure this soon after an incident closed is folded into it ('recurred'), not a new incident
RATE_WINDOW_S = 6 * 3600
RATE_CAP = 3                  # recovery and Remote Control notices per rolling window (dropped and counted beyond it); alerts are never under this cap
ALERT_SPACING_S = 1800        # at most one NEW incident alert per this long; a deferred alert goes out as soon as the spacing allows
ALERT_MAX = 12                # hard ceiling of alerts per RATE_WINDOW_S
REFAIL_NEW_S = 600            # a failure that persists this long after a delivered recovery is a new incident, not a fold
RETRY_LOW_S = 3600            # steady re-drive rate of an undelivered alert after the first MAX_SENDS fast hand-offs (no hard cap while the incident is active)
AMBIG_S = 6 * 3600            # after the fast hand-offs, an AMBIGUOUS (unknown or pending) alert outcome is re-driven this slowly: at most 4 duplicates a day
MAX_MISSED = 99
CLOSED_EPOCHS = 16            # closed incident epochs remembered so the same incident never reopens
QUIET_RECOVER_S = 900         # logged in and no fresh failure for this long closes an incident without waiting for a resumed turn
MAX_FILES = 400               # newest transcripts only
MAX_CANDIDATES = 20000        # stat calls while listing; beyond this the listing is marked truncated
TAIL_BYTES = 256 * 1024       # only the tail window of each transcript is read
MAX_LINE = 64 * 1024
MAX_EVENTS = 2000
SCAN_BUDGET_S = 20.0
STATUS_TIMEOUT_S = 15
REGISTRY_FILES = 200
REGISTRY_BYTES = 16 * 1024
NAME_MAX = 48
NAMES_LISTED = 6
DETAIL_MAX = 160
STATE_LIMIT = 64 * 1024
SIDS_MAX = 64
RETRY_S = 300                 # a notice nobody could take (no backend, failed) is retried at this spacing
UNVERIFIED_AFTER_DEDUPS = 3   # `deduped` with an unknown outcome this many times: stop asking, record it unverified
RC_COOLDOWN_S = 6 * 3600      # a Remote Control incident is announced at most this often
RECEIPT_TAIL = 1 << 20
SURVIVORS_LISTED = 24

CLASSES = ('auth-required', 'remote-control-disconnect', 'none', 'unknown')
EVIDENCE = ('auth-failed', 'rc-oauth-unavailable', 'rc-session-creation-failed', 'idle', 'none')
STATUSES = ('logged-in', 'logged-out', 'unknown')
CORROBORATION = ('status-logged-out', 'multiple-sessions', 'rc-oauth-unavailable')
VERDICTS = ('PASS', 'WARN', 'FAIL', 'NOT_VERIFIED')
HARD_KINDS = ('live-fingerprint',)
WARN_KINDS = ('live-label', 'live-ticket-worktree', 're-enqueued-after-release', 'enqueued-repeated')
LIVE = ('QUEUED', 'ELIGIBLE', 'GRANTED', 'CLAIMED')
RC_AUTH_TEXT = 'oauth token unavailable'
RC_OTHER_TEXT = 'session creation failed'
NAME_RE = re.compile(r'[A-Za-z0-9 ._()-]{1,48}')
HOST_RE = re.compile(r'[A-Za-z0-9._-]{1,24}')
URLISH = re.compile(r'(?i)(https?|www\.|\b[a-z0-9-]+\.(com|net|org|io|dev|app|ai|co|me|xyz|sh|info|ru|cn)\b)')
LABEL_OK = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,63}')
EVIDENCE_OK = re.compile(r'[a-z0-9-]{1,40}')
TS_RE = re.compile(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?')


def load(name):
    spec = spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def parse_ts(text):
    """Epoch seconds of an ISO-8601 timestamp, or None. Hand-parsed: Python 3.9's fromisoformat rejects 'Z' and long fractions."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    match = TS_RE.fullmatch(text)
    if not match:
        return None
    try:
        y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
        if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 61 and 2000 <= y < 2100):
            return None
        epoch = calendar.timegm((y, mo, d, h, mi, s))
    except (ValueError, OverflowError):
        return None
    zone = match.group(7)
    if zone and zone != 'Z':
        sign = 1 if zone[0] == '+' else -1
        digits = zone[1:].replace(':', '')
        epoch -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return float(epoch)


def digest(*parts, n=16):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:n]


def sanitize_name(value, fallback):
    """A session name that is safe to show in a notice: fixed charset, bounded, never URL-shaped. Anything else becomes `fallback`."""
    if not isinstance(value, str) or not value.isprintable():
        return fallback
    text = ' '.join(value.split())
    if not NAME_RE.fullmatch(text) or URLISH.search(text) or not re.search(r'[A-Za-z0-9]', text):
        return fallback
    return text


def short_host(raw=None):
    name = raw if raw is not None else socket.gethostname()
    name = str(name).split('.')[0]
    return name if HOST_RE.fullmatch(name) else 'host'


def read_small(path, limit):
    """Bytes of a regular, non-symlink file, at most `limit`; None when unreadable or not a regular file."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit + 1)[:limit + 1]
    except OSError:
        return None
    finally:
        os.close(fd)


# ---- transcript scan (read-only, bounded) ----------------------------------------------------------------------------------------

def list_transcripts(root, now):
    """Newest `.jsonl` files two levels under `root`, symlinks never followed. Returns (list of (mtime, path), truncated, future_files).
    A file whose mtime is later than now + SKEW_S is dropped and counted."""
    found, seen, truncated, future = [], 0, False, 0
    try:
        top = list(os.scandir(str(root)))
    except OSError:
        return [], False, 0
    for proj in top:
        try:
            if proj.is_symlink() or not proj.is_dir(follow_symlinks=False):
                continue
            inner = list(os.scandir(proj.path))
        except OSError:
            continue
        for entry in inner:
            if not entry.name.endswith('.jsonl'):
                continue
            seen += 1
            if seen > MAX_CANDIDATES:
                return _newest(found), True, future
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime > now + SKEW_S:
                future += 1
            elif mtime >= now - WINDOW_S:
                found.append((mtime, entry.path))
    return _newest(found), truncated, future


def _newest(found):
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:MAX_FILES]


def tail_lines(path):
    """Complete lines from the last TAIL_BYTES of a regular file; lines over MAX_LINE are skipped, never parsed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return []
        start = max(0, info.st_size - TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, TAIL_BYTES)
    except OSError:
        return []
    finally:
        os.close(fd)
    lines = data.split(b'\n')
    if start > 0 and lines:
        lines = lines[1:]            # the first line of a mid-file window is a fragment
    return [line for line in lines if 0 < len(line) <= MAX_LINE]


def classify_record(rec):
    """kind of one transcript record from structured fields only: fail, ok, rc_oauth, rc_other, or None."""
    if not isinstance(rec, dict):
        return None
    kind = rec.get('type')
    if rec.get('isApiErrorMessage') is True:
        return 'fail' if rec.get('error') == 'authentication_failed' else None
    if 'error' in rec:
        return None
    if kind == 'assistant':
        return 'ok'
    if kind == 'system':
        text = rec.get('content')
        if isinstance(text, str) and len(text) < 2000 and 'remote control disconnected' in text.lower():
            low = text.lower()
            if RC_AUTH_TEXT in low:
                return 'rc_oauth'
            if RC_OTHER_TEXT in low:
                return 'rc_other'
    return None


def file_events(path, now):
    """[(sid, ts, kind)] and the number of future-dated records dropped (ts later than now + SKEW_S)."""
    out, future = [], 0
    stem = Path(path).stem
    for line in tail_lines(path):
        if not (b'"assistant"' in line or b'authentication_failed' in line or b'emote' in line):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        kind = classify_record(rec)
        if kind is None:
            continue
        ts = parse_ts(rec.get('timestamp'))
        if ts is None:
            continue
        if ts > now + SKEW_S:
            future += 1
            continue
        sid = rec.get('sessionId')
        out.append((sid if isinstance(sid, str) and 0 < len(sid) <= 80 else stem, ts, kind))
    return out, future


def scan_transcripts(root, now, mono):
    """({sid: [(ts, kind)]}, files_scanned, truncated, {sid: newest file mtime}, future_dropped) over the newest transcripts within the time budget."""
    files, truncated, future = list_transcripts(root, now)
    deadline = mono() + SCAN_BUDGET_S
    events, mtimes, scanned = {}, {}, 0
    for mtime, path in files:
        if mono() > deadline:
            truncated = True
            break
        scanned += 1
        found, dropped = file_events(path, now)
        future += dropped
        for sid, ts, kind in found:
            bucket = events.setdefault(sid, [])
            mtimes[sid] = max(mtimes.get(sid, 0.0), mtime)
            if len(bucket) < MAX_EVENTS:
                bucket.append((ts, kind))
    return events, scanned, truncated, mtimes, future


# ---- sources (injectable) ---------------------------------------------------------------------------------------------------------

def claude_status():
    """'logged-in', 'logged-out' or 'unknown' from `claude auth status` JSON (key `loggedIn` only). Output is never echoed or stored."""
    home = Path(os.environ.get('HOME', ''))
    candidates = [os.environ.get('CLAUDE_AUTH_WATCH_CLAUDE_BIN', ''), str(home / '.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude']
    binary = next((c for c in candidates if c and os.access(c, os.X_OK)), None)
    if not binary:
        return 'unknown'
    try:
        done = subprocess.run([binary, 'auth', 'status'], capture_output=True, text=True, timeout=STATUS_TIMEOUT_S, stdin=subprocess.DEVNULL)
        data = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 'unknown'
    if isinstance(data, dict) and data.get('loggedIn') is True:
        return 'logged-in'
    if isinstance(data, dict) and data.get('loggedIn') is False:
        return 'logged-out'
    return 'unknown'


def read_registry(directory):
    """[{'sid','name','pid'}] from `<directory>/*.json`, or None when the directory cannot be read. Only these three fields are kept."""
    try:
        names = sorted(n for n in os.listdir(str(directory)) if n.endswith('.json'))[:REGISTRY_FILES]
    except OSError:
        return None
    out = []
    for name in names:
        raw = read_small(Path(directory) / name, REGISTRY_BYTES)
        try:
            data = json.loads(raw) if raw is not None and len(raw) <= REGISTRY_BYTES else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get('sessionId'), str):
            continue
        pid = data.get('pid')
        out.append({'sid': data['sessionId'][:80], 'name': data.get('name') if isinstance(data.get('name'), str) else None,
                    'pid': pid if type(pid) is int and pid > 0 else None})
    return out


def pid_alive(pid):
    """True/False from `ps -p` (no signal is sent, no command line read); None when ps cannot answer."""
    try:
        done = subprocess.run(['/bin/ps', '-p', str(int(pid)), '-o', 'pid='], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return bool(done.stdout.strip())


def read_receipts(queue_dir):
    """Receipt dicts from receipts.jsonl.1 and receipts.jsonl (tail of each, symlinks refused). Never mutates."""
    out = []
    for name in ('receipts.jsonl.1', 'receipts.jsonl'):
        path = Path(queue_dir) / name
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                continue
            os.lseek(fd, max(0, info.st_size - RECEIPT_TAIL), os.SEEK_SET)
            data = os.read(fd, RECEIPT_TAIL)
        except OSError:
            continue
        finally:
            os.close(fd)
        for line in data.split(b'\n'):
            if 0 < len(line) <= 4096:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    return out


class Sources:
    """Everything the watch reads, each injectable. `home_defaults` wires the real read-only readers under HOME."""

    def __init__(self, projects_dir, sessions_dir, status=claude_status, registry=None, alive=pid_alive, queue=None, receipts=None, host=None):
        self.projects_dir, self.sessions_dir = Path(projects_dir), Path(sessions_dir)
        self.status = status
        self.registry = registry or (lambda: read_registry(self.sessions_dir))
        self.alive = alive
        self.queue = queue or self._real_queue
        self.receipts = receipts or self._real_receipts
        self.host = short_host(host)

    @staticmethod
    def _real_queue():
        da = load('disk-admission')
        entries, problems = da._queue_entries(da.default_state_path())
        return entries, problems

    @staticmethod
    def _real_receipts():
        da = load('disk-admission')
        return read_receipts(da.queue_dir(da.default_state_path()))

    @classmethod
    def home_defaults(cls):
        home = Path(os.environ.get('HOME') or str(Path.home()))
        return cls(home / '.claude/projects', home / '.claude/sessions')


# ---- analysis ---------------------------------------------------------------------------------------------------------------------

def analyze(src, now, mono=time.monotonic):
    """Per-session state from the transcripts and registry. {'sessions': {sid: view}, 'scanned', 'truncated'}.

    A session's failure is 'unrecovered' from its FIRST relevant event after its last successful assistant record."""
    events, scanned, truncated, mtimes, future = scan_transcripts(src.projects_dir, now, mono)
    try:
        registry = src.registry()
    except Exception:  # noqa: BLE001 - an injected or real reader failing means "unknown", never a crash
        registry = None
    by_sid = {r['sid']: r for r in registry} if registry is not None else None
    views = {}
    for sid, evs in events.items():
        last_ok = native = native_last = oauth = oauth_last = other = other_last = None
        unrec = {'fail': [], 'rc_oauth': [], 'rc_other': []}
        for ts, kind in sorted(evs):
            if kind == 'ok':
                last_ok = ts
                native = native_last = oauth = oauth_last = other = other_last = None
                unrec = {'fail': [], 'rc_oauth': [], 'rc_other': []}
                continue
            unrec[kind].append(ts)
            if kind == 'fail':
                native, native_last = (native if native is not None else ts), ts
            elif kind == 'rc_oauth':
                oauth, oauth_last = (oauth if oauth is not None else ts), ts
            elif kind == 'rc_other':
                other, other_last = (other if other is not None else ts), ts
        first_fresh = {k: next((t for t in v if t >= now - FRESH_S), None) for k, v in unrec.items()}
        key = digest(sid, n=12)
        reg = by_sid.get(sid) if by_sid is not None else None
        gone = by_sid is not None and (reg is None or (reg['pid'] is not None and src.alive(reg['pid']) is False))
        views[sid] = {'hash': key, 'name': sanitize_name(reg['name'] if reg else None, 'session-' + key[:4]), 'last_ok': last_ok,
                      'native': native, 'native_last': native_last, 'oauth': oauth, 'oauth_last': oauth_last, 'other': other,
                      'other_last': other_last, 'fresh_first': first_fresh, 'mtime': mtimes.get(sid, 0.0), 'gone': gone, 'events': sorted(evs)}
    return {'sessions': views, 'scanned': scanned, 'truncated': truncated, 'future_ts': future}


def get_status(src):
    try:
        value = src.status()
    except Exception:  # noqa: BLE001 - a failing/timing-out/garbled status is "unknown", never an alert
        return 'unknown'
    return value if value in STATUSES else 'unknown'


def _fresh(view, key, now):
    """The newest unrecovered record of this kind is at most FRESH_S old AND so is its transcript file: a stale or idle session never opens an incident."""
    last = view[key + '_last']
    return last is not None and now - last <= FRESH_S and now - view['mtime'] <= FRESH_S


def decide(analysis, status, now):
    """(verdict, epoch, corroboration list, {sid: (class, evidence)}) from the analysis, the auth status and the clock.

    Opening rules (only fresh evidence counts; registry state never makes a stale transcript live):
      status logged-out : any fresh native failure, or a fresh RC "OAuth token unavailable".
      status unknown    : a fresh native failure PLUS a fresh RC "OAuth token unavailable"; several sessions alone are not enough.
      status logged-in  : vetoes, unless a failure <= STRONG_FRESH_S old with no later success exists AND (>= 2 sessions in one cluster or an RC corroboration)."""
    live = {sid: v for sid, v in analysis['sessions'].items() if not v['gone']}
    fresh_native = {sid: v for sid, v in live.items() if _fresh(v, 'native', now)}
    fresh_oauth = {sid: v for sid, v in live.items() if _fresh(v, 'oauth', now)}
    fresh_other = {sid: v for sid, v in live.items() if _fresh(v, 'other', now)}
    verdict, epoch, corr = 'none', None, []
    if fresh_native:
        epoch = min(v['fresh_first']['fail'] for v in fresh_native.values())     # first FRESH unrecovered failure, not an old one from a continuous run
        lasts = [v['native_last'] for v in fresh_native.values()]
        cluster = len(lasts) >= 2 and max(lasts) - min(lasts) <= FRESH_S
        rc = any(v['oauth_last'] >= epoch for v in fresh_oauth.values())
        strong = any(now - last <= STRONG_FRESH_S for last in lasts)
        opens = False
        if status == 'logged-out':
            opens, corr = True, ['status-logged-out']
        elif status == 'logged-in':
            opens = strong and (cluster or rc)
        else:
            opens = rc
        if opens:
            corr += (['multiple-sessions'] if cluster else []) + (['rc-oauth-unavailable'] if rc else [])
            verdict = 'auth-required'
    elif fresh_oauth and status == 'logged-out':
        epoch, corr, verdict = min(v['fresh_first']['rc_oauth'] for v in fresh_oauth.values()), ['status-logged-out'], 'auth-required'
    if verdict != 'auth-required':
        epoch = None
        if fresh_other:
            verdict, epoch = 'remote-control-disconnect', min(v['fresh_first']['rc_other'] for v in fresh_other.values())
        elif fresh_native or fresh_oauth:
            verdict = 'unknown'
    classes = {}
    for sid, v in live.items():
        if sid in fresh_native or sid in fresh_oauth:
            classes[sid] = ('auth-required' if verdict == 'auth-required' else 'unknown', 'auth-failed' if sid in fresh_native else 'rc-oauth-unavailable')
        elif sid in fresh_other:
            classes[sid] = ('remote-control-disconnect', 'rc-session-creation-failed')
        elif v['native'] is not None or v['oauth'] is not None or v['other'] is not None:
            classes[sid] = ('none', 'idle')      # unrecovered evidence that is no longer fresh: listed, never an incident
    return verdict, (int(epoch) if epoch is not None else None), corr, classes


def render_scan(analysis, status, now, host):
    verdict, epoch, corr, classes = decide(analysis, status, now)
    rows = sorted(({'name': analysis['sessions'][sid]['name'], 'class': c, 'evidence': e, 'since': iso(
        analysis['sessions'][sid]['native'] or analysis['sessions'][sid]['oauth'] or analysis['sessions'][sid]['other'])}
        for sid, (c, e) in classes.items()), key=lambda r: (CLASSES.index(r['class']), r['name']))
    return {'schema': 1, 'at': iso(now), 'host': host, 'verdict': verdict, 'status': status, 'corroboration': corr,
            'incident_epoch': epoch, 'affected': len(rows), 'sessions': rows[:NAMES_LISTED], 'sessions_more': max(0, len(rows) - NAMES_LISTED),
            'files_scanned': analysis['scanned'], 'truncated': analysis['truncated'], 'future_ts_dropped': analysis['future_ts']}


def scan(src, now=None, mono=time.monotonic):
    """Read-only closed-schema scan. The auth status is consulted only when a session shows auth evidence."""
    now = time.time() if now is None else now
    analysis = analyze(src, now, mono)
    suspicious = any(_fresh(v, k, now) for v in analysis['sessions'].values() if not v['gone'] for k in ('native', 'oauth'))
    status = get_status(src) if suspicious else 'unknown'
    return render_scan(analysis, status, now, src.host)


# ---- notices and state ------------------------------------------------------------------------------------------------------------

def parse_emit(stdout):
    seen = re.search(r'emit-resource: (queued|deduped|no-backend|cleared)(?: delivery=(sent|pending|dropped|unknown))?', stdout or '')
    return (seen.group(1), seen.group(2)) if seen else (None, None)


class Notifier:
    """session-notify as the transport: `emit-resource --strict` for a notice, `--clear --scope auth` at the end of an incident."""

    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def _run(self, argv):
        try:
            done = self.runner([sys.executable, str(HERE / 'session-notify.py'), 'emit-resource'] + argv, capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return done

    def emit(self, cause, recovery, action, signature, detail):
        argv = ['--cause', cause, '--recovery', recovery, '--owner-action', action, '--signature', signature, '--strict']
        if detail:
            argv += ['--detail', detail]
        done = self._run(argv)
        result, delivery = parse_emit(getattr(done, 'stdout', '') if done is not None else '')
        if done is None or done.returncode != 0 or result is None:
            return {'result': 'failed', 'delivery': None}
        return {'result': result, 'delivery': delivery}

    def clear(self):
        done = self._run(['--clear', '--scope', 'auth'])
        return done is not None and done.returncode == 0


def fnum(x, lo=0.0, hi=4.2e9):
    """A finite float within [lo, hi] from an int or float (never a bool, NaN, infinity or an out-of-range or huge integer), else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        return float(x) if lo <= x <= hi else None
    except OverflowError:
        return None


def inum(x, lo=0, hi=10 ** 9):
    return x if type(x) is int and lo <= x <= hi else None


def new_notice():
    return {'handed_off': False, 'delivered': False, 'pending': False, 'dropped': False, 'unverified': False, 'result': 'none',
            'attempts': 0, 'sends': 0, 'unknown_dedups': 0, 'needs_clear': False, 'next_try': 0.0}


def clean_notice(raw):
    base = new_notice()
    if not isinstance(raw, dict):
        return base
    for key in ('handed_off', 'delivered', 'pending', 'dropped', 'unverified', 'needs_clear'):
        if isinstance(raw.get(key), bool):
            base[key] = raw[key]
    for key in ('attempts', 'sends', 'unknown_dedups'):
        if inum(raw.get(key), 0, 10 ** 6) is not None:
            base[key] = raw[key]
    nt = fnum(raw.get('next_try'))
    if nt is not None:
        base['next_try'] = nt
    if raw.get('result') in ('none', 'queued', 'deduped', 'no-backend', 'failed'):
        base['result'] = raw['result']
    return base


def notice_due(rec, now):
    """True while the notice still needs the transport: never handed off, or handed off and pending, and its retry time has come."""
    if rec['delivered'] or rec['dropped'] or rec['unverified']:
        return False
    return now >= rec['next_try']


def attempt(notifier, rec, args, now, persistent=False):
    """One hand-off to session-notify. A pending, dropped or unknown-outcome notice is re-armed (`--clear --scope auth`, then a fresh emit) and re-driven. A
    `persistent` notice (an alert) is never abandoned while its incident is open: MAX_SENDS fast hand-offs at REDRIVE_S, then one every RETRY_LOW_S."""
    if rec['needs_clear']:
        notifier.clear()                     # re-arms this incident's dedup key only (the auth session), so an unconfirmed alert can be emitted again
        rec['needs_clear'] = False
    res = notifier.emit(*args)
    rec['attempts'] += 1
    result, delivery = res['result'], res['delivery']
    rec['result'] = result if result in ('queued', 'deduped', 'no-backend', 'failed') else 'failed'
    if result in ('queued', 'deduped'):
        rec['handed_off'] = True
        rec['sends'] += 1
        if delivery == 'sent':
            rec['delivered'], rec['pending'] = True, False
        else:
            rec['pending'] = True
            if persistent:
                rec['needs_clear'] = delivery != 'pending'        # dropped or unknown: the dedup tombstone must go before the next emit
                rec['next_try'] = now + (REDRIVE_S if rec['sends'] < MAX_SENDS else RETRY_LOW_S if delivery == 'dropped' else AMBIG_S)
            else:
                if delivery == 'dropped':
                    rec['needs_clear'] = True
                elif delivery != 'pending':       # outcome unknown (an earlier call finished it): never claim delivered
                    rec['unknown_dedups'] += 1
                    rec['unverified'] = rec['unknown_dedups'] >= UNVERIFIED_AFTER_DEDUPS
                rec['next_try'] = now + REDRIVE_S
                if rec['sends'] >= MAX_SENDS:
                    rec['pending'], rec['dropped'] = False, delivery == 'dropped'
                    rec['unverified'] = rec['unverified'] or delivery != 'dropped'
    else:
        rec['next_try'] = now + RETRY_S
    return {'kind': args[0], 'result': rec['result'], 'handed_off': rec['handed_off'], 'delivered': rec['delivered'], 'pending': rec['pending']}


def _send(state, notifier, kind, rec, args, now):
    """`attempt` behind the notice policy. ALERTS are never under the rolling cap: at most one NEW alert per ALERT_SPACING_S (and ALERT_MAX per window); a
    spaced-out alert is deferred and retried the moment the spacing allows. RECOVERY and Remote Control notices share RATE_CAP per RATE_WINDOW_S and are dropped
    and counted beyond it (`rate`)."""
    log = [e for e in state['sent_log'] if 0 <= now - e[0] < RATE_WINDOW_S]
    state['sent_log'] = log
    if not rec['handed_off']:
        if kind == 'auth-required':
            stamps = [e[0] for e in log if e[1] == 'auth-required']
            wait = max([0.0] + [t + ALERT_SPACING_S - now for t in stamps])
            if len(stamps) >= ALERT_MAX:
                wait = max(wait, min(stamps) + RATE_WINDOW_S - now)
            if wait > 0:
                rec['next_try'] = now + wait
                return {'kind': kind, 'result': 'deferred', 'handed_off': False, 'delivered': False, 'pending': False}
        elif sum(1 for e in log if e[1] != 'auth-required') >= RATE_CAP:
            state['rate']['suppressed'] += 1
            state['rate']['last'] = now
            return {'kind': kind, 'result': 'rate-limited', 'handed_off': False, 'delivered': False, 'pending': False}
    was = rec['handed_off']
    res = attempt(notifier, rec, args, now, persistent=(kind == 'auth-required'))
    if rec['handed_off'] and not was:
        state['sent_log'] = (log + [[now, kind]])[-64:]
        if kind == 'auth-required':
            state['missed'] = 0                      # the detail of this alert carried the count of earlier unannounced outages
    return res


def detail_text(host, names, more=0, missed=0, inc=''):
    """`host=H names=a,b +N more` within the closed AUTH_DETAIL charset and DETAIL_MAX characters (names are dropped to fit)."""
    names = list(names)
    while True:
        text = 'host=%s names=%s' % (host, ','.join(names)) + (' +%d more' % more if more else '') + (' missed=%d' % missed if missed else '') + (' inc=%s' % inc[:6] if inc else '')
        if len(text) <= DETAIL_MAX or not names:
            return text[:DETAIL_MAX]
        names.pop()
        more += 1


def empty_state():
    return {'schema': 1, 'last_tick': None, 'gaps': 0, 'sent_log': [], 'missed': 0, 'rate': {'suppressed': 0, 'last': 0.0},
            'closed': [],
            'auth': {'active': False, 'phase': 'none', 'epoch': 0, 'key': '', 'started': 0.0, 'last_fresh': 0.0, 'clean_since': 0.0, 'closed_at': 0.0, 'recurred': 0, 'fold_pending': False, 'recurred_at': 0.0, 'sids': {}, 'names': [], 'more': 0, 'alert': new_notice(), 'recovery': new_notice()},
            'rc': {'active': False, 'epoch': 0, 'key': '', 'last_start': 0.0, 'names': [], 'more': 0, 'notice': new_notice()}, 'last': None}


def clean_names(raw):
    out = []
    if isinstance(raw, list):
        for item in raw[:NAMES_LISTED]:
            if isinstance(item, str) and NAME_RE.fullmatch(item) and not URLISH.search(item):
                out.append(item)
    return out


def sanitize_state(raw):
    """Every persisted value is type-checked and range-checked here: finite, within a sane epoch range, no bool-as-number, no huge integer. Never raises."""
    state = empty_state()
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return state, True
    last = raw.get('last_tick')
    if isinstance(last, dict):
        wall, mono = fnum(last.get('wall')), fnum(last.get('mono'), 0.0, 1e13)
        if wall is not None and mono is not None:
            state['last_tick'] = {'wall': wall, 'mono': mono, 'boot': inum(last.get('boot'), 0, 4 * 10 ** 9) or 0}
    state['gaps'] = inum(raw.get('gaps'), 0, 10 ** 6) or 0
    state['missed'] = inum(raw.get('missed'), 0, MAX_MISSED) or 0
    log = raw.get('sent_log')
    if isinstance(log, list):
        kinds = ('auth-required', 'signin-recovered', 'remote-control-disconnect')
        state['sent_log'] = [[fnum(e[0]), e[1]] for e in log[-64:] if isinstance(e, list) and len(e) == 2 and fnum(e[0]) is not None and e[1] in kinds]
    rate = raw.get('rate')
    if isinstance(rate, dict):
        state['rate']['suppressed'] = inum(rate.get('suppressed'), 0, 10 ** 9) or 0
        state['rate']['last'] = fnum(rate.get('last')) or 0.0
    if isinstance(raw.get('closed'), list):
        state['closed'] = [e for e in raw['closed'][-CLOSED_EPOCHS:] if inum(e, 1, 4 * 10 ** 9) is not None]
    auth = raw.get('auth')
    if isinstance(auth, dict):
        a = state['auth']
        a['active'] = auth.get('active') is True
        a['phase'] = auth.get('phase') if auth.get('phase') in ('none', 'alerting', 'recovered') else 'none'
        a['epoch'] = inum(auth.get('epoch'), 0, 4 * 10 ** 9) or 0
        for k in ('started', 'last_fresh', 'clean_since', 'closed_at', 'recurred_at'):
            a[k] = fnum(auth.get(k)) or 0.0
        a['fold_pending'] = auth.get('fold_pending') is True
        a['key'] = auth['key'] if isinstance(auth.get('key'), str) and re.fullmatch(r'[0-9a-f]{16}', auth['key']) else ''
        sids = auth.get('sids')
        if isinstance(sids, dict):
            a['sids'] = {k: v for k, v in list(sids.items())[:SIDS_MAX] if isinstance(k, str) and re.fullmatch(r'[0-9a-f]{12}', k) and v in ('native', 'oauth')}
        a['recurred'] = inum(auth.get('recurred'), 0, 10 ** 6) or 0
        a['names'], a['more'] = clean_names(auth.get('names')), inum(auth.get('more'), 0, 10 ** 6) or 0
        a['alert'], a['recovery'] = clean_notice(auth.get('alert')), clean_notice(auth.get('recovery'))
        if a['active'] and (not (a['key'] and a['epoch']) or a['phase'] not in ('alerting', 'recovered')):     # a wedged record (garbage phase) is reset, never trusted
            state['auth'] = empty_state()['auth']
    rc = raw.get('rc')
    if isinstance(rc, dict):
        r = state['rc']
        r['active'] = rc.get('active') is True
        r['epoch'] = inum(rc.get('epoch'), 0, 4 * 10 ** 9) or 0
        r['key'] = rc['key'] if isinstance(rc.get('key'), str) and re.fullmatch(r'[0-9a-f]{16}', rc['key']) else ''
        r['last_start'] = fnum(rc.get('last_start')) or 0.0
        r['names'], r['more'] = clean_names(rc.get('names')), inum(rc.get('more'), 0, 10 ** 6) or 0
        r['notice'] = clean_notice(rc.get('notice'))
        if r['active'] and not (r['key'] and r['epoch']):
            state['rc'] = empty_state()['rc']
    if isinstance(raw.get('last'), dict):
        keep = ('at', 'verdict', 'status', 'auth', 'rc', 'sent', 'rate_limited', 'rate_limited_at', 'outage_unannounced')
        state['last'] = {k: v for k, v in raw['last'].items() if k in keep and (isinstance(v, (str, bool, type(None))) or inum(v, 0, 10 ** 9) is not None)
                         and (not isinstance(v, str) or len(v) <= 64)}
    return state, False


def clamp_state(state, now):
    """Schedule times read from disk can never push work out of reach: a retry time is at most RETRY_LOW_S ahead, any recorded past time is at most now."""
    for rec in (state['auth']['alert'], state['auth']['recovery'], state['rc']['notice']):
        rec['next_try'] = min(rec['next_try'], now + RETRY_LOW_S)
    for holder, keys in ((state['auth'], ('started', 'last_fresh', 'clean_since', 'closed_at', 'recurred_at')), (state['rc'], ('last_start',)), (state['rate'], ('last',))):
        for k in keys:
            holder[k] = min(holder[k], now)


def watch_dir(override=None):
    value = override or os.environ.get('CLAUDE_AUTH_WATCH_DIR')
    if value:
        return Path(value)
    return Path(os.environ.get('HOME') or str(Path.home())) / '.cache/coding-agent-environment/claude-auth-watch'


def write_state(path, state):
    text = json.dumps(state, sort_keys=True)
    if len(text) > STATE_LIMIT:                 # bounded: drop the optional detail before refusing to persist
        state['last'] = None
        text = json.dumps(state, sort_keys=True)
    tmp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.replace(str(tmp), str(path))


class Env:
    """Clock, source readers, transport and state directory of one tick; every one is injectable."""

    def __init__(self, sources, notifier=None, clock=time.time, mono=time.monotonic, boot=None, directory=None):
        self.sources, self.notifier = sources, notifier or Notifier()
        self.clock, self.mono, self.boot = clock, mono, boot or boot_time
        self.directory = watch_dir(directory)


def boot_time():
    try:
        out = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
        return int(re.search(r'sec = (\d+)', out).group(1))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return 0


def tick(env, send=False, if_due=False):
    """One idempotent pass. Returns the result dict (also persisted as status). Without `send` it observes and never changes incident state."""
    directory = env.directory
    if directory.is_symlink():
        return {'ran': False, 'why': 'watch directory is a symlink'}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(str(directory), 0o700)
    if os.environ.get('CLAUDE_AUTH_WATCH_DISABLED') == '1' or (directory / 'DISABLED').exists():
        return {'ran': False, 'why': 'kill switch'}
    try:
        lock_fd = os.open(str(directory / 'watch.lock'), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return {'ran': False, 'why': 'lock unavailable'}
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        return {'ran': False, 'why': 'another tick holds the lock'}
    try:
        return _tick_locked(env, directory, send, if_due)
    finally:
        os.close(lock_fd)


def _tick_locked(env, directory, send, if_due):
    now, mono, boot = env.clock(), env.mono(), env.boot()
    path = directory / 'state.json'
    raw = read_small(path, STATE_LIMIT)
    try:
        parsed = json.loads(raw) if raw is not None and len(raw) <= STATE_LIMIT else None
    except ValueError:
        parsed = None
    state, reset = sanitize_state(parsed)
    clamp_state(state, now)
    last = state['last_tick']
    if if_due and last and 0 <= now - last['wall'] < INTERVAL_S:
        return {'ran': False, 'why': 'not due'}
    gap = False
    if last:
        if boot and last['boot'] and boot != last['boot']:
            gap = True
        elif now - last['wall'] > INTERVAL_S * GAP_FACTOR or (now - last['wall']) - (mono - last['mono']) > 60 or now < last['wall']:
            gap = True
    if gap:
        # A gap (sleep, reboot, clock step, stopped agent) is not evidence of anything: stored times are clamped to now and retries become due.
        state['gaps'] = min(state['gaps'] + 1, 10 ** 6)
        for rec in (state['auth']['alert'], state['auth']['recovery'], state['rc']['notice']):
            rec['next_try'] = 0.0
        state['rc']['last_start'] = min(state['rc']['last_start'], now)
    src = env.sources
    analysis = analyze(src, now, env.mono)
    live = [v for v in analysis['sessions'].values() if not v['gone']]
    needs_status = state['auth']['active'] or any(_fresh(v, k, now) for v in live for k in ('native', 'oauth'))
    status = get_status(src) if needs_status else 'unknown'
    verdict, epoch, corr, classes = decide(analysis, status, now)
    out = {'ran': True, 'at': iso(now), 'verdict': verdict, 'status': status, 'gap': gap, 'state_reset': reset, 'sent': False, 'notices': []}
    host = src.host
    names_of = lambda c: sorted({analysis['sessions'][sid]['name'] for sid, (cl, _) in classes.items() if cl == c})  # noqa: E731
    auth, rc = state['auth'], state['rc']
    if send:
        _drive_auth(env, state, analysis, verdict, epoch, classes, status, host, names_of, now, out)
        if not state['auth']['active']:
            _drive_rc(env, state, verdict, epoch, host, names_of, now, out)
    elif verdict in ('auth-required', 'remote-control-disconnect') and not (auth['active'] if verdict == 'auth-required' else rc['active']):
        out['notices'].append({'kind': verdict, 'result': 'would-send', 'handed_off': False, 'delivered': False, 'pending': False})
    auth = state['auth']      # a closed incident replaces the dict
    out['auth'] = out.pop('auth_note', None) or ('recovering' if auth['phase'] == 'recovered' else ('active' if auth['active'] else 'inactive'))
    out['rc'] = 'active' if rc['active'] else 'inactive'
    out['sent'] = any(n['result'] in ('queued', 'deduped') for n in out['notices'])
    state['last_tick'] = {'wall': now, 'mono': mono, 'boot': boot}
    state['last'] = {'at': out['at'], 'verdict': verdict, 'status': status, 'auth': out['auth'], 'rc': out['rc'], 'sent': out['sent'],
                     'rate_limited': state['rate']['suppressed'], 'rate_limited_at': iso(state['rate']['last']) if state['rate']['last'] else None,
                     'outage_unannounced': state['missed']}
    out['rate_limited'], out['outage_unannounced'] = state['rate']['suppressed'], state['missed']
    write_state(path, state)
    return out


def _signature(host, epoch, tag=''):
    return digest(host, tag, epoch)


def _alert_args(state, auth, host):
    return ('auth-required', 'no-auto-recovery', 'sign-in-claude', auth['key'], detail_text(host, auth['names'], auth['more'], state['missed'], auth['key']))


def _join(auth, analysis, classes):
    for sid, (cl, ev) in classes.items():          # sessions that failed later join the incident; its epoch and key never move
        if cl == 'auth-required' and len(auth['sids']) < SIDS_MAX:
            auth['sids'].setdefault(analysis['sessions'][sid]['hash'], 'native' if ev == 'auth-failed' else 'oauth')


def _open_incident(env, state, analysis, epoch, classes, host, names_of, now, out):
    auth = state['auth']
    names = names_of('auth-required')
    auth.update(active=True, phase='alerting', epoch=epoch, key=_signature(host, epoch), started=now, last_fresh=now, clean_since=0.0, closed_at=0.0, recurred=0,
                fold_pending=False, recurred_at=0.0, names=names[:NAMES_LISTED], more=max(0, len(names) - NAMES_LISTED), alert=new_notice(), recovery=new_notice(), sids={})
    _join(auth, analysis, classes)
    out['notices'].append(_send(state, env.notifier, 'auth-required', auth['alert'], _alert_args(state, auth, host), now))


def _drive_auth(env, state, analysis, verdict, epoch, classes, status, host, names_of, now, out):
    auth, notifier = state['auth'], env.notifier
    fresh = any(_fresh(v, 'native', now) for v in analysis['sessions'].values() if not v['gone'])
    if not auth['active']:
        if verdict != 'auth-required':
            return
        if auth['key'] and auth['closed_at'] and 0 <= now - auth['closed_at'] <= REFOLD_S:
            # A re-failure shortly after a close is the same incident recurring (reopened, counted, no new alert), unless a recovery was DELIVERED and the
            # failure has already lasted REFAIL_NEW_S: then it is a new incident with its own alert.
            told = auth['recovery']['handed_off'] or auth['recovery']['dropped']     # a recovery the cap dropped was still a real, evidenced recovery
            if not (told and now - epoch >= REFAIL_NEW_S) and epoch not in state['closed'][:-1]:
                auth.update(active=True, phase='alerting', last_fresh=now, clean_since=0.0, recurred=auth['recurred'] + 1, recovery=new_notice(), fold_pending=told,
                            recurred_at=now)
                _join(auth, analysis, classes)
                out['auth_note'] = 'recurred'
                return
        if epoch in state['closed']:
            return                                  # a closed incident (same epoch) never reopens; a new fresh failure has a new epoch
        _open_incident(env, state, analysis, epoch, classes, host, names_of, now, out)
        return
    if fresh:
        auth['last_fresh'] = now
        auth['clean_since'] = 0.0
        if auth['phase'] == 'recovered':             # failed again before the recovery notice went out: still the same incident
            auth.update(phase='alerting', recurred=auth['recurred'] + 1, recovery=new_notice())
            out['auth_note'] = 'recurred'
        if auth['fold_pending'] and verdict == 'auth-required' and now - auth['recurred_at'] >= REFAIL_NEW_S and epoch not in state['closed']:
            _close_auth(state, notifier, clear=False, now=now)           # the failure outlived the debounce after a delivered recovery: a new incident
            _open_incident(env, state, analysis, epoch, classes, host, names_of, now, out)
            out['auth_note'] = 'new-incident-after-recovery'
            return
    if now - max(auth['started'], auth['last_fresh']) > WINDOW_S:
        _close_auth(state, notifier, clear=False, now=now)   # aged from the incident's own start and its last fresh failure, never from the epoch
        out['auth_note'] = 'closed-silently-after-48h'
        return
    if auth['phase'] == 'alerting':
        _join(auth, analysis, classes)
        if not auth['clean_since'] and notice_due(auth['alert'], now) and (not auth['alert']['handed_off'] or auth['alert']['pending']):
            out['notices'].append(_send(state, notifier, 'auth-required', auth['alert'], _alert_args(state, auth, host), now))
        how = _recovered(auth, analysis, now) if status == 'logged-in' else None
        if how is None:
            auth['clean_since'] = 0.0
        elif not auth['clean_since']:
            auth['clean_since'] = now              # debounce: the account must stay clean for QUIET_RECOVER_S before anything is announced or closed
        elif now - auth['clean_since'] >= QUIET_RECOVER_S:
            if how == 'resumed':
                auth['phase'] = 'recovered'
            else:
                _close_auth(state, notifier, clear=False, now=now)   # no affirmative evidence the account works: no recovery claim, no notice
                out['auth_note'] = 'closed-without-resume-evidence'
                return
    if auth['phase'] == 'recovered':
        if not auth['alert']['handed_off']:
            _close_auth(state, notifier, clear=False, now=now)        # nobody was ever told: a recovery notice would only confuse
            out['auth_note'] = 'closed-without-resume-evidence'
            return
        if not auth['recovery']['handed_off'] and not auth['recovery']['dropped'] and notice_due(auth['recovery'], now):
            res = _send(state, notifier, 'signin-recovered', auth['recovery'], ('signin-recovered', 'sessions-resumed', 'verify-resumed-work', auth['key'],
                                                                           detail_text(host, auth['names'], auth['more'], 0, auth['key'])), now)
            out['notices'].append(res)
            if res['result'] == 'rate-limited':
                auth['recovery']['dropped'] = True      # recoveries are dropped before alerts when the cap is hit (counted in `rate`)
        if auth['recovery']['handed_off']:
            _close_auth(state, notifier, clear=True, now=now)
        elif auth['recovery']['dropped']:
            _close_auth(state, notifier, clear=False, now=now)


def _recovered(auth, analysis, now):
    """'resumed' (affirmative: a listed session produced a successful turn after the epoch), 'quiet' (no evidence of a working account) or None while ANY live
    session has a fresh unrecovered failure. Idle sessions never block. The caller debounces it for QUIET_RECOVER_S."""
    live = [v for v in analysis['sessions'].values() if not v['gone']]
    if any(_fresh(v, 'native', now) for v in live):
        return None
    by_hash = {v['hash']: v for v in analysis['sessions'].values()}
    if any(by_hash[k]['last_ok'] is not None and by_hash[k]['last_ok'] > auth['epoch'] for k in auth['sids'] if k in by_hash):
        return 'resumed'
    return 'quiet'


def _close_auth(state, notifier, clear, now):
    """End the incident, remember its epoch (it never reopens as a NEW incident) and keep the record for REFOLD_S so a quick re-failure folds into it.
    Only a real recovery clears session-notify's dedup; a silent close must not re-arm the key. An incident that ended before its alert could be handed
    off is recorded as `outage-unannounced` (counted in state and status) and folded into the next alert's detail as `missed=N`."""
    auth = state['auth']
    if auth['epoch']:
        state['closed'] = (state['closed'] + [auth['epoch']])[-CLOSED_EPOCHS:]
    if auth['active'] and auth['key'] and not auth['alert']['handed_off']:
        state['missed'] = min(MAX_MISSED, state['missed'] + 1)
    if clear:
        notifier.clear()
    auth.update(active=False, phase='none', closed_at=now, clean_since=0.0, fold_pending=False)


def _drive_rc(env, state, verdict, epoch, host, names_of, now, out):
    rc, notifier = state['rc'], env.notifier
    if verdict != 'remote-control-disconnect':
        if rc['active']:
            rc.update(active=False, notice=new_notice())     # quiet end: no notice for the end of a Remote Control incident
        return
    if not rc['active']:
        if 0 <= now - rc['last_start'] < RC_COOLDOWN_S:
            return
        names = names_of('remote-control-disconnect')
        rc.update(active=True, epoch=epoch, key=_signature(host, epoch, 'rc'), last_start=now, names=names[:NAMES_LISTED], more=max(0, len(names) - NAMES_LISTED),
                  notice=new_notice())
    elif not notice_due(rc['notice'], now) or (rc['notice']['handed_off'] and not rc['notice']['pending']):
        return
    res = _send(state, notifier, 'remote-control-disconnect', rc['notice'], ('remote-control-disconnect', 'remote-control-only', 'reconnect-remote-control', rc['key'],
                                                                         detail_text(host, rc['names'], rc['more'], 0, rc['key'])), now)
    out['notices'].append(res)
    if res['result'] == 'rate-limited':
        rc['notice']['dropped'] = True                 # Remote Control notices are dropped first when the cap is hit (counted in `rate`)


# ---- reconcile (read-only) --------------------------------------------------------------------------------------------------------

def clean_label(value):
    return value if isinstance(value, str) and LABEL_OK.fullmatch(value) else 'label-unreadable'


def reconcile(src, since, now=None, mono=time.monotonic):
    """After sign-in: what survived, what completed during the outage, whether each session resumed, and whether any work was submitted twice.

    Closed schema, read-only. FAIL only on hard evidence (more than one live entry with the same fingerprint). Same ticket and worktree with
    different jobs, a shared label with different fingerprints, and a label enqueued again (a legitimate retry) are WARN with the evidence
    listed. NOT_VERIFIED when a live entry's session cannot be mapped to a transcript session or a session shows no resumed work."""
    now = time.time() if now is None else now
    since = float(since)
    analysis = analyze(src, now, mono)
    try:
        entries, problems = src.queue()
    except Exception:  # noqa: BLE001
        entries, problems = [], ['queue-unreadable']
    try:
        receipts = src.receipts()
    except Exception:  # noqa: BLE001
        receipts = []
    entries = [e for e in entries if isinstance(e, dict) and isinstance(e.get('id'), str)]
    by_state = {}
    for e in entries:
        by_state[e.get('state')] = by_state.get(e.get('state'), 0) + 1
    survivors = []
    for e in entries:
        if e.get('state') not in ('QUEUED', 'GRANTED', 'CLAIMED'):
            continue
        holder = e.get('holder') if isinstance(e.get('holder'), dict) else e.get('waiter') if isinstance(e.get('waiter'), dict) else None
        pid = holder.get('pid') if holder else None
        alive = src.alive(pid) if type(pid) is int else None
        survivors.append({'id': e['id'], 'state': e['state'], 'label': clean_label(e.get('label')),
                          'holder': 'alive' if alive is True else 'gone' if alive is False else 'unknown'})
    rows = []
    for r in receipts:
        ts = parse_ts(r.get('ts'))
        if ts is not None:
            rows.append((ts, r))
    rows.sort(key=lambda item: item[0])
    completed = []
    for ts, r in rows:
        if ts >= since and r.get('event') in ('released', 'expired', 'cancelled'):
            ev = r.get('evidence')
            completed.append({'id': str(r.get('id'))[:8], 'label': clean_label(r.get('label')), 'event': r['event'], 'at': iso(ts),
                              'holder_exited': r.get('holder_alive') is False,
                              'evidence': ev if isinstance(ev, str) and EVIDENCE_OK.fullmatch(ev) else 'other'})
    entry_session = {e['id']: e.get('session_id') for e in entries}
    found = []

    def add(kind, label, sid, count):
        found.append({'kind': kind, 'severity': 'FAIL' if kind in HARD_KINDS else 'WARN', 'label': clean_label(label), 'count': count, 'session': sid})

    live = [e for e in entries if e.get('state') in LIVE]
    for field, kind in (('fingerprint', 'live-fingerprint'), ('label', 'live-label')):
        groups = {}
        for e in live:
            groups.setdefault(e.get(field), []).append(e)
        for members in groups.values():
            if len(members) > 1 and (kind == 'live-fingerprint' or len({m.get('fingerprint') for m in members}) > 1):
                add(kind, members[0].get('label'), members[0].get('session_id'), len(members))
    pairs = {}
    for e in live:
        for ticket in (e.get('tickets') if isinstance(e.get('tickets'), list) else []):
            pairs.setdefault((str(ticket), str(e.get('worktree'))), []).append(e)
    for members in pairs.values():
        if len(members) > 1 and len({m.get('fingerprint') for m in members}) > 1:
            add('live-ticket-worktree', members[0].get('label'), members[0].get('session_id'), len(members))
    released_at, enqueued = {}, {}
    for ts, r in rows:
        label = r.get('label')
        if r.get('event') == 'released' and ts >= since:
            released_at.setdefault(label, ts)
        if r.get('event') == 'enqueued' and ts >= since:
            enqueued.setdefault(label, []).append(r)
    for label, items in enqueued.items():
        sid = entry_session.get(items[0].get('id'))
        if label in released_at and any((parse_ts(i.get('ts')) or 0) > released_at[label] for i in items):
            add('re-enqueued-after-release', label, sid, len(items))
        elif len(items) > 1:
            add('enqueued-repeated', label, sid, len(items))
    known = set(analysis['sessions'])
    unmapped = sum(1 for e in live if e.get('session_id') not in known)
    sessions = []
    for sid, v in sorted(analysis['sessions'].items(), key=lambda kv: kv[1]['name']):
        fails = [ts for ts, kind in v['events'] if kind == 'fail' and ts >= since]
        if not fails:
            continue
        resumed = next((ts for ts, kind in v['events'] if kind == 'ok' and ts > fails[0]), None)
        mine = [d for d in found if d['session'] == sid]
        verdict = ('FAIL' if any(d['severity'] == 'FAIL' for d in mine) else 'NOT_VERIFIED' if resumed is None else 'WARN' if mine else 'PASS')
        sessions.append({'name': v['name'], 'resumed_at': iso(resumed) if resumed is not None else None, 'verdict': verdict,
                         'findings': sorted({d['kind'] for d in mine})})
    if any(d['severity'] == 'FAIL' for d in found):
        overall = 'FAIL'
    elif unmapped or problems or not sessions or any(s['verdict'] == 'NOT_VERIFIED' for s in sessions):
        overall = 'NOT_VERIFIED'
    elif found:
        overall = 'WARN'
    else:
        overall = 'PASS'
    return {'schema': 1, 'since': iso(since), 'at': iso(now), 'verdict': overall,
            'queue': {'by_state': {str(k): n for k, n in sorted(by_state.items(), key=lambda kv: str(kv[0]))}, 'problems': len(problems),
                      'unmapped_live_entries': unmapped},
            'survivors': survivors[:SURVIVORS_LISTED], 'survivors_more': max(0, len(survivors) - SURVIVORS_LISTED),
            'completed_during_outage': completed[:SURVIVORS_LISTED], 'completed_more': max(0, len(completed) - SURVIVORS_LISTED),
            'sessions': sessions[:NAMES_LISTED * 4], 'findings': [{k: v for k, v in d.items() if k != 'session'} for d in found[:SURVIVORS_LISTED]]}


# ---- launchd ----------------------------------------------------------------------------------------------------------------------

def plist(script, send=True):
    flags = '<string>tick</string>' + ('<string>--send</string>' if send else '') + '<string>--if-due</string>'
    local_bin = Path(os.environ.get('HOME') or str(Path.home())) / '.local/bin'
    return '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.coding-agent-environment.claude-auth-watch</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>%s</string>%s</array>
<key>StartInterval</key><integer>%d</integer>
<key>RunAtLoad</key><true/>
<key>ProcessType</key><string>Background</string>
<key>Nice</key><integer>10</integer>
<key>LowPriorityIO</key><true/>
<key>ThrottleInterval</key><integer>30</integer>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:%s</string></dict>
<key>StandardOutPath</key><string>/dev/null</string>
<key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
''' % (script, flags, INTERVAL_S, local_bin)


def since_arg(text):
    try:
        value = float(text)
    except ValueError:
        value = parse_ts(text)
    if value is None or not (0 <= value < 4e9):
        raise argparse.ArgumentTypeError('--since must be an epoch in seconds or an ISO UTC time')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('scan').add_argument('--json', action='store_true')
    t = sub.add_parser('tick')
    t.add_argument('--send', action='store_true', help='emit notices through session-notify (default: observe only)')
    t.add_argument('--if-due', action='store_true')
    t.add_argument('--json', action='store_true')
    sub.add_parser('status')
    r = sub.add_parser('reconcile')
    r.add_argument('--since', required=True, type=since_arg)
    pl = sub.add_parser('plist')
    pl.add_argument('--checkout', required=True)
    pl.add_argument('--observe-only', action='store_true', help='omit --send: the agent only observes')
    args = parser.parse_args(argv)
    if args.cmd == 'plist':
        script = Path(args.checkout).expanduser().resolve() / 'scripts/claude-auth-watch.py'
        if not script.is_file():
            print('no scripts/claude-auth-watch.py under %s' % args.checkout, file=sys.stderr)
            return 2
        print(plist(script, send=not args.observe_only), end='')
        return 0
    if args.cmd == 'status':
        raw = read_small(watch_dir() / 'state.json', STATE_LIMIT)
        try:
            state, _ = sanitize_state(json.loads(raw) if raw else None)
        except ValueError:
            state = empty_state()
        print(json.dumps(state['last'] or {'ran': False, 'why': 'no tick yet'}, indent=1, sort_keys=True))
        return 0
    src = Sources.home_defaults()
    if args.cmd == 'scan':
        print(json.dumps(scan(src), indent=1, sort_keys=True))
        return 0
    if args.cmd == 'reconcile':
        print(json.dumps(reconcile(src, args.since), indent=1, sort_keys=True))
        return 0
    result = tick(Env(src), send=args.send, if_due=args.if_due)
    print(json.dumps(result, indent=1, sort_keys=True) if args.json else '%s verdict=%s auth=%s rc=%s sent=%s' % (
        result.get('at', '-'), result.get('verdict', result.get('why')), result.get('auth'), result.get('rc'), result.get('sent')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
