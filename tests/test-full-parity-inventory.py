#!/usr/bin/env python3
"""Catch omissions and invalid dispositions in the exhaustive parity census."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
CLASSES = {'ALREADY_EQUIVALENT', 'PORTABLE_SHARED_SOURCE',
           'CODEX_NATIVE_ADAPTER_NEEDED', 'HOST_SPECIFIC_NO_PARITY',
           'SECURITY_RESTRICTED', 'CURRENTLY_BLOCKED', 'OBSOLETE_OR_REDUNDANT'}

class Census(unittest.TestCase):
    def test_every_tracked_claude_surface_has_one_complete_disposition(self):
        rows = json.loads((ROOT / 'docs/full-parity-inventory.json').read_text())['rows']
        ids = [row['id'] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        tracked = set(subprocess.check_output(['git', 'ls-files', '.claude'], cwd=ROOT, text=True).splitlines())
        self.assertFalse(tracked - set(ids))
        fields = {'claude_source', 'semantic_behavior', 'current_codex', 'desired_codex',
                  'config_auth_security', 'verification', 'drift_owner', 'migration_reuse'}
        for row in rows:
            with self.subTest(surface=row['id']):
                self.assertIn(row['classification'], CLASSES)
                self.assertTrue(all(row.get(k) for k in fields))

    def test_authored_setting_fields_and_hook_groups_are_not_hidden_by_container(self):
        ids = {r['id'] for r in json.loads((ROOT / 'docs/full-parity-inventory.json').read_text())['rows']}
        for source in ['.claude/settings.json', '.claude/settings.global-only.json']:
            for key, value in json.loads((ROOT / source).read_text()).items():
                if key == '_scope_note':
                    continue
                if key == 'hooks':
                    expected = [f'{source}#hooks.{event}.{i}' for event, groups in value.items() for i in range(len(groups))]
                elif key in ['env', 'enabledPlugins', 'extraKnownMarketplaces']:
                    expected = [f'{source}#{key}.{name}' for name in value]
                else:
                    expected = [f'{source}#{key}']
                self.assertFalse(set(expected) - ids)

if __name__ == '__main__':
    unittest.main()
