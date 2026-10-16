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


if __name__ == '__main__':
    unittest.main()
