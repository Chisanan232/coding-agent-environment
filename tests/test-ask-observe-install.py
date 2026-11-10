#!/usr/bin/env python3
"""The observer installer adds exactly four hook groups, keeps everything else, and rolls back surgically."""
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('ask_observe_install', ROOT / 'scripts/ask-observe-install.py')
ai = module_from_spec(spec)
spec.loader.exec_module(ai)

LIVE = {
    'permissions': {'allow': ['Bash(git status)'], 'ask': ['Bash(rtk proxy *)'], 'deny': []},
    'hooks': {
        'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'rtk hook claude'}]}],
        'PostToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python3 ~/.claude/hooks/bg-track.py'}]}],
        'Notification': [{'hooks': [{'type': 'command', 'command': 'terminal-notifier'}]}],
    },
    'statusLine': {'type': 'command', 'command': 'python3 ~/.claude/statusline.py'},
}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.live = t / 'settings.json'
        self.live.write_text(json.dumps(LIVE, indent=2) + '\n')
        self.src = t / 'ask-observe.py'
        self.src.write_text('# observer\n')
        self.dest = t / 'hooks' / 'ask-observe.py'
        self.backups = t / 'backups'
        self.command = ai.command_for(self.dest)

    def tearDown(self):
        self.tmp.cleanup()

    def run_mode(self, mode, *extra, expect=True):
        argv = [mode, '--live', str(self.live), '--hook-src', str(self.src), '--hook-dest', str(self.dest),
                '--backup-dir', str(self.backups), '--unmerged-ok', *extra]
        out = io.StringIO()
        with redirect_stdout(out):
            code = ai.main(argv)
        return code, out.getvalue()

    def pins(self):
        return ['--expect-hook-sha', ai.rollout.sha(self.src), '--expect-change-sha', ai.change_sha(self.command)]

    def apply(self):
        code, out = self.run_mode('apply', *self.pins())
        self.assertEqual(code, 0, out)
        return out.split('--receipt ')[1].split()[0] if '--receipt' in out else None

    def settings(self):
        return json.loads(self.live.read_text())


class Pure(unittest.TestCase):
    CMD = 'python3 ~/.claude/hooks/ask-observe.py hook'

    def test_with_hooks_appends_only_the_four_groups(self):
        new = ai.with_hooks(LIVE, self.CMD)
        for event in ai.EVENTS:
            self.assertEqual(new['hooks'][event][-1], ai.group_for(event, self.CMD))
        self.assertEqual(new['hooks']['PreToolUse'], LIVE['hooks']['PreToolUse'])
        self.assertEqual(new['hooks']['PostToolUse'][:-1], LIVE['hooks']['PostToolUse'])
        self.assertEqual(new['permissions'], LIVE['permissions'])
        self.assertEqual(new['statusLine'], LIVE['statusLine'])
        self.assertNotIn('PermissionRequest', LIVE['hooks'])  # input not mutated

    def test_groups_are_observe_only_and_bounded(self):
        for event, group in ai.change_set(self.CMD).items():
            self.assertEqual(group['hooks'], [{'type': 'command', 'command': self.CMD, 'timeout': 5}])
        self.assertEqual(ai.change_set(self.CMD)['Notification']['matcher'], 'permission_prompt')
        self.assertNotIn('matcher', ai.change_set(self.CMD)['PermissionRequest'])

    def test_state_absent_installed_conflict(self):
        self.assertEqual(ai.overall(LIVE, self.CMD), 'absent')
        self.assertEqual(ai.overall(ai.with_hooks(LIVE, self.CMD), self.CMD), 'installed')
        self.assertEqual(ai.overall(ai.with_hooks(ai.with_hooks(LIVE, self.CMD), self.CMD), self.CMD), 'conflict')
        partial = json.loads(json.dumps(LIVE))
        partial['hooks']['PermissionRequest'] = [ai.group_for('PermissionRequest', self.CMD)]
        self.assertEqual(ai.overall(partial, self.CMD), 'conflict')

    def test_without_hooks_is_the_inverse_and_keeps_later_unrelated_edits(self):
        installed = ai.with_hooks(LIVE, self.CMD)
        self.assertEqual(ai.without_hooks(installed, self.CMD), LIVE)
        installed['permissions']['allow'].append('Bash(ls)')
        installed['hooks']['Stop'] = [{'hooks': [{'type': 'command', 'command': 'x'}]}]
        out = ai.without_hooks(installed, self.CMD)
        self.assertIn('Bash(ls)', out['permissions']['allow'])
        self.assertIn('Stop', out['hooks'])
        self.assertNotIn('PermissionRequest', out['hooks'])

    def test_without_hooks_never_drops_a_foreign_hooks_object(self):
        self.assertEqual(ai.without_hooks({'hooks': {}}, self.CMD), {'hooks': {}})

    def test_change_sha_depends_on_the_command(self):
        self.assertNotEqual(ai.change_sha(self.CMD), ai.change_sha('python3 /x/ask-observe.py hook'))

    def test_dest_problem(self):
        self.assertIsNone(ai.dest_problem('/h/.claude/hooks/ask-observe.py'))
        for bad in ('~/ask-observe.py', 'ask-observe.py', '/h/../ask-observe.py', '/h/other.py', '/h//ask-observe.py'):
            self.assertIsNotNone(ai.dest_problem(bad), bad)


