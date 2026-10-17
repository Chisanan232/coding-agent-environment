#!/usr/bin/env python3
"""Durable campaign resume coordinator (SPE-110).

A campaign that is waiting on an external event (a PR merge, a time) records a
checkpoint file. A non-privileged launchd user agent runs `tick` on an interval;
`tick` is plain code and makes no model calls. When a recorded event is observed
it resumes the recorded Claude Code session once with `claude --resume <id> -p`.

Guarantees:
- Event driven. Nothing is resumed until an authoritative handle (`gh pr view`
  state, or a wall-clock `not_before`) reports the event. No LLM polling.
- Each event is consumed at most once per registration (deduplication; stale or
  repeated events are ignored). `register` declares a fresh wait.
- A per-campaign LIFETIME budget (`max_resumes`, kept across re-registers) and
  `cooldown_s` bound the rate, so a failing resume cannot become a restart loop.
  The budget is spent BEFORE the launch, so a crash or a refused launch still
  counts. A failed resume, or the budget being spent, makes the campaign
  WAITING_OPERATOR; only the operator `reset` command clears it.
- Events are evaluated with no lock held and re-verified under it before commit.
- A lease (coordinator pid + token + expiry) marks a resume in flight. A tick
  that finds a live lease does nothing; a dead or expired lease is recovered.
- A resume is admitted only when scripts/disk-admission.py check exits 0.
- The resumed command has a hard timeout; its process group is killed on expiry.
- A session whose transcript changed in the last 120 s, or is not found, is not
  resumed (stat only; transcripts are never read), checked again just before the
  budget is spent. This is a best-effort idle heuristic: a session that is open but
  quiet for 120 s or more (a pending prompt, a long tool call) can still be resumed. `receipts.jsonl` and each `<name>.log` keep one previous
  generation beyond 1 MiB.
- Every decision writes one JSONL receipt. Receipts never contain the prompt
  text, environment or command lines beyond the session id. The per-campaign
  `<name>.log` holds the resumed session's raw output (mode 0600, one previous generation kept past 1 MiB)
  and may contain anything that session printed.

Limits, stated plainly: this tool resumes Claude Code sessions only; Codex has
no verified equivalent here. It runs only while the launchd agent is loaded, and
installing that agent is an operator action (`plist` prints it, installs nothing).
The resume prompt is a fixed constant; checkpoint contents are never executed
and never become prompt text. Nothing coordinates with a session the user still
has open interactively, and admission is checked but not reserved, so a heavy
resume has a time-of-check window. A headless `-p` resume with no permission
flags acts only within the settings it inherits. The checkpoint directory is advisory state, not a
security boundary against a same-user attacker.

Exit codes: 0 success, 2 usage or invalid input, 77 refused.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import sys

SCHEMA = 1
STATES = ('WAIT_DEPENDENCY', 'READY', 'RUNNING', 'WAITING_OPERATOR', 'DONE')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$')
DEFAULT_MAX_RESUMES = 3
DEFAULT_COOLDOWN_S = 900
DEFAULT_TIMEOUT_S = 3600
MAX_TIMEOUT_S = 4 * 3600
DEFAULT_BUDGET_USD = '5'
MIN_IDLE_S = 120
MAX_LOG_BYTES = 1024 * 1024
REFUSED_EXIT = 77
PROMPT = ('A recorded dependency of this campaign is now satisfied. Re-read live state '
          '(Jira, GitHub, the worktree) before acting, continue the campaign under the '
          'engineering-workflow contract, and update the campaign checkpoint when done or blocked.')


class CheckpointError(Exception):
    pass


def account_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def default_dir():
    return account_home() / '.cache/coding-agent-environment/campaigns'


def iso_utc(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_utc(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def _private_dir(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise CheckpointError('state directory is a symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


@contextmanager
def campaign_lock(directory, name):
    # Short critical sections only: the lock is never held while a resume runs.
    path = _private_dir(directory) / ('%s.lock' % name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_json_atomic(path, record):
    temp = path.with_name('%s.%s.tmp' % (path.name, secrets.token_hex(4)))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(record, handle, sort_keys=True, indent=1)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def checkpoint_path(directory, name):
    return Path(directory) / ('%s.json' % name)


def validate_event(event):
    if not isinstance(event, dict) or not isinstance(event.get('id'), str) or not NAME_RE.match(event['id']):
        raise CheckpointError('event needs an id matching %s' % NAME_RE.pattern)
    kind = event.get('kind')
    if kind == 'pr_merged':
        if not isinstance(event.get('repo'), str) or not REPO_RE.match(event['repo']):
            raise CheckpointError('pr_merged needs repo owner/name')
        if not isinstance(event.get('number'), int) or isinstance(event['number'], bool) or event['number'] < 1:
            raise CheckpointError('pr_merged needs a positive integer number')
    elif kind == 'file_exists':
        path = event.get('path')
        if not isinstance(path, str) or not os.path.isabs(path) or '\0' in path or '/../' in path + '/':
            raise CheckpointError('file_exists needs an absolute path without ..')
    elif kind == 'not_before':
        try:
            parse_utc(event.get('at', ''))
        except (ValueError, TypeError):
            raise CheckpointError('not_before needs at=YYYY-MM-DDTHH:MM:SSZ')
    else:
        raise CheckpointError('unknown event kind: %r' % (kind,))
    return event


def event_key(event):
    """Content identity of an event: the same wait re-declared is the same key, a different wait is not."""
    if event['kind'] == 'pr_merged':
        return 'pr_merged:%s#%d' % (event['repo'], event['number'])
    if event['kind'] == 'file_exists':
        return 'file_exists:%s' % event['path']
    return 'not_before:%s' % event['at']


def validate(record):
    if not isinstance(record, dict) or record.get('schema') != SCHEMA:
        raise CheckpointError('wrong schema')
    if not NAME_RE.match(str(record.get('campaign', ''))):
        raise CheckpointError('bad campaign name')
    if not UUID_RE.match(str(record.get('session_id', ''))):
        raise CheckpointError('session_id must be a lowercase UUID')
    if not os.path.isabs(str(record.get('cwd', ''))):
        raise CheckpointError('cwd must be absolute')
    if record.get('state') not in STATES:
        raise CheckpointError('bad state')
    if record.get('admission_weight') != 'light':
        # Admission is a check, not a reservation (SPE-109 reserve needs a byte size this tool cannot know),
        # so automatic heavy resumes stay disabled until a reviewed reservation integration exists.
        raise CheckpointError('admission_weight must be light; automatic heavy resumes are disabled')
    events = record.get('events')
    if not isinstance(events, list) or not events:
        raise CheckpointError('at least one event is required')
    ids = [validate_event(event)['id'] for event in events]
    if len(set(ids)) != len(ids):
        raise CheckpointError('duplicate event ids')
    for key, low, high in (('max_resumes', 1, 20), ('cooldown_s', 60, 86400), ('timeout_s', 60, MAX_TIMEOUT_S)):
        if not isinstance(record.get(key), int) or isinstance(record[key], bool) or not low <= record[key] <= high:
            raise CheckpointError('%s must be an integer in %d..%d' % (key, low, high))
    if not isinstance(record.get('consumed'), list) or not isinstance(record.get('resumes_used'), int):
        raise CheckpointError('bad bookkeeping fields')
    return record


def load(directory, name):
    path = checkpoint_path(directory, name)
    if path.is_symlink() or not path.is_file():
        raise CheckpointError('no regular checkpoint for %s' % name)
    try:
        return validate(json.loads(path.read_text()))
    except ValueError as exc:
        raise CheckpointError('unparsable checkpoint: %s' % exc)


def register(directory, name, session_id, cwd, events, now, max_resumes=DEFAULT_MAX_RESUMES,
             cooldown_s=DEFAULT_COOLDOWN_S, timeout_s=DEFAULT_TIMEOUT_S, admission_weight='light'):
    """Declare a wait. The spent budget, a live lease, WAITING_OPERATOR and the consumed state of any event whose content is unchanged carry over."""
    record = {'schema': SCHEMA, 'campaign': name, 'session_id': session_id, 'cwd': cwd,
              'state': 'WAIT_DEPENDENCY', 'events': events, 'consumed': [], 'resumes_used': 0,
              'max_resumes': max_resumes, 'cooldown_s': cooldown_s, 'timeout_s': timeout_s,
              'admission_weight': admission_weight, 'last_resume_at': None, 'next_attempt_after': None,
              'lease': None, 'registered_at': iso_utc(now)}
    if not NAME_RE.match(name):
        raise CheckpointError('bad campaign name')
    with campaign_lock(directory, name):
        try:
            previous = load(directory, name)
            record['resumes_used'] = previous['resumes_used']
            keys = {event_key(event) for event in events}
            record['consumed'] = [key for key in previous['consumed'] if key in keys]
            record['last_resume_at'] = previous['last_resume_at']
            record['lease'] = previous['lease']
            record['next_attempt_after'] = previous['next_attempt_after']
            if previous['state'] == 'WAITING_OPERATOR':
                record['state'] = 'WAITING_OPERATOR'
        except CheckpointError:
            pass
        validate(record)
        write_json_atomic(checkpoint_path(directory, name), record)
    return record


def _rotate(path, limit=MAX_LOG_BYTES):
    """Keep one previous generation so an append-only file cannot grow without bound."""
    try:
        if path.stat().st_size > limit:
            os.replace(path, path.with_name(path.name + '.1'))
    except FileNotFoundError:
        pass


def session_age(session_id, now):
    """Seconds since the session transcript last changed (stat only, never read), None when it is not found."""
    newest = None
    config = os.environ.get('CLAUDE_CONFIG_DIR')
    base = Path(config) if config and os.path.isabs(config) else account_home() / '.claude'
    for path in (base / 'projects').glob('*/%s.jsonl' % session_id):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        newest = modified if newest is None else max(newest, modified)
    return None if newest is None else max(0.0, now.timestamp() - newest)


def receipt(directory, now, campaign, decision, **detail):
    line = json.dumps(dict(detail, ts=iso_utc(now), campaign=campaign, decision=decision), sort_keys=True)
    path = _private_dir(directory) / 'receipts.jsonl'
    # A dedicated lock makes rotate-then-append atomic, so two writers cannot destroy the previous generation.
    with campaign_lock(directory, '.receipts'):
        _rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (line + '\n').encode())
        finally:
            os.close(fd)


def gh_pr_state(repo, number):
    """Authoritative PR state from GitHub, or None when it cannot be read (never a guess)."""
    try:
        done = subprocess.run(['gh', 'pr', 'view', str(number), '--repo', repo, '--json', 'state', '--jq', '.state'],
                              capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def admission_admits(weight):
    """Fail closed: only an explicit exit 0 from the admission adapter admits."""
    script = Path(__file__).with_name('disk-admission.py')
    try:
        done = subprocess.run([sys.executable, str(script), 'check', '--weight', weight], capture_output=True,
                              timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def run_claude(argv, cwd, timeout, log_path, on_start=None):
    """Run one resume in its own process group; kill the whole group at the timeout."""
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
    except OSError as exc:
        return {'outcome': 'launch_failed', 'error': type(exc).__name__}
    finally:
        os.close(fd)
    if on_start:
        on_start(process.pid)
    try:
        return {'outcome': 'exited', 'code': process.wait(timeout=timeout)}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return {'outcome': 'timeout'}


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lease_live(lease, now):
    """Live while unexpired and either the coordinator or its resumed child still runs."""
    if not lease or parse_utc(lease['expires']) <= now:
        return False
    return _alive(lease['pid']) or bool(lease.get('child') and _alive(lease['child']))


def _save(directory, record):
    write_json_atomic(checkpoint_path(directory, record['campaign']), record)


def _blocker(record, clock, age):
    if not os.path.isdir(record['cwd']):
        return 'cwd_missing'
    if not shutil.which('claude'):
        return 'claude_not_found'
    seconds = age(record['session_id'], clock())
    if seconds is None:
        return 'session_not_found'
    # A session whose transcript changed recently is probably open and in use: never resume it underneath the user.
    return 'session_active' if seconds < MIN_IDLE_S else None


def _observe(directory, name, record, clock, pr_state):
    """Evaluate events with no lock held (gh can take 20 s per event). State is monotonic, so _commit re-checks."""
    hit, unreadable = [], False
    for event in record['events']:
        if event_key(event) in record['consumed']:
            continue
        key = event_key(event)
        if event['kind'] == 'pr_merged':
            state = pr_state(event['repo'], event['number'])
            unreadable = unreadable or state is None
            if state == 'MERGED':
                hit.append(key)
        elif event['kind'] == 'file_exists':
            if os.path.isfile(event['path']):
                hit.append(key)
        elif clock() >= parse_utc(event['at']):
            hit.append(key)
    return hit, unreadable


def _screen(directory, name, clock, pr_state, admit, age):
    """Phase A and B: cheap checks under the lock, slow checks outside it. Returns (hit ids, blocker) or None."""
    with campaign_lock(directory, name):
        try:
            record = load(directory, name)
        except CheckpointError as exc:
            receipt(directory, clock(), name, 'invalid_checkpoint', detail=str(exc))
            return None
        now = clock()
        if record['state'] in ('DONE', 'WAITING_OPERATOR') or lease_live(record['lease'], now):
            return None
        if record['lease']:
            record.update(lease=None, state='WAIT_DEPENDENCY')
            _save(directory, record)
            receipt(directory, now, name, 'crash_recovered', session_id=record['session_id'])
        if record['next_attempt_after'] and parse_utc(record['next_attempt_after']) > now:
            return None
    hit, unreadable = _observe(directory, name, record, clock, pr_state)
    if unreadable:
        # Rate-limited: a launchd job with no usable `gh` must be visible, not silent.
        with campaign_lock(directory, name):
            fresh = load(directory, name)
            last = fresh.get('last_unreadable_at')
            if not last or parse_utc(last) + timedelta(hours=1) <= clock():
                fresh['last_unreadable_at'] = iso_utc(clock())
                _save(directory, fresh)
                receipt(directory, clock(), name, 'github_unreadable')
    if not hit:
        return None
    return hit, _blocker(record, clock, age) or (None if admit(record['admission_weight']) else 'admission_denied')


def _commit(directory, name, hit, blocker, execute, clock, age):
    """Phase C: re-verify under the lock, then spend the budget and take the lease."""
    with campaign_lock(directory, name):
        try:
            record = load(directory, name)
        except CheckpointError:
            return None
        now = clock()
        if record['state'] in ('DONE', 'WAITING_OPERATOR') or lease_live(record['lease'], now):
            return None
        # Compare content, not position: a re-register during the gh call may have replaced the wait.
        current = {event_key(event) for event in record['events']}
        hit = [key for key in hit if key not in record['consumed'] and key in current]
        if not hit:
            return None
        if record['resumes_used'] >= record['max_resumes']:
            record['state'] = 'WAITING_OPERATOR'
            _save(directory, record)
            receipt(directory, now, name, 'budget_exhausted', resumes_used=record['resumes_used'])
            return None
        if not execute:
            receipt(directory, now, name, 'dry_run_would_resume', events=hit)
            return None
        cooldown = timedelta(seconds=record['cooldown_s'])
        if not blocker:
            # The session may have become active while events and admission were being read.
            seconds = age(record['session_id'], now)
            blocker = 'session_not_found' if seconds is None else ('session_active' if seconds < MIN_IDLE_S else None)
        if blocker:
            record['next_attempt_after'] = iso_utc(now + cooldown)
            _save(directory, record)
            receipt(directory, now, name, blocker, events=hit)
            return None
        # Spend the budget and consume the events BEFORE launching, so a crash cannot cause a repeat resume.
        token = secrets.token_hex(8)
        record['consumed'] = record['consumed'] + hit
        record.update(resumes_used=record['resumes_used'] + 1, last_resume_at=iso_utc(now), state='RUNNING',
                      next_attempt_after=iso_utc(now + cooldown),
                      lease={'pid': os.getpid(), 'child': None, 'token': token,
                             'expires': iso_utc(now + timedelta(seconds=record['timeout_s'] + 60))})
        _save(directory, record)
        receipt(directory, now, name, 'resume_started', events=hit, session_id=record['session_id'],
                resumes_used=record['resumes_used'])
        argv = [shutil.which('claude'), '--resume', record['session_id'], '-p', PROMPT, '--max-budget-usd', DEFAULT_BUDGET_USD]
        return {'argv': argv, 'cwd': record['cwd'], 'timeout': record['timeout_s'], 'token': token}


def _record_child(directory, name, token, pid):
    with campaign_lock(directory, name):
        record = load(directory, name)
        if (record['lease'] or {}).get('token') == token:
            record['lease']['child'] = pid
            _save(directory, record)


def _finish(directory, name, clock, plan, result):
    state_after = 'unreadable'
    with campaign_lock(directory, name):
        try:
            record = load(directory, name)
        except CheckpointError:
            record = None
        if record is not None:
            if (record['lease'] or {}).get('token') == plan['token']:
                record['lease'] = None
                if record['state'] == 'RUNNING':
                    # No new checkpoint was written. A failed run needs a human; a clean exit just waits.
                    failed = result['outcome'] != 'exited' or result.get('code') != 0
                    record['state'] = 'WAITING_OPERATOR' if failed else 'WAIT_DEPENDENCY'
                _save(directory, record)
            state_after = record['state']
        receipt(directory, clock(), name, 'resume_finished', state_after=state_after, **result)


def reset(directory, name, now):
    """Operator action: return a campaign to WAIT_DEPENDENCY with a fresh budget and no consumed events."""
    if not NAME_RE.match(name):
        raise CheckpointError('bad campaign name')
    with campaign_lock(directory, name):
        record = load(directory, name)
        if lease_live(record['lease'], now):
            raise CheckpointError('a resume is still running')
        record.update(state='WAIT_DEPENDENCY', resumes_used=0, consumed=[], lease=None, next_attempt_after=None)
        _save(directory, record)
        receipt(directory, now, name, 'operator_reset')


def tick(directory, now, execute, pr_state=gh_pr_state, admit=admission_admits, runner=run_claude, age=session_age):
    """One pass over every checkpoint. `now` is a datetime or a zero-argument clock. No model call except the resume."""
    clock = now if callable(now) else (lambda: now)
    started = []
    for path in sorted(_private_dir(directory).glob('*.json')):
        name = path.stem
        if not NAME_RE.match(name):
            continue
        try:
            screened = _screen(directory, name, clock, pr_state, admit, age)
            plan = _commit(directory, name, *screened, execute, clock, age) if screened else None
            if plan:
                started.append(name)
                log_path = _private_dir(directory) / ('%s.log' % name)
                _rotate(log_path)
                result = runner(plan['argv'], plan['cwd'], plan['timeout'], log_path,
                                on_start=lambda pid: _record_child(directory, name, plan['token'], pid))
                _finish(directory, name, clock, plan, result)
        except Exception as exc:
            # One campaign's failure must not stop the others; the receipt carries the type only.
            receipt(directory, clock(), name, 'tick_error', error=type(exc).__name__)
    return started


def build_plist(python, script):
    """launchd user agent: interval tick, no KeepAlive, so a failure cannot respawn in a loop."""
    return plistlib.dumps({
        'Label': 'com.coding-agent-environment.campaign-resume',
        'ProgramArguments': [python, script, 'tick', '--execute'],
        'StartInterval': 300,
        'RunAtLoad': False,
        'ProcessType': 'Background',
        # launchd's default PATH lacks homebrew and ~/.local/bin, where gh and claude live.
        'EnvironmentVariables': {'HOME': str(account_home()),
                                 'PATH': ':'.join([str(account_home() / '.local/bin'), '/opt/homebrew/bin',
                                                   '/usr/local/bin', '/usr/bin', '/bin'])},
        'StandardOutPath': str(default_dir() / 'launchd.out'),
        'StandardErrorPath': str(default_dir() / 'launchd.err'),
    }).decode()


def build_parser():
    # No state-directory flag: the checkpoint location must not be redirectable from argv.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    reg = commands.add_parser('register', allow_abbrev=False, help='record a waiting campaign')
    reg.add_argument('--campaign', required=True)
    reg.add_argument('--session-id', required=True)
    reg.add_argument('--cwd', required=True)
    reg.add_argument('--pr-merged', action='append', default=[], metavar='OWNER/REPO#N')
    reg.add_argument('--file-exists', action='append', default=[], metavar='ABS_PATH', help='operator-done marker or QA artifact')
    reg.add_argument('--not-before', action='append', default=[], metavar='YYYY-MM-DDTHH:MM:SSZ')
    reg.add_argument('--max-resumes', type=int, default=DEFAULT_MAX_RESUMES)
    reg.add_argument('--cooldown-s', type=int, default=DEFAULT_COOLDOWN_S)
    reg.add_argument('--timeout-s', type=int, default=DEFAULT_TIMEOUT_S)
    reg.add_argument('--weight', choices=('light',), default='light', help='heavy automatic resumes are disabled')
    stat = commands.add_parser('status', allow_abbrev=False, help='print every checkpoint')
    stat.add_argument('--json', action='store_true', required=True)
    tick_cmd = commands.add_parser('tick', allow_abbrev=False, help='observe events and resume due campaigns')
    tick_cmd.add_argument('--execute', action='store_true', help='actually resume; without it only receipts a dry run')
    back = commands.add_parser('reset', allow_abbrev=False, help='operator: clear WAITING_OPERATOR and restore the budget')
    back.add_argument('--campaign', required=True)
    commands.add_parser('plist', allow_abbrev=False, help='print the launchd agent definition; installs nothing')
    return parser


def parse_events(args):
    events = []
    for index, spec in enumerate(args.pr_merged):
        match = re.match(r'^([^#]+)#(\d+)$', spec)
        if not match:
            raise CheckpointError('--pr-merged needs OWNER/REPO#N')
        events.append({'id': 'pr%d-%s' % (index, match.group(2)), 'kind': 'pr_merged',
                       'repo': match.group(1), 'number': int(match.group(2))})
    for index, path in enumerate(args.file_exists):
        events.append({'id': 'file%d' % index, 'kind': 'file_exists', 'path': path})
    for index, at in enumerate(args.not_before):
        events.append({'id': 'time%d' % index, 'kind': 'not_before', 'at': at})
    return events


def main(argv=None, directory=None, now=None):
    directory = directory or default_dir()
    args = build_parser().parse_args(argv)
    now = now or datetime.now(timezone.utc)
    try:
        if args.command == 'plist':
            print(build_plist(sys.executable, str(Path(__file__).resolve())), end='')
            return 0
        if args.command == 'register':
            record = register(directory, args.campaign, args.session_id, args.cwd, parse_events(args), now,
                              args.max_resumes, args.cooldown_s, args.timeout_s, args.weight)
            print('registered %s: %d event(s), budget %d' % (record['campaign'], len(record['events']), record['max_resumes']))
            return 0
        if args.command == 'reset':
            reset(directory, args.campaign, now)
            print('reset %s' % args.campaign)
            return 0
        if args.command == 'tick':
            started = tick(directory, lambda: datetime.now(timezone.utc), args.execute)
            print('resumed: %s' % (', '.join(started) or 'none'))
            return 0
        rows = []
        for path in sorted(_private_dir(directory).glob('*.json')):
            try:
                rows.append(load(directory, path.stem))
            except CheckpointError as exc:
                rows.append({'campaign': path.stem, 'error': str(exc)})
        print(json.dumps(rows, sort_keys=True))
        return 0
    except CheckpointError as exc:
        print('invalid: %s' % exc)
        return 2


def run(argv=None, **options):
    try:
        return main(argv, **options)
    except SystemExit:
        raise
    except Exception as exc:
        print('refused: internal error: %s' % exc)
        return REFUSED_EXIT


if __name__ == '__main__':
    raise SystemExit(run())
