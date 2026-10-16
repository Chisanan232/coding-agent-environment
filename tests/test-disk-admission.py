#!/usr/bin/env python3
"""Disk-pressure admission core refuses unsafe work and fails closed."""
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_admission', ROOT / 'scripts/disk-admission.py')
da = module_from_spec(spec)
spec.loader.exec_module(da)
GIB = 1024 ** 3


class Tiers(unittest.TestCase):
    def test_tier_boundaries(self):
        cases = [(0, 'emergency'), (20 * GIB - 1, 'emergency'), (20 * GIB, 'low'),
                 (100 * GIB - 1, 'low'), (100 * GIB, 'stable'), (200 * GIB - 1, 'stable'),
                 (200 * GIB, 'wide'), (10 ** 15, 'wide')]
        for free, expected in cases:
            self.assertEqual(da.tier(free), expected, free)
        # Negative control: a decimal-GB threshold would misplace this value.
        self.assertEqual(da.tier(100 * 10 ** 9), 'low')


class Decisions(unittest.TestCase):
    def test_heavy_refused_below_100_gib(self):
        for free in (0, 19 * GIB, 50 * GIB, 100 * GIB - 1):
            admit, _, reason = da.decide('heavy', free, {})
            self.assertFalse(admit, free)
            self.assertIn('refused', reason)
        # Negative control: at the boundary heavy work is admitted.
        for free in (100 * GIB, 150 * GIB, 500 * GIB):
            self.assertTrue(da.decide('heavy', free, {})[0], free)

    def test_light_allowed_low_refused_emergency(self):
        for free in (20 * GIB, 60 * GIB, 100 * GIB, 300 * GIB):
            self.assertTrue(da.decide('light', free, {})[0], free)
        # Negative control: below the emergency line light work is refused too.
        for free in (0, 10 * GIB, 20 * GIB - 1):
            admit, _, reason = da.decide('light', free, {})
            self.assertFalse(admit, free)
            self.assertIn('emergency', reason)

    def test_unknown_weight_is_rejected(self):
        with self.assertRaises(ValueError):
            da.decide('medium', 500 * GIB, {})

    def test_latch_set_below_20_held_until_50(self):
        admit, state, _ = da.decide('light', 19 * GIB, {})
        self.assertFalse(admit)
        self.assertTrue(state['latch'])
        # Held through recovery, then released exactly at 50 GiB.
        for free in (20 * GIB, 49 * GIB, 50 * GIB - 1):
            self.assertTrue(da.decide('light', free, state)[1]['latch'], free)
        self.assertFalse(da.decide('light', 50 * GIB, state)[1]['latch'])
        # Negative control: without a prior latch 20..<50 GiB never latches.
        self.assertFalse(da.decide('light', 30 * GIB, {})[1]['latch'])

    def test_latch_refuses_light_below_50(self):
        latched = {'latch': True}
        for free in (25 * GIB, 49 * GIB):
            admit, _, reason = da.decide('light', free, latched)
            self.assertFalse(admit, free)
            self.assertIn('latched', reason)
        self.assertFalse(da.decide('heavy', 150 * GIB - 100 * GIB - 1, latched)[0])
        # Negative control: the same free space without a latch admits light work.
        self.assertTrue(da.decide('light', 25 * GIB, {})[0])
        self.assertTrue(da.decide('light', 50 * GIB, latched)[0])


