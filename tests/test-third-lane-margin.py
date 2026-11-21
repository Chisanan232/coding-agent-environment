#!/usr/bin/env python3
"""Lane-3 disk margin (SPE-176, ADR 0017 revision 11): a third heavy lane needs free - reserved - request >= HEAVY_FLOOR + 60 GiB at
every lane site (reserve, advance, claim, backfill candidate, head protection), and no other lane is touched.

Every test runs against a private temporary scheduler state with throwaway /bin/sleep processes and injected free and host
samples; the owner's admission state and queue are never read or written."""
from datetime import timedelta
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


bf = load('admission_backfill_for_margin', 'tests/test-admission-backfill.py')
da, GIB, decl = bf.da, bf.GIB, bf.decl
da.THIRD_LANE_ENABLED = True
da.FOURTH_LANE_ENABLED = False
NEED = 160 * GIB                        # HEAVY_FLOOR + THIRD_LANE_MARGIN
A, B, C, D = ('cargo-target:/t/%s' % n for n in 'abcd')


class Base(bf.Base):
    def host(self):
        sample = super().host()
        sample['cores'] = 64            # the cpu budget is not under test
        return sample

    def two_running(self):
        self.running('job1', 'build-crate', [A], 20)
        self.running('job2', 'build-crate', [B], 20)

    def reserve(self, label, free, gib=20, proc=None, keys=(C,)):
        proc = proc or self.spawn()
        return da.reserve(label, gib * GIB, proc.pid, self.path, lambda: free, now=self.now, declared=decl('build-crate', list(keys)),
                          host_fn=self.host, grant_environ={}), proc

    def claim_real(self, qid, free):
        entry = self.entries()[qid]
        wrapper = next(p for p in self.procs if p.pid == entry['waiter']['pid'])
        declared = {k: entry[k] for k in ('class', 'mem_bytes', 'cpu_slots', 'test_threads', 'excl_keys')}
        return da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ['cargo', 'test', entry['label']], str(self.worktree),
                              lambda: free, pid=wrapper.pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                              declared=declared, host_fn=self.host, grant_environ={})


