#!/usr/bin/env python3
"""Offline coverage for managed frontend skill activation, recovery and rollback."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('visuals', ROOT / 'scripts/visual-skills.py')
visuals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(visuals)


class FrontendLifecycle(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.area = Path(temp.name)
        self.home = self.area / 'home'
        self.home.mkdir()
        self.base = self.home / '.local/share/coding-agent-environment/visual-skills/frontend'
        self.repo = self.area / 'upstream'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.url = 'https://github.com/example/fixture.git'
        self.root = self.area / 'manifest'
        (self.root / 'codex').mkdir(parents=True)
        self.name = 'test-skill'
        self.spec = {'repository': self.url, 'paths': ['test-skill', 'LICENSE'],
                     'skill_path': self.name, 'state': 'active', 'role': 'general'}
        self.export = self.area / 'tree'
        (self.export / self.name).mkdir(parents=True)
        (self.export / 'LICENSE').write_text('Fixture license\n')
        self.configure('first')
        env = patch.dict(os.environ, {
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': f'url.{self.repo.as_uri()}.insteadOf',
            'GIT_CONFIG_VALUE_0': self.url, 'GIT_ALLOW_PROTOCOL': 'file',
            'CODING_AGENT_SYNC_HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
            'CODEX_HOME': str(self.home / '.codex'),
        })
        env.start()
        self.addCleanup(env.stop)
        self.targets = [self.home / host / 'skills' / self.name for host in ('.agents', '.claude')]

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def manifest(self):
        data = {'visuals': {'frontend': {'version': 1, 'packages': {self.name: self.spec}}}}
        (self.root / 'codex/subtraction-skills.json').write_text(json.dumps(data))

    def configure(self, body):
        (self.repo / self.name).mkdir(exist_ok=True)
        for path, text in [('LICENSE', 'Fixture license\n'), (self.name + '/SKILL.md', body)]:
            (self.export / path).write_text(text)
            (self.repo / path).write_text(text)
        self.git('add', '.')
        self.git('commit', '-qm', body)
        self.spec.update(revision=self.git('rev-parse', 'HEAD'),
                         integrity='sha256:' + visuals.digest_tree(self.export))
        self.manifest()

    def run_mode(self, mode='apply'):
        args = argparse.Namespace(repo_root=self.root, rollback=mode == 'rollback',
                                  check=mode == 'check', dry_run=mode == 'dry-run')
        return visuals.frontend_skills(args, self.home, self.base)

    def test_install_upgrade_quarantine_and_rollback(self):
        foreign = self.home / '.claude/skills/engineering/SKILL.md'
        foreign.parent.mkdir(parents=True)
        foreign.write_text('unrelated')
        with patch.object(visuals, 'run_git', side_effect=AssertionError):
            self.assertEqual(self.run_mode('dry-run'), 0)
            self.assertEqual(self.run_mode('check'), 1)
            self.assertFalse(self.base.exists())
        self.run_mode()
        receipt = self.base / 'receipt.json'
        installed = receipt.stat().st_mtime_ns
        with patch.object(visuals, 'run_git', side_effect=AssertionError):
            self.assertEqual(self.run_mode(), 0)
            self.assertEqual(self.run_mode('check'), 0)
        self.assertEqual(receipt.stat().st_mtime_ns, installed)
        first = self.targets[0].resolve()
        self.configure('second')
        self.run_mode()
        self.assertNotEqual(self.targets[0].resolve(), first)
        with patch.object(visuals, 'run_git', side_effect=AssertionError):
            self.run_mode('rollback')
            self.run_mode('rollback')
        self.assertTrue(all(p.resolve() == first for p in self.targets))
        self.assertEqual(self.run_mode('check'), 1)
        self.spec['state'] = 'quarantined'
        self.manifest()
        self.run_mode()
        self.assertTrue(all(not os.path.lexists(p) for p in self.targets))
        self.run_mode('rollback')
        self.assertTrue(all(p.resolve() == first for p in self.targets))
        self.assertEqual(foreign.read_text(), 'unrelated')

    def test_unrecorded_legacy_discovery_alias_is_preserved_and_reported(self):
        self.run_mode()
        alias = self.targets[1].parent / '.visual-skills-1234'
        alias.symlink_to(self.targets[1].resolve())
        for action in ('check', 'rollback', 'apply'):
            with self.assertRaisesRegex(visuals.VisualSkillsError, 'Unrecorded frontend discovery alias'):
                self.run_mode(action)
            self.assertTrue(alias.is_symlink())

    def test_interruption_recovers_both_hosts(self):
        self.run_mode()
        self.configure('second')
        count = 0
        original = Path.symlink_to

        def interrupt(target, wanted):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt
            original(target, wanted)

        with patch.object(Path, 'symlink_to', interrupt), self.assertRaises(KeyboardInterrupt):
            self.run_mode()
        self.assertIn('pending', json.loads((self.base / 'receipt.json').read_text()))
        self.assertFalse(os.path.lexists(self.targets[1]))
        self.assertTrue(all(not list(p.parent.glob('.visual-skills-*')) for p in self.targets))
        self.assertEqual(self.run_mode('check'), 1)
        self.run_mode()
        self.assertTrue(all((p / 'SKILL.md').read_text() == 'second' for p in self.targets))
        self.run_mode('rollback')
        self.assertTrue(all((p / 'SKILL.md').read_text() == 'first' for p in self.targets))
        self.spec['state'] = 'quarantined'
        self.manifest()
        self.run_mode()
        for target in self.targets:
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_bad_integrity_and_foreign_state_fail_before_activation(self):
        self.spec['integrity'] = 'sha256:' + '0' * 64
        self.manifest()
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'digest mismatch'):
            self.run_mode()
        self.assertFalse(any(os.path.lexists(p) for p in self.targets))
        target = self.targets[1]
        target.parent.mkdir(parents=True)
        target.symlink_to(self.repo)
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'outside recorded'):
            self.run_mode()
        self.assertEqual(target.readlink(), self.repo)
        self.assertFalse((self.base / 'receipt.json').exists())

    def test_existing_duplicate_fails_without_adoption(self):
        target = self.home / '.codex/skills' / self.name
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('foreign')
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'Duplicate'):
            self.run_mode()
        self.assertFalse(any(os.path.lexists(p) for p in self.targets))
        self.assertEqual((target / 'SKILL.md').read_text(), 'foreign')

    def test_snapshot_corruption_and_symlinked_ancestor_refused(self):
        self.run_mode()
        skill = self.targets[0] / 'SKILL.md'
        skill.chmod(0o644)
        skill.write_text('drift')
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'integrity failure'):
            self.run_mode('check')
        self.targets[0].parent.rename(self.home / 'saved')
        self.targets[0].parent.symlink_to(self.home / 'saved')
        with self.assertRaises(visuals.VisualSkillsError):
            self.run_mode()
        self.assertEqual(skill.read_text(), 'drift')

    def test_quarantined_duplicate_blocks_check(self):
        self.spec['state'] = 'quarantined'
        self.manifest()
        foreign = self.home / '.codex/skills' / self.name
        foreign.mkdir(parents=True)
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'Duplicate'):
            self.run_mode('check')
        self.assertTrue(foreign.is_dir())

    def test_rollback_keeps_damaged_snapshot_for_inspection(self):
        self.run_mode()
        original = self.targets[0].resolve()
        self.configure('second')
        self.run_mode()
        corrupted = self.targets[0].resolve() / 'SKILL.md'
        corrupted.chmod(0o644)
        corrupted.write_text('changed')
        self.run_mode('rollback')
        self.assertEqual(self.targets[0].resolve(), original)
        self.assertEqual(corrupted.read_text(), 'changed')

    def test_invalid_or_replaced_receipt_is_preserved(self):
        self.base.mkdir(parents=True)
        receipt = self.base / 'receipt.json'
        receipt.write_text('null')
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'not owned'):
            self.run_mode()
        receipt.unlink()
        stage = visuals.fetch_snapshot

        def concurrent(*args):
            snapshot = stage(*args)
            receipt.write_text('{"foreign":true}')
            return snapshot

        with patch.object(visuals, 'fetch_snapshot', concurrent), self.assertRaisesRegex(
                visuals.VisualSkillsError, 'changed while staging'):
            self.run_mode()
        self.assertEqual(receipt.read_text(), '{"foreign":true}')
        self.assertFalse(any(os.path.lexists(p) for p in self.targets))

    def test_conflicting_primaries_and_duplicate_skill_names_refused(self):
        path = self.root / 'codex/subtraction-skills.json'
        manifest = json.loads(path.read_text())
        packages = manifest['visuals']['frontend']['packages']
        packages['second'] = dict(self.spec, skill_path='second', paths=['second', 'LICENSE'])
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'Multiple active primary'):
            self.run_mode('dry-run')
        self.manifest()
        (self.root / 'codex/shared-skills.json').write_text(json.dumps({'skills': [self.name]}))
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'duplicates an authored'):
            self.run_mode('check')
        (self.root / 'codex/shared-skills.json').write_text(json.dumps({
            'skills': ['authored-primary'], 'frontend': {'authored-primary': 'general'}}))
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'Multiple active primary'):
            self.run_mode('check')
        path.write_text('{"visuals":{}, "visuals":{}}')
        with self.assertRaisesRegex(visuals.VisualSkillsError, 'Duplicate key'):
            self.run_mode('check')


if __name__ == '__main__':
    unittest.main()
