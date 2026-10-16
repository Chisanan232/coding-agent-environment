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

    def test_plan_refuses_a_conflicting_existing_entry(self):
        data = hr.with_gate(LIVE)
        data['hooks']['PreToolUse'][-1]['hooks'][0]['command'] += ' --admission-dir /x'
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        code, text = self.run_cli('plan')
        self.assertEqual(code, 2)
        self.assertIn('different disk-admission-gate entry', text)

    def test_plan_reports_when_reserialising_would_change_formatting(self):
        compact = self.live_text.replace('"allow": [\n      "Bash(git status)"\n    ]', '"allow": ["Bash(git status)"]')
        self.assertNotEqual(compact, self.live_text)
        self.live.write_text(compact)
        code, text = self.run_cli('plan')
        self.assertEqual(code, 0)
        self.assertIn('changes its formatting', text)
        self.live.write_text(self.live_text)
        self.assertNotIn('changes its formatting', self.run_cli('plan')[1])

    def pins(self, *extra):
        return ['--expect-live-sha', hashlib.sha256(self.live.read_bytes()).hexdigest(),
                '--expect-hook-sha', hashlib.sha256(self.src.read_bytes()).hexdigest(), '--unmerged-ok', *extra]

    def apply(self, *extra):
        return self.run_cli('apply', *self.pins(*extra))

    def test_apply_installs_the_hook_and_adds_exactly_one_group(self):
        code, text = self.apply()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o755)
        new = json.loads(self.live.read_text())
        self.assertEqual(new, hr.with_gate(LIVE))
        self.assertEqual(hr.gate_state(new), 'installed')
        self.assertEqual(self.live.read_text(), json.dumps(hr.with_gate(LIVE), indent=2, ensure_ascii=False) + '\n')
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o600)

    def test_apply_backs_up_the_settings_hash_exact_and_writes_a_receipt(self):
        before = self.live.read_bytes()
        self.assertEqual(self.apply()[0], 0)
        (backup,) = [p for p in self.backups.iterdir() if p.name.startswith('settings.json.pre-SPE-109.')]
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        (receipt,) = [p for p in self.backups.iterdir() if p.name.endswith('.json') and 'receipt' in p.name]
        data = json.loads(receipt.read_text())
        self.assertEqual(data['settings_backup'], str(backup))
        self.assertEqual(data['settings_sha_before'], hashlib.sha256(before).hexdigest())
        self.assertTrue(data['hook_created'])

    def test_apply_preserves_every_other_hook_and_the_pretooluse_order(self):
        self.assertEqual(self.apply()[0], 0)
        new = json.loads(self.live.read_text())
        self.assertEqual(new['hooks']['PreToolUse'][:3], LIVE['hooks']['PreToolUse'])
        commands = [h['command'] for g in new['hooks']['PreToolUse'] for h in g['hooks']]
        self.assertEqual(commands, ['rtk hook claude', '~/.claude/hooks/cbm-code-discovery-gate',
                                    '~/.claude/hooks/fornax-guard', 'python3 ~/.claude/hooks/disk-admission-gate.py'])
        self.assertEqual(new['hooks']['PostToolUse'], LIVE['hooks']['PostToolUse'])
        self.assertEqual(new['hooks']['SessionStart'], LIVE['hooks']['SessionStart'])
        self.assertEqual(new['permissions'], LIVE['permissions'])
        self.assertEqual(new['statusLine'], LIVE['statusLine'])

    def test_second_apply_is_a_no_op(self):
        self.assertEqual(self.apply()[0], 0)
        after = self.snapshot_tree()
        code, text = self.run_cli('apply')
        self.assertEqual(code, 0)
        self.assertIn('already installed', text)
        self.assertEqual(self.snapshot_tree(), after)

    def test_wrong_hook_sha_is_refused_and_nothing_is_written(self):
        before = self.snapshot_tree()
        code, text = self.run_cli('apply', '--expect-live-sha', hashlib.sha256(self.live.read_bytes()).hexdigest(),
                                  '--expect-hook-sha', '0' * 64, '--unmerged-ok')
        self.assertEqual(code, 3)
        self.assertIn('do not match', text)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertFalse(self.dest.exists())

    def test_wrong_live_sha_is_refused_and_nothing_is_written(self):
        before = self.snapshot_tree()
        code, _ = self.run_cli('apply', '--expect-live-sha', '1' * 64,
                               '--expect-hook-sha', hashlib.sha256(self.src.read_bytes()).hexdigest(), '--unmerged-ok')
        self.assertEqual(code, 3)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertEqual(self.run_cli('apply', '--unmerged-ok')[0], 3)

    def test_symlinked_settings_are_refused(self):
        real = self.root / 'claude/real-settings.json'
        real.write_text(self.live_text)
        self.live.unlink()
        self.live.symlink_to(real)
        code, text = self.apply()
        self.assertEqual(code, 4)
        self.assertIn('symlink', text)
        self.assertFalse(self.dest.exists())
        self.assertEqual(real.read_text(), self.live_text)

    def test_symlinked_hook_destination_is_refused(self):
        target = self.root / 'elsewhere.py'
        target.write_text('untouched')
        self.dest.parent.mkdir()
        self.dest.symlink_to(target)
        code, _ = self.apply()
        self.assertEqual(code, 4)
        self.assertEqual(target.read_text(), 'untouched')
        self.assertEqual(self.live.read_text(), self.live_text)

    def test_enforce_mode_file_blocks_apply_unless_allowed(self):
        self.mode.parent.mkdir(parents=True)
        self.mode.write_text('enforce\n')
        before = self.snapshot_tree()
        code, text = self.apply()
        self.assertEqual(code, 9)
        self.assertIn('WARN', text)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertEqual(self.apply('--allow-enforce')[0], 0)

    def test_warn_or_absent_mode_file_allows_apply(self):
        self.assertEqual(self.apply()[0], 0)
        self.live.write_text(self.live_text)
        self.dest.unlink()
        self.mode.parent.mkdir(parents=True)
        self.mode.write_text('warn\n')
        self.assertEqual(self.apply()[0], 0)

    def test_unmerged_hook_file_is_refused_without_the_rehearsal_flag(self):
        code, text = self.run_cli('apply', '--expect-live-sha', hashlib.sha256(self.live.read_bytes()).hexdigest(),
                                  '--expect-hook-sha', hashlib.sha256(self.src.read_bytes()).hexdigest())
        self.assertEqual(code, 7)
        self.assertIn('outside the repository', text)
        self.assertFalse(self.dest.exists())

    def test_conflicting_existing_entry_is_refused_by_apply(self):
        data = hr.with_gate(LIVE)
        data['hooks']['PreToolUse'][-1]['hooks'][0]['command'] += ' --admission-dir /x'
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        before = self.snapshot_tree()
        self.assertEqual(self.apply()[0], 2)
        self.assertEqual(self.snapshot_tree(), before)

    def test_reformatting_settings_needs_an_explicit_flag(self):
        compact = self.live_text.replace('"allow": [\n      "Bash(git status)"\n    ]', '"allow": ["Bash(git status)"]')
        self.live.write_text(compact)
        before = self.snapshot_tree()
        code, text = self.apply()
        self.assertEqual(code, 8)
        self.assertIn('--allow-reformat', text)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertEqual(self.apply('--allow-reformat')[0], 0)

    def test_an_existing_different_hook_file_is_backed_up(self):
        self.dest.parent.mkdir()
        self.dest.write_text('old hook')
        os.chmod(self.dest, 0o700)
        self.assertEqual(self.apply()[0], 0)
        (backup,) = [p for p in self.backups.iterdir() if p.name.startswith('disk-admission-gate.py.pre-SPE-109.')]
        self.assertEqual(backup.read_text(), 'old hook')
        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())
        data = json.loads(next(p for p in self.backups.iterdir() if 'receipt' in p.name).read_text())
        self.assertFalse(data['hook_created'])
        self.assertEqual(data['hook_backup'], str(backup))


if __name__ == '__main__':
    unittest.main()
