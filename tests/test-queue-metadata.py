#!/usr/bin/env python3
"""Admission metadata (SPE-176): the exclusion key, class and exclusion of a queued job must describe what the job really builds.

Found 2026-11-26 on the live queue: a `bash script` job that exports its own CARGO_TARGET_DIR was keyed on the shared target (the wrapper
cannot see a variable set inside the script), a `cargo build --workspace` script was charged as a single crate, and an omitted --class
left a pytest gate classless. Every test runs against temporary state; the owner's queue is never read or written.
"""
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location, spec_from_loader
from pathlib import Path
import io
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
GIB = 2 ** 30
loader = SourceFileLoader('heavy_run_meta', str(ROOT / 'scripts/heavy-run'))
hr = module_from_spec(spec_from_loader('heavy_run_meta', loader))
loader.exec_module(hr)


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bf = load('backfill_for_meta', 'tests/test-admission-backfill.py')
da = bf.da
cs = load('classify_for_meta', 'scripts/classify-step.py')


def args_for(command, **kw):
    base = dict(command=command, workload_class=None, cargo_target_dir='', no_cargo_target=False, bytes=12 * GIB)
    base.update(kw)
    return types.SimpleNamespace(**base)


class Scripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve()

    def script(self, body):
        path = self.dir / 'job.sh'
        path.write_text('#!/bin/bash\n' + body)
        return ['bash', str(path)]

    def prepare(self, body, **kw):
        args = args_for(self.script(body), **kw)
        env = {}
        with redirect_stderr(io.StringIO()):
            error = hr.prepare(args, env)
        return error, env, args

    def key(self, env, klass='build-workspace'):
        fields, err = da.resolve_workload(klass, None, None, None, env=env, home=str(self.dir), cwd=str(self.dir), argv=['bash', 'job.sh'])
        self.assertIsNone(err)
        return fields['excl_keys']

    def test_a_literal_target_the_script_exports_becomes_the_exclusion_key(self):
        private = self.dir / 'private-1'
        error, env, _ = self.prepare(f'export CARGO_TARGET_DIR={private}\ncargo build -p x\n')
        self.assertIsNone(error)
        self.assertEqual(self.key(env), ['cargo-target:' + str(private)])

    def test_the_form_with_other_variables_on_the_line_is_read_too(self):
        private = self.dir / 'private-2'
        error, env, _ = self.prepare(f'export CARGO_TARGET_DIR={private} RUSTUP_TOOLCHAIN=stable\n')
        self.assertIsNone(error)
        self.assertEqual(self.key(env), ['cargo-target:' + str(private)])

    def test_two_jobs_on_the_same_private_target_exclude_each_other_and_distinct_ones_do_not(self):
        a, b = self.dir / 'p-a', self.dir / 'p-b'
        key_a = self.prepare(f'export CARGO_TARGET_DIR={a}\n')[1]
        key_a2 = self.prepare(f'export CARGO_TARGET_DIR={a}\n')[1]
        key_b = self.prepare(f'export CARGO_TARGET_DIR={b}\n')[1]
        shared = {'CARGO_TARGET_DIR': str(self.dir / 'shared-target')}
        self.assertTrue(da.excl_keys_clash(self.key(key_a)[0], self.key(key_a2)[0]))
        self.assertFalse(da.excl_keys_clash(self.key(key_a)[0], self.key(key_b)[0]))
        self.assertFalse(da.excl_keys_clash(self.key(key_a)[0], self.key(shared)[0]), 'a private target never clashes with the shared one')
        self.assertTrue(da.excl_keys_clash(self.key(shared)[0], self.key({'CARGO_TARGET_DIR': str(self.dir / 'shared-target')})[0]),
                        'true shared-target contention is kept')

    def test_a_variable_target_needs_a_declaration(self):
        error, _, _ = self.prepare('export CARGO_TARGET_DIR=$HOME/t\n')
        self.assertIn('--cargo-target-dir', error)
        declared = self.dir / 'declared'
        error, env, _ = self.prepare('export CARGO_TARGET_DIR=$HOME/t\n', cargo_target_dir=str(declared))
        self.assertIsNone(error)
        self.assertEqual(env['CARGO_TARGET_DIR'], str(declared))

    def test_a_declaration_that_contradicts_the_script_is_refused(self):
        error, _, _ = self.prepare(f'export CARGO_TARGET_DIR={self.dir}/one\n', cargo_target_dir=str(self.dir / 'two'))
        self.assertIn('differs', error)

    def test_two_different_targets_in_one_script_are_refused(self):
        error, _, _ = self.prepare(f'export CARGO_TARGET_DIR={self.dir}/one\nCARGO_TARGET_DIR={self.dir}/two cargo build\n')
        self.assertIn('2 different', error)

    def test_a_workspace_build_may_not_be_declared_as_a_single_crate(self):
        body = f'export CARGO_TARGET_DIR={self.dir}/t\ncargo build --workspace --bins 2>&1 | tail -3\n'
        error, _, _ = self.prepare(body, workload_class='build-crate')
        self.assertIn('build-workspace', error)
        error, _, _ = self.prepare(body, workload_class='build-workspace')
        self.assertIsNone(error)

    def test_a_single_crate_script_and_a_comment_are_not_workspace_builds(self):
        error, _, _ = self.prepare('# cargo build --workspace is not run here\ncargo nextest run -p aa-gateway --test t\n',
                                   workload_class='build-crate')
        self.assertIsNone(error)

    def test_all_targets_and_all_features_are_not_a_workspace_build(self):
        for body in ('cargo clippy -p aa-gateway --all-targets -- -D warnings\n', 'cargo test -p a --all-features\n',
                     "echo 'cargo test --workspace'\n"):
            error, _, _ = self.prepare(body, workload_class='build-crate')
            self.assertIsNone(error, body)
        for body in ('cargo test --all\n', 'cd r && cargo build --workspace\n', 'cargo build \\\n  --workspace\n'):
            error, _, _ = self.prepare(body, workload_class='build-crate')
            self.assertIn('build-workspace', error, body)

    def test_a_tilde_or_relative_target_needs_a_declaration(self):
        for value in ('~/x', 'target', './t'):
            error, _, _ = self.prepare(f'export CARGO_TARGET_DIR={value}\n')
            self.assertIn('--cargo-target-dir', error, value)

    def test_a_bash_c_chain_and_a_chained_export_set_the_key(self):
        private = self.dir / 'inline'
        args = args_for(['bash', '-c', f'cd /tmp && export CARGO_TARGET_DIR={private}; cargo test -p a'])
        env = {}
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(hr.prepare(args, env))
        self.assertEqual(self.key(env), ['cargo-target:' + str(private)])
        error, env, _ = self.prepare(f'cd /tmp && export CARGO_TARGET_DIR={private}\n')
        self.assertIsNone(error)
        self.assertEqual(self.key(env), ['cargo-target:' + str(private)])

    def test_launchers_and_flags_before_the_cargo_subcommand_are_seen(self):
        for body in ('cargo --locked test --workspace\n', 'env cargo test --workspace\n', 'time cargo build --workspace\n', 'cargo +stable test --workspace\n'):
            error, _, _ = self.prepare(body, workload_class='build-crate')
            self.assertIn('build-workspace', error, body)

    def test_a_target_dir_inside_the_script_conflicts_with_a_declaration(self):
        error, _, _ = self.prepare('cargo test -p a --target-dir /x\n', cargo_target_dir=str(self.dir / 'd'))
        self.assertIn('--target-dir', error)

    def test_a_declared_directory_beside_a_classifier_verdict_is_not_a_contradiction(self):
        args = args_for(['/bin/bash', '-c', 'pytest'], cargo_target_dir=str(self.dir / 'd'), workload_class='build-crate')
        self.assertIsNone(hr.prepare(args, {}))

    def test_a_non_script_command_is_left_alone(self):
        args = args_for(['cargo', 'build', '--workspace'], workload_class='build-crate')
        self.assertIsNone(hr.prepare(args, {}))

    def test_workspace_build_is_charged_at_the_workspace_class(self):
        fields, err = da.resolve_workload('build-workspace', None, None, None, env={}, home=str(self.dir), cwd=str(self.dir))
        crate, _ = da.resolve_workload('build-crate', None, None, None, env={}, home=str(self.dir), cwd=str(self.dir))
        self.assertIsNone(err)
        self.assertGreater(fields['cpu_slots'], crate['cpu_slots'])
        self.assertGreater(fields['mem_bytes'], crate['mem_bytes'])
        _, err = da.resolve_workload('build-workspace', None, None, 12 * 10**9, env={}, home=str(self.dir), cwd=str(self.dir))
        self.assertIn('below the build-workspace default', err)

    def test_no_cargo_target_takes_no_exclusion_but_keeps_the_charge(self):
        fields, err = da.resolve_workload('build-crate', None, None, None, env={}, home=str(self.dir), cwd=str(self.dir), cargo_target=False)
        self.assertIsNone(err)
        self.assertEqual(fields['excl_keys'], [])
        self.assertGreater(fields['mem_bytes'], 0)
        self.assertGreater(fields['cpu_slots'], 0)


