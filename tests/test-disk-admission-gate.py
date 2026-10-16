#!/usr/bin/env python3
"""The disk-admission PreToolUse gate classifies heavy builds and only ever denies."""
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import tempfile
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


if __name__ == '__main__':
    unittest.main()
