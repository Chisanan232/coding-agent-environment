#!/usr/bin/env python3
"""Step classification (owner policy 2026-11-26): only a genuinely heavy step enters the heavy queue; a light step runs at once whatever the
lanes hold; an unrecognised step is refused rather than guessed; a dependent step waits for its prerequisite's result.

The runner is the fake of test-heavy-run.py (no real admission state is touched). Its mode 'held' models every heavy lane occupied: a
direct start is refused (queue-ahead), the job queues and is never granted.
"""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location, spec_from_loader
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIB = 2 ** 30


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cs = load('classify_step', 'scripts/classify-step.py')
hrt = load('heavy_run_tests', 'tests/test-heavy-run.py')
loader = SourceFileLoader('heavy_run_sc', str(ROOT / 'scripts/heavy-run'))
hr = module_from_spec(spec_from_loader('heavy_run_sc', loader))
loader.exec_module(hr)

NO_TRUST = '/nonexistent/trust.json'


class Classifier(unittest.TestCase):
    def kind(self, *argv, trust=NO_TRUST):
        out = cs.classify(list(argv), trust)
        return out['weight'], out['class']

    def test_cargo_workspace_package_and_light_subcommands(self):
        self.assertEqual(self.kind('cargo', 'build', '--workspace', '--bins'), ('heavy', 'build-workspace'))
        self.assertEqual(self.kind('cargo', 'test'), ('heavy', 'build-workspace'))            # no selector: the whole workspace
        self.assertEqual(self.kind('cargo', 'nextest', 'run', '-p', 'one'), ('heavy', 'build-crate'))
        self.assertEqual(self.kind('cargo', '+nightly', 'clippy', '-p', 'one', '--lib'), ('heavy', 'build-crate'))
        self.assertEqual(self.kind('cargo', 'fmt', '--all', '--', '--check'), ('light', 'light'))
        self.assertEqual(self.kind('rtk', 'cargo', 'metadata'), ('light', 'light'))

    def test_native_python_gate_is_build_crate_not_a_docker_suite(self):
        self.assertEqual(self.kind('uv', 'run', '--locked', '--all-extras', '--no-build', '--no-binary-package', 'circinus-runtime',
                                   'pytest', '--cov=circinus'), ('heavy', 'build-crate'))
        self.assertEqual(self.kind('python3', '-m', 'mypy', 'src'), ('heavy', 'build-crate'))
        self.assertEqual(self.kind('timeout', '600', 'pytest', '-q'), ('heavy', 'build-crate'))

    def test_containers_are_docker_exclusive_and_inspection_is_light(self):
        self.assertEqual(self.kind('docker', 'compose', 'up'), ('heavy', 'container-suite'))
        self.assertEqual(self.kind('docker', 'ps'), ('light', 'light'))

    def test_read_and_lint_commands_are_light(self):
        for argv in (('gh', 'pr', 'view', '3'), ('git', 'status'), ('ruff', 'check', '.'), ('jq', '.', 'x'), ('claude', '--help')):
            self.assertEqual(self.kind(*argv)[0], 'light', argv)

    def test_an_unrecognised_step_is_unknown_never_light(self):
        for argv in (('./mystery.sh',), ('bash', '-c', 'x'), ('pre-commit', 'run'), ('cargo', 'frobnicate'), ()):
            self.assertEqual(self.kind(*argv)[0], 'unknown', argv)

    def trust(self, checks):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        self.addCleanup(os.unlink, handle.name)
        json.dump({'schema': 1, 'repos': {'o/r': {'checks': [{'name': str(i), 'argv': c} for i, c in enumerate(checks)]}}}, handle)
        handle.close()
        return handle.name

    def test_the_local_ci_recorder_takes_the_heaviest_pinned_check(self):
        record = ['python3', '/x/owner-merge-local-ci.py', 'record', '--repo', 'o/r', '--pr', '1']
        py = self.trust([['uv', 'run', 'ruff', 'check'], ['uv', 'run', 'pytest'], ['uv', 'build']])
        self.assertEqual(self.kind(*record, trust=py), ('heavy', 'build-crate'))
        rust = self.trust([['cargo', 'fmt'], ['cargo', 'build', '--workspace']])
        self.assertEqual(self.kind(*record, trust=rust), ('heavy', 'build-workspace'))
        boxed = self.trust([['pytest'], ['docker', 'run', 'x']])
        self.assertEqual(self.kind(*record, trust=boxed), ('heavy', 'container-suite'))
        docs = self.trust([['ruff', 'check'], ['git', 'diff', '--check']])
        self.assertEqual(self.kind(*record, trust=docs), ('light', 'light'))

    def test_the_recorder_is_unknown_without_pinned_checks_or_with_an_unrecognised_one(self):
        record = ['python3', '/x/owner-merge-local-ci.py', 'record', '--repo', 'o/r', '--pr', '1']
        self.assertEqual(self.kind(*record, trust=NO_TRUST)[0], 'unknown')
        self.assertEqual(self.kind(*record, trust=self.trust([['pytest'], ['./odd.sh']]))[0], 'unknown')
        other = ['python3', '/x/owner-merge-local-ci.py', 'record', '--repo', 'other/repo', '--pr', '1']
        self.assertEqual(self.kind(*other, trust=self.trust([['pytest']]))[0], 'unknown')


