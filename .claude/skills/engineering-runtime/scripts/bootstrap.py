#!/usr/bin/env python3
"""Select native paths without reading another provider's configuration."""
import os
from pathlib import Path
import shlex
import subprocess
import json
import sys

host_root = Path(__file__).resolve().parents[3]
if host_root.name not in {'.claude', '.codex'}:
    raise SystemExit('Run the installed engineering-runtime skill, not a relocated helper.')
try:
    git_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    git_env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0'})
    result = subprocess.run(['git', '-C', str(Path.cwd()), 'rev-parse', '--show-toplevel'],
                            env=git_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=5, check=True)
    repo = Path(result.stdout.strip()).resolve(strict=True)
    git_discovery_available = True
except (OSError, subprocess.SubprocessError):
    repo = None
    git_discovery_available = False
context = repo / host_root.name if repo is not None else None
context_result = {'status': 'legacy_uncoordinated' if git_discovery_available else 'unavailable'}
context_present = False
if context is not None:
    context_file = context / '.current-context.json'
    try:
        context_file.lstat()
        context_present = True
    except FileNotFoundError:
        context_present = False
    except OSError:
        context_present = True
        context_result = {'status': 'unavailable'}
if context_present and context_result['status'] != 'unavailable':
    context_helper = Path(__file__).resolve().with_name('ticket-state.py')
    try:
        inspected = subprocess.run([sys.executable, str(context_helper), 'context', str(context), 'inspect'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, timeout=8, check=True)
        context_result = json.loads(inspected.stdout)
        if not isinstance(context_result, dict):
            context_result = {'status': 'unavailable'}
        elif context_result.get('status') == 'managed':
            state = context_result.get('state')
            identifiers_valid = all(isinstance(context_result.get(key), str) and context_result[key]
                                    for key in ('generation', 'scope_id', 'token'))
            active_tuple_valid = (isinstance(context_result.get('ticket'), str)
                                  and isinstance(context_result.get('worktree'), str)
                                  and (context_result.get('release') is None
                                       or isinstance(context_result.get('release'), str)))
            cleared_tuple_valid = all(context_result.get(key) is None
                                      for key in ('ticket', 'worktree', 'release'))
            if (state not in ('active', 'cleared') or not identifiers_valid
                    or state == 'active' and not active_tuple_valid
                    or state == 'cleared' and not cleared_tuple_valid):
                context_result = {'status': 'unavailable'}
        elif context_result.get('status') != 'unavailable':
            context_result = {'status': 'unavailable'}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError):
        context_result = {'status': 'unavailable'}
context_status = context_result['status']
context_managed = context_status == 'managed'
context_active = context_managed and context_result.get('state') == 'active'
values = {
    'ENGINEERING_RUNTIME': str(Path(__file__).resolve().parent),
    'ENGINEERING_CONTEXT_DIR': str(context) if context is not None else '',
    'ENGINEERING_STATE_DIR': str(host_root),
    'ENGINEERING_CONFIG_ENV': str(host_root / ('config.env' if host_root.name == '.claude' else 'engineering.env')),
    'ENGINEERING_INSTRUCTIONS': (str(repo / ('.claude/CLAUDE.md' if host_root.name == '.claude' else 'AGENTS.md'))
                                 if repo is not None else ''),
    'ENGINEERING_QUALITY_COOLDOWN_FILE': str(host_root / '.quality-gate-last-run'),
    'ENGINEERING_CONTEXT_STATUS': context_status,
    'ENGINEERING_CONTEXT_GENERATION': context_result.get('generation', '') if context_managed else '',
    'ENGINEERING_CONTEXT_SCOPE_ID': context_result.get('scope_id', '') if context_managed else '',
    'ENGINEERING_CONTEXT_CAS_TOKEN': context_result.get('token', '') if context_managed else '',
}
for kind in ['TICKET', 'WORKTREE', 'RELEASE']:
    key = 'ENGINEERING_CURRENT_' + kind
    if context_managed:
        values[key] = context_result.get(kind.lower(), '') or '' if context_active else ''
    elif context_status == 'unavailable':
        values[key] = ''
    else:
        prior = os.environ.get(key)
        if not prior and host_root.name == '.claude':
            prior = os.environ.get('CLAUDE_CURRENT_' + kind)
        marker = context / ('.current-' + kind.lower())
        values[key] = prior or (marker.read_text().strip() if marker.is_file() else '')
for kind in ['ISSUE_TRACKER', 'E2E_COMMAND', 'INTEGRATION_TEST_COMMAND', 'STALE_PR_DAYS']:
    key = 'ENGINEERING_' + kind
    values[key] = os.environ.get(key, '')
    if not values[key] and host_root.name == '.claude':
        values[key] = os.environ.get('CLAUDE_' + kind, '')
# Explicit native defaults prevent inherited Claude state/skip flags from selecting Codex behavior.
for suffix, default in {
    'SESSION_NOTES_DIR': str(host_root / 'session-notes'),
    'WORKFLOW_STATE_DIR': str(host_root / 'workflow-state'),
    'AUDIT_LOG_DIR': str(host_root / 'audit'),
    'SENTINEL_DIR': str(host_root / 'sentinels'),
    'CIRCUIT_BREAKER_DIR': str(host_root / 'circuit-breaker'),
    'DECISION_LOG_DIR': str(host_root / 'decisions'),
    'STRICT': '0', 'SKIP_PRECOMMIT_GATE': '0', 'SKIP_AUDIT': '0', 'SKIP_TEST_GATE': '0',
    'CIRCUIT_BREAKER_THRESHOLD': '5', 'DECISION_LOG_ENABLED': '1', 'DECISION_LOG_MAX_CONTEXT': '500',
}.items():
    values['ENGINEERING_' + suffix] = os.environ.get('ENGINEERING_' + suffix,
        os.environ.get('CLAUDE_' + suffix, default) if host_root.name == '.claude' else default)
try:
    from storage import resolve as resolve_storage
    storage_pin = json.loads((Path(__file__).resolve().parent.parent / 'references/storage-core.json').read_text())
    storage_bin, storage_status = resolve_storage(Path.home(), storage_pin)
except (ImportError, OSError, ValueError, KeyError, TypeError):
    storage_bin, storage_status = '', 'unavailable'
values['ENGINEERING_STORAGE_BIN'] = storage_bin
values['ENGINEERING_STORAGE_STATUS'] = storage_status
for key, value in values.items():
    print('export ' + key + '=' + shlex.quote(value))
