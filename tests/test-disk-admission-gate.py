#!/usr/bin/env python3
"""The disk-admission PreToolUse gate classifies heavy builds and only ever denies."""
from importlib.util import module_from_spec, spec_from_file_location
import os
import io
import json
from pathlib import Path
import tempfile
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_gate', ROOT / '.claude/hooks/disk-admission-gate.py')
gate = module_from_spec(spec)
spec.loader.exec_module(gate)


class Classification(unittest.TestCase):
    def heavy(self, command):
        return gate.classify(command)['heavy']

    def test_light_commands_and_quoted_text_are_not_heavy(self):
        for command in ('cargo --version', "git commit -m 'cargo build'", 'grep cargo src/main.rs',
                        'echo cargo build', 'cargo fmt', 'cargo', 'npm run build', 'docker ps',
                        'docker buildx ls', 'ls -la', 'cat Cargo.toml', 'make build', 'command -v cargo',
                        'sh -c "echo cargo build"'):
            self.assertFalse(self.heavy(command), command)
        # Negative control: the same words as a real command are heavy.
        self.assertTrue(self.heavy('cargo build'))

    def test_each_supported_heavy_tool_is_heavy(self):
        for command in ('cargo build', 'cargo test -p x', 'cargo check', 'cargo doc', 'cargo clippy', 'cargo nextest run',
                        'cargo bench', 'cargo rustdoc', 'cargo +nightly build', 'cargo -C x test', '/opt/homebrew/bin/cargo build',
                        'maturin build', 'maturin develop', 'npm install', 'npm ci', 'npm build', 'pnpm install',
                        'pnpm build', 'yarn install', 'yarn', 'uv sync', 'uv pip install x', 'pip install x',
                        'python3 -m pip install x', 'docker build .', 'docker buildx build .', 'xcodebuild -scheme x'):
            self.assertTrue(self.heavy(command), command)

    def test_env_prefix_chaining_and_rtk_twin(self):
        for command in ('FOO=1 cargo build', 'A=1 B=2 cargo test', 'env FOO=1 cargo check', 'cd crates/x && cargo test',
                        'git pull; cargo build', 'ls | cargo build', '(cd x && cargo build)', 'time cargo build',
                        'rtk cargo build', 'rtk cargo nextest run -p x', 'FOO=1 rtk cargo test'):
            verdict = gate.classify(command)
            self.assertTrue(verdict['heavy'], command)
            self.assertEqual(verdict['confidence'], 'exact', command)
        self.assertEqual(gate.classify('rtk cargo build')['kind'], 'cargo')
        # Negative control: env prefix on a non-heavy command stays light.
        self.assertFalse(self.heavy('FOO=1 cargo --version'))
        self.assertFalse(self.heavy('cd cargo && ls build'))

    def test_shell_dash_c_is_heavy_but_best_effort(self):
        for command in ('bash -c "cargo build"', 'sh -c "cd x && cargo test"', 'zsh -lc "npm ci"'):
            verdict = gate.classify(command)
            self.assertTrue(verdict['heavy'], command)
            self.assertEqual(verdict['confidence'], 'best-effort', command)
        # Unparsable input is reported as such and never treated as heavy.
        verdict = gate.classify("echo 'unterminated cargo build")
        self.assertEqual((verdict['heavy'], verdict['confidence']), (False, 'unparsable'))

    def test_documented_blind_spots_are_not_detected(self):
        for command in ('make build', 'just build', 'npm run build', './target/debug/tool', 'xargs cargo build < list'):
            self.assertFalse(self.heavy(command), command)
        doc = (ROOT / '.claude/hooks/disk-admission-gate.py').read_text()
        for name in ('make', 'just', 'npm run', 'direct binaries', '`!` shell', 'IDE'):
            self.assertIn(name, doc)


class TargetAdvice(unittest.TestCase):
    def advice(self, command, exists=lambda path: False):
        return gate.target_dir_advice(gate.classify(command), exists)

    def test_new_scratch_target_dir_gets_shared_target_advice(self):
        scratch = '/private/tmp/claude-501/x/scratchpad/target'
        for command in ('CARGO_TARGET_DIR=%s cargo build' % scratch, 'cargo test --target-dir %s' % scratch,
                        'cargo test --target-dir=%s' % scratch, 'env CARGO_TARGET_DIR=/tmp/t cargo check',
                        'rtk cargo build --target-dir /var/folders/ab/T/t'):
            message = self.advice(command)
            self.assertIn('shared target', message or '', command)
            self.assertIn('.cargo/shared-target', message)
        # Negative controls: existing dir, shared target, non-scratch path, no override, non-cargo.
        self.assertIsNone(self.advice('CARGO_TARGET_DIR=%s cargo build' % scratch, lambda path: True))
        self.assertIsNone(self.advice('cargo build --target-dir ~/.cargo/shared-target'))
        self.assertIsNone(self.advice('cargo build --target-dir /Users/someone/work/target'))
        self.assertIsNone(self.advice('cargo build'))
        self.assertIsNone(self.advice('npm ci'))
        self.assertIsNone(self.advice('cargo build --target-dir relative/target'))


