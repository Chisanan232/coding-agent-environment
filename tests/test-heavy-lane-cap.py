#!/usr/bin/env python3
"""Two heavy lanes (SPE-160): lane 1 is admitted by the disk rules alone, later lanes also need the resource budget.

The shipped cap may be one or two; every test here pins the cap it exercises."""
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
import subprocess
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


da = load('disk_admission', 'scripts/disk-admission.py')
shadow = load('shadow_budget_tests', 'tests/test-shadow-budget.py')
queue_tests = load('admission_queue_tests', 'tests/test-admission-queue.py')
GIB = 1024 ** 3
TWO = {'stable': 2, 'wide': 2}
ONE = {'stable': 1, 'wide': 1}


def fields(cwd='/work/wt-a', name='build-crate'):
    """Declared fields as the CLI resolves them; the Cargo target key follows the working directory."""
    out, error = da.resolve_workload(name, None, None, None, env={}, home='/nonexistent-home', cwd=cwd)
    assert error is None
    return out


class Base(queue_tests.QueueBase):
    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.dict(da.HEAVY_LANES, TWO)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spawn(self):
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        return proc

    def host_fn(self, **kw):
        return lambda: shadow.host(time=self.now + timedelta(seconds=1), **kw)

    def take(self, label, cwd='/work/wt-a', host=True, grant=True, free=400 * GIB, nbytes=20 * GIB, proc=None, declared='auto', **kw):
        """Reserve for a real process the way `run` does: with a host sample and the environment the job will get."""
        proc = proc or self.spawn()
        return da.reserve(label, nbytes, proc.pid, self.path, lambda: free, now=self.now,
                          declared=fields(cwd) if declared == 'auto' else declared,
                          host_fn=self.host_fn(**kw) if host is True else host, grant_environ={} if grant else None)

    def lanes(self):
        return sorted(p.stem for p in (self.path.parent / 'reservations').glob('*.json'))


