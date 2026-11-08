#!/usr/bin/env python3
"""Class-level Cargo defaults (SPE-161): build-cold-isolated runs without incremental compilation unless the job chose otherwise."""
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


def declared(name):
    out, error = da.resolve_workload(name, None, None, None, env={}, home='/nonexistent-home', cwd='/work/wt')
    assert error is None
    return out


class ClassEnvTests(unittest.TestCase):
    def test_isolated_build_turns_incremental_off_when_unset(self):
        self.assertEqual(da.class_env(declared('build-cold-isolated'), {}), {'CARGO_INCREMENTAL': '0'})

    def test_a_job_that_chose_its_own_value_keeps_it(self):
        for value in ('1', '0', ''):
            with self.subTest(value=value):
                self.assertEqual(da.class_env(declared('build-cold-isolated'), {'CARGO_INCREMENTAL': value}), {})

    def test_warm_and_other_classes_keep_cargo_defaults(self):
        for name in ('build-crate', 'build-workspace', 'container-suite', 'light', 'remote'):
            with self.subTest(name=name):
                self.assertEqual(da.class_env(declared(name), {}), {})

    def test_legacy_runs_without_a_class_change_nothing(self):
        self.assertEqual(da.class_env(None, {}), {})
        self.assertEqual(da.class_env({}, {}), {})
        self.assertEqual(da.class_env({'class': None}, {}), {})

    def test_the_default_does_not_depend_on_a_budget(self):
        # grant_env is silent without a budget (sampling not requested); the class default is not tied to it.
        self.assertEqual(da.grant_env(None, declared('build-cold-isolated'), {}), {})
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CARGO_INCREMENTAL', None)
            da.apply_grant_env(None, declared('build-cold-isolated'))
            self.assertEqual(os.environ.get('CARGO_INCREMENTAL'), '0')

    def test_apply_never_overrides_the_job_or_touches_other_classes(self):
        with unittest.mock.patch.dict(os.environ, {'CARGO_INCREMENTAL': '1'}):
            da.apply_grant_env(None, declared('build-cold-isolated'))
            self.assertEqual(os.environ['CARGO_INCREMENTAL'], '1')
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CARGO_INCREMENTAL', None)
            da.apply_grant_env(None, declared('build-workspace'))
            da.apply_grant_env(None, None)
            self.assertNotIn('CARGO_INCREMENTAL', os.environ)

    def test_no_profile_or_toolchain_variable_is_ever_set(self):
        allowed = {'CARGO_INCREMENTAL'}
        for defaults in da.CLASS_ENV.values():
            self.assertLessEqual(set(defaults), allowed)


if __name__ == '__main__':
    unittest.main()
