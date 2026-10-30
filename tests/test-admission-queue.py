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


class Claim(QueueBase):
    ARGV = ['cargo', 'nextest', 'run', '--token', SECRET_ARG]

    def grant(self, ticket='HORO-2070'):
        qid = self.enqueue(ticket)['id']
        self.advance()
        return qid

    def claim(self, qid, argv=None, label=None, nbytes=20 * GIB, cwd=None, free=400 * GIB, pid=None, now=None, memory_ok=True,
              token_fn=None):
        calls = {'exec': [], 'memory': 0}

        def exec_fn(program, args):
            calls['exec'].append((program, list(args)))

        def memory_fn():
            calls['memory'] += 1
            return (True, 'ok') if memory_ok else (False, 'memory pressure')

        kwargs = {} if token_fn is None else {'token_fn': token_fn}
        out = da.queue_claim(self.path, qid, label or 'nda-horo-2070', nbytes, self.ARGV if argv is None else argv,
                             str(cwd or self.worktree), lambda: free, pid=pid or os.getpid(), now=now or self.now,
                             memory_fn=memory_fn, exec_fn=exec_fn, **kwargs)
        out['calls'] = calls
        return out

    def test_valid_claim_writes_the_holder_reservation_and_execs(self):
        qid = self.grant()
        out = self.claim(qid)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['calls']['exec'], [('cargo', self.ARGV)])
        self.assertEqual(out['calls']['memory'], 1)
        entry = self.entries()[qid]
        self.assertEqual(entry['state'], 'CLAIMED')
        self.assertEqual(entry['holder']['pid'], os.getpid())
        record = json.loads((self.path.parent / 'reservations/nda-horo-2070.json').read_text())
        self.assertEqual(record['queue_id'], qid)
        events = [json.loads(line)['event'] for line in (self.path.parent / 'queue/receipts.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['enqueued', 'granted', 'claimed'])
        self.assertFalse((self.path.parent / 'queue/granted' / qid).exists())

    def test_wrong_job_worktree_label_or_bytes_is_refused_and_keeps_the_grant(self):
        qid = self.grant()
        other = Path(self.temp.name) / 'elsewhere'
        other.mkdir()
        cases = [dict(argv=['cargo', 'test']), dict(cwd=other), dict(label='someone-else'), dict(nbytes=21 * GIB)]
        for bad in cases:
            out = self.claim(qid, **bad)
            self.assertFalse(out['ok'], bad)
            self.assertEqual(out['calls']['exec'], [])
        self.assertEqual(self.entries()[qid]['state'], 'GRANTED')
        self.assertTrue(self.claim(qid)['ok'])

    def test_not_granted_or_lapsed_or_unknown_is_refused(self):
        qid = self.grant('HORO-2070')
        waiting = self.enqueue('HORO-2071')['id']
        self.assertIn('not granted', self.claim(waiting, label='nda-horo-2071')['reason'])
        late = self.claim(qid, now=self.now + timedelta(hours=3))
        self.assertFalse(late['ok'])
        self.assertIn('lapsed', late['reason'])
        self.assertFalse(self.claim('q999999')['ok'])

    def test_failed_entry_write_leaves_no_reservation_and_no_exec(self):
        qid = self.grant()
        real = da._write_queue_entry

        def broken(state_path, entry):
            raise OSError('disk full')

        da._write_queue_entry = broken
        try:
            out = self.claim(qid)
        finally:
            da._write_queue_entry = real
        self.assertFalse(out['ok'])
        self.assertEqual(out['calls']['exec'], [])
        self.assertFalse((self.path.parent / 'reservations/nda-horo-2070.json').exists())
        self.assertEqual(self.entries()[qid]['state'], 'GRANTED')

    def test_second_claim_of_the_same_grant_is_refused(self):
        qid = self.grant()
        self.assertTrue(self.claim(qid)['ok'])
        again = self.claim(qid)
        self.assertFalse(again['ok'])
        self.assertEqual(again['calls']['exec'], [])

    def test_memory_refusal_keeps_the_grant_and_a_retry_succeeds(self):
        qid = self.grant()
        refused = self.claim(qid, memory_ok=False)
        self.assertFalse(refused['ok'])
        self.assertEqual(refused['exit'], 77)
        self.assertEqual(self.entries()[qid]['state'], 'GRANTED')
        self.assertTrue(self.claim(qid)['ok'])

    def test_a_holder_that_appeared_after_the_grant_blocks_the_claim(self):
        qid = self.grant()
        intruder = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(intruder)
        with da.state_lock(self.path):  # bypasses reserve(): a wrapper that does not honour the queue guard
            da.write_reservation(self.path, 'late-intruder', 10 * GIB, intruder.pid, da.process_token(intruder.pid),
                                 self.now + timedelta(hours=1))
        out = self.claim(qid)
        self.assertFalse(out['ok'])
        self.assertIn('lane-cap', out['reason'])
        self.assertEqual(self.entries()[qid]['state'], 'GRANTED')

    def test_disk_floor_is_revalidated_at_claim_time(self):
        qid = self.grant()
        out = self.claim(qid, free=110 * GIB)
        self.assertFalse(out['ok'])
        self.assertIn('over-capacity', out['reason'])
        out = self.claim(qid, free=50 * GIB)
        self.assertFalse(out['ok'])

    def test_exec_failure_releases_the_reservation_and_the_entry(self):
        qid = self.grant()
        out = da.queue_claim(self.path, qid, 'nda-horo-2070', 20 * GIB, self.ARGV, str(self.worktree), lambda: 400 * GIB,
                             pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'),
                             exec_fn=lambda program, args: (_ for _ in ()).throw(OSError('boom')))
        self.assertEqual(out['exit'], 127)
        self.assertFalse((self.path.parent / 'reservations/nda-horo-2070.json').exists())
        entry = self.entries()[qid]
        self.assertEqual((entry['state'], entry['release']['evidence']), ('RELEASED', 'exec-failed'))


class ReleaseEvidence(QueueBase):
    def claimed(self, ticket='HORO-2070'):
        """Entry claimed by a real subprocess standing in for the exec'd job."""
        qid = self.enqueue(ticket)['id']
        self.advance()
        job = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(job)
        out = da.queue_claim(self.path, qid, 'nda-' + ticket.lower(), 20 * GIB, Claim.ARGV, str(self.worktree), lambda: 400 * GIB,
                             pid=job.pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda program, args: None)
        self.assertTrue(out['ok'], out)
        return qid, job

    def test_exit_of_the_verified_holder_releases_and_grants_the_next_entry(self):
        a, job = self.claimed('HORO-2070')
        b = self.enqueue('HORO-2071')['id']
        self.assertEqual(self.advance()['granted'], [])
        self.assertEqual(self.entries()[a]['state'], 'CLAIMED')
        job.kill()
        job.wait()
        out = self.advance()
        self.assertEqual(out['granted'], [b])
        release = self.entries()[a]['release']
        self.assertEqual((self.entries()[a]['state'], release['evidence'], release['holder_alive']), ('RELEASED', 'holder-exited', False))
        self.assertTrue((self.path.parent / 'queue/granted' / b).exists())

    def test_past_ttl_but_alive_holder_is_not_released(self):
        a, job = self.claimed()
        self.enqueue('HORO-2071')
        out = self.advance(now=self.now + timedelta(hours=12))
        self.assertEqual(out['granted'], [])
        self.assertEqual(self.entries()[a]['state'], 'CLAIMED')

    def test_pid_reuse_is_recorded_as_such(self):
        a, job = self.claimed()
        reservation = self.path.parent / 'reservations/nda-horo-2070.json'
        entry_path = self.path.parent / 'queue' / (a + '.json')
        entry = json.loads(entry_path.read_text())
        entry['holder']['start_token'] = 'Mon Jan  1 00:00:00 2001'
        entry_path.write_text(json.dumps(entry))
        record = json.loads(reservation.read_text())
        record['start_token'] = 'Mon Jan  1 00:00:00 2001'
        reservation.write_text(json.dumps(record))
        self.advance()
        self.assertEqual(self.entries()[a]['release']['evidence'], 'pid-reused')

    def test_unverifiable_holder_stays_claimed(self):
        a, job = self.claimed()
        self.enqueue('HORO-2071')

        def broken(pid):
            raise da.ReservationError('ps unavailable')

        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, token_fn=broken)
        self.assertEqual(out['granted'], [])
        self.assertEqual(self.entries()[a]['state'], 'CLAIMED')

    def test_explicit_release_of_a_live_holder_does_not_free_the_lane(self):
        a, job = self.claimed()
        b = self.enqueue('HORO-2071')['id']
        da.release('nda-horo-2070', self.path)  # any same-uid caller can do this while the job runs
        out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertEqual(self.entries()[a]['state'], 'CLAIMED')
        proc = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(proc)
        da.queue_cancel(self.path, b, now=self.now)
        refused = da.reserve('plain', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now)
        self.assertFalse(refused['admit'], 'a removed reservation file must not free a running job lane')
        c = self.enqueue('HORO-2072')['id']
        job.kill()
        job.wait()
        out = self.advance()
        self.assertEqual(out['granted'], [c])
        self.assertEqual(self.entries()[a]['release']['evidence'], 'holder-exited')

    def test_per_entry_unverifiable_holder_keeps_the_lane(self):
        a, job = self.claimed()
        self.enqueue('HORO-2071')
        calls = {'n': 0}
        real = da.process_token

        def flaky(pid):
            calls['n'] += 1
            if calls['n'] == 1:
                return real(pid)  # live_reservations sees a healthy holder
            raise da.ReservationError('ps unavailable')

        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, token_fn=flaky)
        self.assertGreaterEqual(calls['n'], 2)
        self.assertEqual((out['granted'], self.entries()[a]['state']), ([], 'CLAIMED'))

    def test_end_to_end_release_grant_claim_and_no_duplicate_run(self):
        libra = self.holder('libra-c1-fullgate')
        b = self.enqueue('HORO-2070')['id']
        self.assertEqual(self.advance()['granted'], [])
        self.assertIsNone(libra.poll())
        libra.kill()
        libra.wait()
        self.assertEqual(self.advance()['granted'], [b])
        self.assertTrue((self.path.parent / 'queue/granted' / b).exists())
        job = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(job)
        runs = []
        first = da.queue_claim(self.path, b, 'nda-horo-2070', 20 * GIB, Claim.ARGV, str(self.worktree), lambda: 400 * GIB, pid=job.pid,
                               now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: runs.append(p))
        second = da.queue_claim(self.path, b, 'nda-horo-2070', 20 * GIB, Claim.ARGV, str(self.worktree), lambda: 400 * GIB, pid=job.pid,
                                now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: runs.append(p))
        self.assertEqual((first['ok'], second['ok'], runs), (True, False, ['cargo']))
        job.kill()
        job.wait()
        self.advance()
        self.assertEqual(self.entries()[b]['state'], 'RELEASED')


