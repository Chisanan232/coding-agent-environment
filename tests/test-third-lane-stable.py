#!/usr/bin/env python3
"""Third heavy lane in the stable tier (SPE-176, ADR 0017 revision 12).

`heavy_lane_cap` gives lane 3 to every tier in THIRD_LANE_TIERS (stable and wide) while THIRD_LANE_ENABLED; lane 4 stays wide-only
behind FOURTH_LANE_ENABLED (off). The lane-3 disk margin (free - reserved - request >= 160 GiB, ADR 0017 revision 11) is unchanged, so in
the stable tier (free below 200 GiB) a third lane opens only while all reservations plus the request stay within 40 GiB.

Every test runs against a private temporary scheduler state with throwaway /bin/sleep processes and injected free and host samples; the
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


bf = load('admission_backfill_for_stable_lane', 'tests/test-admission-backfill.py')
da, GIB, decl = bf.da, bf.GIB, bf.decl
da.THIRD_LANE_ENABLED = True
da.FOURTH_LANE_ENABLED = False
NEED = 160 * GIB                        # HEAVY_FLOOR + THIRD_LANE_MARGIN
A, B, C, D = ('cargo-target:/t/%s' % n for n in 'abcd')


class Base(bf.Base):
    cores = 64                          # the cpu budget is not under test unless a test lowers it

    def host(self):
        sample = super().host()
        sample['cores'] = self.cores
        return sample

    def small_running(self, n=2, gib=5):
        for index, key in enumerate((A, B, D)[:n]):
            self.running('job%d' % (index + 1), 'build-crate', [key], gib)

    def reserve(self, label, free, gib=5, keys=(C,)):
        proc = self.spawn()
        return da.reserve(label, gib * GIB, proc.pid, self.path, lambda: free, now=self.now, declared=decl('build-crate', list(keys)),
                          host_fn=self.host, grant_environ={})


class CapTable(unittest.TestCase):
    def cap(self, tier, third=True, fourth=False):
        with unittest.mock.patch.object(da, 'THIRD_LANE_ENABLED', third), unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', fourth):
            return da.heavy_lane_cap(tier)

    def test_the_shipped_constants(self):
        self.assertEqual(da.THIRD_LANE_TIERS, ('stable', 'wide'))
        self.assertEqual(da.HEAVY_LANES, {'emergency': 0, 'low': 0, 'stable': 2, 'wide': 2})   # the two-lane base is untouched
        self.assertEqual((da.LANE_CEILING, da.FOUR_LANE_CEILING), (3, 4))
        shipped = load('disk_admission_stable_lane_shipped', 'scripts/disk-admission.py')
        self.assertIs(shipped.THIRD_LANE_ENABLED, True)
        self.assertIs(shipped.FOURTH_LANE_ENABLED, False)

    def test_stable_and_wide_have_three_lanes_and_nothing_below_stable_has_any(self):
        self.assertEqual([self.cap(t) for t in ('emergency', 'low', 'stable', 'wide')], [0, 0, 3, 3])
        self.assertEqual([da.heavy_lane_cap(t, True) for t in ('emergency', 'low', 'stable', 'wide')], [0, 0, 0, 0])

    def test_the_fourth_lane_is_never_reachable_in_the_stable_tier_and_stays_off_in_wide(self):
        self.assertEqual(self.cap('stable', fourth=True), 3)
        self.assertEqual(self.cap('wide', fourth=False), 3)
        self.assertEqual(self.cap('wide', fourth=True), 4)      # only when its own reviewed flag is flipped
        with unittest.mock.patch.object(da, 'THIRD_LANE_ENABLED', True), unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', True), \
                unittest.mock.patch.dict(da.HEAVY_LANES, {'stable': 9}):
            self.assertEqual(da.heavy_lane_cap('stable'), 4)    # an edited table is still capped by the ceiling, never reaches lane 4 by a tier rule

    def test_the_kill_switch_restores_two_lanes_in_every_tier(self):
        self.assertEqual([self.cap(t, third=False) for t in ('emergency', 'low', 'stable', 'wide')], [0, 0, 2, 2])
        self.assertEqual(self.cap('wide', third=False, fourth=True), 2)

    def test_a_tier_left_out_of_the_set_keeps_two_lanes(self):
        with unittest.mock.patch.object(da, 'THIRD_LANE_ENABLED', True), unittest.mock.patch.object(da, 'THIRD_LANE_TIERS', ('wide',)):
            self.assertEqual((da.heavy_lane_cap('stable'), da.heavy_lane_cap('wide')), (2, 3))


class StableReserve(Base):
    def test_a_third_lane_opens_in_the_stable_tier_only_at_the_margin(self):
        self.small_running()                                     # two 5 GiB holders: 10 GiB reserved
        edge = NEED + 10 * GIB + 5 * GIB                         # 175 GiB free is the stable tier
        self.assertEqual(da.tier(edge), 'stable')
        refused = self.reserve('job3', edge - GIB // 10)
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('budget: lane 3 needs 159.9 GiB free'), refused['reason'])
        admitted = self.reserve('job3', edge)
        self.assertTrue(admitted['admit'], admitted)
        self.assertIn('lane 3 of 3', admitted['reason'])
        self.assertEqual(admitted['tier'], 'stable')

    def test_the_cpu_budget_still_gates_the_stable_third_lane(self):
        self.small_running()
        self.cores = 4                                           # three build-crate jobs no longer fit the cpu limit
        out = self.reserve('job3', 190 * GIB)
        self.assertFalse(out['admit'], out)
        self.assertTrue(out['reason'].startswith('budget: '), out['reason'])
        self.assertNotIn('lane 3 needs', out['reason'])          # the disk margin passes (190 - 10 - 5 = 175); the cpu limit refuses

    def test_the_memory_budget_still_gates_the_stable_third_lane(self):
        self.small_running()
        proc = self.spawn()
        out = da.reserve('job3', 5 * GIB, proc.pid, self.path, lambda: 190 * GIB, now=self.now,
                         declared=decl('build-crate', [C], mem_gib=200), host_fn=self.host, grant_environ={})
        self.assertFalse(out['admit'], out)
        self.assertIn('memory', out['reason'])

    def test_a_fourth_lane_is_refused_in_the_stable_tier_by_the_lane_cap_even_with_its_flag_on(self):
        for fourth in (False, True):
            with self.subTest(fourth=fourth), unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', fourth):
                self.reap()
                self.setUp()
                self.small_running(3, 2)
                out = self.reserve('job4', 199 * GIB, gib=2, keys=(C,))
                self.assertFalse(out['admit'])
                self.assertTrue(out['reason'].startswith('lane-cap: 3 of 3 heavy lanes in use'), out['reason'])

    def test_the_fourth_lane_stays_off_in_the_wide_tier(self):
        self.small_running(3, 2)
        out = self.reserve('job4', 600 * GIB, gib=2, keys=(C,))
        self.assertFalse(out['admit'])
        self.assertTrue(out['reason'].startswith('lane-cap: 3 of 3 heavy lanes in use'), out['reason'])

    def test_the_kill_switch_refuses_the_third_lane_in_the_stable_tier(self):
        self.small_running()
        with unittest.mock.patch.object(da, 'THIRD_LANE_ENABLED', False):
            out = self.reserve('job3', 199 * GIB)
        self.assertFalse(out['admit'])
        self.assertTrue(out['reason'].startswith('lane-cap: 2 of 2 heavy lanes in use'), out['reason'])


class TodaysNumbers(unittest.TestCase):
    def test_no_combination_of_the_measured_stable_numbers_admits_a_third_lane(self):
        """Free 160-184 GiB, 32-55 GiB held, 25-40 GiB requested: the margin leaves at most 184 - 32 - 25 = 127 GiB, 33 short of 160."""
        for free in range(160, 185, 6):
            for held in (32, 40, 55):
                for request in (25, 32, 40):
                    why = da._third_lane_refusal(3, free * GIB, held * GIB, request * GIB)
                    self.assertIsNotNone(why, (free, held, request))
                    self.assertTrue(why.startswith('budget: lane 3 needs '), why)
                    self.assertEqual(da.tier(free * GIB), 'stable')
                    self.assertEqual(da.heavy_lane_cap('stable'), 3)    # the cap allows it; the margin is what refuses


class HeadProtectionStable(Base):
    """Two jobs run; the head waits on the first one's key. In the stable tier a candidate takes lane 3, the head takes lane 3 again once
    the blocker releases: the head's own margin must hold, and a tier drop from wide to stable no longer removes the head's lane."""
    def shape(self, head_gib, free):
        self.blocker, _ = self.running('blocker', 'build-crate', [A], 5)
        self.running('other', 'build-crate', [B], 5)
        self.head = self.enqueue('head', 'build-crate', [A], head_gib)
        self.cand = self.enqueue('cand', 'build-crate', [C], 5)
        entries = self.entries()
        live, _ = da.live_reservations(self.path, self.now)
        return da._protects_head(self.host(), entries[self.head], entries[self.cand], [{'label': 'blocker'}], live, [], [], self.now,
                                 da.tier(free * GIB), free * GIB, 10 * GIB, 2, 3)

    def test_the_head_keeps_its_lane_three_at_the_margin_and_loses_it_below(self):
        # candidate 5 + holders 10 + head h: free - 15 - h >= 160
        ok, math = self.shape(head_gib=10, free=185)
        self.assertEqual((math['tier_after'], math['lanes_after'], math['cap']), ('stable', 3, 3))
        self.assertTrue(ok, math)
        self.assertEqual(math['disk_left'], 160 * GIB)
        self.reap()
        self.setUp()
        ok, math = self.shape(head_gib=11, free=185)
        self.assertFalse(ok, math)
        self.assertGreaterEqual(math['disk_left'], da.HEAVY_FLOOR)   # above the floor: only the lane-3 margin refuses

    def test_a_candidate_is_backfilled_in_the_stable_tier_only_while_the_head_keeps_the_margin(self):
        for head_gib, backfilled in ((10, True), (11, False)):
            with self.subTest(head_gib=head_gib), bf.backfill():
                self.reap()
                self.setUp()
                self.shape(head_gib, 185)
                out = self.advance(free=185 * GIB)
                self.assertEqual(out['backfilled'], [self.cand] if backfilled else [], out)


class Replay(Base):
    """A synthetic queue in a temporary state with injected free and host samples: key-disjoint small waiters."""
    def waiters(self, n=3, gib=5):
        return [self.enqueue('w%d' % i, 'build-crate', ['cargo-target:/t/w%d' % i], gib) for i in range(n)]

    def test_three_key_disjoint_small_waiters_are_all_granted_in_the_stable_tier(self):
        ids = self.waiters()
        out = self.advance(free=190 * GIB)                       # stable; 190 - 10 - 5 = 175 >= 160 for the third
        self.assertEqual(out['granted'], ids, out)
        self.assertEqual([self.state(i) for i in ids], ['GRANTED'] * 3)

    def test_at_today_s_stable_numbers_with_50_gib_held_no_third_lane_is_granted(self):
        self.running('held-a', 'build-crate', [A], 25)
        self.running('held-b', 'build-crate', [B], 25)
        ids = self.waiters(1, gib=25)
        for free in (160, 172, 184):
            with self.subTest(free=free):
                out = self.advance(free=free * GIB)
                self.assertEqual(out['granted'], [], out)
                self.assertEqual(self.state(ids[0]), 'QUEUED')

    def test_the_kill_switch_grants_only_two_of_the_same_waiters(self):
        ids = self.waiters()
        with unittest.mock.patch.object(da, 'THIRD_LANE_ENABLED', False):
            out = self.advance(free=190 * GIB)
        self.assertEqual(out['granted'], ids[:2], out)

    def test_a_granted_third_lane_is_claimed_in_the_stable_tier_with_its_margin_and_refused_without(self):
        self.small_running()
        qid = self.enqueue('job3', 'build-crate', [C], 5)
        self.assertEqual(self.advance(free=190 * GIB)['granted'], [qid])
        entry = self.entries()[qid]
        wrapper = next(p for p in self.procs if p.pid == entry['waiter']['pid'])
        declared = {k: entry[k] for k in ('class', 'mem_bytes', 'cpu_slots', 'test_threads', 'excl_keys')}

        def claim(free):
            return da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ['cargo', 'test', entry['label']], str(self.worktree),
                                  lambda: free, pid=wrapper.pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                                  declared=declared, host_fn=self.host, grant_environ={})
        refused = claim(NEED + 15 * GIB - GIB // 10)
        self.assertFalse(refused['ok'])
        self.assertTrue(refused['reason'].startswith('budget: lane 3 needs 159.9 GiB free'), refused['reason'])
        self.assertTrue(claim(NEED + 15 * GIB)['ok'])


if __name__ == '__main__':
    unittest.main()
