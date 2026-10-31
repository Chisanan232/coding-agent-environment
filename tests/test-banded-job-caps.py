#!/usr/bin/env python3
"""Banded per-job caps (SPE-150 PR6): granted jobs and test threads exported to a declared Cargo class, never above the job's own."""
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
import os
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
shadow = load('shadow_budget_tests', 'tests/test-shadow-budget.py')
queue_tests = load('admission_queue_tests', 'tests/test-admission-queue.py')
GIB = 1024 ** 3
VARS = ('CARGO_BUILD_JOBS', 'RUST_TEST_THREADS', 'NEXTEST_TEST_THREADS')


def fields(name='build-crate'):
    out, error = da.resolve_workload(name, None, None, None, env={}, home='/nonexistent-home', cwd='/work/wt')
    assert error is None
    return out


def budget(name='build-crate', band='GREEN', **kw):
    request = shadow.request(dict(fields(name), excl_keys=[]) if name else {})
    return da.budget_decision(shadow.host(band=band, **kw), [], request, shadow.CAPS)


class GrantEnv(unittest.TestCase):
    def test_green_exports_the_full_class_defaults(self):
        self.assertEqual(da.grant_env(budget(), fields(), {}), {'CARGO_BUILD_JOBS': '4', 'RUST_TEST_THREADS': '4'})
        self.assertEqual(da.grant_env(budget('build-workspace'), fields('build-workspace'), {}),
                         {'CARGO_BUILD_JOBS': '8', 'RUST_TEST_THREADS': '8', 'NEXTEST_TEST_THREADS': '8'})
        self.assertIn('NEXTEST_TEST_THREADS', da.grant_env(budget('build-cold-isolated'), fields('build-cold-isolated'), {}))

    def test_amber_reduces_to_the_class_minimum(self):
        self.assertEqual(da.grant_env(budget(band='AMBER'), fields(), {}), {'CARGO_BUILD_JOBS': '2', 'RUST_TEST_THREADS': '2'})
        self.assertEqual(da.grant_env(budget('build-workspace', 'AMBER'), fields('build-workspace'), {})['CARGO_BUILD_JOBS'], '4')

    def test_red_grants_none(self):
        self.assertEqual(da.grant_env(budget(band='RED'), fields(), {}), {})

    def test_an_unavailable_budget_changes_nothing(self):
        for bad in (None, {}, da.budget_decision(None, [], None, shadow.CAPS), {'status': 'unavailable', 'granted_jobs': 4}):
            self.assertEqual(da.grant_env(bad, fields(), {}), {})

    def test_no_class_or_a_class_without_an_entry_exports_nothing(self):
        self.assertEqual(da.grant_env(budget(), {}, {}), {})
        self.assertEqual(da.grant_env(budget(), None, {}), {})
        for name in ('light', 'remote', 'container-suite'):
            self.assertEqual(da.grant_env(budget(), fields(name), {}), {}, name)

    def test_an_explicit_setting_is_respected_and_only_ever_lowered(self):
        env = {'CARGO_BUILD_JOBS': '2', 'RUST_TEST_THREADS': '16'}
        self.assertEqual(da.grant_env(budget(), fields(), env), {'CARGO_BUILD_JOBS': '2', 'RUST_TEST_THREADS': '4'})
        self.assertEqual(da.grant_env(budget(), fields(), {'CARGO_BUILD_JOBS': '4'})['CARGO_BUILD_JOBS'], '4')

    def test_unusable_existing_values_take_the_grant(self):
        for bad in ('', 'many', '0', '-1', '2.5'):
            self.assertEqual(da.grant_env(budget(), fields(), {'CARGO_BUILD_JOBS': bad})['CARGO_BUILD_JOBS'], '4', bad)

    def test_the_cap_is_still_one_lane(self):
        self.assertEqual(da.HEAVY_LANES, {'emergency': 0, 'low': 0, 'stable': 1, 'wide': 1})


class RunEnv(queue_tests.QueueBase):
    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in VARS:
            os.environ.pop(name, None)
        self.seen = []

    def run_job(self, declared, host_fn, label='job'):
        def capture(program, args):
            self.seen.append({k: os.environ.get(k) for k in VARS})
        return da.run_reserved(label, 20 * GIB, ['true'], self.path, lambda: 400 * GIB, exec_fn=capture, declared=declared, host_fn=host_fn)

    def host_fn(self, **kw):
        return lambda: shadow.host(time=self.now + timedelta(days=3650), **kw)

    def test_green_and_amber_runs_export_their_band(self):
        self.assertEqual(self.run_job(fields(), self.host_fn()), 0)
        self.assertEqual(self.seen[-1], {'CARGO_BUILD_JOBS': '4', 'RUST_TEST_THREADS': '4', 'NEXTEST_TEST_THREADS': None})
        da.release('job', self.path)
        for name in VARS:
            os.environ.pop(name, None)
        self.assertEqual(self.run_job(fields(), self.host_fn(band='AMBER')), 0)
        self.assertEqual(self.seen[-1]['CARGO_BUILD_JOBS'], '2')

    def test_red_unavailable_and_unrequested_budgets_leave_the_environment_alone(self):
        def boom():
            raise OSError('no sample')
        for label, hf in (('red', self.host_fn(band='RED')), ('down', boom), ('none', None)):
            self.assertEqual(self.run_job(fields(), hf, label), 0)
            self.assertEqual(self.seen[-1], dict.fromkeys(VARS), label)
            da.release(label, self.path)

    def test_legacy_runs_are_untouched(self):
        self.assertEqual(self.run_job({}, self.host_fn()), 0)
        self.assertEqual(self.seen[-1], dict.fromkeys(VARS))

    def test_an_explicit_lower_setting_survives(self):
        os.environ['CARGO_BUILD_JOBS'] = '1'
        os.environ['RUST_TEST_THREADS'] = '32'
        self.run_job(fields(), self.host_fn())
        self.assertEqual((self.seen[-1]['CARGO_BUILD_JOBS'], self.seen[-1]['RUST_TEST_THREADS']), ('1', '4'))

    def test_a_refused_run_never_reaches_the_exec_or_the_environment(self):
        proc = self.holder('busy')
        self.assertEqual(self.run_job(fields(), self.host_fn()), 77)
        self.assertEqual(self.seen, [])
        self.assertIsNone(os.environ.get('CARGO_BUILD_JOBS'))
        del proc

    def test_queue_claim_exports_the_same_grant(self):
        self.assertTrue(self.enqueue(declared=fields())['ok'])
        qid = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)['granted'][0]
        entry = self.entries()[qid]
        out = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], shadow.ARGV, str(self.worktree), lambda: 400 * GIB,
                             pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'),
                             exec_fn=lambda p, a: self.seen.append({k: os.environ.get(k) for k in VARS}), declared=fields(),
                             host_fn=self.host_fn(band='AMBER'))
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.seen[-1]['CARGO_BUILD_JOBS'], '2')
        self.assertEqual(out['budget']['granted_jobs'], 2)


if __name__ == '__main__':
    unittest.main()
