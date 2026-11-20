#!/usr/bin/env python3
"""Four heavy lanes, end to end through the Python API (SPE-176, ADR 0017 revision 8).

A real temporary state directory and real throwaway /bin/sleep processes, the stub memory gate, injected free space and host samples.
The fourth-lane flag is patched ON inside each test only; the shipped value (OFF) has its own run here. Nothing here touches the live
admission state, the live queue or the real memory gate."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lanes = load('heavy_lane_cap_tests', 'tests/test-heavy-lane-cap.py')
da, shadow, GIB, fields = lanes.da, lanes.shadow, lanes.GIB, lanes.fields
ENOUGH = 280 * GIB            # exactly 200 GiB left after four 20 GiB reservations
STRAY = [{'root_pid': 424242, 'members': [424242]}]
SHIPPED_FOURTH = da.FOURTH_LANE_ENABLED   # the constant of the module under test, as shipped


class Harness(lanes.Base):
    fourth = True

    def setUp(self):
        super().setUp()
        for name, value in (('THIRD_LANE_ENABLED', True), ('FOURTH_LANE_ENABLED', self.fourth)):
            patcher = unittest.mock.patch.object(da, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ids = []

    def host(self, **kw):
        kw.setdefault('cores', 64)
        return self.host_fn(**kw)

    def queue(self, n):
        qid = self.enqueue('SPE-%d' % (500 + n), declared=fields('/work/wt-%d' % n))['id']      # disjoint Cargo target per entry
        self.ids.append(qid)
        return qid

    def advance(self, free=ENOUGH, **kw):
        return da.queue_advance(self.path, lambda: free, now=self.now, host_fn=self.host(**kw))

    def claim(self, qid, n, free=ENOUGH, **kw):
        entry = self.entries()[qid]
        return da.queue_claim(self.path, qid, entry['label'], entry['bytes'], shadow.ARGV, str(self.worktree), lambda: free,
                              pid=self.spawn().pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                              declared=fields('/work/wt-%d' % n), grant_environ={}, host_fn=self.host(**kw))

    def no_latch_effect(self):
        self.assertEqual(sorted(p.name for p in self.path.parent.rglob('*') if 'latch' in p.name.lower()), [])


class FourLanes(Harness):
    def test_four_stamped_entries_are_granted_claimed_and_a_fifth_is_refused(self):
        for n in range(1, 5):
            self.queue(n)
        self.assertEqual(self.advance()['granted'], self.ids)
        for n, qid in enumerate(self.ids, 1):
            out = self.claim(qid, n)
            self.assertTrue(out['ok'], out)
        self.assertEqual(self.states(), ['CLAIMED'] * 4)
        self.assertEqual(len(self.lanes()), 4)
        fifth = self.take('fifth', cwd='/work/wt-9', cores=64, free=ENOUGH)
        self.assertFalse(fifth['admit'])
        self.assertTrue(fifth['reason'].startswith('lane-cap: 4 of 4 heavy lanes in use'), fifth['reason'])
        self.assertEqual(da.snapshot(self.path, lambda: (ENOUGH, 'test'), now=self.now)['heavy_lanes_allowed'], 4)

    def test_insufficient_capacity_names_lane_four_and_leaves_no_latch_effect(self):
        for n in range(1, 4):
            self.queue(n)
        self.assertEqual(self.advance()['granted'], self.ids)
        self.queue(4)
        cases = (('free space below 200 GiB after the reservations', dict(free=ENOUGH - GIB // 10), 'GiB free after all reservations'),
                 ('an unmanaged process tree', dict(workloads=STRAY), 'unmanaged process tree'),
                 ('an AMBER host', dict(band='AMBER', band_reasons=['load per core 3.0']), 'AMBER'))
        for why, kw, text in cases:
            asked = []
            free = kw.get('free', ENOUGH)
            out = da.queue_advance(self.path, lambda: free, now=self.now,
                                   host_fn=self.host(**{k: v for k, v in kw.items() if k != 'free'}),
                                   memory_fn=lambda: asked.append(1) or (True, 'ok'))
            self.assertEqual(out['granted'], [], why)
            self.assertTrue(out['reason'].startswith('budget: lane 4 needs '), (why, out['reason']))
            self.assertIn(text, out['reason'], why)
            self.assertEqual(asked, [], why)                    # the memory gate (and so its latch) is never consulted
            self.assertEqual(self.states()[3], 'QUEUED', why)
        self.no_latch_effect()
        self.assertEqual(self.advance()['granted'], [self.ids[3]])       # capacity back: the same entry is granted

    def test_killing_every_holder_frees_all_lanes_and_the_next_stamped_entry_is_granted(self):
        for n in range(1, 6):
            self.queue(n)
        self.assertEqual(self.advance()['granted'], self.ids[:4])
        for n, qid in enumerate(self.ids[:4], 1):
            self.assertTrue(self.claim(qid, n)['ok'])
        for _ in range(da.WITNESSES_REQUIRED):                  # supervisor polling witnesses each running holder
            self.assertEqual(self.advance()['granted'], [])
        for proc in self.holders:
            proc.kill()
            proc.wait()
        out = self.advance()
        self.assertEqual(sorted(out['released']), sorted(self.ids[:4]))
        self.assertEqual(out['granted'], [self.ids[4]])
        self.assertEqual(self.lanes(), [])

    def test_an_old_unstamped_caller_is_never_given_lane_three_or_four(self):
        for n in range(1, 5):
            self.queue(n)
        for qid in self.ids[2:]:
            with da.state_lock(self.path):
                entries, _ = da._queue_entries(self.path)
                entry = next(e for e in entries if e['id'] == qid)
                del entry['lane_protocol']                      # what a caller from before this change writes
                da._write_queue_entry(self.path, entry)
        self.assertEqual(self.advance()['granted'], self.ids[:2])
        again = self.advance()
        self.assertEqual(again['granted'], [])
        self.assertEqual(again['reason'], 'budget: lane 3 needs a stamped runner (head %s)' % self.ids[2])
        self.assertEqual(self.states(), ['GRANTED', 'GRANTED', 'QUEUED', 'QUEUED'])


class ShippedDefault(Harness):
    """The shipped constants (third lane on, fourth off): today's three-lane behaviour, unchanged."""
    fourth = False

    def test_the_shipped_default_is_off(self):
        self.assertIs(SHIPPED_FOURTH, False)

    def test_three_lanes_are_granted_and_the_fourth_waits(self):
        for n in range(1, 5):
            self.queue(n)
        self.assertEqual(self.advance()['granted'], self.ids[:3])
        for n, qid in enumerate(self.ids[:3], 1):
            self.assertTrue(self.claim(qid, n)['ok'])
        again = self.advance()
        self.assertEqual((again['granted'], again['reason']), ([], 'lane occupied'))
        self.assertEqual(self.states(), ['CLAIMED'] * 3 + ['QUEUED'])
        self.assertEqual(da.snapshot(self.path, lambda: (ENOUGH, 'test'), now=self.now)['heavy_lanes_allowed'], 3)


if __name__ == '__main__':
    unittest.main()