class Classification(unittest.TestCase):
    PYTEST = ('cd {home}/engineering-supervisor && export PYTHONPATH=src:.; uvx --python /opt/homebrew/bin/python3.14 --from pytest==9.2.0 '
              '--with mcp==2.3.0 --with jsonschema==4.26.0 pytest -q -o pythonpath=src . > {home}/.local/share/es/pytest.log 2>&1; '
              'echo "exit=$?" >> {home}/.local/share/es/pytest.log')

    def verdict(self, script):
        return cs.classify(['/bin/bash', '-c', script])

    def test_the_supervisor_pytest_shape_is_a_native_gate_without_cargo(self):
        out = self.verdict(self.PYTEST.format(home=os.path.expanduser('~')))
        self.assertEqual((out['weight'], out['class'], out.get('cargo')), ('heavy', 'build-crate', False))

    def test_a_chain_with_a_cargo_segment_keeps_the_exclusion(self):
        out = self.verdict('cd /tmp && cargo build -p x; echo "exit=$?"')
        self.assertEqual((out['class'], out.get('cargo')), ('build-crate', None))

    def test_hostile_or_unmodelled_chains_are_unknown(self):
        home = os.path.expanduser('~')
        for script in ('pytest | tee x', 'pytest $(id)', 'pytest > /etc/x', 'export PATH=/x; pytest', 'pytest & sleep 1', 'rm -rf x',
                       'uvx --from pytest==9.2.0 ruff check', 'uvx --from pytest pytest', 'uvx --index-url http://x pytest', 'pytest *.py',
                       f'pytest > {home}/../../etc/x', 'bash -c "pytest"', 'cd $HOME && pytest', 'export CARGO_TARGET_DIR=$X; pytest'):
            self.assertEqual(self.verdict(script)['weight'], 'unknown', script)

    def test_uvx_must_be_pinned_and_name_its_own_tool(self):
        for argv in (['uvx', 'cat', 'x'], ['uvx', 'git', 'status'], ['uvx', 'pytest'], ['uvx', '--python', '/tmp/evil', '--from', 'pytest==9.2.0', 'pytest'],
                     ['uvx', '--from', 'pytest', 'pytest']):
            self.assertEqual(cs.classify(['/Users/' + os.path.basename(os.path.expanduser('~')) + '/.local/bin/uvx'] + argv[1:])['weight'], 'unknown', argv)

    def test_only_the_pinned_uvx_gate_drops_the_cargo_exclusion(self):
        self.assertIsNone(cs.classify(['pytest', '-q']).get('cargo'))
        self.assertIsNone(cs.classify(['npm', 'test']).get('cargo'))
        self.assertIsNone(cs.classify(['uv', 'build']).get('cargo'))
        self.assertIs(cs.classify(['uvx', '--from', 'pytest==9.2.0', 'pytest', '-q']).get('cargo'), False)

    def test_a_mid_word_hash_is_not_a_comment(self):
        for script in ('ls a#b; cargo build', 'ls a#b && cargo test --workspace', 'ls x#; cargo clippy'):
            self.assertNotEqual(self.verdict(script)['weight'], 'light', script)

    def test_a_light_chain_stays_light(self):
        self.assertEqual(self.verdict('cd /tmp && git status')['weight'], 'light')


