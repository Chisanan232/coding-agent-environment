#!/usr/bin/env python3
"""Heavy-lane queue (SPE-111 PR2): durable entries, FIFO grant, non-preemptive lanes, no double booking."""
from importlib.util import module_from_spec, spec_from_file_location
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_admission', ROOT / 'scripts/disk-admission.py')
da = module_from_spec(spec)
spec.loader.exec_module(da)
GIB = 1024 ** 3
SECRET_ARG = 'sk-test-ARGV-SECRET-0123456789'


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.path = root / 'admission/state.json'
        self.now = datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc)
        self.worktree = root / 'wt'
        self.worktree.mkdir()
        self.holders = []
        self.addCleanup(self.stop_holders)

    def stop_holders(self):
        for proc in self.holders:
            proc.kill()
            proc.wait()

    def holder(self, label, hours=1):
        """A real process that holds the heavy lane through a real reservation."""
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        result = da.reserve(label, 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now,
                            ttl=int(hours * 3600) if hours > 0 else 3600)
        self.assertTrue(result['admit'], result)
        return proc

    def enqueue(self, ticket='HORO-2070', label=None, argv=None, **extra):
        args = dict(label=label or 'nda-' + ticket.lower(), tickets=[ticket], repo='horonom/nda', worktree=str(self.worktree),
                    harness='codex', session_id='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab', nbytes=20 * GIB,
                    argv=['cargo', 'nextest', 'run', '--token', SECRET_ARG] if argv is None else argv, now=self.now)
        args.update(extra)
        return da.queue_enqueue(self.path, **args)

    def advance(self, free=400 * GIB, now=None):
        return da.queue_advance(self.path, lambda: free, now=now or self.now)

    def entries(self):
        return {e['id']: e for e in da.queue_status(self.path, now=self.now)['entries']}

    def states(self):
        return [e['state'] for e in sorted(self.entries().values(), key=lambda e: e['seq'])]


