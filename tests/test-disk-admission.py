#!/usr/bin/env python3
"""Disk-pressure admission core refuses unsafe work and fails closed."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
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
        self.path.parent.mkdir(parents=True)
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


if __name__ == '__main__':
    unittest.main()