class OmittedClass(unittest.TestCase):
    def test_an_omitted_class_is_classified_not_charged_the_whole_host(self):
        parsed = hr.parse(['--label', 'x', '--bytes', '1000', '--', '/bin/echo', 'hi'])
        self.assertIsNone(parsed.workload_class)
        out = io.StringIO()
        with unittest.mock.patch.object(hr, 'admitted', side_effect=AssertionError('light must not enter the queue')), \
                redirect_stderr(out), unittest.mock.patch.object(hr.subprocess, 'call', return_value=0) as call:
            rc = hr.main(['--label', 'x', '--bytes', '1000', '--', '/bin/echo', 'hi'], env={})
        self.assertEqual(rc, 0)
        self.assertTrue(call.called)
        self.assertIn('(omitted) auto: light', out.getvalue())

    def test_a_heavy_command_gets_its_class_and_a_raised_byte_estimate(self):
        seen = {}

        def admitted(args, env, **_):
            seen.update(cls=args.workload_class, nbytes=args.bytes, no_target=args.no_cargo_target)
            return 0
        pytest = ['/bin/bash', '-c', 'cd /tmp && export PYTHONPATH=src:.; uvx --from pytest==9.2.0 pytest -q']
        with unittest.mock.patch.object(hr, 'admitted', side_effect=admitted), redirect_stderr(io.StringIO()):
            rc = hr.main(['--label', 'x', '--bytes', str(512 * 2**20), '--'] + pytest, env={})
        self.assertEqual(rc, 0)
        self.assertEqual((seen['cls'], seen['no_target']), ('build-crate', True))
        self.assertEqual(seen['nbytes'], da.WORKLOAD_CLASSES['build-crate']['disk_gib'] * GIB)

    def test_an_unclassifiable_command_stays_classless_with_the_warning(self):
        seen = {}
        out = io.StringIO()
        with unittest.mock.patch.object(hr, 'admitted', side_effect=lambda a, e, **_: seen.update(cls=a.workload_class) or 0), redirect_stderr(out):
            hr.main(['--label', 'x', '--bytes', '1000', '--', '/opt/homebrew/bin/mystery-tool'], env={})
        self.assertIsNone(seen['cls'])
        self.assertIn('WARNING no --class', out.getvalue())