class Mode(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mode'

    def mode(self, content=None):
        if content is not None:
            self.path.write_bytes(content)
        return gate.read_mode(self.path)

    def test_mode_file_defaults_to_warn_and_only_exact_enforce_enforces(self):
        self.assertEqual(self.mode(), 'warn')
        self.assertEqual(self.mode(b'enforce\n'), 'enforce')
        self.assertEqual(self.mode(b'  enforce  '), 'enforce')
        # Negative controls: warn, anything else, wrong case, empty and binary all fall back to warn.
        for content in (b'warn\n', b'', b'ENFORCE', b'enforce please', b'\xff\xfe', b'off'):
            self.assertEqual(self.mode(content), 'warn', content)
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(gate.read_mode(self.path), 'warn')
        self.path.unlink()
        self.path.symlink_to(Path(self.temp.name) / 'elsewhere')
        (Path(self.temp.name) / 'elsewhere').write_text('enforce')
        self.assertEqual(gate.read_mode(self.path), 'warn')


class Decisions(unittest.TestCase):
    def run_gate(self, command, mode, runner, tool='Bash'):
        payload = json.dumps({'tool_name': tool, 'tool_input': {'command': command}, 'session_id': 's'})
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, mode_fn=lambda: mode, runner=runner)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def refuses():
        return 77, 'heavy refused in tier low; tier=low free=35.9 GiB'

    def test_warn_mode_allows_and_adds_context_when_admission_would_refuse(self):
        code, out, err = self.run_gate('cargo build', 'warn', self.refuses)
        self.assertEqual((code, err), (0, ''))
        context = json.loads(out)['hookSpecificOutput']
        self.assertEqual(context['hookEventName'], 'PreToolUse')
        self.assertIn('would block', context['additionalContext'])
        self.assertIn('tier=low', context['additionalContext'])
        # Negative control: an admitted heavy command is allowed silently.
        self.assertEqual(self.run_gate('cargo build', 'warn', lambda: (0, 'admitted')), (0, '', ''))

    def test_enforce_mode_denies_with_exit_2_and_a_clear_message(self):
        for command in ('cargo build', 'rtk cargo test', 'cd x && npm ci', 'bash -c "cargo check"'):
            code, out, err = self.run_gate(command, 'enforce', self.refuses)
            self.assertEqual((code, out), (2, ''), command)
            self.assertIn('BLOCKED by disk-admission-gate', err)
            self.assertIn('tier=low', err)
        # Negative controls: admitted heavy and any non-heavy command are allowed.
        self.assertEqual(self.run_gate('cargo build', 'enforce', lambda: (0, 'ok')), (0, '', ''))
        self.assertEqual(self.run_gate('git status', 'enforce', self.refuses), (0, '', ''))

    def test_fail_closed_only_in_enforce(self):
        def raises():
            raise RuntimeError('adapter exploded')
        for runner in (lambda: (None, 'timed out'), lambda: (2, 'usage'), lambda: (1, 'crash'), raises):
            code, out, err = self.run_gate('cargo build', 'enforce', runner)
            self.assertEqual((code, out), (2, ''))
            self.assertIn('could not be evaluated', err)
            code, out, err = self.run_gate('cargo build', 'warn', runner)
            self.assertEqual((code, err), (0, ''))
            self.assertIn('could not be evaluated', json.loads(out)['hookSpecificOutput']['additionalContext'])
        # A non-heavy command never reaches the adapter, so its failure cannot block it.
        self.assertEqual(self.run_gate('ls', 'enforce', raises), (0, '', ''))

    def test_gate_never_rewrites_the_command(self):
        outputs = []
        for mode in ('warn', 'enforce'):
            for runner in (self.refuses, lambda: (0, 'ok'), lambda: (None, 'down')):
                outputs.append(self.run_gate('FOO=1 rtk cargo build --target-dir /tmp/new-x', mode, runner))
        for code, out, err in outputs:
            for text in (out, err):
                self.assertNotIn('updatedInput', text)
                self.assertNotIn('"command"', text)
                self.assertNotIn('permissionDecision', text)
        self.assertEqual({code for code, _, _ in outputs}, {0, 2})

    def test_unusable_input_is_allowed_and_target_advice_never_denies(self):
        def forbidden():
            raise AssertionError('adapter must not run')
        for raw in ('', 'not json', '[]', '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": {"command": 5}}'):
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(gate.main(raw, out, err, mode_fn=lambda: 'enforce', runner=forbidden), 0, raw)
            self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertEqual(self.run_gate('cargo build', 'enforce', forbidden, tool='Read'), (0, '', ''))
        code, out, err = self.run_gate('CARGO_TARGET_DIR=/private/tmp/never-existed-x cargo build', 'enforce',
                                       lambda: (0, 'ok'))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('shared target', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_adapter_is_resolved_from_this_checkout_and_asks_for_heavy(self):
        self.assertEqual(gate.adapter_path(), ROOT / 'scripts/disk-admission.py')
        with mock.patch.object(gate.subprocess, 'run', return_value=mock.Mock(returncode=77, stdout='refused\n', stderr='')) as run:
            self.assertEqual(gate.run_adapter(), (77, 'refused'))
        self.assertEqual(run.call_args.args[0][2:], ['check', '--weight', 'heavy'])
        with mock.patch.object(gate.subprocess, 'run', side_effect=gate.subprocess.TimeoutExpired('x', 1)):
            self.assertEqual(gate.run_adapter()[0], None)


class Newlines(unittest.TestCase):
    def heavy(self, command):
        return gate.classify(command)['heavy']

    def test_newline_chain_is_heavy(self):
        self.assertTrue(self.heavy('echo a\ncargo build'))
        self.assertTrue(self.heavy('ls\n\n  npm ci\n'))
        # Negative control: the same lines without a heavy command stay light.
        self.assertFalse(self.heavy('echo a\necho cargo build'))


if __name__ == '__main__':
    unittest.main()