class Helper(unittest.TestCase):
    def test_only_lane_three_is_judged_and_the_boundary_is_exact(self):
        for lane in (1, 2, 4):
            self.assertIsNone(da._third_lane_refusal(lane, 0, 0, 0), lane)
        self.assertEqual(da.THIRD_LANE_MARGIN, 60 * GIB)
        self.assertEqual(da.HEAVY_FLOOR + da.THIRD_LANE_MARGIN, NEED)
        self.assertIsNone(da._third_lane_refusal(3, NEED + 40 * GIB + 20 * GIB, 40 * GIB, 20 * GIB))
        why = da._third_lane_refusal(3, NEED + 40 * GIB + 20 * GIB - GIB // 10, 40 * GIB, 20 * GIB)
        self.assertEqual(why, 'budget: lane 3 needs 159.9 GiB free after all reservations and this request, 160 GiB required')

    def test_the_margin_is_the_documented_trial_criterion(self):
        self.assertEqual(da.THIRD_LANE_MARGIN, da.TRIAL_CRITERIA['free_margin_bytes'])


class Reserve(Base):
    def test_lane_three_is_refused_at_159_9_left_and_admitted_at_160(self):
        self.two_running()
        refused, _ = self.reserve('job3', NEED + 40 * GIB + 20 * GIB - GIB // 10)
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('budget: lane 3 needs 159.9 GiB free'), refused['reason'])
        self.assertFalse((da.reservations_dir(self.path) / 'job3.json').exists())
        admitted, _ = self.reserve('job3', NEED + 40 * GIB + 20 * GIB)
        self.assertTrue(admitted['admit'], admitted)
        self.assertIn('lane 3 of 3', admitted['reason'])

    def test_a_same_process_refresh_of_lane_three_is_exempt(self):
        self.two_running()
        first, proc = self.reserve('job3', NEED + 40 * GIB + 20 * GIB)
        self.assertTrue(first['admit'], first)
        refresh, _ = self.reserve('job3', 205 * GIB, proc=proc)    # wide tier, 145 GiB left: below the margin, but it already holds the lane
        self.assertTrue(refresh['admit'], refresh)

    def test_lanes_one_and_two_never_need_the_margin(self):
        free = 140 * GIB                 # stable tier, two lanes: 140 - 20 - 20 = 100 GiB left, above the floor and below the margin
        for n in (1, 2):
            out, _ = self.reserve('job%d' % n, free, keys=(A if n == 1 else B,))
            self.assertTrue(out['admit'], out)

    def test_lane_three_is_judged_by_the_margin_in_every_mode_and_lane_four_by_its_own(self):
        with unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', True):
            self.two_running()
            self.assertIn('lane 3 needs', self.reserve('job3', NEED + 60 * GIB - GIB // 10)[0]['reason'])
            self.assertTrue(self.reserve('job3', NEED + 60 * GIB)[0]['admit'])
            four = self.reserve('job4', 260 * GIB, keys=(D,))[0]
            self.assertFalse(four['admit'])
            self.assertTrue(four['reason'].startswith('budget: lane 4 needs '), four['reason'])   # its own 200 GiB rule, unchanged
            self.assertTrue(self.reserve('job4', 280 * GIB, keys=(D,))[0]['admit'])


class Advance(Base):
    def test_a_queued_third_lane_is_granted_only_with_the_margin(self):
        self.two_running()
        qid = self.enqueue('job3', 'build-crate', [C], 20)
        out = self.advance(free=NEED + 60 * GIB - GIB // 10)
        self.assertEqual(out['granted'], [])
        self.assertTrue(out['reason'].startswith('budget: lane 3 needs 159.9 GiB free'), out['reason'])
        self.assertEqual(self.advance(free=NEED + 60 * GIB)['granted'], [qid])


class Claim(Base):
    def test_a_granted_third_lane_is_refused_at_claim_below_the_margin_and_keeps_its_grant(self):
        self.two_running()
        qid = self.enqueue('job3', 'build-crate', [C], 20)
        self.assertEqual(self.advance()['granted'], [qid])
        refused = self.claim_real(qid, NEED + 60 * GIB - GIB // 10)
        self.assertFalse(refused['ok'])
        self.assertTrue(refused['reason'].startswith('budget: lane 3 needs 159.9 GiB free'), refused['reason'])
        self.assertEqual(self.state(qid), 'GRANTED')
        self.assertTrue(self.claim_real(qid, NEED + 60 * GIB)['ok'])


class HeadProtection(Base):
    """Two jobs run; the head waits on the first one's key. A candidate takes lane 3 and the head takes lane 3 again once the blocker
    releases, so the head's own lane-3 margin must hold when the candidate is judged."""
    def shape(self):
        self.blocker, _ = self.running('blocker', 'build-crate', [A], 20)
        self.running('other', 'build-crate', [B], 20)
        self.head = self.enqueue('head', 'build-crate', [A], 60)
        self.cand = self.enqueue('cand', 'build-crate', [C], 32)

    # Two 20 GiB holders, a 32 GiB candidate and a 60 GiB head. The tier check (free - 40 - 32 stays wide, so 272) is already met well
    # before the head's lane-3 margin (free - 40 - 32 - 60 >= 160, so 292): between the two only the margin refuses.
    def test_a_candidate_that_would_leave_the_head_short_of_the_lane_three_margin_is_refused(self):
        self.shape()
        out = self.advance(free=290 * GIB)
        self.assertEqual((out['granted'], out['backfilled']), ([], []))
        self.assertEqual(self.state(self.cand), 'QUEUED')

    def test_protects_head_states_the_lane_three_margin_directly(self):
        self.shape()
        entries = self.entries()
        live, _ = da.live_reservations(self.path, self.now)
        args = (self.host(), entries[self.head], entries[self.cand], [{'label': 'blocker'}], live, [], [], self.now, 'wide')
        ok, math = da._protects_head(*args, 290 * GIB, 40 * GIB, 2, 3)
        self.assertFalse(ok)
        self.assertEqual(math['disk_left'], 158 * GIB)       # above the floor: only the lane-3 margin refuses
        self.assertGreaterEqual(math['disk_left'], da.HEAVY_FLOOR)
        self.assertTrue(da._protects_head(*args, 292 * GIB, 40 * GIB, 2, 3)[0])

    def test_a_margin_only_head_refusal_is_logged_with_its_own_reason(self):
        self.shape()
        with unittest.mock.patch.object(da, 'BACKFILL_ENABLED', True):
            out = self.advance(free=290 * GIB)             # disk, lanes and tier all pass for the head: only its lane-3 margin refuses
        self.assertEqual(out['backfilled'], [])
        logged = [d for d in self.receipts('backfill-decision') if d['id'] == self.cand]
        self.assertEqual([(d['outcome'], d['reason'], d['blocked_id']) for d in logged],
                         [('refused', 'head-unprotected-lane3-margin', self.head)])
        self.assertIn('head-unprotected-lane3-margin', da.BACKFILL_DECISION_REASONS)

    def test_the_same_candidate_is_backfilled_once_the_head_has_room(self):
        self.shape()
        self.assertEqual(self.advance(free=292 * GIB)['backfilled'], [self.cand])

    def test_a_backfilled_grant_is_revoked_at_claim_when_the_head_would_miss_the_margin(self):
        self.shape()
        self.assertEqual(self.advance(free=320 * GIB)['backfilled'], [self.cand])
        out = self.claim_real(self.cand, 290 * GIB)           # its own lane-3 margin holds (290 - 40 - 32 = 218); the head's does not
        self.assertFalse(out['ok'])
        self.assertIn('(head-unprotected)', out['reason'])
        self.assertEqual(self.state(self.cand), 'EXPIRED')


class Replay(Base):
    """The 2026-11-21 queue shape: one claimed cargo-target holder (40 GiB) and waiters of 35 (same target), 30 and 25 (docker), 10 GiB."""
    def shape(self, head_keys, small_keys=('small',)):
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        self.waiters = [self.enqueue('cargo', 'build-workspace', head_keys, 35),
                        self.enqueue('docker-a', 'container-suite', ['docker'], 30),
                        self.enqueue('docker-b', 'container-suite', ['docker'], 25),
                        self.enqueue('small', 'build-crate', list(small_keys), 10)]

    def test_at_160_gib_nothing_is_granted_the_head_fails_the_floor(self):
        self.shape([bf.SHARED])
        out = self.advance(free=160 * GIB)
        self.assertEqual(out['granted'], [])
        self.assertTrue(out['reason'].startswith('over-capacity: 160.0 GiB free, 40.0 GiB reserved, 35.0 GiB requested'), out['reason'])

    def test_at_184_gib_nothing_is_granted_every_backfill_candidate_misses_the_head_floor(self):
        self.shape([bf.SHARED])
        out = self.advance(free=184 * GIB)
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['backfilled'], [])
        self.assertEqual(out['reason'], 'budget: excl_key overlap with holder')     # the head fits the floor (109) and waits on its key only
        # the smallest candidate leaves 184 - 40 - 10 - 35 = 99 GiB for the head: one GiB under the floor, so no third lane is reached
        self.assertEqual([self.state(q) for q in self.waiters], ['QUEUED'] * 4)

    def test_at_260_gib_three_disjoint_jobs_run_together(self):
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        second = self.enqueue('second', 'build-workspace', [bf.LOCAL], 35)
        third = self.enqueue('third', 'container-suite', ['docker'], 25)      # 260 - 75 - 25 = 160: exactly the margin
        out = self.advance(free=260 * GIB)
        self.assertEqual(out['granted'], [second, third])

    def test_at_260_gib_a_30_gib_third_job_misses_the_margin_by_five(self):
        self.running('holder', 'build-workspace', [bf.SHARED], 40)
        second = self.enqueue('second', 'build-workspace', [bf.LOCAL], 35)
        third = self.enqueue('third', 'container-suite', ['docker'], 30)
        out = self.advance(free=260 * GIB)
        self.assertEqual(out['granted'], [second])
        self.assertEqual(self.state(third), 'QUEUED')
        refused = [r for r in self.receipts('shadow-budget') if r['id'] == third][-1]
        self.assertEqual(refused['budget']['enforced'], 'refuse')     # the budget itself passes (fits_if_cap_were_3); the margin refuses
        self.assertEqual(self.advance(free=265 * GIB)['granted'], [third])         # 265 - 75 - 30 = 160: exactly the margin


if __name__ == '__main__':
    unittest.main()
