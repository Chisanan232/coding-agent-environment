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


if __name__ == '__main__':
    unittest.main()
