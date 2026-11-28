#!/usr/bin/env python3
"""release-line skill: packaging consistency, fixture-home install on both hosts, and safety text."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / '.claude/skills/release-line'
FILES = ['SKILL.md', 'references/algorithms.md']


class Packaging(unittest.TestCase):
    def test_skill_dir_manifest_and_inventory_agree(self):
        manifest = json.loads((ROOT / 'codex/shared-skills.json').read_text())['skills']
        self.assertIn('release-line', manifest)
        authored = {p.parent.name for p in (ROOT / '.claude/skills').glob('*/SKILL.md')}
        self.assertEqual(authored, set(manifest) | {'evidence-first-briefing', 'engineering-workflow'})
        rows = {r['id']: r for r in json.loads((ROOT / 'docs/full-parity-inventory.json').read_text())['rows']}
        for name in FILES:
            row = rows['.claude/skills/release-line/' + name]
            self.assertEqual(row['classification'], 'PORTABLE_SHARED_SOURCE')
            self.assertEqual(row['claude_source'], row['id'])

    def test_frontmatter_is_valid(self):
        text = (SKILL / 'SKILL.md').read_text()
        match = re.match(r'---\nname: (.+)\ndescription: "(.+)"\n---\n', text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), 'release-line')
        self.assertGreater(len(match.group(2)), 40)
        self.assertEqual(text.count('<!-- claude-only:start -->'), text.count('<!-- claude-only:end -->'))

    def test_safety_rules_present_and_no_bypass_instruction(self):
        text = (SKILL / 'SKILL.md').read_text()
        for required in ['Never force-push', 'Never run `gh workflow run`', 'Never admin-merge',
                         'owner-merge exception', 'recorded owner GO', 'scripts/release-line.py',
                         '$HOME/Bryant-Developments/coding-agent-environment-main-view/scripts/release-line.py', '--repo OWNER/REPO']:
            with self.subTest(required=required):
                self.assertIn(required, text)
        everything = text + (SKILL / 'references/algorithms.md').read_text()
        for bypass in ['--no-verify', '--admin', 'push --force', 'push -f', 'git push --force']:
            with self.subTest(bypass=bypass):
                # Mentions are allowed only inside a prohibition line.
                for line in everything.splitlines():
                    if bypass in line:
                        self.assertRegex(line, r'(?i)\bnever\b|\bdo not\b')

    def test_release_preparation_pointer_keeps_the_gate(self):
        text = (ROOT / '.claude/skills/release-preparation/SKILL.md').read_text()
        self.assertIn('run only under a recorded owner GO; otherwise stop and report BLOCKED', text)


class Install(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release line ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.repo = Path(self.temp.name) / 'repo'
        for name in ['.claude', 'codex', 'scripts', 'docs']:
            shutil.copytree(ROOT / name, self.repo / name)
        self.env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(self.home))
        manifest_path = self.repo / 'codex/subtraction-skills.json'
        manifest = json.loads(manifest_path.read_text())
        for name, spec in manifest['skills'].items():
            folder = self.home / '.agents/skills' / name
            folder.mkdir(parents=True)
            body = b'Offline external canonical fixture'
            (folder / 'SKILL.md').write_bytes(body)
            spec['files'] = {'SKILL.md': hashlib.sha256(body).hexdigest()}
        manifest_path.write_text(json.dumps(manifest))

    def install(self, flag, *extra):
        return subprocess.run(['bash', str(self.repo / 'scripts/profile-install.sh'), flag, *extra],
                              env=self.env, text=True, capture_output=True)

    def test_installs_to_both_hosts_and_check_reports_no_drift(self):
        for flag, host in [('--claude-skills', '.claude'), ('--global', '.codex')]:
            with self.subTest(host=host):
                result = self.install(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in FILES:
                    installed = (self.home / host / 'skills/release-line' / name).read_bytes()
                    expected = (SKILL / name).read_bytes()
                    if host == '.codex':
                        expected = re.sub(rb'<!-- claude-only:start -->.*?<!-- claude-only:end -->\n?', b'', expected, flags=re.S)
                    self.assertEqual(installed, expected)
                check = self.install(flag, '--check')
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        codex_text = (self.home / '.codex/skills/release-line/SKILL.md').read_text()
        self.assertNotIn('claude-only', codex_text)

    def test_check_detects_drift_in_installed_release_line(self):
        self.assertEqual(self.install('--claude-skills').returncode, 0)
        target = self.home / '.claude/skills/release-line/references/algorithms.md'
        target.write_text('local edit')
        self.assertEqual(self.install('--claude-skills', '--check').returncode, 1)

    def test_manifest_without_skill_dir_is_refused(self):
        shutil.rmtree(self.repo / '.claude/skills/release-line')
        result = self.install('--claude-skills')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('complete authored inventory', result.stderr)


if __name__ == '__main__':
    unittest.main()