class Enqueue(QueueBase):
    def test_entry_is_durable_private_and_identified(self):
        out = self.enqueue()
        self.assertTrue(out['ok'], out)
        entry = self.entries()[out['id']]
        self.assertEqual((entry['state'], entry['seq'], entry['harness'], entry['tickets']), ('QUEUED', 1, 'codex', ['HORO-2070']))
        self.assertEqual(entry['owner_uid'], os.getuid())
        self.assertEqual(entry['worktree'], str(self.worktree))
        self.assertEqual(len(entry['fingerprint']), 64)
        files = list((self.path.parent / 'queue').glob('q*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    def test_argv_is_never_stored_anywhere(self):
        self.enqueue()
        self.advance()
        for path in (self.path.parent).rglob('*'):
            if path.is_file():
                self.assertNotIn(SECRET_ARG, path.read_text(errors='replace'), str(path))

    def test_live_fingerprint_is_idempotent_and_released_needs_again(self):
        first = self.enqueue()
        again = self.enqueue()
        self.assertEqual((again['ok'], again['id']), (True, first['id']))
        self.assertEqual(len(self.entries()), 1)
        da.queue_cancel(self.path, first['id'], now=self.now)
        refused = self.enqueue()
        self.assertFalse(refused['ok'])
        self.assertIn('--again', refused['reason'])
        self.assertTrue(self.enqueue(again=True)['ok'])

    def test_bad_requests_are_refused(self):
        for bad in (dict(label='bad label!'), dict(tickets=['x y']), dict(tickets=[]), dict(harness='tmux'),
                    dict(session_id='x'), dict(nbytes=0), dict(worktree='/does/not/exist'), dict(argv=[]),
                    dict(worktree='relative/path')):
            self.assertFalse(self.enqueue(**bad)['ok'], bad)
        self.assertEqual(self.entries(), {})


class Grant(QueueBase):
    def test_fifo_and_one_grant_per_lane(self):
        a, b = self.enqueue('HORO-2070'), self.enqueue('HORO-2071')
        out = self.advance()
        self.assertEqual(out['granted'], [a['id']])
        self.assertEqual(self.states(), ['GRANTED', 'QUEUED'])
        self.assertEqual(self.advance()['granted'], [])  # an unclaimed grant occupies the lane
        self.assertEqual(self.states(), ['GRANTED', 'QUEUED'])
        self.assertTrue((self.path.parent / 'queue/granted' / a['id']).exists())
        self.assertFalse((self.path.parent / 'queue/granted' / b['id']).exists())

    def test_live_holder_blocks_then_release_grants_without_preempting(self):
        libra = self.holder('libra-c1-fullgate')
        a = self.enqueue()
        self.assertEqual(self.advance()['granted'], [])
        self.assertEqual(self.states(), ['QUEUED'])
        self.assertIsNone(libra.poll(), 'queue code must never touch the holder')
        libra.kill()
        libra.wait()
        self.assertEqual(self.advance()['granted'], [a['id']])

    def test_past_ttl_holder_still_blocks_the_grant(self):
        self.holder('libra-c1-fullgate')
        self.enqueue()
        later = self.now + timedelta(hours=9)
        self.assertEqual(self.advance(now=later)['granted'], [])

    def test_tier_without_heavy_lane_marks_first_in_line_eligible_only(self):
        self.enqueue('HORO-2070')
        self.enqueue('HORO-2071')
        out = self.advance(free=50 * GIB)
        self.assertEqual((out['granted'], self.states()), ([], ['ELIGIBLE', 'QUEUED']))

    def test_emergency_tier_grants_nothing(self):
        self.enqueue()
        out = self.advance(free=5 * GIB)
        self.assertEqual((out['granted'], self.states()), ([], ['ELIGIBLE']))

    def test_lapsed_grant_expires_and_passes_to_next(self):
        a, b = self.enqueue('HORO-2070'), self.enqueue('HORO-2071')
        self.advance()
        later = self.now + timedelta(hours=2)
        out = self.advance(now=later)
        self.assertEqual(out['expired'], [a['id']])
        self.assertEqual(out['granted'], [b['id']])
        self.assertEqual(self.states(), ['EXPIRED', 'GRANTED'])
        self.assertFalse((self.path.parent / 'queue/granted' / a['id']).exists())

    def test_wait_limit_expires_a_queued_entry(self):
        self.holder('libra-c1-fullgate')
        a = self.enqueue(wait_s=3600)
        out = self.advance(now=self.now + timedelta(hours=2))
        self.assertEqual(out['expired'], [a['id']])

    def test_cancel_frees_a_unclaimed_grant_for_the_next_entry(self):
        a, b = self.enqueue('HORO-2070'), self.enqueue('HORO-2071')
        self.advance()
        self.assertTrue(da.queue_cancel(self.path, a['id'], now=self.now)['ok'])
        self.assertEqual(self.advance()['granted'], [b['id']])
        self.assertFalse((self.path.parent / 'queue/granted' / a['id']).exists())
        self.assertFalse(da.queue_cancel(self.path, 'q999999', now=self.now)['ok'])


class NoDoubleBooking(QueueBase):
    def test_plain_reserve_is_refused_behind_the_queue(self):
        self.enqueue()
        proc = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(proc)
        refused = da.reserve('someone-else', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now)
        self.assertFalse(refused['admit'])
        self.assertIn('queue-ahead', refused['reason'])

    def test_same_label_refresh_of_a_live_holder_still_passes(self):
        proc = self.holder('libra-c1-fullgate')
        self.enqueue()
        refreshed = da.reserve('libra-c1-fullgate', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now)
        self.assertTrue(refreshed['admit'], refreshed)

    def test_reserve_is_unchanged_when_the_queue_is_empty_or_terminal(self):
        a = self.enqueue()
        da.queue_cancel(self.path, a['id'], now=self.now)
        proc = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(proc)
        self.assertTrue(da.reserve('plain', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now)['admit'])

    def test_corrupt_entry_blocks_the_queue_and_is_never_deleted(self):
        self.enqueue()
        bad = self.path.parent / 'queue/q000002.json'
        bad.write_text('{"not": "an entry"')
        out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertIn('queue-entry-unreadable', out['reason'])
        self.assertTrue(bad.exists())
        proc = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(proc)
        refused = da.reserve('plain', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now)
        self.assertFalse(refused['admit'])

    def test_concurrent_advance_grants_exactly_one(self):
        for n in range(3):
            self.enqueue('HORO-207%d' % n)
        results = []

        def work():
            results.append(da.queue_advance(self.path, lambda: 400 * GIB, now=self.now))

        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(len(r['granted']) for r in results), 1, results)
        self.assertEqual(self.states().count('GRANTED'), 1)


class Receipts(QueueBase):
    def test_every_transition_leaves_a_private_receipt_line(self):
        a = self.enqueue()
        self.advance()
        da.queue_cancel(self.path, a['id'], now=self.now)
        receipts = self.path.parent / 'queue/receipts.jsonl'
        lines = [json.loads(line) for line in receipts.read_text().splitlines()]
        self.assertEqual([line['event'] for line in lines], ['enqueued', 'granted', 'cancelled'])
        self.assertTrue(all(line['id'] == a['id'] for line in lines))
        self.assertEqual(stat.S_IMODE(receipts.stat().st_mode), 0o600)


class Cli(QueueBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = da.run(list(argv), state_path=self.path, free_fn=lambda: 400 * GIB)
        return rc, out.getvalue()

    def test_queue_commands_round_trip_and_status_prints_handoff_without_running_it(self):
        rc, out = self.run_cli('queue', 'enqueue', '--label', 'nda-horo-2070', '--ticket', 'HORO-2070', '--repo', 'horonom/nda',
                               '--worktree', str(self.worktree), '--harness', 'codex',
                               '--session-id', '0b1d2e3f-aaaa-bbbb-cccc-1234567890ab', '--bytes', str(20 * GIB), '--', 'cargo', 'test', SECRET_ARG)
        self.assertEqual(rc, 0, out)
        qid = json.loads(out)['id']
        self.assertEqual(self.run_cli('queue', 'advance')[0], 0)
        rc, out = self.run_cli('queue', 'status', '--json')
        status = json.loads(out)
        entry = [e for e in status['entries'] if e['id'] == qid][0]
        self.assertEqual(entry['state'], 'GRANTED')
        self.assertIn('codex exec resume', entry['handoff']['resume'])
        self.assertIn(qid, entry['handoff']['claim'])
        self.assertNotIn(SECRET_ARG, out)
        self.assertEqual(self.run_cli('queue', 'cancel', '--id', qid)[0], 0)
        self.assertNotEqual(self.run_cli('queue', 'cancel', '--id', 'q999999')[0], 0)


if __name__ == '__main__':
    unittest.main()