class Flow(Env):
    def test_plan_prints_hashes_diff_and_changes_nothing(self):
        before = self.live.read_bytes()
        code, out = self.run_mode('plan')
        self.assertEqual(code, 0)
        self.assertIn(ai.rollout.sha(self.src), out)
        self.assertIn(ai.change_sha(self.command), out)
        self.assertIn('"PermissionRequest"', out)
        self.assertEqual(self.live.read_bytes(), before)
        self.assertFalse(self.dest.exists())

    def test_apply_needs_both_pins(self):
        for extra in ([], ['--expect-hook-sha', 'x', '--expect-change-sha', ai.change_sha(self.command)],
                      ['--expect-hook-sha', ai.rollout.sha(self.src), '--expect-change-sha', 'x']):
            before = self.live.read_bytes()
            code, _ = self.run_mode('apply', *extra)
            self.assertEqual(code, 3)
            self.assertEqual(self.live.read_bytes(), before)
        self.assertFalse(self.dest.exists())

    def test_apply_installs_file_and_four_groups_and_preserves_the_rest(self):
        self.apply()
        got = self.settings()
        self.assertEqual(ai.without_hooks(got, self.command), LIVE)
        self.assertEqual(ai.overall(got, self.command), 'installed')
        self.assertTrue(os.access(self.dest, os.X_OK))
        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())

    def test_apply_is_idempotent(self):
        self.apply()
        after = self.live.read_bytes()
        code, out = self.run_mode('apply', *self.pins())
        self.assertEqual((code, self.live.read_bytes()), (0, after))
        self.assertIn('already installed', out)

    def test_apply_refuses_a_foreign_observer_entry(self):
        data = json.loads(json.dumps(LIVE))
        data['hooks']['PermissionRequest'] = [{'hooks': [{'type': 'command', 'command': 'python3 /other/ask-observe.py hook'}]}]
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        before = self.live.read_bytes()
        code, _ = self.run_mode('apply', *self.pins())
        self.assertEqual(code, 2)
        self.assertEqual(self.live.read_bytes(), before)

    def test_apply_refuses_reformatting_without_the_flag(self):
        self.live.write_text(json.dumps(LIVE) + '\n')
        before = self.live.read_bytes()
        code, _ = self.run_mode('apply', *self.pins())
        self.assertEqual((code, self.live.read_bytes()), (8, before))
        code, _ = self.run_mode('apply', *self.pins(), '--allow-reformat')
        self.assertEqual(code, 0)

    def test_apply_refuses_invalid_settings(self):
        self.live.write_text('{not json')
        code, _ = self.run_mode('apply', *self.pins())
        self.assertEqual(code, 10)
        self.live.write_text(json.dumps({'hooks': {'Notification': 'x'}}))
        self.assertEqual(self.run_mode('apply', *self.pins())[0], 10)

    def test_apply_refuses_a_symlinked_settings_file(self):
        real = self.live.with_name('real.json')
        self.live.rename(real)
        self.live.symlink_to(real)
        self.assertEqual(self.run_mode('apply', *self.pins())[0], 4)

    def test_unmerged_hook_file_is_refused_without_the_rehearsal_flag(self):
        out = io.StringIO()
        argv = ['apply', '--live', str(self.live), '--hook-src', str(self.src), '--hook-dest', str(self.dest),
                '--backup-dir', str(self.backups), *self.pins()]
        with redirect_stdout(out):
            code = ai.main(argv)
        self.assertEqual(code, 7)  # the temp file is outside the repository
        self.assertFalse(self.dest.exists())

    def test_verify_ok_then_detects_a_removed_group_and_a_changed_hook_file(self):
        receipt = self.apply()
        self.assertEqual(self.run_mode('verify', '--receipt', receipt)[0], 0)
        self.dest.write_text('# tampered\n')
        code, out = self.run_mode('verify')
        self.assertEqual(code, 1)
        self.assertIn('hash differs', out)

    def test_verify_flags_changed_permissions(self):
        receipt = self.apply()
        data = self.settings()
        data['permissions']['deny'].append('Bash(rm *)')
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        code, out = self.run_mode('verify', '--receipt', receipt)
        self.assertEqual(code, 1)
        self.assertIn('permissions differ', out)

    def test_rollback_restores_settings_and_removes_created_hook_file(self):
        receipt = self.apply()
        code, out = self.run_mode('rollback', '--receipt', receipt)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.settings(), LIVE)
        self.assertFalse(self.dest.exists())

    def test_rollback_keeps_edits_made_after_apply(self):
        receipt = self.apply()
        data = self.settings()
        data['permissions']['allow'].append('Bash(ls)')
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        self.run_mode('rollback', '--receipt', receipt)
        got = self.settings()
        self.assertIn('Bash(ls)', got['permissions']['allow'])
        self.assertEqual(ai.overall(got, self.command), 'absent')
        self.assertTrue(list(self.live.parent.glob('settings.json.pre-rollback.*')))

    def test_rollback_twice_is_harmless(self):
        receipt = self.apply()
        self.run_mode('rollback', '--receipt', receipt)
        code, out = self.run_mode('rollback', '--receipt', receipt)
        self.assertEqual(code, 0)
        self.assertEqual(self.settings(), LIVE)

    def test_rollback_leaves_a_modified_hook_file(self):
        receipt = self.apply()
        self.dest.write_text('# edited by someone\n')
        code, out = self.run_mode('rollback', '--receipt', receipt)
        self.assertEqual(code, 0)
        self.assertTrue(self.dest.exists())
        self.assertIn('left', out)

    def test_rollback_restores_a_replaced_hook_file(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_text('# old\n')
        receipt = self.apply()
        self.assertEqual(self.dest.read_text(), '# observer\n')
        self.run_mode('rollback', '--receipt', receipt)
        self.assertEqual(self.dest.read_text(), '# old\n')

    def test_rollback_refuses_a_foreign_entry(self):
        receipt = self.apply()
        data = self.settings()
        data['hooks']['PermissionRequest'].append({'hooks': [{'type': 'command', 'command': 'python3 /o/ask-observe.py hook'}]})
        self.live.write_text(json.dumps(data, indent=2) + '\n')
        before = self.live.read_bytes()
        self.assertEqual(self.run_mode('rollback', '--receipt', receipt)[0], 2)
        self.assertEqual(self.live.read_bytes(), before)

    def test_receipt_and_backup_are_private_and_hold_the_pre_apply_bytes(self):
        before = self.live.read_bytes()
        receipt = self.apply()
        self.assertEqual(stat.S_IMODE(os.stat(receipt).st_mode), 0o600)
        data = json.loads(Path(receipt).read_text())
        self.assertEqual(Path(data['settings_backup']).read_bytes(), before)

    def test_rollback_refuses_to_reformat_later_edits_without_the_flag(self):
        receipt = self.apply()
        data = self.settings()
        data['permissions']['allow'].append('Bash(\u00e9)')
        self.live.write_text(json.dumps(data, indent=2, ensure_ascii=True) + '\n')
        before = self.live.read_bytes()
        self.assertEqual(self.run_mode('rollback', '--receipt', receipt)[0], 8)
        self.assertEqual(self.live.read_bytes(), before)
        self.assertEqual(self.run_mode('rollback', '--receipt', receipt, '--allow-reformat')[0], 0)

    def test_rollback_removes_keys_apply_created_and_keeps_ones_that_existed(self):
        bare = {'permissions': {'allow': []}}
        self.live.write_text(json.dumps(bare, indent=2) + '\n')
        receipt = self.apply()
        self.run_mode('rollback', '--receipt', receipt)
        self.assertEqual(self.settings(), bare)
        empty_list = {'hooks': {'PermissionRequest': []}}
        self.live.write_text(json.dumps(empty_list, indent=2) + '\n')
        receipt = self.apply()
        self.run_mode('rollback', '--receipt', receipt)
        self.assertEqual(self.settings(), empty_list)

    def test_malformed_or_missing_receipt_is_a_refusal_not_a_traceback(self):
        self.assertEqual(self.run_mode('rollback', '--receipt', str(self.backups / 'nope.json'))[0], 2)
        bad = Path(self.tmp.name) / 'bad.json'
        bad.write_text('{"live": "/x"}')
        self.assertEqual(self.run_mode('rollback', '--receipt', str(bad))[0], 2)

    def test_a_tampered_receipt_path_is_refused(self):
        receipt = self.apply()
        data = json.loads(Path(receipt).read_text())
        data['hook_dest'] = '/etc/cron.d/evil'
        Path(receipt).write_text(json.dumps(data))
        self.assertEqual(self.run_mode('rollback', '--receipt', receipt)[0], 2)

    def test_missing_hook_source_is_a_refusal(self):
        self.src.unlink()
        self.assertEqual(self.run_mode('apply', '--expect-hook-sha', 'x', '--expect-change-sha', 'x')[0], 2)

    def test_lock_contention_refuses_apply_and_rollback(self):
        receipt = self.apply()
        self.backups.mkdir(exist_ok=True)
        lock = ai.hr.take_lock(self.backups)
        try:
            self.assertEqual(self.run_mode('rollback', '--receipt', receipt)[0], 11)
        finally:
            os.close(lock)

    def test_a_settings_change_during_apply_undoes_the_hook_and_leaves_no_receipt(self):
        original = ai.hr.replace_if_unchanged

        def racing(path, data, expected):
            path.write_text(path.read_text() + ' ')
            return original(path, data, expected)
        ai.hr.replace_if_unchanged = racing
        try:
            code, _ = self.run_mode('apply', *self.pins())
        finally:
            ai.hr.replace_if_unchanged = original
        self.assertEqual(code, 5)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.backups.glob('ask-observe.receipt.*')), [])

    def test_an_io_failure_during_the_replace_leaves_no_half_state(self):
        original = ai.hr.replace_if_unchanged

        def failing(path, data, expected):
            raise OSError('disk full')
        ai.hr.replace_if_unchanged = failing
        try:
            with self.assertRaises(OSError):
                self.run_mode('apply', *self.pins())
        finally:
            ai.hr.replace_if_unchanged = original
        self.assertFalse(self.dest.exists())
        self.assertEqual(ai.overall(self.settings(), self.command), 'absent')
        self.assertEqual(list(self.backups.glob('ask-observe.receipt.*')), [])

    def test_receipt_exists_before_settings_change_and_records_created_keys(self):
        receipt = json.loads(Path(self.apply()).read_text())
        self.assertEqual((receipt['hooks_created'], receipt['events_created']), (False, ['PermissionRequest', 'PostToolUseFailure']))

    def test_backups_dir_is_private(self):
        self.apply()
        self.assertEqual(stat.S_IMODE(os.stat(self.backups).st_mode), 0o700)

    def test_a_receipt_pointing_at_a_backup_outside_the_backup_dir_is_refused(self):
        receipt = self.apply()
        data = json.loads(Path(receipt).read_text())
        data['hook_backup'] = str(Path(self.tmp.name) / 'planted.py')
        Path(receipt).write_text(json.dumps(data))
        self.assertEqual(self.run_mode('rollback', '--receipt', receipt)[0], 2)

    def test_a_failure_before_the_receipt_is_written_undoes_the_hook_file(self):
        original = ai.rollout.write_new
        calls = []

        def failing(path, data, mode):
            calls.append(path)
            if 'receipt' in Path(path).name:
                raise OSError('disk full')
            return original(path, data, mode)
        ai.rollout.write_new = failing
        try:
            with self.assertRaises(OSError):
                self.run_mode('apply', *self.pins())
        finally:
            ai.rollout.write_new = original
        self.assertFalse(self.dest.exists())

    def test_plan_and_verify_refuse_a_missing_hook_file(self):
        self.src.unlink()
        self.assertEqual(self.run_mode('plan')[0], 2)
        self.assertEqual(self.run_mode('verify')[0], 2)

    def test_an_existing_backup_dir_is_tightened_to_private(self):
        self.backups.mkdir(mode=0o755)
        os.chmod(self.backups, 0o755)
        self.apply()
        self.assertEqual(stat.S_IMODE(os.stat(self.backups).st_mode), 0o700)

    def test_rollback_refuses_a_symlinked_backup_dir_and_does_not_chmod_its_target(self):
        receipt = self.apply()
        data = json.loads(Path(receipt).read_text())
        data['settings_backup'] = data['hook_backup'] = None  # so only the symlinked directory itself can refuse
        Path(receipt).write_text(json.dumps(data))
        real = Path(self.tmp.name) / 'real-backups'
        real.mkdir(mode=0o755)
        os.chmod(real, 0o755)
        link = Path(self.tmp.name) / 'link-backups'
        link.symlink_to(real)
        code, _ = self.run_mode('rollback', '--receipt', receipt, '--backup-dir', str(link))
        self.assertEqual(code, 4)
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
