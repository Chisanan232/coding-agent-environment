#!/usr/bin/env python3
"""Select native paths without reading another provider's configuration."""
import os
from pathlib import Path
import shlex
import subprocess

host_root = Path(__file__).resolve().parents[3]
if host_root.name not in {'.claude', '.codex'}:
    raise SystemExit('Run the installed engineering-runtime skill, not a relocated helper.')
try:
    repo = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], stderr=subprocess.DEVNULL, text=True).strip())
except subprocess.CalledProcessError:
    repo = Path.cwd()
context = repo / host_root.name
values = {
    'ENGINEERING_RUNTIME': str(Path(__file__).resolve().parent),
    'ENGINEERING_CONTEXT_DIR': str(context),
    'ENGINEERING_STATE_DIR': str(host_root),
    'ENGINEERING_CONFIG_ENV': str(host_root / ('config.env' if host_root.name == '.claude' else 'engineering.env')),
    'ENGINEERING_INSTRUCTIONS': str(repo / ('.claude/CLAUDE.md' if host_root.name == '.claude' else 'AGENTS.md')),
    'ENGINEERING_QUALITY_COOLDOWN_FILE': str(host_root / '.quality-gate-last-run'),
}
for kind in ['TICKET', 'WORKTREE', 'RELEASE']:
    key = 'ENGINEERING_CURRENT_' + kind
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
for key, value in values.items():
    print('export ' + key + '=' + shlex.quote(value))
