#!/usr/bin/env python3
"""SPE-188: a head refused by the disk floor alone must not starve later jobs that fit.

Live shape (2026-11-24): q001041 (40 GiB) at the head with 135 GiB free and the 100 GiB floor; 135 - 40 = 95 < 100, so nothing was
granted while q001042..q001045, four of them 20 GiB or less, would each have fit. Bounded backfill only covered a head refused by an
exclusion key a running job holds. These tests cover the floor-only head: the same bounds (BACKFILL_MAX, waiter proven alive,
requeue-capable, the 600 s grant window, the claim-time recheck) plus the rule that the head must be unable to start even on an empty
queue, so backfill can never keep it from a disk that has become large enough.

Every test runs against a private temporary scheduler state; the owner's admission state and queue are never read or written."""
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = spec_from_file_location('test_admission_backfill_base', ROOT / 'tests/test-admission-backfill.py')
tb = module_from_spec(_spec)
sys.modules[_spec.name] = tb
_spec.loader.exec_module(tb)
da, GIB, backfill = tb.da, tb.GIB, tb.backfill

OTHER = 'cargo-target:/Users/dev/.cargo/targets/other'


class FloorOnlyHead(tb.Base):
    def shape(self):
        """No job is running. The head needs 40 GiB, three smaller jobs follow, all on distinct keys."""
        self.head = self.enqueue('la-cargo-head', 'build-workspace', [tb.LOCAL], 40)
        self.small_a = self.enqueue('gate-recording', 'build-workspace', [tb.SHARED], 20)
        self.small_b = self.enqueue('measurement', 'build-workspace', [OTHER], 20)

    def test_a_small_job_passes_a_head_the_floor_refuses(self):
        self.shape()
        with backfill():
            out = self.advance(free=135 * GIB)
        self.assertEqual(out['backfilled'], [self.small_a])
        self.assertEqual(self.state(self.head), 'QUEUED')
        self.assertEqual(self.state(self.small_a), 'GRANTED')
        record = self.receipts('backfill')[0]
        self.assertEqual((record['blocked_id'], record['reason'], record['blockers']), (self.head, 'head-floor-only', []))
        self.assertEqual((record['in_flight'], record['cap']), (0, 1))
        self.assertEqual(record['math']['disk_left'], 115 * GIB)

    def test_without_the_fix_shape_the_head_alone_is_refused_by_the_floor(self):
        """Control: with backfill off nothing passes the head, which is the starvation the live queue showed."""
        self.shape()
        with backfill(enabled=False):
            out = self.advance(free=135 * GIB)
        self.assertEqual(out.get('granted', []), [])
        self.assertIn('over-capacity', out['reason'])

    def test_at_most_backfill_max_jobs_are_in_flight(self):
        self.shape()
        with backfill():
            first = self.advance(free=135 * GIB)
            second = self.advance(free=135 * GIB)
        self.assertEqual(first['backfilled'], [self.small_a])
        self.assertEqual(second.get('backfilled', []), [])
        self.assertEqual(self.state(self.small_b), 'QUEUED')

    def test_a_candidate_that_does_not_fit_with_the_margin_is_refused(self):
        """A 32 GiB job leaves 103 GiB: above the plain floor but below the floor plus the 4 GiB grant-time hysteresis."""
        self.head = self.enqueue('la-cargo-head', 'build-workspace', [tb.LOCAL], 40)
        big = self.enqueue('almost-too-big', 'build-workspace', [tb.SHARED], 32)   # 135 - 32 = 103 < 100 + 4
        with backfill():
            out = self.advance(free=135 * GIB)
        self.assertEqual(out.get('backfilled', []), [])
        self.assertEqual(self.state(big), 'QUEUED')
        decisions = self.receipts('backfill-decision')
        self.assertEqual(decisions[-1]['reason'], 'head-unprotected-hysteresis')

    def test_a_head_that_fits_on_an_empty_queue_is_never_overtaken_for_the_floor(self):
        """160 GiB free: the head alone would fit (160 - 40 >= 100). A held reservation must not let later jobs keep taking its disk."""
        self.holder, self.holder_proc = self.running('running-job', 'build-workspace', [tb.SHARED], 25)
        self.head = self.enqueue('la-cargo-head', 'build-workspace', [tb.LOCAL], 40)
        later = self.enqueue('small', 'build-workspace', [OTHER], 10)
        with backfill():
            out = self.advance(free=160 * GIB)   # 160 - 25 - 40 = 95 < 100: refused only because of the running job's reservation
        self.assertEqual(out.get('backfilled', []), [])
        self.assertEqual(self.state(later), 'QUEUED')

    def test_the_head_is_granted_first_when_the_disk_grows(self):
        self.shape()
        with backfill():
            self.advance(free=135 * GIB)
            out = self.advance(free=400 * GIB)
        self.assertEqual(out['granted'][0], self.head)

    def test_a_waiter_that_is_not_alive_or_not_requeue_capable_is_not_backfilled(self):
        self.head = self.enqueue('la-cargo-head', 'build-workspace', [tb.LOCAL], 40)
        old = self.enqueue('old-wrapper', 'build-workspace', [tb.SHARED], 20, capable=False)
        gone = self.enqueue('no-waiter', 'build-workspace', [OTHER], 20, waiter=False)
        with backfill():
            out = self.advance(free=135 * GIB)
        self.assertEqual(out.get('backfilled', []), [])
        self.assertEqual((self.state(old), self.state(gone)), ('QUEUED', 'QUEUED'))

    def test_a_job_sharing_the_heads_key_is_not_backfilled(self):
        self.head = self.enqueue('la-cargo-head', 'build-workspace', [tb.LOCAL], 40)
        same = self.enqueue('same-key', 'build-workspace', [tb.LOCAL], 20)
        with backfill():
            out = self.advance(free=135 * GIB)
        self.assertEqual(out.get('backfilled', []), [])
        self.assertEqual(self.state(same), 'QUEUED')

    def test_the_backfilled_grant_lapses_after_its_window_and_the_head_stays_first(self):
        self.shape()
        with backfill():
            self.advance(free=135 * GIB)
            out = self.advance(free=135 * GIB, now=self.now + timedelta(seconds=da.BACKFILL_GRANT_S + 1))
        self.assertIn(self.small_a, out['expired'])
        self.assertEqual(self.state(self.head), 'QUEUED')

    def test_the_claim_time_recheck_keeps_a_floor_only_grant(self):
        """The claim path asks _backfill_protection_lost; a head still waiting for free space alone must not revoke the grant."""
        self.shape()
        with backfill():
            self.advance(free=135 * GIB)
            entries = da._queue_entries(self.path)[0]
            entry = next(e for e in entries if e['id'] == self.small_a)
            lost = da._backfill_protection_lost(self.host(), entry, entries, [], [], self.now, {'free_bytes': 135 * GIB, 'tier': 'wide', 'latch': False},
                                                0, 0, 3)
        self.assertIsNone(lost)

    def test_the_claim_time_recheck_defers_to_head_protection_once_the_head_fits(self):
        self.shape()
        with backfill():
            self.advance(free=135 * GIB)
            entries = da._queue_entries(self.path)[0]
            entry = next(e for e in entries if e['id'] == self.small_a)
            lost = da._backfill_protection_lost(self.host(), entry, entries, [], [], self.now, {'free_bytes': 400 * GIB, 'tier': 'wide', 'latch': False},
                                                0, 0, 3)
        self.assertIsNone(lost, 'a head that now fits outright is judged by _protects_head, which a 20 GiB grant beside 400 GiB free keeps')


if __name__ == '__main__':
    unittest.main()
