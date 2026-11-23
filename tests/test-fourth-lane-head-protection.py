#!/usr/bin/env python3
"""Lane-4 head protection (SPE-176, ADR 0017 revision 11/12 prerequisite): with four-lane mode on, a backfill candidate must not
use up the 200 GiB FOURTH_LANE_MARGIN the blocked head will need once it is the fourth job, mirroring the lane-3 rule.

Runs against a private temporary scheduler state with throwaway /bin/sleep processes and injected free and host samples; the
owner's admission state and queue are never read or written."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tm = load('third_lane_margin_for_lane4', 'tests/test-third-lane-margin.py')
da, GIB = tm.da, tm.GIB
A, B, C, D = tm.A, tm.B, tm.C, tm.D


class Helper(unittest.TestCase):
    def setUp(self):
        for name in ('THIRD_LANE_ENABLED', 'FOURTH_LANE_ENABLED'):
            patcher = unittest.mock.patch.object(da, name, True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_lane_four_in_four_lane_mode_is_judged_and_the_boundary_is_exact(self):
        for lane in (1, 2, 3, 5):
            self.assertIsNone(da._fourth_lane_margin_refusal(lane, 0, 0, 0), lane)
        need = da.FOURTH_LANE_MARGIN
        self.assertIsNone(da._fourth_lane_margin_refusal(4, need + 40 * GIB + 20 * GIB, 40 * GIB, 20 * GIB))
        why = da._fourth_lane_margin_refusal(4, need + 40 * GIB + 20 * GIB - GIB // 10, 40 * GIB, 20 * GIB)
        self.assertTrue(why.startswith('budget: lane 4 needs 199.9 GiB free'), why)

    def test_four_lane_mode_off_never_judges_lane_four(self):
        with unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', False):
            self.assertIsNone(da._fourth_lane_margin_refusal(4, 0, 0, 0))


class HeadProtection(tm.Base):
    """Three jobs run; the head waits on the first one's key. A candidate would take a free lane and the head takes lane 4 once the
    blocker releases, so the head's own 200 GiB margin must hold when the candidate is judged."""
    def setUp(self):
        super().setUp()
        for name in ('THIRD_LANE_ENABLED', 'FOURTH_LANE_ENABLED'):
            patcher = unittest.mock.patch.object(da, name, True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def shape(self):
        self.running('blocker', 'build-crate', [A], 20)
        self.running('second', 'build-crate', [B], 20)
        self.running('third', 'build-crate', [D], 20)
        self.head = self.enqueue('head', 'build-crate', [A], 60)
        self.cand = self.enqueue('cand', 'build-crate', [C], 32)

    def args(self):
        entries = self.entries()
        live, _ = da.live_reservations(self.path, self.now)
        return (self.host(), entries[self.head], entries[self.cand], [{'label': 'blocker'}], live, [], [], self.now, 'wide')

    # Three 20 GiB holders (60 reserved), a 32 GiB candidate and a 60 GiB head: head left = free - 60 - 32 - 60, which must reach 200.
    def test_a_candidate_that_would_leave_the_head_short_of_the_lane_four_margin_is_refused(self):
        self.shape()
        args = self.args()
        ok, math = da._protects_head(*args, 351 * GIB, 60 * GIB, 3, 4)
        self.assertFalse(ok)
        self.assertEqual(math['disk_left'], 199 * GIB)           # above the floor: only the lane-4 margin refuses
        self.assertGreaterEqual(math['disk_left'], da.HEAVY_FLOOR)
        self.assertEqual(da._head_unprotected_part(math, [{'label': 'blocker'}]), 'head-unprotected-lane4-margin')
        self.assertTrue(da._protects_head(*args, 352 * GIB, 60 * GIB, 3, 4)[0])

    def test_with_four_lane_mode_off_the_same_figures_are_not_refused_for_lane_four(self):
        self.shape()
        with unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', False):
            _, math = da._protects_head(*self.args(), 351 * GIB, 60 * GIB, 3, 4)
            self.assertNotEqual(da._head_unprotected_part(math, [{'label': 'blocker'}]), 'head-unprotected-lane4-margin')

    def test_an_unstamped_head_is_not_protected_when_it_would_take_lane_three_or_four_in_four_lane_mode(self):
        self.shape()
        args = list(self.args())
        args[1] = {k: v for k, v in args[1].items() if k != 'lane_protocol'}          # an entry written by older code
        ok, math = da._protects_head(*args, 352 * GIB, 60 * GIB, 3, 4)
        self.assertFalse(ok)
        self.assertIs(math['head_stamped'], False)
        self.assertEqual(da._head_unprotected_part(math, [{'label': 'blocker'}]), 'head-unprotected-lanes')
        stamped = list(self.args())
        self.assertTrue(da._protects_head(*stamped, 352 * GIB, 60 * GIB, 3, 4)[0])

    def test_queue_advance_backfills_behind_a_lane_four_head_only_with_the_head_margin(self):
        self.shape()
        with unittest.mock.patch.object(da, 'BACKFILL_ENABLED', True):
            short = self.advance(free=330 * GIB)          # the candidate fits lane 4 (292 GiB) but the head would be 22 GiB short of 200
            self.assertEqual((short['granted'], short['backfilled']), ([], []))
            self.assertEqual(self.state(self.cand), 'QUEUED')
            logged = [d for d in self.receipts('backfill-decision') if d['id'] == self.cand]
            self.assertEqual([(d['outcome'], d['reason'], d['blocked_id']) for d in logged],
                             [('refused', 'head-unprotected-lane4-margin', self.head)])
            enough = self.advance(free=360 * GIB)
            self.assertEqual(enough['backfilled'], [self.cand])

    def test_the_new_reason_is_in_the_closed_vocabulary(self):
        self.assertIn('head-unprotected-lane4-margin', da.BACKFILL_DECISION_REASONS)


if __name__ == '__main__':
    unittest.main()
