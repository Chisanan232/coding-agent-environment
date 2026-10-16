#!/usr/bin/env python3
"""Disk-pressure admission core refuses unsafe work and fails closed."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest

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


if __name__ == '__main__':
    unittest.main()