class Backfill(bf.Base):
    """Real scheduler decisions: the head waits on the shared target; what may overtake it."""

    def test_a_private_target_job_is_backfilled_past_a_shared_target_head(self):
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        head = self.enqueue('head', 'build-workspace', [bf.SHARED], 40)
        private = self.enqueue('aasm-private', 'build-workspace', ['cargo-target:/Users/dev/.cargo/private-1'], 40)
        with bf.backfill():
            out = self.advance()
        self.assertEqual(out['backfilled'], [private])
        self.assertEqual(self.state(head), 'QUEUED')

    def test_a_job_on_a_private_target_a_running_job_holds_is_refused(self):
        key = 'cargo-target:/Users/dev/.cargo/private-1'
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        self.running('private-run', 'build-crate', [key])
        self.enqueue('head', 'build-workspace', [bf.SHARED], 40)
        same = self.enqueue('private-again', 'build-crate', [key])
        with bf.backfill():
            out = self.advance()
        self.assertFalse(out.get('backfilled'))
        self.assertEqual(self.state(same), 'QUEUED')

    def test_a_pure_python_gate_with_no_exclusion_is_backfilled(self):
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        self.enqueue('head', 'build-workspace', [bf.SHARED], 40)
        pytest = self.enqueue('es-pytest', 'build-crate', [])
        with bf.backfill():
            out = self.advance()
        self.assertEqual(out['backfilled'], [pytest])

    def test_a_shared_target_job_is_never_backfilled_past_a_shared_target_head(self):
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        self.enqueue('head', 'build-workspace', [bf.SHARED], 40)
        same = self.enqueue('shared-too', 'build-crate', [bf.SHARED])
        with bf.backfill():
            out = self.advance()
        self.assertFalse(out.get('backfilled'))
        self.assertEqual(self.state(same), 'QUEUED')


if __name__ == '__main__':
    unittest.main()
