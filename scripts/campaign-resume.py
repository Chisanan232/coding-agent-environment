#!/usr/bin/env python3
"""Durable campaign resume coordinator (SPE-110).

A campaign that is waiting on an external event (a PR merge, a time) records a
checkpoint file. A non-privileged launchd user agent runs `tick` on an interval;
`tick` is plain code and makes no model calls. When a recorded event is observed
it resumes the recorded Claude Code session once with `claude --resume <id> -p`.

Guarantees:
- Event driven. Nothing is resumed until an authoritative handle (`gh pr view`
  state, or a wall-clock `not_before`) reports the event. No LLM polling.
- Each event id is consumed at most once (deduplication; stale or repeated
  events are ignored).
- A per-campaign budget (`max_resumes`) and `cooldown_s` bound the rate, so a
  failing resume cannot become a restart loop. The budget is spent BEFORE the
  launch, so a crash or a refused launch still counts. At the budget the
  campaign becomes WAITING_OPERATOR and is never resumed again by this tool.
- A lease (coordinator pid + token + expiry) marks a resume in flight. A tick
  that finds a live lease does nothing; a dead or expired lease is recovered.
- A resume is admitted only when scripts/disk-admission.py check exits 0.
- The resumed command has a hard timeout; its process group is killed on expiry.
- Every decision writes one JSONL receipt. Receipts never contain the prompt
  text, environment or command lines beyond the session id.

Limits, stated plainly: this tool resumes Claude Code sessions only; Codex has
no verified equivalent here. It runs only while the launchd agent is loaded, and
installing that agent is an operator action (`plist` prints it, installs nothing).
The resume prompt is a fixed constant; checkpoint contents are never executed
and never become prompt text. The checkpoint directory is advisory state, not a
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
    elif kind == 'not_before':
        try:
            parse_utc(event.get('at', ''))
        except (ValueError, TypeError):
            raise CheckpointError('not_before needs at=YYYY-MM-DDTHH:MM:SSZ')
    else:
        raise CheckpointError('unknown event kind: %r' % (kind,))
    return event


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
    if record.get('admission_weight') not in ('light', 'heavy'):
        raise CheckpointError('admission_weight must be light or heavy')
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
    """Create or replace a checkpoint. A re-register keeps the spent budget and consumed events."""
    record = {'schema': SCHEMA, 'campaign': name, 'session_id': session_id, 'cwd': cwd,
              'state': 'WAIT_DEPENDENCY', 'events': events, 'consumed': [], 'resumes_used': 0,
              'max_resumes': max_resumes, 'cooldown_s': cooldown_s, 'timeout_s': timeout_s,
              'admission_weight': admission_weight, 'last_resume_at': None, 'next_attempt_after': None,
              'lease': None, 'registered_at': iso_utc(now)}
    with campaign_lock(directory, name):
        try:
            previous = load(directory, name)
            record['consumed'] = previous['consumed']
            record['resumes_used'] = previous['resumes_used']
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


def receipt(directory, now, campaign, decision, **detail):
    line = json.dumps(dict(detail, ts=iso_utc(now), campaign=campaign, decision=decision), sort_keys=True)
    fd = os.open(_private_dir(directory) / 'receipts.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
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


def event_satisfied(event, now, pr_state):
    if event['kind'] == 'not_before':
        return now >= parse_utc(event['at'])
    return pr_state(event['repo'], event['number']) == 'MERGED'


def admission_admits(weight):
    """Fail closed: only an explicit exit 0 from the admission adapter admits."""
    script = Path(__file__).with_name('disk-admission.py')
    try:
        done = subprocess.run([sys.executable, str(script), 'check', '--weight', weight], capture_output=True,
                              timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def run_claude(argv, cwd, timeout, log_path):
    """Run one resume in its own process group; kill the whole group at the timeout."""
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
    except OSError as exc:
        return {'outcome': 'launch_failed', 'error': type(exc).__name__}
    finally:
        os.close(fd)
    try:
        return {'outcome': 'exited', 'code': process.wait(timeout=timeout)}
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return {'outcome': 'timeout'}


def lease_live(lease, now):
    if not lease:
        return False
    try:
        os.kill(lease['pid'], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return parse_utc(lease['expires']) > now


def _save(directory, record):
    write_json_atomic(checkpoint_path(directory, record['campaign']), record)


def _claim(directory, name, now, execute, pr_state, admit):
    """Decide under the lock whether to resume; returns the launch plan or None."""
    with campaign_lock(directory, name):
        try:
            record = load(directory, name)
        except CheckpointError as exc:
            receipt(directory, now, name, 'invalid_checkpoint', detail=str(exc))
            return None
        if record['state'] in ('DONE', 'WAITING_OPERATOR'):
            return None
        if lease_live(record['lease'], now):
            return None
        if record['lease']:
            record.update(lease=None, state='WAIT_DEPENDENCY')
            _save(directory, record)
            receipt(directory, now, name, 'crash_recovered', session_id=record['session_id'])
        pending = [event for event in record['events'] if event['id'] not in record['consumed']]
        hit = [event['id'] for event in pending if event_satisfied(event, now, pr_state)]
        if not hit:
            return None
        if record['resumes_used'] >= record['max_resumes']:
            record['state'] = 'WAITING_OPERATOR'
            _save(directory, record)
            receipt(directory, now, name, 'budget_exhausted', resumes_used=record['resumes_used'])
            return None
        if record['next_attempt_after'] and parse_utc(record['next_attempt_after']) > now:
            return None
        if not execute:
            receipt(directory, now, name, 'dry_run_would_resume', events=hit)
            return None
        claude = shutil.which('claude')
        blocker = None
        if not os.path.isdir(record['cwd']):
            blocker = 'cwd_missing'
        elif not claude:
            blocker = 'claude_not_found'
        elif not admit(record['admission_weight']):
            blocker = 'admission_denied'
        if blocker:
            record['next_attempt_after'] = iso_utc(now + timedelta(seconds=record['cooldown_s']))
            _save(directory, record)
            receipt(directory, now, name, blocker, events=hit)
            return None
        # Spend the budget and consume the events BEFORE launching, so a crash cannot cause a repeat resume.
        token = secrets.token_hex(8)
        record['consumed'] = record['consumed'] + hit
        record.update(resumes_used=record['resumes_used'] + 1, last_resume_at=iso_utc(now), state='RUNNING',
                      next_attempt_after=iso_utc(now + timedelta(seconds=record['cooldown_s'])),
                      lease={'pid': os.getpid(), 'token': token, 'expires': iso_utc(now + timedelta(seconds=record['timeout_s'] + 60))})
        _save(directory, record)
        receipt(directory, now, name, 'resume_started', events=hit, session_id=record['session_id'],
                resumes_used=record['resumes_used'])
        argv = [claude, '--resume', record['session_id'], '-p', PROMPT, '--max-budget-usd', DEFAULT_BUDGET_USD]
        return {'argv': argv, 'cwd': record['cwd'], 'timeout': record['timeout_s'], 'token': token}


def _finish(directory, name, now, plan, result):
    with campaign_lock(directory, name):
        record = load(directory, name)
        if (record['lease'] or {}).get('token') == plan['token']:
            record['lease'] = None
            if record['state'] == 'RUNNING':
                # The session exited without writing a new checkpoint: wait for operator or a re-register.
                record['state'] = 'WAIT_DEPENDENCY'
            _save(directory, record)
        receipt(directory, now, name, 'resume_finished', state_after=record['state'], **result)


def tick(directory, now, execute, pr_state=gh_pr_state, admit=admission_admits, runner=run_claude):
    """One pass over every checkpoint. Makes no model call except the single resume it launches."""
    started = []
    for path in sorted(_private_dir(directory).glob('*.json')):
        name = path.stem
        if not NAME_RE.match(name):
            continue
        plan = _claim(directory, name, now, execute, pr_state, admit)
        if plan:
            result = runner(plan['argv'], plan['cwd'], plan['timeout'], _private_dir(directory) / ('%s.log' % name))
            _finish(directory, name, now, plan, result)
            started.append(name)
    return started


def build_plist(python, script):
    """launchd user agent: interval tick, no KeepAlive, so a failure cannot respawn in a loop."""
    return plistlib.dumps({
        'Label': 'com.coding-agent-environment.campaign-resume',
        'ProgramArguments': [python, script, 'tick', '--execute'],
        'StartInterval': 300,
        'RunAtLoad': False,
        'ProcessType': 'Background',
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
    reg.add_argument('--not-before', action='append', default=[], metavar='YYYY-MM-DDTHH:MM:SSZ')
    reg.add_argument('--max-resumes', type=int, default=DEFAULT_MAX_RESUMES)
    reg.add_argument('--cooldown-s', type=int, default=DEFAULT_COOLDOWN_S)
    reg.add_argument('--timeout-s', type=int, default=DEFAULT_TIMEOUT_S)
    reg.add_argument('--weight', choices=('light', 'heavy'), default='light')
    stat = commands.add_parser('status', allow_abbrev=False, help='print every checkpoint')
    stat.add_argument('--json', action='store_true', required=True)
    tick_cmd = commands.add_parser('tick', allow_abbrev=False, help='observe events and resume due campaigns')
    tick_cmd.add_argument('--execute', action='store_true', help='actually resume; without it only receipts a dry run')
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
        if args.command == 'tick':
            started = tick(directory, now, args.execute)
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
