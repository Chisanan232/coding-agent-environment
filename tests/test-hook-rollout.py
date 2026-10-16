#!/usr/bin/env python3
"""The gate hook rollout changes exactly one settings entry and is reversible."""
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('hook_rollout', ROOT / 'scripts/hook-rollout.py')
hr = module_from_spec(spec)
spec.loader.exec_module(hr)

LIVE = {
    'permissions': {'allow': ['Bash(git status)'], 'deny': []},
    'hooks': {
        'PreToolUse': [
            {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'rtk hook claude'}]},
            {'matcher': 'Grep|Glob', 'hooks': [{'type': 'command', 'command': '~/.claude/hooks/cbm-code-discovery-gate', 'timeout': 5}]},
            {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '~/.claude/hooks/fornax-guard'}]},
        ],
        'PostToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python3 ~/.claude/hooks/bg-track.py'}]}],
        'SessionStart': [{'hooks': [{'type': 'command', 'command': '~/.claude/hooks/libra-governor'}]}],
    },
    'statusLine': {'type': 'command', 'command': 'python3 ~/.claude/statusline.py'},
}


class Pure(unittest.TestCase):
    def test_with_gate_appends_one_group_and_preserves_everything_else(self):
        new = hr.with_gate(LIVE)
        groups = new['hooks']['PreToolUse']
        self.assertEqual(groups[:-1], LIVE['hooks']['PreToolUse'])
        self.assertEqual(groups[-1], {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python3 ~/.claude/hooks/disk-admission-gate.py'}]})
        self.assertEqual(len(groups), len(LIVE['hooks']['PreToolUse']) + 1)
        for key in LIVE:
            if key != 'hooks':
                self.assertEqual(new[key], LIVE[key])
        self.assertEqual(new['hooks']['PostToolUse'], LIVE['hooks']['PostToolUse'])
        self.assertEqual(new['hooks']['SessionStart'], LIVE['hooks']['SessionStart'])
        # The input is not mutated.
        self.assertEqual(len(LIVE['hooks']['PreToolUse']), 3)

    def test_gate_state_distinguishes_absent_installed_and_conflict(self):
        self.assertEqual(hr.gate_state(LIVE), 'absent')
        self.assertEqual(hr.gate_state(hr.with_gate(LIVE)), 'installed')
        twice = hr.with_gate(hr.with_gate(LIVE))
        self.assertEqual(hr.gate_state(twice), 'conflict')
        other = json.loads(json.dumps(LIVE))
        other['hooks']['PreToolUse'].append({'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'python3 ~/.claude/hooks/disk-admission-gate.py --admission-dir /x'}]})
        self.assertEqual(hr.gate_state(other), 'conflict')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.live = self.root / 'claude/settings.json'
        self.live.parent.mkdir()
        self.live_text = json.dumps(LIVE, indent=2, ensure_ascii=False) + '\n'
        self.live.write_text(self.live_text)
        os.chmod(self.live, 0o600)
        self.src = self.root / 'src/disk-admission-gate.py'
        self.src.parent.mkdir()
        self.src.write_text('#!/usr/bin/env python3\nprint("gate")\n')
        self.dest = self.root / 'claude/hooks/disk-admission-gate.py'
        self.mode = self.root / 'cache/admission/mode'
        self.backups = self.root / 'claude/backups'

    def args(self, mode, *extra):
        return [mode, '--live', str(self.live), '--hook-src', str(self.src), '--hook-dest', str(self.dest),
                '--mode-file', str(self.mode), '--backup-dir', str(self.backups), *extra]

    def run_cli(self, mode, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = hr.main(self.args(mode, *extra))
        return code, out.getvalue()

    def snapshot_tree(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob('*')) if p.is_file()}

    def test_plan_writes_nothing_and_reports_hashes(self):
        before = self.snapshot_tree()
        code, text = self.run_cli('plan')
        self.assertEqual(code, 0)
        self.assertIn(hashlib.sha256(self.live_text.encode()).hexdigest(), text)
        self.assertIn(hashlib.sha256(self.src.read_bytes()).hexdigest(), text)
        self.assertIn('append 1 PreToolUse group', text)
        self.assertIn('mode file: absent', text)
        self.assertEqual(self.snapshot_tree(), before)


if __name__ == '__main__':
    unittest.main()