class Reserve(Base):
    def test_a_second_job_that_fits_is_admitted_beside_a_running_one(self):
        self.assertTrue(self.take('one')['admit'])
        two = self.take('two', cwd='/work/wt-b')
        self.assertTrue(two['admit'], two)
        self.assertIn('lane 2 of 2', two['reason'])
        self.assertEqual(self.lanes(), ['one', 'two'])
        self.assertEqual(two['budget']['enforced'], 'admit')

    def test_the_first_job_never_needs_a_host_sample_or_a_budget(self):
        for label, host in (('none', None), ('boom', self.boom)):
            self.assertTrue(self.take(label, host=host, declared=None)['admit'], label)
            da.release(label, self.path)

    @staticmethod
    def boom():
        raise OSError('ps exploded')

    def test_no_host_sample_means_one_lane(self):
        self.assertTrue(self.take('one')['admit'])
        refused = self.take('two', cwd='/work/wt-b', host=None)
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('budget: no host sample'), refused['reason'])

    def test_an_unavailable_budget_means_one_lane(self):
        self.assertTrue(self.take('one')['admit'])
        refused = self.take('two', cwd='/work/wt-b', host=self.boom)
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('budget: unavailable'), refused['reason'])
        self.assertIn('enforced', refused['budget'])
        self.assertEqual(self.lanes(), ['one'])

    def test_amber_and_red_admit_no_concurrent_job(self):
        self.assertTrue(self.take('one')['admit'])
        for band in ('AMBER', 'RED'):
            refused = self.take('two', cwd='/work/wt-b', band=band, band_reasons=['load per core 3.0'])
            self.assertFalse(refused['admit'], band)
            self.assertTrue(refused['reason'].startswith('budget: '), refused['reason'])
            self.assertIn(band, refused['reason'])
            self.assertEqual(refused['budget']['enforced'], 'refuse')

    def test_a_holder_without_an_estimate_or_a_recorded_job_limit_blocks_the_second_lane(self):
        legacy = self.take('legacy', declared=None, grant=False)
        self.assertTrue(legacy['admit'])
        refused = self.take('two', cwd='/work/wt-b')
        self.assertFalse(refused['admit'])
        self.assertIn('memory:', refused['reason'])
        da.release('legacy', self.path)
        self.write_unlimited('unlimited', '/work/wt-a')      # classed, but a record that carries no job limit (before SPE-171)
        refused = self.take('two', cwd='/work/wt-b')
        self.assertFalse(refused['admit'])
        self.assertIn('cpu: 16 slots in use + 4 requested > 16 cores', refused['reason'])

    def write_unlimited(self, label, cwd):
        proc = self.spawn()
        with da.state_lock(self.path):
            da.write_reservation(self.path, label, 20 * GIB, proc.pid, da.process_token(proc.pid), self.now + timedelta(hours=1),
                                 declared=dict(fields(cwd), created=da.iso_utc(self.now)))
        return proc

    def test_run_records_the_granted_jobs_it_exports(self):
        """`run` (run_reserved) execs the job itself, so it records the CARGO_BUILD_JOBS it exports (SPE-160)."""
        self.assertTrue(self.take('one')['admit'])
        env = {k: v for k, v in os.environ.items() if k not in ('CARGO_BUILD_JOBS', 'RUST_TEST_THREADS', 'NEXTEST_TEST_THREADS')}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            code = da.run_reserved('two', 20 * GIB, ['true'], self.path, lambda: 400 * GIB, exec_fn=lambda p, a: None,
                                   declared=fields('/work/wt-b'), host_fn=lambda: shadow.host(time=da.datetime.now(da.timezone.utc)
                                                                                           + timedelta(seconds=1)))
            exported = os.environ.get('CARGO_BUILD_JOBS')
        try:
            self.assertEqual(code, 0)
            self.assertEqual(exported, '4')
            self.assertEqual(self.recorded('two'), 4)
        finally:
            da.release('two', self.path)

    def recorded(self, label):
        return json.loads((self.path.parent / 'reservations' / (label + '.json')).read_text()).get('granted_jobs')

    def test_a_non_exec_reserve_of_a_declared_class_is_charged_its_slots_not_every_core(self):
        first = self.take('cli', grant=None)                  # the `reserve` CLI: nothing is exec'd, no environment
        self.assertTrue(first['admit'])
        self.assertEqual(self.recorded('cli'), 4)
        two = self.take('two', cwd='/work/wt-b')
        self.assertTrue(two['admit'], two)
        holders = {h['label']: h for h in two['budget']['holders']}
        self.assertEqual(holders['cli']['cpu_slots'], 4)

    def test_a_non_exec_refresh_never_changes_the_recorded_limit(self):
        proc = self.spawn()
        self.write_unlimited_for('old', proc, '/work/wt-a')
        refresh = lambda **kw: da.reserve('old', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now,
                                          declared=fields('/work/wt-a'), host_fn=self.host_fn(**kw), grant_environ=None)
        self.assertTrue(refresh()['admit'])
        self.assertIsNone(self.recorded('old'))                       # still unlimited, still charged every core
        refused = self.take('two', cwd='/work/wt-b')
        self.assertIn('cpu: 16 slots in use', refused['reason'])
        da.release('old', self.path)
        self.assertTrue(da.reserve('amber', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared=fields('/work/wt-a'),
                                   host_fn=self.host_fn(band='AMBER'), grant_environ=None)['admit'])
        self.assertEqual(self.recorded('amber'), 2)
        self.assertTrue(da.reserve('amber', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared=fields('/work/wt-a'),
                                   host_fn=self.host_fn(), grant_environ=None)['admit'])
        self.assertEqual(self.recorded('amber'), 2)

    def write_unlimited_for(self, label, proc, cwd):
        with da.state_lock(self.path):
            da.write_reservation(self.path, label, 20 * GIB, proc.pid, da.process_token(proc.pid), self.now + timedelta(hours=1),
                                 declared=dict(fields(cwd), created=da.iso_utc(self.now)))

    def test_a_non_exec_reserve_without_a_budget_records_the_declared_slots(self):
        self.assertTrue(self.take('cli', grant=None, host=None)['admit'])      # lane 1 needs no host sample
        self.assertEqual(self.recorded('cli'), 4)

    def test_a_non_exec_reserve_without_a_class_records_nothing(self):
        self.assertTrue(self.take('legacy', declared=None, grant=None)['admit'])
        self.assertIsNone(self.recorded('legacy'))

    def test_non_exec_holders_sharing_a_target_or_the_docker_daemon_are_still_refused(self):
        self.assertTrue(self.take('cli', grant=None)['admit'])
        refused = self.take('two', grant=None)                # same working directory: same Cargo target key
        self.assertFalse(refused['admit'])
        self.assertIn('excl_key overlap with cli', refused['reason'])
        da.release('cli', self.path)
        docker = lambda: fields('/work/wt-a', name='container-suite')
        self.assertTrue(self.take('d1', cwd='/work/wt-a', declared=docker(), grant=None)['admit'])
        refused = self.take('d2', cwd='/work/wt-b', declared=docker(), grant=None)
        self.assertFalse(refused['admit'])
        self.assertIn('excl_key overlap with d1', refused['reason'])

    def test_two_isolated_cold_builds_with_separate_targets_are_admitted_together(self):
        cold = lambda n: fields('/work/wt-%d' % n, name='build-cold-isolated')
        self.assertTrue(self.take('c1', declared=cold(1), grant=None)['admit'])
        two = self.take('c2', declared=cold(2), grant=None)
        self.assertTrue(two['admit'], two)

    def test_a_shared_cargo_target_refuses(self):
        self.assertTrue(self.take('one')['admit'])
        refused = self.take('two')                                         # same working directory, same target key
        self.assertFalse(refused['admit'])
        self.assertIn('excl_key overlap with one', refused['reason'])

    def test_cpu_slots_beyond_the_cores_refuse(self):
        self.assertTrue(self.take('one')['admit'])
        refused = self.take('two', cwd='/work/wt-b', cores=6)
        self.assertFalse(refused['admit'])
        self.assertIn('cpu: 4 slots in use + 4 requested > 6 cores', refused['reason'])

    def test_the_byte_floor_still_refuses_when_the_budget_fits(self):
        self.assertTrue(self.take('one', free=150 * GIB, nbytes=20 * GIB)['admit'])
        refused = self.take('two', cwd='/work/wt-b', free=150 * GIB, nbytes=35 * GIB)
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('over-capacity'), refused['reason'])
        self.assertTrue(self.take('two', cwd='/work/wt-b', free=150 * GIB, nbytes=30 * GIB)['admit'])   # exactly the floor

    def test_a_third_job_meets_the_lane_cap_not_the_budget(self):
        self.assertTrue(self.take('one')['admit'])
        self.assertTrue(self.take('two', cwd='/work/wt-b')['admit'])
        third = self.take('three', cwd='/work/wt-c')
        self.assertFalse(third['admit'])
        self.assertTrue(third['reason'].startswith('lane-cap: 2 of 2 heavy lanes in use'), third['reason'])
        self.assertTrue(third['budget']['fits_if_cap_were_3'])

    def test_a_waiting_queue_entry_still_blocks_a_direct_reserve(self):
        self.assertTrue(self.take('one')['admit'])
        self.assertTrue(self.enqueue(declared=fields('/work/wt-q'))['ok'])
        refused = self.take('two', cwd='/work/wt-b')
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('queue-ahead'), refused['reason'])

    def test_a_running_second_job_can_refresh_while_the_host_is_amber(self):
        self.assertTrue(self.take('one')['admit'])
        proc = self.spawn()
        self.assertTrue(self.take('two', cwd='/work/wt-b', proc=proc)['admit'])
        refresh = self.take('two', cwd='/work/wt-b', proc=proc, band='AMBER')
        self.assertTrue(refresh['admit'], refresh)
        self.assertEqual(self.lanes(), ['one', 'two'])

    def test_exactly_two_of_eight_concurrent_requests_are_admitted(self):
        procs = [self.spawn() for _ in range(8)]
        results = []
        workers = [threading.Thread(target=lambda n=n: results.append(self.take('job-%d' % n, cwd='/work/wt-%d' % n, proc=procs[n])))
                   for n in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for r in results if r['admit']), 2, [r['reason'] for r in results])
        self.assertEqual(len(self.lanes()), 2)


