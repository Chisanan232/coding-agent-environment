import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'scripts/statusline-install.py'


class StatuslineInstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.settings = self.home / '.claude/settings.json'
        self.settings.parent.mkdir()
        self.original = {'env': {'SYNTH_SECRET': 'kept'}, 'hooks': {}, 'statusLine': {'type': 'command', 'command': 'old', 'padding': 1, 'refreshInterval': 3}}
        self.settings.write_text(json.dumps(self.original))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(INSTALL), '--home', str(self.home), *args], capture_output=True)

    def test_adoption_preserves_settings_and_is_idempotent(self):
        before = self.settings.read_bytes()
        result = self.invoke('--adopt')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.settings.read_text())
        command = data['statusLine']['command']
        self.assertIn(str(Path(sys.executable).resolve()), command)
        data['statusLine']['command'] = 'old'
        self.assertEqual(data, self.original)
        backups = list((self.home / '.claude/backups/coding-agent-environment').iterdir())
        self.assertTrue(any(p.read_bytes() == before for p in backups))
        snapshot = self.settings.read_bytes()
        self.assertEqual(self.invoke('--check').returncode, 0)
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.settings.read_bytes(), snapshot)
        self.assertEqual(len(list(backups[0].parent.iterdir())), len(backups))

    def test_check_and_unapproved_adoption_do_not_write(self):
        before = self.settings.read_bytes()
        self.assertEqual(self.invoke('--check').returncode, 1)
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.home / '.claude/statusline.py').exists())

    def test_symlink_target_is_rejected_before_any_write(self):
        outside = self.home / 'outside'
        outside.write_text('untouched')
        (self.settings.parent / 'statusline.py').symlink_to(outside)
        before = self.settings.read_bytes()
        self.assertEqual(self.invoke('--adopt').returncode, 1)
        self.assertEqual(outside.read_text(), 'untouched')
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.settings.parent / 'statusline-compositor.py').exists())


if __name__ == '__main__':
    unittest.main()