class ReviewRegressions(QueueBase):
    def test_rewound_sequence_counter_never_reuses_an_id(self):
        a = self.enqueue('HORO-2070')['id']
        (self.path.parent / 'queue/seq.json').write_text('{"next": 1}')
        b = self.enqueue('HORO-2071')['id']
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.entries()), 2)

    def test_lapsed_entries_do_not_block_plain_reserve(self):
        self.enqueue(wait_s=3600)
        proc = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(proc)
        later = self.now + timedelta(days=3)
        out = da.reserve('plain', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=later)
        self.assertTrue(out['admit'], out)

    def test_lapsed_grant_does_not_block_plain_reserve(self):
        self.enqueue()
        self.advance()
        proc = subprocess.Popen(['/bin/sleep', '300'])
        self.holders.append(proc)
        out = da.reserve('plain', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now + timedelta(hours=2))
        self.assertTrue(out['admit'], out)

    def test_grant_requires_the_bytes_to_fit_above_the_floor(self):
        big = self.enqueue('HORO-2070', nbytes=500 * GIB)['id']
        out = self.advance(free=210 * GIB)
        self.assertEqual(out['granted'], [])
        self.assertIn('over-capacity', out['reason'])
        self.assertEqual(self.entries()[big]['state'], 'QUEUED')

    def test_symlinked_seq_file_is_refused(self):
        self.enqueue('HORO-2070')
        seq = self.path.parent / 'queue/seq.json'
        seq.unlink()
        target = Path(self.temp.name) / 'victim.json'
        target.write_text('{"next": 7}')
        os.symlink(target, seq)
        out = self.enqueue('HORO-2071')
        self.assertFalse(out['ok'])

    def test_status_on_a_symlinked_queue_dir_is_a_clean_refusal(self):
        self.enqueue()
        queue = self.path.parent / 'queue'
        moved = self.path.parent / 'queue-real'
        queue.rename(moved)
        os.symlink(moved, queue)
        out = da.queue_status(self.path, now=self.now)
        self.assertEqual(out['entries'], [])
        self.assertTrue(out['problems'])

    def test_status_hides_the_handoff_of_a_lapsed_grant(self):
        self.enqueue()
        self.advance()
        later = self.now + timedelta(hours=2)
        entry = list(da.queue_status(self.path, now=later)['entries'])[0]
        self.assertNotIn('handoff', entry)

    def test_same_job_from_another_session_is_not_handed_the_first_sessions_entry(self):
        first = self.enqueue()
        other = self.enqueue(session_id='ffffffff-aaaa-bbbb-cccc-000000000000')
        self.assertFalse(other['ok'])
        self.assertIn(first['id'], other['reason'])

    def test_cancel_refuses_another_sessions_entry_when_a_session_is_given(self):
        a = self.enqueue()['id']
        self.assertFalse(da.queue_cancel(self.path, a, now=self.now, session_id='ffffffff-aaaa-bbbb-cccc-000000000000')['ok'])
        self.assertTrue(da.queue_cancel(self.path, a, now=self.now, session_id='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab')['ok'])

    def test_fingerprint_is_salted_per_store(self):
        a = self.enqueue()['id']
        fp1 = self.entries()[a]['fingerprint']
        other = Path(self.temp.name) / 'other/state.json'
        da.queue_enqueue(other, label='nda-horo-2070', tickets=['HORO-2070'], repo='horonom/nda', worktree=str(self.worktree),
                         harness='codex', session_id='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab', nbytes=20 * GIB,
                         argv=['cargo', 'nextest', 'run', '--token', SECRET_ARG], now=self.now)
        fp2 = [e for e in da.queue_status(other)['entries']][0]['fingerprint']
        self.assertNotEqual(fp1, fp2)

    def test_empty_salt_file_is_a_clean_refusal_with_recovery_advice(self):
        self.enqueue('HORO-2070')
        (self.path.parent / 'queue/fingerprint.salt').write_text('')
        out = self.enqueue('HORO-2071')
        self.assertFalse(out['ok'])
        self.assertIn('queue-salt-unreadable', out['reason'])

    def test_a_failing_receipt_does_not_report_a_persisted_change_as_failed(self):
        receipts = self.path.parent / 'queue/receipts.jsonl'
        self.enqueue('HORO-2070')
        receipts.unlink()
        os.symlink(Path(self.temp.name) / 'nowhere', receipts)
        out = self.enqueue('HORO-2071')
        self.assertTrue(out['ok'], out)
        self.assertIn(out['id'], self.entries())
        self.assertFalse(out['receipt_ok'])


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
