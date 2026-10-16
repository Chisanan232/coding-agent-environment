#!/usr/bin/env python3
"""The disk-admission PreToolUse gate classifies heavy builds and only ever denies."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
