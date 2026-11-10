#!/usr/bin/env python3
"""The observer records prompts and results, never decides, and never claims consent."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('ask_observe', ROOT / 'scripts/ask-observe.py')
ao = module_from_spec(spec)
spec.loader.exec_module(ao)

CMD = {'command': 'git -C /scratch worktree prune --dry-run --expire=1111.seconds.ago'}
OTHER = {'command': 'git -C /scratch worktree prune --dry-run --expire=2222.seconds.ago'}


class Observe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['ASK_OBSERVE_DIR'] = os.path.join(self.tmp.name, 'o')

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, event, tool_input=CMD, session='s1', **extra):
        payload = {'hook_event_name': event, 'session_id': session, 'tool_name': 'Bash', 'tool_input': tool_input, **extra}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ao.main(['hook'], stdin=io.StringIO(json.dumps(payload)))
        return code, out.getvalue()

    def rows(self):
        return ao.read(ao.observe_dir())

    def test_hook_prints_nothing_and_exits_zero_so_it_cannot_decide(self):
        for event in ('PermissionRequest', 'PostToolUse', 'PostToolUseFailure'):
            self.assertEqual(self.hook(event), (0, ''))

    def test_garbage_input_is_harmless(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ao.main(['hook'], stdin=io.StringIO('not json')), 0)
            self.assertEqual(ao.main(['hook'], stdin=io.StringIO('[1]')), 0)
        self.assertEqual((out.getvalue(), self.rows()), ('', []))

    def test_unrelated_events_are_not_recorded(self):
        self.hook('PreToolUse')
        self.hook('Notification', notification_type='idle_prompt')
        self.assertEqual(self.rows(), [])

    def test_permission_prompt_notification_is_recorded(self):
        self.hook('Notification', tool_input=None, notification_type='permission_prompt')
        self.assertEqual(self.rows()[0]['notification_type'], 'permission_prompt')

    def test_files_are_private_and_no_tool_use_id_is_invented(self):
        self.hook('PermissionRequest')
        path = os.path.join(ao.observe_dir(), 'events.jsonl')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIsNone(self.rows()[0]['tool_use_id'])

    def test_no_argument_text_is_persisted_so_no_secret_shape_can_leak(self):
        secrets = ['postgresql://app:Hunter2pw@db/x', 'ghp_ABCDEF0123456789abcdef', 'sk-live-0123456789', 'AKIA0123456789ABCDEF',
                   'Authorization: Bearer abc123', 'GITHUB_PAT=zzz999', 'plain-password-value']
        for secret in secrets:
            self.hook('PermissionRequest', tool_input={'command': f'psql {secret}'})
            self.hook('PostToolUse', tool_input={'command': f'export X={secret}'})
            self.hook('PermissionRequest', tool_input={'file_path': f'/tmp/{secret}', 'url': f'https://u:{secret}@h/'})
        raw = Path(ao.observe_dir(), 'events.jsonl').read_text()
        for secret in secrets:
            self.assertNotIn(secret, raw)
        for row in self.rows():
            self.assertIn(row['preview'], ('psql', 'export', ''))

    def test_the_preview_is_only_the_program_name(self):
        self.hook('PermissionRequest', tool_input={'command': '/usr/bin/git ' + 'x' * 5000})
        self.assertEqual(self.rows()[0]['preview'], 'git')
        self.hook('PermissionRequest', tool_input={'command': 'weird;name --x'})
        self.assertEqual(self.rows()[1]['preview'], '')


class Report(unittest.TestCase):
    def row(self, event, at, tool_input=CMD, session='s1'):
        return {'at': at, 'event': event, 'session': session, 'tool': 'Bash', 'key': ao.input_key(tool_input), 'preview': tool_input['command']}

    def test_prompt_with_no_result_is_pending_denied_or_expired_and_says_how_long(self):
        rows = [self.row('PermissionRequest', '2026-11-10T02:00:00Z')]
        entry = ao.report(rows, '2026-11-10T02:20:00Z')[0]
        self.assertEqual((entry['outcome'], entry['waiting_s']), ('PROMPTED_NO_RESULT', 1200))

    def test_prompt_then_result_is_executed_after_prompt(self):
        rows = [self.row('PermissionRequest', '2026-11-10T02:00:00Z'), self.row('PostToolUse', '2026-11-10T02:30:00Z')]
        self.assertEqual(ao.report(rows, '2026-11-10T03:00:00Z')[0]['outcome'], 'RAN_AFTER_OPEN_PROMPT')

    def test_result_for_a_different_command_never_closes_the_prompt(self):
        rows = [self.row('PermissionRequest', '2026-11-10T02:00:00Z'), self.row('PostToolUse', '2026-11-10T02:01:00Z', OTHER)]
        outcomes = [e['outcome'] for e in ao.report(rows, '2026-11-10T02:02:00Z')]
        self.assertEqual(outcomes, ['PROMPTED_NO_RESULT', 'RAN_WITHOUT_PROMPT'])

    def test_result_in_a_different_session_never_closes_the_prompt(self):
        rows = [self.row('PermissionRequest', '2026-11-10T02:00:00Z'), self.row('PostToolUse', '2026-11-10T02:01:00Z', session='s2')]
        self.assertEqual(ao.report(rows, '2026-11-10T02:02:00Z')[0]['outcome'], 'PROMPTED_NO_RESULT')

    def test_one_result_closes_one_prompt_so_a_duplicate_event_cannot_close_two(self):
        rows = [self.row('PermissionRequest', '2026-11-10T02:00:00Z'), self.row('PermissionRequest', '2026-11-10T02:00:05Z'),
                self.row('PostToolUse', '2026-11-10T02:01:00Z'), self.row('PostToolUse', '2026-11-10T02:01:00Z')]
        outcomes = [e['outcome'] for e in ao.report(rows, '2026-11-10T02:02:00Z')]
        self.assertEqual(outcomes, ['RAN_AFTER_OPEN_PROMPT', 'RAN_AFTER_OPEN_PROMPT'])
        self.assertEqual(len(outcomes), 2)

    def test_failure_after_prompt_and_unprompted_run_are_distinguished(self):
        rows = [self.row('PermissionRequest', '2026-11-10T02:00:00Z'), self.row('PostToolUseFailure', '2026-11-10T02:01:00Z'),
                self.row('PostToolUse', '2026-11-10T02:03:00Z', OTHER)]
        outcomes = [e['outcome'] for e in ao.report(rows, '2026-11-10T02:04:00Z')]
        self.assertEqual(outcomes, ['FAILED_AFTER_OPEN_PROMPT', 'RAN_WITHOUT_PROMPT'])


class NoDecisionPath(unittest.TestCase):
    def test_source_never_emits_a_permission_decision(self):
        text = (ROOT / 'scripts/ask-observe.py').read_text()
        for word in ('permissionDecision', 'hookSpecificOutput', '"behavior"', 'updatedInput'):
            self.assertNotIn(word, text.split('"""', 2)[2])


if __name__ == '__main__':
    unittest.main()
