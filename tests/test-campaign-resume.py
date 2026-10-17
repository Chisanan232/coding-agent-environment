#!/usr/bin/env python3
"""Campaign resume coordinator records durable checkpoints and never loops."""
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('campaign_resume', ROOT / 'scripts/campaign-resume.py')
cr = module_from_spec(spec)
spec.loader.exec_module(cr)

SESSION = '01234567-89ab-cdef-0123-456789abcdef'
NOW = datetime(2026, 10, 17, 6, 0, 0, tzinfo=timezone.utc)
PR_EVENT = {'id': 'pr0-76', 'kind': 'pr_merged', 'repo': 'Chisanan232/coding-agent-environment', 'number': 76}


def make(directory, **overrides):
    options = dict(name='spe-108', session_id=SESSION, cwd='/tmp', events=[dict(PR_EVENT)], now=NOW)
    options.update(overrides)
    return cr.register(directory, options.pop('name'), options.pop('session_id'), options.pop('cwd'),
                       options.pop('events'), options.pop('now'), **options)


class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'campaigns'
        self.addCleanup(self.tmp.cleanup)

    def test_register_writes_private_valid_checkpoint(self):
        make(self.dir)
        path = self.dir / 'spe-108.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used'], record['consumed']), ('WAIT_DEPENDENCY', 0, []))

    def test_rejects_malformed_input(self):
        bad = [
            dict(name='../escape'), dict(session_id='not-a-uuid'), dict(cwd='relative/path'),
            dict(events=[]), dict(events=[{'id': 'x', 'kind': 'shell', 'cmd': 'rm -rf /'}]),
            dict(events=[dict(PR_EVENT, repo='a/b; rm')]), dict(events=[dict(PR_EVENT, number=0)]),
            dict(events=[dict(PR_EVENT), dict(PR_EVENT)]), dict(max_resumes=0), dict(cooldown_s=1),
            dict(timeout_s=cr.MAX_TIMEOUT_S + 1), dict(admission_weight='huge'),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(cr.CheckpointError):
                make(self.dir, **overrides)
        self.assertEqual(list(self.dir.glob('*.json')), [])

    def test_reregister_keeps_spent_budget_and_consumed_events(self):
        make(self.dir)
        record = cr.load(self.dir, 'spe-108')
        record.update(resumes_used=2, consumed=['pr0-76'], state='WAITING_OPERATOR')
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        again = make(self.dir, events=[dict(PR_EVENT), dict(PR_EVENT, id='pr1-77', number=77)])
        self.assertEqual((again['resumes_used'], again['consumed'], again['state']), (2, ['pr0-76'], 'WAITING_OPERATOR'))

    def test_load_refuses_symlink_and_garbage(self):
        make(self.dir)
        (self.dir / 'link.json').symlink_to(self.dir / 'spe-108.json')
        with self.assertRaises(cr.CheckpointError):
            cr.load(self.dir, 'link')
        (self.dir / 'junk.json').write_text('{not json')
        with self.assertRaises(cr.CheckpointError):
            cr.load(self.dir, 'junk')

    def test_symlinked_state_directory_is_refused(self):
        real = Path(self.tmp.name) / 'real'
        real.mkdir()
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(real)
        with self.assertRaises(cr.CheckpointError):
            make(link)

    def test_plist_has_interval_and_no_restart_loop(self):
        plist = plistlib.loads(cr.build_plist('/usr/bin/python3', '/x/campaign-resume.py').encode())
        self.assertEqual(plist['ProgramArguments'][2:], ['tick', '--execute'])
        self.assertEqual(plist['StartInterval'], 300)
        self.assertNotIn('KeepAlive', plist)
        self.assertFalse(plist['RunAtLoad'])

    def test_cli_register_status_and_invalid_exit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cr.run(['register', '--campaign', 'c1', '--session-id', SESSION, '--cwd', '/tmp',
                           '--pr-merged', 'Chisanan232/coding-agent-environment#76'], directory=self.dir, now=NOW)
        self.assertEqual(code, 0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cr.run(['status', '--json'], directory=self.dir), 0)
        self.assertEqual(json.loads(out.getvalue())[0]['campaign'], 'c1')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cr.run(['register', '--campaign', 'c2', '--session-id', 'bad', '--cwd', '/tmp',
                                     '--not-before', '2026-10-18T00:00:00Z'], directory=self.dir, now=NOW), 2)


if __name__ == '__main__':
    unittest.main()
