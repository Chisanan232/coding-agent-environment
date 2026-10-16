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
        self.command = hr.command_for(self.dest)
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
        data = hr.with_gate(LIVE, self.command)
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
        self.assertEqual(new, hr.with_gate(LIVE, self.command))
        self.assertEqual(hr.gate_state(new, self.command), 'installed')
        self.assertEqual(self.live.read_text(), json.dumps(hr.with_gate(LIVE, self.command), indent=2, ensure_ascii=False) + '\n')
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
                                    '~/.claude/hooks/fornax-guard', self.command])
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
        data = hr.with_gate(LIVE, self.command)
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

    def test_a_concurrent_settings_change_aborts_the_write(self):
        real = hr.rollout.write_new

        def write_then_change(path, data, mode):
            real(path, data, mode)
            if path.name.startswith('settings.json.pre-SPE-109'):
                self.live.write_text(self.live_text.replace('git status', 'git log'))
        with mock.patch.object(hr.rollout, 'write_new', write_then_change):
            code, text = self.apply()
        self.assertEqual(code, 5)
        self.assertIn('changed while', text)
        self.assertFalse(self.dest.exists())
        self.assertNotIn('disk-admission-gate', self.live.read_text())

    def test_verify_passes_after_apply_and_reports_the_mode(self):
        self.assertEqual(self.apply()[0], 0)
        receipt = next(p for p in self.backups.iterdir() if 'receipt' in p.name)
        code, text = self.run_cli('verify', '--receipt', str(receipt))
        self.assertEqual(code, 0, text)
        self.assertIn('verify OK', text)
        self.assertIn('mode file: absent', text)

    def test_verify_fails_before_apply_and_for_a_duplicate_entry(self):
        self.assertEqual(self.run_cli('verify')[0], 1)
        self.assertEqual(self.apply()[0], 0)
        data = json.loads(self.live.read_text())
        data['hooks']['PreToolUse'].append(hr.gate_group(self.command))
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        code, text = self.run_cli('verify')
        self.assertEqual(code, 1)
        self.assertIn('found 2', text)

    def test_verify_fails_on_a_changed_hook_file(self):
        self.assertEqual(self.apply()[0], 0)
        self.dest.write_text('tampered')
        os.chmod(self.dest, 0o755)
        code, text = self.run_cli('verify')
        self.assertEqual(code, 1)
        self.assertIn('hash differs', text)

    def test_verify_flags_an_enforce_mode_file_unless_allowed(self):
        self.assertEqual(self.apply()[0], 0)
        self.mode.parent.mkdir(parents=True)
        self.mode.write_text('enforce')
        code, text = self.run_cli('verify')
        self.assertEqual(code, 1)
        self.assertIn('enforce', text)
        self.assertEqual(self.run_cli('verify', '--allow-enforce')[0], 0)

    def test_verify_fails_when_a_deny_rule_was_added(self):
        self.assertEqual(self.apply()[0], 0)
        receipt = next(p for p in self.backups.iterdir() if 'receipt' in p.name)
        data = json.loads(self.live.read_text())
        data['permissions']['deny'] = ['Bash(cargo build:*)']
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        code, text = self.run_cli('verify', '--receipt', str(receipt))
        self.assertEqual(code, 1)
        self.assertIn('deny', text)

    def receipt(self):
        return next(p for p in self.backups.iterdir() if 'receipt' in p.name)

    def test_rollback_restores_the_settings_hash_exact_and_removes_the_new_hook(self):
        before = self.live.read_bytes()
        self.assertEqual(self.apply()[0], 0)
        code, text = self.run_cli('rollback', '--receipt', str(self.receipt()))
        self.assertEqual(code, 0, text)
        self.assertEqual(hashlib.sha256(self.live.read_bytes()).hexdigest(), hashlib.sha256(before).hexdigest())
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o600)
        self.assertFalse(self.dest.exists())
        self.assertEqual(len([p for p in self.live.parent.iterdir() if '.pre-rollback.' in p.name]), 1)

    def test_rollback_restores_a_previous_hook_file(self):
        self.dest.parent.mkdir()
        self.dest.write_text('old hook')
        os.chmod(self.dest, 0o700)
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.run_cli('rollback', '--receipt', str(self.receipt()))[0], 0)
        self.assertEqual(self.dest.read_text(), 'old hook')

    def test_rollback_is_idempotent(self):
        self.assertEqual(self.apply()[0], 0)
        receipt = str(self.receipt())
        self.assertEqual(self.run_cli('rollback', '--receipt', receipt)[0], 0)
        after = self.snapshot_tree()
        code, text = self.run_cli('rollback', '--receipt', receipt)
        self.assertEqual(code, 0)
        self.assertIn('already match', text)
        self.assertEqual(self.snapshot_tree(), after)

    def test_rollback_refuses_a_tampered_backup(self):
        self.assertEqual(self.apply()[0], 0)
        backup = next(p for p in self.backups.iterdir() if p.name.startswith('settings.json.pre-SPE-109.'))
        backup.write_text('{"hooks": {}}')
        installed = self.live.read_bytes()
        code, text = self.run_cli('rollback', '--receipt', str(self.receipt()))
        self.assertEqual(code, 3)
        self.assertIn('no longer matches', text)
        self.assertEqual(self.live.read_bytes(), installed)

    def test_rollback_keeps_a_hook_file_that_was_changed_after_apply(self):
        self.assertEqual(self.apply()[0], 0)
        self.dest.write_text('operator edit')
        code, text = self.run_cli('rollback', '--receipt', str(self.receipt()))
        self.assertEqual(code, 0)
        self.assertIn('left', text)
        self.assertEqual(self.dest.read_text(), 'operator edit')

    def test_rollback_without_a_receipt_is_refused(self):
        code, text = self.run_cli('rollback')
        self.assertEqual(code, 2)
        self.assertIn('--receipt', text)

    def refuses_bad_settings(self, content):
        self.live.write_bytes(content)
        before = self.snapshot_tree()
        for mode in ('plan', 'verify'):
            code, text = self.run_cli(mode)
            self.assertEqual(code, 10, (mode, text))
            self.assertIn('REFUSING', text)
        code, text = self.apply('--allow-reformat')
        self.assertEqual(code, 10, text)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertFalse(self.dest.exists())

    def test_null_hooks_object_is_refused_cleanly(self):
        self.refuses_bad_settings(b'{"hooks": null, "permissions": {}}\n')

    def test_null_pretooluse_is_refused_cleanly(self):
        self.refuses_bad_settings(b'{"hooks": {"PreToolUse": null}}\n')
        self.refuses_bad_settings(b'{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": null}]}}\n')

    def test_a_root_array_is_refused_cleanly(self):
        self.refuses_bad_settings(b'[{"hooks": {}}]\n')

    def test_non_utf8_settings_are_refused_cleanly(self):
        self.refuses_bad_settings(b'{"statusLine": "\xff\xfe"}\n')

    def test_invalid_json_and_a_missing_file_are_refused_cleanly(self):
        self.refuses_bad_settings(b'{not json')
        self.live.unlink()
        self.assertEqual(self.run_cli('plan')[0], 10)

    def test_symlinked_backup_directory_is_refused(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.backups.parent.mkdir(exist_ok=True)
        self.backups.symlink_to(elsewhere)
        before = self.snapshot_tree()
        code, text = self.apply()
        self.assertEqual(code, 4)
        self.assertIn('symlink', text)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.live.read_text(), self.live_text)

    def test_a_change_between_the_temp_write_and_the_rename_is_detected(self):
        real_chmod = os.chmod

        def chmod_then_change(path, mode):
            real_chmod(path, mode)
            if os.path.basename(str(path)).startswith('settings.json.hookroll.'):
                self.live.write_text(self.live_text.replace('git status', 'git log'))
        with mock.patch.object(hr.os, 'chmod', chmod_then_change):
            code, text = self.apply()
        self.assertEqual(code, 5)
        self.assertIn('immediately before the write', text)
        self.assertEqual(self.live.read_text(), self.live_text.replace('git status', 'git log'))
        self.assertFalse(self.dest.exists())
        self.assertEqual([p.name for p in self.live.parent.iterdir() if '.hookroll.' in p.name], [])

    def test_a_second_apply_while_the_lock_is_held_is_refused(self):
        import fcntl
        self.backups.mkdir(parents=True)
        holder = os.open(str(self.backups / '.hook-rollout.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        before = self.snapshot_tree()
        code, text = self.apply()
        self.assertEqual(code, 11)
        self.assertIn('another apply', text)
        self.assertEqual(self.snapshot_tree(), before)
        fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertEqual(self.apply()[0], 0)

    def test_rollback_detects_a_change_just_before_the_rename(self):
        self.assertEqual(self.apply()[0], 0)
        receipt = str(self.receipt())
        real_chmod = os.chmod

        def chmod_then_change(path, mode):
            real_chmod(path, mode)
            if os.path.basename(str(path)).startswith('settings.json.hookroll.'):
                self.live.write_text('{"statusLine": "concurrent writer"}\n')
        with mock.patch.object(hr.os, 'chmod', chmod_then_change):
            code, text = self.run_cli('rollback', '--receipt', receipt)
        self.assertEqual(code, 5)
        self.assertIn('immediately before the restore', text)
        self.assertEqual(self.live.read_text(), '{"statusLine": "concurrent writer"}\n')

    def test_apply_writes_the_command_for_the_given_destination(self):
        self.assertEqual(self.apply()[0], 0)
        command = json.loads(self.live.read_text())['hooks']['PreToolUse'][-1]['hooks'][0]['command']
        self.assertEqual(command, 'python3 %s' % self.dest)
        self.assertTrue(Path(command.split(' ', 1)[1]).is_file())

    def test_an_entry_pointing_at_a_different_destination_is_a_conflict(self):
        self.assertEqual(self.apply()[0], 0)
        other = self.root / 'other/disk-admission-gate.py'
        code, text = self.run_cli('plan', '--hook-dest', str(other))
        self.assertEqual(code, 2)
        self.assertIn('different disk-admission-gate entry', text)
        before = self.snapshot_tree()
        self.assertEqual(self.run_cli('apply', '--hook-dest', str(other), *self.pins())[0], 2)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertEqual(self.run_cli('verify', '--hook-dest', str(other))[0], 1)

    def refuses_dest(self, value):
        before = self.snapshot_tree()
        for mode in ('plan', 'apply', 'verify'):
            code, text = self.run_cli(mode, '--hook-dest', value, *(self.pins()[:4] if mode == 'apply' else []),
                                      *(['--unmerged-ok'] if mode == 'apply' else []))
            self.assertEqual(code, 12, (mode, value, text))
            self.assertIn('--hook-dest', text)
        self.assertEqual(self.snapshot_tree(), before)
        self.assertEqual(self.live.read_text(), self.live_text)
        self.assertFalse(os.path.exists('~') and 'x' in os.listdir('~'))

    def test_a_relative_hook_destination_is_refused(self):
        self.refuses_dest('hooks/disk-admission-gate.py')
        self.refuses_dest('./disk-admission-gate.py')

    def test_a_tilde_hook_destination_is_refused_without_creating_a_directory(self):
        self.refuses_dest('~/x/disk-admission-gate.py')
        self.assertFalse(Path('~').exists() and (Path('~') / 'x').exists())

    def test_a_destination_with_dot_dot_components_is_refused(self):
        self.refuses_dest(str(self.root) + '/a/../b/disk-admission-gate.py')
        self.refuses_dest(str(self.root) + '//double/disk-admission-gate.py')


class CommandDerivation(unittest.TestCase):
    def test_command_uses_the_tilde_form_under_home_and_quotes_other_paths(self):
        self.assertEqual(hr.command_for(hr.HOME / '.claude/hooks/disk-admission-gate.py'),
                         'python3 ~/.claude/hooks/disk-admission-gate.py')
        self.assertEqual(hr.command_for(hr.HOME / '.claude/hooks/disk-admission-gate.py'), hr.GATE_COMMAND)
        self.assertEqual(hr.command_for('/opt/hooks/disk-admission-gate.py'), 'python3 /opt/hooks/disk-admission-gate.py')
        self.assertEqual(hr.command_for('/opt/my hooks/g.py'), "python3 '/opt/my hooks/g.py'")


if __name__ == '__main__':
    unittest.main()
