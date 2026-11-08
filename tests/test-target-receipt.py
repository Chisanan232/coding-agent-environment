#!/usr/bin/env python3
"""Build-time owner receipts for isolated Cargo targets (SPE-161 follow-up): ownership is claimed by creation only."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


da = load('disk_admission', 'scripts/disk-admission.py')
rl = load('resource_lifecycle', 'scripts/resource-lifecycle.py')
SESSION = 'f4323153-becb-4647-9f6c-40250cc182c8'
CLASS = 'build-cold-isolated'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'home'
        (self.home / '.cargo/shared-target/debug').mkdir(parents=True)
        self.base = Path(self.tmp.name).resolve()

    def write(self, target, session=SESSION, class_name=CLASS, env=None):
        environ = {'CARGO_TARGET_DIR': str(target)} if env is None else env
        return da.write_target_receipt(class_name, session, environ, home=self.home)

    def receipt(self, target):
        return json.loads((Path(target) / '.ca-owner.json').read_text())


class WriteTests(Base):
    def test_a_new_target_gets_a_private_receipt(self):
        target = self.base / 'iso/new'
        self.assertEqual(self.write(target), 'written')
        data = self.receipt(target)
        self.assertEqual((data['session_id'], data['reproducible'], data['private']), (SESSION, True, True))
        self.assertEqual(stat.S_IMODE((target / '.ca-owner.json').stat().st_mode), 0o600)

    def test_an_existing_empty_target_gets_one(self):
        target = self.base / 'iso'
        target.mkdir(mode=0o700)
        self.assertEqual(self.write(target), 'written')

    def test_a_non_empty_target_is_not_claimed(self):
        target = self.base / 'iso'
        target.mkdir(mode=0o700)
        (target / 'debug').mkdir()
        self.assertEqual(self.write(target), 'skipped:not-fresh')
        self.assertFalse((target / '.ca-owner.json').exists())

    def test_an_existing_receipt_is_never_overwritten(self):
        target = self.base / 'iso'
        target.mkdir(mode=0o700)
        (target / '.ca-owner.json').write_text('{"session_id": "someone-else-1234"}')
        self.assertEqual(self.write(target), 'skipped:not-fresh')
        self.assertIn('someone-else', (target / '.ca-owner.json').read_text())

    def test_the_shared_target_inside_it_and_above_it_are_refused(self):
        shared = self.home / '.cargo/shared-target'
        for target in (shared, shared / 'debug', shared / 'new-child', self.home / '.cargo', self.home):
            with self.subTest(target=str(target)):
                self.assertEqual(self.write(target), 'skipped:shared-target')
        self.assertFalse((shared / '.ca-owner.json').exists())

    def test_a_symlink_is_never_followed(self):
        real = self.base / 'real'
        real.mkdir(mode=0o700)
        link = self.base / 'link'
        link.symlink_to(real)
        self.assertEqual(self.write(link), 'skipped:symlink')
        self.assertEqual(list(real.iterdir()), [])

    def test_a_symlink_to_the_shared_target_is_refused(self):
        link = self.base / 'to-shared'
        link.symlink_to(self.home / '.cargo/shared-target')
        self.assertIn(self.write(link), ('skipped:symlink', 'skipped:shared-target'))
        self.assertFalse((self.home / '.cargo/shared-target/.ca-owner.json').exists())

    def test_only_an_explicit_absolute_cargo_target_dir_counts(self):
        for env in ({}, {'CARGO_TARGET_DIR': ''}, {'CARGO_TARGET_DIR': 'relative/target'}, {'CARGO_TARGET_DIR': 'x\0y'}):
            with self.subTest(env=env):
                self.assertEqual(self.write(self.base / 'iso', env=env), 'skipped:no-explicit-target')
        self.assertFalse((self.base / 'iso').exists())

    def test_other_classes_and_missing_or_malformed_sessions_write_nothing(self):
        target = self.base / 'iso'
        for class_name in ('build-crate', 'build-workspace', 'light', None):
            self.assertEqual(self.write(target, class_name=class_name), 'skipped:class')
        for session in (None, '', 'short', 'bad session id!', 7):
            self.assertEqual(self.write(target, session=session), 'skipped:no-session')
        self.assertFalse(target.exists())

    def test_a_group_writable_target_is_not_claimed(self):
        target = self.base / 'iso'
        target.mkdir()
        os.chmod(target, 0o770)
        self.assertEqual(self.write(target), 'skipped:not-private')

    def test_odd_input_never_raises(self):
        for environ in (None, {'CARGO_TARGET_DIR': None}, {'CARGO_TARGET_DIR': 5}):
            self.assertTrue(da.write_target_receipt(CLASS, SESSION, environ, home=self.home).startswith('skipped:'))


class LifecycleTests(Base):
    """The receipt this writes is the one resource-lifecycle trusts."""

    def classify(self, target, session):
        sessions = self.base / 'sessions'
        sessions.mkdir(exist_ok=True)
        with unittest.mock.patch.object(rl, 'open_paths', lambda: (set(), True)), \
                unittest.mock.patch.object(rl.os, 'getloadavg', lambda: (0.0, 0.0, 0.0)):
            ctx = rl.make_ctx(session, sessions)
            rec = rl.measure(target, 'cargo_target', ctx)
            return rl.classify(rec, ctx)

    def test_the_owner_session_sees_its_finished_isolated_target_as_class_a(self):
        target = self.base / 'iso'
        self.assertEqual(self.write(target), 'written')
        (target / 'debug').mkdir()
        (target / 'debug/blob').write_bytes(b'x' * 4096)
        self.assertEqual(self.classify(target, SESSION)[0], 'A')

    def test_another_session_does_not_get_class_a(self):
        target = self.base / 'iso'
        self.write(target)
        self.assertNotEqual(self.classify(target, '99999999-9999-9999-9999-999999999999')[0], 'A')


class RunPathTests(Base):
    def run_job(self, class_name, env):
        declared, error = da.resolve_workload(class_name, None, None, None, env={}, home=str(self.home), cwd=str(self.base),
                                              session_id=SESSION)
        self.assertIsNone(error)
        state = self.base / 'state/state.json'
        state.parent.mkdir(mode=0o700, exist_ok=True)
        ran = []
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            code = da.run_reserved('receipt-test', 1024, ['true'], state, free_fn=lambda: 500 * 2 ** 30,
                                   exec_fn=lambda program, args: ran.append(program), declared=declared)
        return code, ran

    def test_run_writes_the_receipt_before_exec(self):
        target = self.base / 'iso-run'
        code, ran = self.run_job(CLASS, {'CARGO_TARGET_DIR': str(target)})
        self.assertEqual((code, ran), (0, ['true']))
        self.assertEqual(self.receipt(target)['session_id'], SESSION)

    def test_run_of_another_class_writes_nothing(self):
        target = self.base / 'iso-run2'
        code, ran = self.run_job('build-crate', {'CARGO_TARGET_DIR': str(target)})
        self.assertEqual(code, 0)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
