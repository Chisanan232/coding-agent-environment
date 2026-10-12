import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProfileStatusline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.company = self.root / 'company'
        self.company.mkdir()
        self.local = self.root / 'local'
        self.overlay = self.local / 'profiles/alpha/claude'
        self.overlay.mkdir(parents=True)
        (self.overlay / 'settings.json').write_text('{}')
        (self.overlay / 'statusline.json').write_text('{"extensions":[]}')
        (self.overlay / 'statusline-runtime').mkdir()
        (self.company / '.coding-agent-profile').write_text('alpha\n')
        fakebin = self.root / 'bin'
        fakebin.mkdir()
        fake = fakebin / 'claude'
        fake.write_text('#!/usr/bin/env python3\nimport os,sys,json\nprint(json.dumps({"argv":sys.argv[1:],"env":{k:os.environ.get(k) for k in ("CODING_AGENT_PROFILE_ACTIVE","CODING_AGENT_STATUSLINE_CONFIG","CODING_AGENT_STATUSLINE_CONTRACT_RUNTIME")}}))\n')
        fake.chmod(0o755)
        clean = {k: v for k, v in os.environ.items() if not k.startswith('CODING_AGENT_') and k != 'CA_DRY_RUN'}
        self.env = dict(clean, HOME=str(self.root), CODING_AGENT_LOCAL_DIR=str(self.local),
                        CODING_AGENT_PROFILE_DIR=str(self.root / 'personal'), PATH=str(fakebin)+':'+os.environ['PATH'])

    def tearDown(self):
        self.temp.cleanup()

    def launch(self, cwd, *args):
        return subprocess.run([str(ROOT / 'bin/ca-claude'), *args], cwd=cwd, env=self.env, capture_output=True, text=True)

    def test_root_nested_and_resume_get_same_registration(self):
        for path in (self.company, self.company / 'project-a/deep', self.company / 'project-b'):
            path.mkdir(parents=True, exist_ok=True)
            result = self.launch(path, '--resume', 'fixture-session')
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['env']['CODING_AGENT_PROFILE_ACTIVE'], '1')
            self.assertEqual(data['env']['CODING_AGENT_STATUSLINE_CONFIG'], str(self.overlay / 'statusline.json'))
            self.assertEqual(data['env']['CODING_AGENT_STATUSLINE_CONTRACT_RUNTIME'], str(self.overlay / 'statusline-runtime'))
            self.assertEqual(data['argv'][-2:], ['--resume', 'fixture-session'])

    def test_no_marker_clears_inherited_provider_selection(self):
        self.env['CODING_AGENT_STATUSLINE_CONFIG'] = 'foreign'
        self.env['CODING_AGENT_STATUSLINE_CONTRACT_RUNTIME'] = 'foreign'
        result = self.launch(self.root)
        data = json.loads(result.stdout)
        self.assertIsNone(data['env']['CODING_AGENT_PROFILE_ACTIVE'])
        self.assertIsNone(data['env']['CODING_AGENT_STATUSLINE_CONFIG'])
        self.assertIsNone(data['env']['CODING_AGENT_STATUSLINE_CONTRACT_RUNTIME'])

    def test_malformed_and_unresolved_are_explicit_not_active(self):
        for marker in ('bad name\n', 'missing\n'):
            (self.company / '.coding-agent-profile').write_text(marker)
            result = self.launch(self.company)
            self.assertEqual(result.returncode, 0)
            self.assertIn('no profile overlay activated', result.stderr)
            self.assertIsNone(json.loads(result.stdout)['env']['CODING_AGENT_PROFILE_ACTIVE'])

    def test_plain_invocation_does_not_activate_tier_b(self):
        result = subprocess.run(['claude'], cwd=self.company, env=self.env, capture_output=True, text=True)
        data = json.loads(result.stdout)
        self.assertIsNone(data['env']['CODING_AGENT_PROFILE_ACTIVE'])
        self.assertIsNone(data['env']['CODING_AGENT_STATUSLINE_CONFIG'])

    def test_managed_runtime_install_and_reinstall(self):
        installer = ROOT / 'scripts/launcher-install.py'
        argv = [os.sys.executable, str(installer), '--home', str(self.root)]
        first = subprocess.run(argv, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(subprocess.run(argv + ['--check'], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(argv, capture_output=True).returncode, 0)
        live = self.root / '.local/lib/coding-agent-environment/bin/ca-claude'
        live.write_text('changed')
        self.assertEqual(subprocess.run(argv, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(argv + ['--adopt'], capture_output=True).returncode, 0)
        self.assertTrue(any(p.read_bytes() == b'changed' for p in (self.root / '.local/state/coding-agent-environment/backups').iterdir()))

    def test_overlay_cannot_override_canonical_statusline(self):
        (self.overlay / 'settings.json').write_text('{"statusLine":{"command":"replacement"}}')
        result = self.launch(self.company)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('replacement', result.stderr)


if __name__ == '__main__':
    unittest.main()