class UnderContention(unittest.TestCase):
    """Every heavy lane occupied ('held'): light starts and finishes, heavy stays queued, a dependent step waits for its prerequisite."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / 'bin').mkdir()
        runner = self.dir / 'runner'
        runner.write_text(hrt.FAKE_RUNNER)
        runner.chmod(runner.stat().st_mode | stat.S_IXUSR)
        body = hrt.COMMAND
        for name in ('pytest', 'echo-light', 'dependent'):    # 'pytest' classifies as a heavy native gate, 'git' as light
            path = self.dir / 'bin' / name
            path.write_text(body.replace("'command.ran'", f"'{name}.ran'"))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        for target, value in ((hr, 'RUNNER'), (hr, 'POLL_S'), (hr, 'NOTICE_EVERY_S')):
            old = getattr(target, value)
            self.addCleanup(setattr, target, value, old)
        hr.RUNNER = str(runner)
        hr.POLL_S = 0.02
        hr.NOTICE_EVERY_S = 10 ** 6
        patcher = unittest.mock.patch.object(hr, 'RECOVERY_LIB', str(self.dir / 'absent-recovery.py'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {'PATH': str(self.dir / 'bin') + ':' + os.environ['PATH'], 'FAKE_DIR': str(self.dir)}
        for sig in (hr.signal.SIGTERM, hr.signal.SIGHUP):
            self.addCleanup(hr.signal.signal, sig, hr.signal.getsignal(sig))

    def mode(self, value):
        (self.dir / 'mode').write_text(value)

    def heavy_run(self, command, max_wait=1, extra=('--class', 'auto')):
        argv = ['--label', 'sc-test', '--bytes', str(10 * GIB), '--max-wait-s', str(max_wait), *extra, '--', *command]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return hr.main(argv, dict(self.env)), err.getvalue()

    def ran(self, name):
        path = self.dir / f'{name}.ran'
        return path.exists()

    def runner_calls(self):
        path = self.dir / 'runner.jsonl'
        return [json.loads(line)['argv'] for line in path.read_text().splitlines() if '"argv"' in line] if path.exists() else []

    def test_a_light_step_starts_and_finishes_while_every_lane_is_held_and_never_touches_the_queue(self):
        self.mode('held')
        # `git` is light; stand in with a real light binary that exits 0
        code, err = self.heavy_run(['git', '--version'])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.runner_calls(), [])              # no enqueue, no run, no release: it never entered the queue

    def test_a_heavy_step_stays_queued_and_does_not_run_while_every_lane_is_held(self):
        self.mode('held')
        code, err = self.heavy_run([str(self.dir / 'bin' / 'pytest'), '-q'])
        self.assertEqual(code, hr.TIMEOUT_EXIT, err)           # not admitted within --max-wait-s
        self.assertFalse(self.ran('pytest'))
        enqueue = [a for a in self.runner_calls() if a[:2] == ['queue', 'enqueue']][0]
        self.assertEqual(enqueue[enqueue.index('--class') + 1], 'build-crate')   # classified, not guessed

    def test_an_unrecognised_step_is_refused_before_it_queues(self):
        self.mode('held')
        code, err = self.heavy_run([str(self.dir / 'bin' / 'dependent')])
        self.assertEqual(code, 64, err)
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.ran('dependent'))

    def test_a_dependent_step_waits_for_its_prerequisite_and_resumes_after_success(self):
        pytest = str(self.dir / 'bin' / 'pytest')
        self.mode('held')                                      # prerequisite queued behind occupied lanes: not admitted, not run
        code, _ = self.heavy_run([pytest])
        self.assertNotEqual(code, 0)
        self.assertFalse(self.ran('pytest'))
        self.assertFalse(self.ran('dependent'), 'the dependent step must not run on a prerequisite that did not run')
        self.mode('queue')                                     # a lane frees: granted, claimed, run, released
        code, err = self.heavy_run([pytest], max_wait=5)
        self.assertEqual(code, 0, err)
        self.assertTrue(self.ran('pytest'))
        code, err = self.heavy_run([str(self.dir / 'bin' / 'echo-light')], extra=('--class', 'light'))   # dependent runs only now
        self.assertEqual(code, 0, err)

    def test_a_heavy_step_that_fails_does_not_release_its_dependent(self):
        self.mode('free')
        self.env['FAKE_COMMAND_RC'] = '3'
        code, _ = self.heavy_run([str(self.dir / 'bin' / 'pytest')])
        self.assertEqual(code, 3)                              # the step's own exit code reaches `&&`


if __name__ == '__main__':
    unittest.main()