class State(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission/state.json'

    def write(self, free, latch):
        with da.state_lock(self.path):
            return da.write_state(self.path, free, latch)

    def test_state_written_atomically_with_timestamp(self):
        self.write(36 * GIB, True)
        record = json.loads(self.path.read_text())
        self.assertEqual(record['version'], 1)
        self.assertEqual((record['free_bytes'], record['tier'], record['latch']), (36 * GIB, 'low', True))
        self.assertTrue(record['timestamp'].endswith('Z'))
        from datetime import datetime
        datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        # A failed replace leaves the previous complete record and no temp file behind.
        with mock.patch.object(da.os, 'replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                self.write(500 * GIB, False)
        self.assertEqual(json.loads(self.path.read_text()), record)
        self.assertEqual(sorted(entry.name for entry in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])

    def test_corrupt_state_fails_closed(self):
        self.assertEqual(da.read_state(self.path), {})
        self.write(150 * GIB, False)
        self.assertEqual(da.read_state(self.path)['latch'], False)
        bad = ['', '{not json', '[]', 'null', json.dumps({'version': 2, 'latch': False}),
               json.dumps({'version': 1, 'latch': 'no', 'free_bytes': 1, 'tier': 'low', 'timestamp': 't'}),
               json.dumps({'version': 1, 'latch': False, 'free_bytes': 1, 'tier': 'huge', 'timestamp': 't'})]
        for content in bad:
            self.path.write_text(content)
            with self.assertRaises(da.StateError, msg=content):
                da.read_state(self.path)

    def test_symlinked_state_path_refused(self):
        target = Path(self.temp.name) / 'elsewhere.json'
        target.write_text('{}')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.symlink_to(target)
        with self.assertRaises(da.StateError):
            da.read_state(self.path)
        with self.assertRaises(da.StateError):
            self.write(10 * GIB, True)
        self.assertEqual(target.read_text(), '{}')
        # A redirected directory is refused as well.
        self.path.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(self.temp.name)
        with self.assertRaises(da.StateError):
            self.write(10 * GIB, True)


class Volume(unittest.TestCase):
    @staticmethod
    def stats():
        return mock.Mock(f_bavail=1000, f_frsize=4096, f_bfree=9999)

    def test_darwin_reads_data_volume(self):
        with mock.patch.object(da.os.path, 'exists', return_value=True), \
                mock.patch.object(da.os, 'statvfs', return_value=self.stats()) as statvfs:
            self.assertEqual(da.read_free_bytes(), 1000 * 4096)
        statvfs.assert_called_once_with('/System/Volumes/Data')
        # Negative control: without the data volume it falls back to root.
        with mock.patch.object(da.os.path, 'exists', return_value=False), \
                mock.patch.object(da.os, 'statvfs', return_value=self.stats()) as statvfs:
            da.read_free_bytes()
        statvfs.assert_called_once_with('/')

    def test_unreadable_volume_fails_closed(self):
        with mock.patch.object(da.os, 'statvfs', side_effect=PermissionError('denied')):
            with self.assertRaises(da.VolumeError):
                da.read_free_bytes()


class Transactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission/state.json'

    def test_concurrent_checks_serialize_state_writes(self):
        inside, peak, guard = [0], [0], threading.Lock()

        def measure():
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.01)
            with guard:
                inside[0] -= 1
            return 150 * GIB

        # Negative control: the detector does see overlap when nothing serializes.
        workers = [threading.Thread(target=measure) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertGreater(peak[0], 1)
        inside[0], peak[0], results = 0, 0, []
        workers = [threading.Thread(target=lambda: results.append(da.evaluate('heavy', self.path, measure)))
                   for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result['admit'] for result in results))
        self.assertEqual(da.read_state(self.path)['free_bytes'], 150 * GIB)
        self.assertEqual(sorted(entry.name for entry in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])


class Cli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission/state.json'

    def run_main(self, argv, free):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = da.main(argv, free_fn=lambda: free, state_path=self.path)
        return code, out.getvalue()

    def test_cli_rejects_free_bytes_override(self):
        home = {**os.environ, 'HOME': self.temp.name}
        script = str(ROOT / 'scripts/disk-admission.py')
        for extra in (['--free-bytes', str(900 * GIB)], ['--state', '/tmp/x'], ['--state-path=/tmp/x'], ['--bogus']):
            result = subprocess.run([sys.executable, script, 'check', '--weight', 'heavy', *extra],
                                    capture_output=True, text=True, env=home)
            self.assertEqual(result.returncode, 2, extra)
            self.assertEqual(result.stdout, '')
        # An unknown weight and an abbreviated flag are usage errors too.
        for argv in (['check', '--weight', 'medium'], ['check', '--wei', 'heavy'], ['status']):
            result = subprocess.run([sys.executable, script, *argv], capture_output=True, text=True, env=home)
            self.assertEqual(result.returncode, 2, argv)
        self.assertFalse((Path(self.temp.name) / '.cache').exists())

    def test_refusal_exit_code_is_77(self):
        code, text = self.run_main(['check', '--weight', 'heavy'], 36 * GIB)
        self.assertEqual(code, 77)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn('tier=low', text)
        self.assertIn('36.0 GiB', text)
        # Negative control: the same request is admitted with enough space.
        self.assertEqual(self.run_main(['check', '--weight', 'heavy'], 150 * GIB)[0], 0)
        self.assertEqual(self.run_main(['check', '--weight', 'light'], 36 * GIB)[0], 0)

    def test_status_json_reports_tier_and_latch(self):
        code, text = self.run_main(['status', '--json'], 10 * GIB)
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual((report['tier'], report['free_bytes'], report['latch']), ('emergency', 10 * GIB, True))
        self.assertIn('timestamp', report)
        code, text = self.run_main(['status', '--json'], 30 * GIB)
        self.assertTrue(json.loads(text)['latch'])


if __name__ == '__main__':
    unittest.main()