class Advance(Base):
    def entry_fields(self, n):
        return fields('/work/wt-%d' % n)

    def queue(self, n, nbytes=20 * GIB, ticket=None):
        return self.enqueue(ticket or 'SPE-%d' % (160 + n), declared=self.entry_fields(n), nbytes=nbytes)['id']

    def advance(self, free=400 * GIB, **kw):
        return da.queue_advance(self.path, lambda: free, now=self.now, host_fn=self.host_fn(**kw))

    def test_two_entries_are_granted_in_one_call_and_a_third_waits(self):
        first, second, third = self.queue(1), self.queue(2), self.queue(3)
        out = self.advance()
        self.assertEqual(out['granted'], [first, second])
        self.assertEqual(self.states(), ['GRANTED', 'GRANTED', 'QUEUED'])
        again = self.advance()
        self.assertEqual((again['granted'], again['reason']), ([], 'lane occupied'))
        self.assertEqual(third, sorted(self.entries())[2])

    def test_a_head_that_does_not_fit_stops_the_queue_and_no_later_entry_overtakes_it(self):
        head = self.queue(1, nbytes=60 * GIB)
        later = self.queue(2, nbytes=10 * GIB)
        out = self.advance(free=150 * GIB)
        self.assertEqual(out['granted'], [])
        self.assertTrue(out['reason'].startswith('over-capacity'), out['reason'])
        self.assertEqual(self.states(), ['QUEUED', 'QUEUED'])

    def test_a_head_the_budget_refuses_stops_the_queue_too(self):
        blocker = self.spawn()
        self.assertTrue(self.take('blocker', cwd='/work/wt-1', proc=blocker)['admit'])
        head, later = self.queue(1), self.queue(2)                        # the head shares the blocker's Cargo target
        out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertTrue(out['reason'].startswith('budget: ') and 'excl_key overlap with blocker' in out['reason'], out['reason'])
        self.assertEqual(self.states(), ['QUEUED', 'QUEUED'])
        self.assertEqual(head, sorted(self.entries())[0])
        self.assertEqual(later, sorted(self.entries())[1])

    def test_bytes_of_a_grant_made_in_the_same_call_count_toward_the_floor(self):
        first = self.queue(1, nbytes=35 * GIB)
        self.queue(2, nbytes=30 * GIB)                                    # 160 - 35 - 30 = 95 < 100, though 160 - 30 would pass
        out = self.advance(free=160 * GIB)
        self.assertEqual(out['granted'], [first])
        self.assertTrue(out['reason'] == 'ok')
        self.assertEqual(self.states(), ['GRANTED', 'QUEUED'])
        again = self.advance(free=160 * GIB)
        self.assertEqual(again['granted'], [])
        self.assertTrue(again['reason'] in ('lane occupied',) or again['reason'].startswith('over-capacity'), again['reason'])

    def test_an_exactly_fitting_pair_is_granted(self):
        self.queue(1, nbytes=35 * GIB)
        self.queue(2, nbytes=25 * GIB)
        self.assertEqual(len(self.advance(free=160 * GIB)['granted']), 2)

    def claim_non_exec(self, n, **kw):
        qid = self.queue(n)
        self.assertEqual(da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, **kw)['granted'][-1:], [qid])
        entry = self.entries()[qid]
        out = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], shadow.ARGV, str(self.worktree), lambda: 400 * GIB,
                             pid=os.getpid() if n == 1 else self.spawn().pid, now=self.now, memory_fn=lambda: (True, 'ok'),
                             exec_fn=lambda p, a: None, declared=self.entry_fields(n), **kw)
        self.assertTrue(out['ok'], out)
        return qid

    def test_a_claimed_job_without_a_recorded_limit_occupies_a_lane_for_the_budget(self):
        old = self.claim_non_exec(1)
        with da.state_lock(self.path):                                     # a claim made before SPE-171 carried no limit
            entries, _ = da._queue_entries(self.path)
            entry = next(e for e in entries if e['id'] == old)
            del entry['holder']['granted_jobs']
            da._write_queue_entry(self.path, entry)
            held = self.path.parent / 'reservations' / (entry['label'] + '.json')
            record = json.loads(held.read_text())
            del record['granted_jobs']
            held.write_text(json.dumps(record))
        self.assertNotIn('granted_jobs', self.entries()[old]['holder'])
        self.queue(2)
        out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertIn('cpu: 16 slots in use + 4 requested > 16 cores', out['reason'])

    def test_a_non_exec_claim_records_its_slots_and_leaves_the_second_lane_open(self):
        old = self.claim_non_exec(1)
        self.assertEqual(self.entries()[old]['holder']['granted_jobs'], 4)
        self.assertEqual(da._entry_granted_jobs(self.path, old), 4)     # what the claim CLI tells the supervisor to honour
        second = self.queue(2)                                             # separate CARGO_TARGET_DIR (working directory)
        out = self.advance()
        self.assertEqual(out['granted'], [second], out)

    def test_a_non_exec_claim_still_refuses_a_job_sharing_its_target_dir(self):
        self.claim_non_exec(1)
        self.queue(1, ticket='SPE-299')                                    # same working directory: same Cargo target key
        out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertIn('excl_key overlap', out['reason'])

    def test_no_host_sample_grants_only_one(self):
        first = self.queue(1)
        self.queue(2)
        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)
        self.assertEqual(out['granted'], [first])


