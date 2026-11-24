#!/usr/bin/env python3
"""SPE-188: floor-only backfill may take at most BACKFILL_LOSS_CAP (20 GiB) of usable space from a head that waits for free space alone.

The head (160 GiB) cannot start on an empty queue at 250 GiB free (250 - 160 = 90 < 100), so it waits for free space and later jobs may
backfill past it (test-admission-floor-backfill.py). These tests cover the cumulative bound: a baseline recorded at the first floor-blocked
pass, the loss counted against it (jobs that keep their output, reclaimed space, several grants at once), the baseline's lifecycle (head
change, head start, head gone, restart, size) and the claim-time recheck.

Every test runs against a private temporary scheduler state; the owner's admission state and queue are never read or written."""
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = spec_from_file_location('test_admission_backfill_base', ROOT / 'tests/test-admission-backfill.py')
tb = module_from_spec(_spec)
sys.modules[_spec.name] = tb
_spec.loader.exec_module(tb)
da, GIB, backfill = tb.da, tb.GIB, tb.backfill

HEAD_GIB, FREE0 = 160, 250
KEY = 'cargo-target:/Users/dev/.cargo/targets/k%d'


class LossCap(tb.Base):
    def head(self, gib=HEAD_GIB, label='la-cargo-head'):
        return self.enqueue(label, 'build-workspace', [tb.LOCAL], gib)

    def small(self, i, gib=12, cpus=2):
        return self.enqueue('s%d' % i, 'build-workspace', [KEY % i], gib, cpus=cpus)

    def baseline(self):
        return da._read_loss_baseline(self.path)

    def run_to_completion(self, qid):
        """The granted job runs, finishes and its process exits; the caller lowers free space by what it kept."""
        holder = self.claim(qid)
        holder.kill()
        holder.wait()

    def decisions(self, reason):
        return [r for r in self.receipts('backfill-decision') if r['reason'] == reason]

    def test_the_first_floor_blocked_pass_records_the_baseline(self):
        head = self.head()
        self.small(0)
        with backfill():
            self.advance(free=FREE0 * GIB)
        record = self.baseline()
        self.assertEqual((record['head_id'], record['baseline_free_bytes']), (head, FREE0 * GIB))
        self.assertEqual(sorted(record), ['baseline_free_bytes', 'first_seen', 'head_id'])

    def test_jobs_that_keep_their_output_are_stopped_at_the_cap(self):
        head = self.head()
        first, second = self.small(0), self.small(1)
        free = FREE0 * GIB
        with backfill():
            out = self.advance(free=free)
        self.assertEqual(out['backfilled'], [first])   # loss 12 GiB <= 20
        self.run_to_completion(first)
        free -= 12 * GIB                              # it kept all it declared
        with backfill():
            out = self.advance(free=free)
        self.assertEqual(out.get('backfilled', []), [])   # 250 - (238 - 12) = 24 GiB > 20
        self.assertEqual(self.state(second), 'QUEUED')
        self.assertEqual(self.state(head), 'QUEUED')
        refusal = self.decisions('cumulative-loss-cap')[-1]
        self.assertEqual((refusal['outcome'], refusal['loss_baseline'], refusal['loss_after'], refusal['loss_cap']),
                         ('refused', FREE0 * GIB, 24 * GIB, 20 * GIB))

    def test_jobs_that_clean_up_are_never_stopped(self):
        self.head()
        free = FREE0 * GIB
        for i in range(6):
            qid = self.small(i)
            with backfill():
                out = self.advance(free=free)
            self.assertEqual(out['backfilled'], [qid], 'round %d' % i)
            self.run_to_completion(qid)                  # nothing kept: free space is unchanged
        self.assertEqual(self.decisions('cumulative-loss-cap'), [])

    def test_reclaimed_space_gives_the_headroom_back(self):
        self.head()
        first, second = self.small(0), self.small(1)
        free = FREE0 * GIB
        with backfill():
            self.advance(free=free)
        self.run_to_completion(first)
        free -= 12 * GIB
        with backfill():
            self.assertEqual(self.advance(free=free).get('backfilled', []), [])
        free += 7 * GIB                                   # 250 - (245 - 12) = 17 GiB <= 20
        with backfill():
            out = self.advance(free=free)
        self.assertEqual(out['backfilled'], [second])
        self.assertEqual(self.baseline()['baseline_free_bytes'], FREE0 * GIB, 'the baseline never moves with free space')

    def test_free_space_above_the_baseline_gives_zero_loss_and_never_moves_the_baseline(self):
        self.head()
        self.small(0)
        with backfill():
            self.advance(free=(FREE0 - 1) * GIB)   # first sight at 249
        record = self.baseline()
        self.assertEqual(record['baseline_free_bytes'], (FREE0 - 1) * GIB)
        self.assertEqual(da._loss_after(record['baseline_free_bytes'], FREE0 * GIB, 0, 0), 0, 'never negative')
        with backfill():
            self.advance(free=(FREE0 + 5) * GIB)   # free grew: a later pass must not move the baseline up
        self.assertEqual(self.baseline()['baseline_free_bytes'], (FREE0 - 1) * GIB)

    def test_simultaneous_grants_are_summed_against_one_cap(self):
        self.head()
        candidates = [self.small(i, gib=8) for i in range(5)]
        with backfill(cap=3):
            out = self.advance(free=FREE0 * GIB)
        granted = [c for c in candidates if self.state(c) in ('GRANTED', 'CLAIMED')]
        self.assertEqual(sorted(out['backfilled']), sorted(granted))
        self.assertEqual(len(granted), 2, '8 + 8 = 16 GiB of loss is within 20; the third would make 24')
        self.assertTrue(self.decisions('cumulative-loss-cap'))
        reserved = sum(self.entries()[c]['bytes'] for c in granted)
        self.assertGreaterEqual(FREE0 * GIB - reserved, (100 + 4) * GIB, 'the byte floor is never breached')

    def test_a_new_head_gets_its_own_baseline(self):
        old = self.head()
        self.small(0)
        free = FREE0 * GIB
        with backfill():
            self.advance(free=free)
        self.assertEqual(self.baseline()['head_id'], old)
        with da.state_lock(self.path):
            entry = self.entries()[old]
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.path, entry)
        new = self.head(gib=170, label='another-head')
        free = 240 * GIB
        with backfill():
            self.advance(free=free)
        record = self.baseline()
        # usable space at the new head's first sighting: 240 free minus the 12 GiB the old head's backfilled grant still reserves
        self.assertEqual((record['head_id'], record['baseline_free_bytes']), (new, 228 * GIB), 'not inherited from the old head')

    def test_the_baseline_is_dropped_when_the_head_starts(self):
        head = self.head()
        with backfill():
            self.advance(free=FREE0 * GIB)
        self.assertEqual(self.baseline()['head_id'], head)
        with backfill():
            out = self.advance(free=400 * GIB)   # 400 - 160 >= 100: the head fits and is granted first
        self.assertEqual(out['granted'][0], head)
        self.assertIsNone(self.baseline())

    def test_the_baseline_is_dropped_when_the_head_stops_being_floor_blocked(self):
        head = self.head()
        self.small(0)
        with backfill():
            self.advance(free=FREE0 * GIB)
        self.assertIsNotNone(self.baseline())
        with backfill():
            self.advance(free=400 * GIB, memory_fn=lambda: (False, 'pressure'))   # the head now fits on an empty queue but the memory gate holds it
        self.assertIsNone(self.baseline())
        self.assertEqual(self.state(head), 'QUEUED')

    def test_the_baseline_is_dropped_when_no_head_waits(self):
        head = self.head()
        with backfill():
            self.advance(free=FREE0 * GIB)
        with da.state_lock(self.path):
            entry = self.entries()[head]
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.path, entry)
        with backfill():
            self.advance(free=FREE0 * GIB)
        self.assertIsNone(self.baseline())

    def test_the_baseline_survives_a_restart_and_keeps_its_first_sighting(self):
        head = self.head()
        with backfill():
            self.advance(free=FREE0 * GIB)
        first = self.baseline()
        with backfill():
            self.advance(free=(FREE0 - 9) * GIB, now=self.now + timedelta(minutes=5))   # a fresh process reads it from disk
        self.assertEqual(self.baseline(), first)
        self.assertEqual(first['head_id'], head)

    def test_the_state_is_three_fixed_keys_and_a_malformed_file_is_rewritten(self):
        self.head()
        with backfill():
            self.advance(free=FREE0 * GIB)
        path = da._loss_baseline_path(self.path)
        self.assertLess(path.stat().st_size, 300)
        for bad in ('not json', json.dumps({'head_id': 'q000001'}), json.dumps({'head_id': 'q000001', 'baseline_free_bytes': -1,
                                                                                 'first_seen': 'x'}),
                    json.dumps({'head_id': 'x' * 5000, 'baseline_free_bytes': 1, 'first_seen': 'x'})):
            path.write_text(bad)
            self.assertIsNone(self.baseline(), bad[:30])
            with backfill():
                self.advance(free=FREE0 * GIB)
            self.assertEqual(sorted(self.baseline()), ['baseline_free_bytes', 'first_seen', 'head_id'])
            self.assertLess(path.stat().st_size, 300)

    def test_the_claim_time_recheck_revokes_a_grant_that_would_exceed_the_cap(self):
        head = self.head()
        qid = self.small(0)
        with backfill():
            self.advance(free=FREE0 * GIB)
            entries = da._queue_entries(self.path)[0]
            entry = next(e for e in entries if e['id'] == qid)

            def lost(free_gib):
                return da._backfill_protection_lost(self.host(), entry, entries, [], [], self.now,
                                                    {'free_bytes': free_gib * GIB, 'tier': 'wide', 'latch': False}, 0, 0, 3, self.path)
            self.assertIsNone(lost(FREE0))                          # 250 - (250 - 12) = 12 GiB
            self.assertIsNone(lost(FREE0 - 8))                      # 250 - (242 - 12) = 20 GiB: at the cap
            self.assertEqual(lost(FREE0 - 9), ('loss-cap', head))    # 21 GiB: over it
            self.assertEqual(lost(FREE0 - 30), ('loss-cap', head))

    def test_a_grant_made_without_a_baseline_is_not_revoked_for_the_cap(self):
        self.head()
        qid = self.small(0)
        with backfill():
            self.advance(free=FREE0 * GIB)
            da._drop_loss_baseline(self.path)
            entries = da._queue_entries(self.path)[0]
            entry = next(e for e in entries if e['id'] == qid)
            lost = da._backfill_protection_lost(self.host(), entry, entries, [], [], self.now,
                                                {'free_bytes': (FREE0 - 40) * GIB, 'tier': 'wide', 'latch': False}, 0, 0, 3, self.path)
        self.assertIsNone(lost)

    def test_an_unwritable_baseline_fails_closed(self):
        self.head()
        small = self.small(0)
        with backfill(), patch.object(da, 'write_json_atomic', side_effect=OSError('disk full')):
            out = self.advance(free=FREE0 * GIB)
        self.assertEqual(out.get('backfilled', []), [])
        self.assertEqual(self.state(small), 'QUEUED')

    def test_the_cap_never_loosens_the_floor_or_the_hysteresis(self):
        """A candidate within the cap but without the 4 GiB grant-time margin is still refused for the margin, and the floor holds."""
        self.head(gib=40)
        big = self.small(0, gib=32)
        with backfill():
            out = self.advance(free=135 * GIB)   # the floor-blocked shape of the live queue: 135 - 32 = 103 < 104
        self.assertEqual(out.get('backfilled', []), [])
        self.assertEqual(self.state(big), 'QUEUED')
        self.assertEqual(self.receipts('backfill-decision')[-1]['reason'], 'head-unprotected-hysteresis')

    def test_the_vocabulary_names_the_new_reason(self):
        self.assertIn('cumulative-loss-cap', da.BACKFILL_DECISION_REASONS)
        self.assertEqual(da.BACKFILL_LOSS_CAP, 20 * GIB)


if __name__ == '__main__':
    unittest.main()