class Claim(Base):
    def granted(self, n=1):
        qid = self.enqueue('SPE-%d' % (200 + n), declared=fields('/work/wt-%d' % n))['id']
        self.assertEqual(da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)['granted'], [qid])
        return qid, self.entries()[qid]

    def occupy(self, label, n):
        """A running capped job holding the Cargo target of working directory n, written directly (a direct reserve is blocked while
        a grant waits)."""
        proc = self.spawn()
        with da.state_lock(self.path):
            da.write_reservation(self.path, label, 20 * GIB, proc.pid, da.process_token(proc.pid), self.now + timedelta(hours=1),
                                 declared=dict(fields('/work/wt-%d' % n), created=da.iso_utc(self.now)), granted_jobs=4)

    def claim(self, qid, entry, n=1, **kw):
        return da.queue_claim(self.path, qid, entry['label'], entry['bytes'], shadow.ARGV, str(self.worktree), lambda: 400 * GIB,
                              pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                              declared=fields('/work/wt-%d' % n), grant_environ={}, **kw)

    def test_a_claim_rechecks_the_budget_and_a_refusal_keeps_the_grant(self):
        qid, entry = self.granted(1)
        self.occupy('late', 1)                                             # shares the granted job's Cargo target
        refused = self.claim(qid, entry, host_fn=self.host_fn())
        self.assertFalse(refused['ok'])
        self.assertTrue(refused['reason'].startswith('budget: ') and 'excl_key overlap with late' in refused['reason'], refused['reason'])
        self.assertEqual(self.entries()[qid]['state'], 'GRANTED')
        self.assertTrue((self.path.parent / 'queue/granted' / qid).exists())
        self.assertEqual(refused['budget']['enforced'], 'refuse')
        da.release('late', self.path)
        ok = self.claim(qid, entry, host_fn=self.host_fn())
        self.assertTrue(ok['ok'], ok)

    def test_a_claim_beside_a_running_job_needs_a_sample_and_a_green_host(self):
        qid, entry = self.granted(1)
        self.occupy('other', 9)
        for host_fn, text in ((None, 'budget: no host sample'), (self.host_fn(band='AMBER'), 'budget: band AMBER')):
            refused = self.claim(qid, entry, host_fn=host_fn)
            self.assertFalse(refused['ok'])
            self.assertTrue(refused['reason'].startswith(text), refused['reason'])
            self.assertEqual(self.entries()[qid]['state'], 'GRANTED')
        ok = self.claim(qid, entry, host_fn=self.host_fn())
        self.assertTrue(ok['ok'], ok)
        self.assertEqual(self.entries()[qid]['holder']['granted_jobs'], 4)


class Rollback(Base):
    def test_lowering_the_cap_never_touches_running_jobs_and_refuses_new_ones(self):
        procs = [self.spawn(), self.spawn()]
        self.assertTrue(self.take('one', proc=procs[0])['admit'])
        self.assertTrue(self.take('two', cwd='/work/wt-b', proc=procs[1])['admit'])
        with unittest.mock.patch.dict(da.HEAVY_LANES, ONE), \
                unittest.mock.patch.object(os, 'kill', side_effect=AssertionError('os.kill must never be called')) as kill, \
                unittest.mock.patch.object(os, 'killpg', side_effect=AssertionError('os.killpg must never be called')) as killpg:
            refused = self.take('three', cwd='/work/wt-c')
            self.assertFalse(refused['admit'])
            self.assertTrue(refused['reason'].startswith('lane-cap: 2 of 1 heavy lanes in use'), refused['reason'])
            self.qid = self.enqueue(declared=fields('/work/wt-q'))['id']
            out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, host_fn=self.host_fn())
            self.assertEqual((out['granted'], out['reason']), ([], 'lane occupied'))
            snap = da.snapshot(self.path, lambda: (400 * GIB, 'test'), now=self.now)
            self.assertEqual((snap['heavy_lanes_allowed'], snap['active_heavy_jobs']), (1, 2))
            self.assertEqual(kill.call_count + killpg.call_count, 0)
        self.assertEqual(self.lanes(), ['one', 'two'])
        self.assertTrue(all(p.poll() is None for p in procs), 'both jobs must still be running')
        self.assertEqual(self.entries()[self.qid]['state'], 'QUEUED')

    def test_a_claimed_job_stays_claimed_after_the_cap_drops(self):
        qid = self.enqueue(declared=fields('/work/wt-1'))['id']
        self.assertEqual(da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)['granted'], [qid])
        entry = self.entries()[qid]
        out = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], shadow.ARGV, str(self.worktree), lambda: 400 * GIB,
                             pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                             declared=fields('/work/wt-1'))
        self.assertTrue(out['ok'], out)
        with unittest.mock.patch.dict(da.HEAVY_LANES, ONE):
            da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, host_fn=self.host_fn())
        self.assertEqual(self.entries()[qid]['state'], 'CLAIMED')


if __name__ == '__main__':
    unittest.main()
