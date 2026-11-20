#!/usr/bin/env python3
"""Bounded backfill (SPE-176, ADR 0017 revision 9): a head blocked solely by an exclusion key a claimed running job holds may be overtaken
by at most BACKFILL_MAX compatible later entries, never on a shared key and never at the cost of the head's own fit once its blocker
releases. Ships OFF: advance then only writes rate-limited backfill-shadow receipts.

Every test runs against a private temporary scheduler state (an explicit state path under a TemporaryDirectory) with throwaway
/bin/sleep processes this file spawns and kills; the owner's admission state and queue are never read or written."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


da = load('disk_admission_backfill', 'scripts/disk-admission.py')
mon = load('admission_monitor_backfill', 'scripts/admission-monitor.py')
rec = load('admission_recovery_backfill', 'scripts/admission-recovery.py')
ma = load('memory_admission_backfill', 'scripts/memory-admission.py')
SHIPPED_BACKFILL_ENABLED, SHIPPED_BACKFILL_MAX = da.BACKFILL_ENABLED, da.BACKFILL_MAX
da.THIRD_LANE_ENABLED = True      # the shipped lane rules: three lanes at the wide tier
da.FOURTH_LANE_ENABLED = False
GIB = 1024 ** 3
SHARED = 'cargo-target:/Users/dev/.cargo/shared-target'
LOCAL = 'cargo-target:/Users/dev/.cargo/targets/local-agent'
SESSION = '0b1d2e3f-aaaa-bbbb-cccc-1234567890ab'


def decl(cls, keys, cpus=None, mem_gib=None):
    spec = da.WORKLOAD_CLASSES[cls]
    jobs = cpus or spec['jobs']
    mem = int(mem_gib * GIB) if mem_gib else (spec['mem_base_mib'] + spec['mem_per_job_mib'] * spec['jobs']) * da.MIB
    return {'class': cls, 'mem_bytes': mem, 'cpu_slots': jobs, 'test_threads': jobs, 'excl_keys': list(keys)}


@contextmanager
def backfill(enabled=True, cap=1):
    with unittest.mock.patch.object(da, 'BACKFILL_ENABLED', enabled), unittest.mock.patch.object(da, 'BACKFILL_MAX', cap):
        yield


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.path = root / 'admission/state.json'
        self.worktree = root / 'wt'
        self.worktree.mkdir()
        self.now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
        self.procs = []
        self.addCleanup(self.reap)
        self.load1, self.band, self.workloads, self.band_fn = 0.0, 'GREEN', [], None
        self.rows, self.rows_prev = {}, {}

    def reap(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()

    def spawn(self):
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def host(self):
        sample = {'ram': 128 * GIB, 'used': 20 * GIB, 'cores': 16, 'load1': self.load1, 'time': self.now + timedelta(seconds=1),
                  'window_s': 2.0, 'rows': dict(self.rows), 'rows_prev': dict(self.rows_prev), 'workloads': list(self.workloads),
                  'band': self.band}
        if self.band_fn:
            sample['band_fn'] = self.band_fn
        return sample

    def enqueue(self, label, cls, keys, gib=None, waiter=True, cpus=None, mem_gib=None):
        proc = self.spawn() if waiter else None
        out = da.queue_enqueue(self.path, label=label, tickets=['HORO-1'], repo='horonom/x', worktree=str(self.worktree), harness='claude',
                               session_id=SESSION, nbytes=(gib or da.WORKLOAD_CLASSES[cls]['disk_gib']) * GIB, argv=['cargo', 'test', label],
                               now=self.now, declared=decl(cls, keys, cpus, mem_gib), waiter_pid=proc.pid if proc else None,
                               ancestor_fn=(lambda pid: True) if proc else None)
        self.assertTrue(out['ok'], out)
        return out['id']

    def claim(self, qid, ttl_h=4):
        """Make `qid` CLAIMED by a real throwaway process, with its reservation, as queue_claim records it."""
        holder = self.spawn()
        token = da.process_token(holder.pid)
        with da.state_lock(self.path):
            entry = next(e for e in da._queue_entries(self.path)[0] if e['id'] == qid)
            da.write_reservation(self.path, entry['label'], entry['bytes'], holder.pid, token, self.now + timedelta(hours=ttl_h), queue_id=qid,
                                 declared=dict(da._declared_fields(entry), created=da.iso_utc(self.now - timedelta(minutes=5))),
                                 granted_jobs=entry['cpu_slots'])
            entry['state'] = 'CLAIMED'
            entry['grant'] = {'at': da.iso_utc(self.now - timedelta(minutes=6)), 'expires': da.iso_utc(self.now + timedelta(minutes=54))}
            entry['holder'] = {'pid': holder.pid, 'start_token': token, 'claimed_at': da.iso_utc(self.now - timedelta(minutes=5)),
                               'granted_jobs': entry['cpu_slots']}
            da._write_queue_entry(self.path, entry)
        return holder

    def running(self, label, cls, keys, gib=None, cpus=None, mem_gib=None):
        qid = self.enqueue(label, cls, keys, gib, cpus=cpus, mem_gib=mem_gib)
        return qid, self.claim(qid)

    def advance(self, free=400 * GIB, now=None, memory_fn=None, waiter_token_fn=None, host=True):
        return da.queue_advance(self.path, lambda: free, now=now or self.now, host_fn=self.host if host else None, memory_fn=memory_fn,
                                waiter_token_fn=waiter_token_fn)

    def entries(self):
        return {e['id']: e for e in da._queue_entries(self.path)[0]}

    def state(self, qid):
        return self.entries()[qid]['state']

    def receipts(self, *events):
        path = da.queue_dir(self.path) / 'receipts.jsonl'
        lines = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [line for line in lines if not events or line['event'] in events]

    def historical(self):
        """The 2026-11-20 shape: 828 runs on the shared target, the head 830 needs it too, then docker jobs and a local-agent build."""
        self.blocker, self.blocker_proc = self.running('p6-verify-2', 'build-workspace', [SHARED], 40)
        self.head = self.enqueue('ent-verify-3', 'build-workspace', [SHARED], 40)
        self.docker_a = self.enqueue('p2556d', 'container-suite', ['docker'], 32)
        self.docker_b = self.enqueue('p2556e', 'container-suite', ['docker'], 32)
        self.local = self.enqueue('la-cargo-28532', 'build-workspace', [LOCAL], 40)


class HistoricalShape(Base):
    def test_only_the_first_compatible_docker_job_is_backfilled(self):
        self.historical()
        with backfill():
            out = self.advance()
        self.assertEqual(out['granted'], [self.docker_a])
        self.assertEqual(out['backfilled'], [self.docker_a])
        self.assertEqual(out['head_reason'], 'budget: excl_key overlap with p6-verify-2')
        self.assertEqual(out['reason'], out['head_reason'])
        self.assertEqual([self.state(q) for q in (self.head, self.docker_a, self.docker_b, self.local)], ['QUEUED', 'GRANTED', 'QUEUED', 'QUEUED'])
        self.assertEqual(set(self.entries()[self.docker_a]['grant']), {'at', 'expires'})   # the grant object is unchanged
        events = [(r['event'], r['id']) for r in self.receipts('backfill', 'granted')]
        self.assertEqual(events, [('backfill', self.docker_a), ('granted', self.docker_a)])
        record = self.receipts('backfill')[0]
        self.assertEqual((record['blocked_id'], record['blocker_id'], record['blocker_label'], record['excl_key'], record['reason']),
                         (self.head, self.blocker, 'p6-verify-2', SHARED, 'head-excl-only'))
        self.assertEqual((record['in_flight'], record['cap']), (0, 1))
        math = record['math']
        self.assertEqual((math['free'], math['reserved'], math['cand_bytes'], math['head_bytes'], math['floor']),
                         (400 * GIB, 40 * GIB, 32 * GIB, 40 * GIB, 100 * GIB))
        self.assertEqual(math['disk_left'], 288 * GIB)
        self.assertEqual((math['in_use'], math['lanes'], math['head_cpu'], math['cpu_limit']), (1, 3, 12, 16))
        self.assertLessEqual(math['head_projected'], math['head_limit'])
        granted = self.receipts('granted')[0]
        self.assertEqual(granted['runner']['protocol'], da.RUNNER_PROTOCOL)
        self.assertFalse([r for r in self.receipts('shadow-budget') if r['id'] != self.head], 'scanned candidates write no shadow-budget receipt')

    def test_with_a_cap_of_two_the_same_key_and_the_cpu_model_refuse_the_rest(self):
        """docker-b shares docker-a's key; la-cargo needs 8 slots beside 8 + 4 running/granted on 16 cores."""
        self.historical()
        with backfill(cap=2):
            out = self.advance()
        self.assertEqual(out['backfilled'], [self.docker_a])
        self.assertEqual(self.state(self.docker_b), 'QUEUED')
        self.assertEqual(self.state(self.local), 'QUEUED')

    def test_a_realistic_host_sample_still_backfills(self):
        """The blocker's tree in the host sample (unmanaged once removed for the head check) and a load-driven band."""
        self.historical()
        pid = self.blocker_proc.pid
        self.workloads = [{'root_pid': pid, 'members': [], 'rss_bytes': 6 * GIB}]
        self.load1 = 9.0
        self.band_fn = lambda lpc: ('GREEN', []) if lpc is None or lpc <= ma.EXIT['load_per_core'] else ('AMBER', [])
        with backfill():
            self.assertEqual(self.advance()['backfilled'], [self.docker_a])

    def test_the_blocker_releasing_gives_the_head_its_lane_first(self):
        self.historical()
        with backfill():
            self.advance()
            self.blocker_proc.kill()
            self.blocker_proc.wait()
            out = self.advance()
        self.assertEqual(out['granted'][:1], [self.head])
        self.assertNotIn('backfilled', out)
        self.assertEqual(self.state(self.blocker), 'RELEASED')
        self.assertEqual(self.state(self.docker_b), 'QUEUED')

    def test_a_backfill_grant_that_lapses_expires_and_frees_the_counter(self):
        self.historical()
        with backfill():
            self.advance()
            later = self.now + timedelta(hours=1, minutes=1)
            out = self.advance(now=later)
        self.assertEqual(out['expired'], [self.docker_a])
        self.assertEqual(self.state(self.docker_a), 'EXPIRED')
        self.assertEqual(self.state(self.head), 'QUEUED')
        self.assertEqual(out['backfilled'], [self.docker_b])     # the counter is derived from durable states, so it frees at once

    def test_an_old_strict_fifo_runner_counts_the_backfill_grant_as_an_occupied_lane(self):
        self.historical()
        with backfill():
            self.advance()
        with backfill(enabled=False), unittest.mock.patch.dict(da.HEAVY_LANES, {'wide': 1}):   # two lanes at wide with the third lane
            out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['reason'], 'lane occupied')
        self.assertEqual(self.state(self.docker_a), 'GRANTED')
        with backfill(enabled=False):
            out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertEqual([self.state(q) for q in (self.head, self.docker_a)], ['QUEUED', 'GRANTED'])


class LiveShape(Base):
    def test_a_head_blocked_by_cpu_is_never_backfilled(self):
        """The 2026-11-20 live queue: 828 and 832 run; the waiter-less head 833 is refused on CPU, not on a key."""
        self.running('p6-verify-2', 'build-workspace', [SHARED], 40)
        self.running('p2556e', 'container-suite', ['docker'], 32)
        head = self.enqueue('la-cargo-28532', 'build-workspace', [LOCAL], 40, waiter=False)
        later = [self.enqueue('p6273r2', 'build-crate', [SHARED], 23), self.enqueue('ent-verify-3', 'build-workspace', [SHARED], 40)]
        with backfill():
            out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertNotIn('head_reason', out)
        self.assertIn('cpu: 12 slots in use + 8 requested > 16 cores', out['reason'])
        self.assertEqual([self.state(q) for q in [head] + later], ['QUEUED'] * 3)


class Ordering(Base):
    def test_a_candidate_sharing_a_key_with_the_head_is_never_granted(self):
        self.running('blocker', 'build-workspace', [SHARED], 40)
        self.enqueue('head', 'build-workspace', [SHARED, 'docker'], 40)
        same = self.enqueue('same', 'container-suite', ['docker'], 32)
        other = self.enqueue('other', 'build-crate', [LOCAL])
        with backfill():
            out = self.advance()
        self.assertEqual(out['backfilled'], [other])
        self.assertEqual(self.state(same), 'QUEUED')

    def test_a_candidate_sharing_a_key_with_a_skipped_entry_is_never_granted(self):
        self.running('blocker', 'build-crate', [SHARED])
        self.enqueue('head', 'build-crate', [SHARED])
        big = self.enqueue('big', 'build-workspace', [LOCAL], 40, cpus=16)    # refused: 4 + 16 slots > 16 cores
        small = self.enqueue('small', 'build-crate', [LOCAL])                 # would fit, but `big` waits on the same key
        docker = self.enqueue('docker', 'container-suite', ['docker'])
        with backfill():
            out = self.advance()
        self.assertEqual(out['backfilled'], [docker])
        self.assertEqual([self.state(big), self.state(small)], ['QUEUED', 'QUEUED'])

    def test_a_candidate_whose_key_another_running_job_holds_is_refused(self):
        self.running('blocker', 'build-workspace', [SHARED], 40)
        self.running('docker-run', 'container-suite', ['docker'], 32)
        self.enqueue('head', 'build-crate', [SHARED])
        docker = self.enqueue('docker', 'container-suite', ['docker'], 32)
        with backfill():
            out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertEqual(out.get('backfilled'), [])
        self.assertEqual(self.state(docker), 'QUEUED')

    def test_a_candidate_that_would_starve_the_head_of_cpu_is_refused(self):
        self.running('blocker', 'build-crate', [SHARED])             # 4 slots
        self.enqueue('head', 'build-workspace', [SHARED], 40)          # 8 slots: fits beside the blocker
        hungry = self.enqueue('hungry', 'build-workspace', [LOCAL], 40, cpus=10)   # fits beside the blocker, but 10 + 8 > 16 for the head
        with backfill():
            out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertEqual(self.state(hungry), 'QUEUED')

    def test_insufficient_disk_headroom_counts_the_blocker_and_refuses(self):
        self.historical()
        free = 200 * GIB      # wide tier; candidate alone: 200 - 40 - 32 = 128 >= 100; head after it: 200 - 40 - 32 - 40 = 88 < 100
        with backfill():
            out = self.advance(free=free)
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['backfilled'], [])
        entries = self.entries()
        live, _ = da.live_reservations(self.path, self.now)
        ok, math = da._protects_head(self.host(), entries[self.head], entries[self.docker_a], [{'label': 'p6-verify-2'}], live, [], [],
                                     self.now, 'wide', free, 40 * GIB, 1, 3)
        self.assertFalse(ok)
        self.assertEqual(math['disk_left'], 88 * GIB)
        self.assertGreaterEqual(math['disk_left'] + 40 * GIB, da.HEAVY_FLOOR, 'it would fit only if the release were credited early')


class Waiters(Base):
    def test_a_waiter_less_candidate_is_skipped_and_its_key_joins_the_skipped_set(self):
        self.running('blocker', 'build-workspace', [SHARED], 40)
        self.enqueue('head', 'build-workspace', [SHARED], 40)
        handoff = self.enqueue('handoff', 'container-suite', ['docker'], waiter=False)
        same = self.enqueue('same', 'container-suite', ['docker'])
        crate = self.enqueue('crate', 'build-crate', [LOCAL])
        with backfill():
            out = self.advance()
        self.assertEqual(out['backfilled'], [crate])
        self.assertEqual([self.state(handoff), self.state(same)], ['QUEUED', 'QUEUED'])

    def test_an_uninspectable_waiter_is_never_backfilled(self):
        self.historical()

        def broken(pid):
            raise OSError('ps unavailable')
        with backfill():
            out = self.advance(waiter_token_fn=broken)
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['cancelled'], [])
        self.assertEqual(out['backfilled'], [])


class Blockers(Base):
    def shape(self):
        self.head = self.enqueue('head', 'build-workspace', [SHARED], 40)
        self.docker = self.enqueue('docker', 'container-suite', ['docker'], 32)

    def assert_no_backfill(self, out):
        self.assertEqual(out['granted'], [])
        self.assertNotIn('backfilled', out)
        self.assertEqual(self.state(self.docker), 'QUEUED')

    def test_a_reservation_made_outside_the_queue_disqualifies(self):
        proc = self.spawn()
        reserved = da.reserve('manual', 40 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared=decl('build-workspace', [SHARED]))
        self.assertTrue(reserved['admit'], reserved)
        self.shape()
        with backfill():
            self.assert_no_backfill(self.advance())

    def test_a_blocker_past_its_time_limit_disqualifies(self):
        qid = self.enqueue('blocker', 'build-workspace', [SHARED], 40)
        self.claim(qid, ttl_h=1)
        self.shape()
        with backfill():
            self.assert_no_backfill(self.advance(now=self.now + timedelta(hours=2)))

    def test_an_orphaned_claim_disqualifies(self):
        self.running('blocker', 'build-workspace', [SHARED], 40)
        (da.reservations_dir(self.path) / 'blocker.json').unlink()     # explicit release while the job still runs: a claimed orphan
        self.shape()
        with backfill():
            self.assert_no_backfill(self.advance())

    def test_an_unclaimed_grant_holding_the_key_disqualifies(self):
        self.enqueue('granted-first', 'build-workspace', [SHARED], 40)
        self.shape()
        with backfill():
            first = self.advance()
            self.assertEqual(len(first['granted']), 1)
            self.assert_no_backfill(self.advance())

    def test_an_unresolved_blocker_disqualifies(self):
        budget = {'status': 'ok', 'band': 'GREEN', 'cap_reason': None, 'resource_reasons': ['excl_key overlap with blocker'],
                  'excl_clash': [{'label': 'blocker', 'kind': 'reservation', 'keys': [SHARED]}], 'would_admit': False}
        entry = {'id': 'q000001', 'label': 'blocker', 'state': 'CLAIMED', 'holder': {'pid': 4242}}
        record = {'label': 'blocker', 'pid': 4242, 'queue_id': 'q000001'}
        head = {'id': 'q000002', 'bytes': GIB}
        args = (1, 3, True, 400 * GIB, 40 * GIB, head)
        self.assertEqual(da._excl_only_blockers(budget, *args, [record], [entry])[0]['id'], 'q000001')
        for flag in ('unresolved', 'past_ttl'):
            self.assertIsNone(da._excl_only_blockers(budget, *args, [dict(record, **{flag: 'x'})], [entry]), flag)
        for kind in ('orphan', 'granted'):
            other = dict(budget, excl_clash=[dict(budget['excl_clash'][0], kind=kind)])
            self.assertIsNone(da._excl_only_blockers(other, *args, [record], [entry]), kind)
        self.assertIsNone(da._excl_only_blockers(dict(budget, resource_reasons=budget['resource_reasons'] + ['cpu: x']), *args, [record], [entry]))
        self.assertIsNone(da._excl_only_blockers(dict(budget, band='AMBER'), *args, [record], [entry]))
        self.assertIsNone(da._excl_only_blockers(budget, 3, 3, True, 400 * GIB, 40 * GIB, head, [record], [entry]), 'no free lane')
        self.assertIsNone(da._excl_only_blockers(budget, 1, 3, False, 400 * GIB, 40 * GIB, head, [record], [entry]), 'floor')


class GlobalRefusals(Base):
    def test_a_tier_without_heavy_lanes_never_backfills(self):
        self.historical()
        with backfill():
            out = self.advance(free=90 * GIB)                  # tier low: no heavy lane
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['eligible'], [self.head])
        self.assertNotIn('backfilled', out)

    def test_the_latch_never_backfills(self):
        self.historical()
        with backfill():
            self.advance(free=10 * GIB)                       # sets the latch
            out = self.advance(free=45 * GIB)                 # below the 50 GiB release: still latched
        self.assertEqual(out['granted'], [])
        self.assertNotIn('backfilled', out)

    def test_the_head_failing_the_disk_floor_never_backfills(self):
        self.running('blocker', 'build-workspace', [SHARED], 40)
        self.enqueue('head', 'build-workspace', [SHARED], 40)
        crate = self.enqueue('crate', 'build-crate', [LOCAL])      # 10 GiB: 170 - 40 - 10 = 120 would pass the floor on its own
        with backfill():
            out = self.advance(free=170 * GIB)                     # the head: 170 - 40 - 40 = 90 < 100
        self.assertEqual(out['granted'], [])
        self.assertTrue(out['reason'].startswith('over-capacity'), out['reason'])
        self.assertNotIn('backfilled', out)
        self.assertEqual(self.state(crate), 'QUEUED')

    def test_a_memory_gate_refusal_grants_nothing(self):
        self.historical()
        with backfill():
            out = self.advance(memory_fn=lambda: (False, 'pressure level 2'))
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['reason'], 'memory-gate: pressure level 2')
        self.assertEqual(self.state(self.docker_a), 'QUEUED')

    def test_no_host_sample_never_backfills(self):
        self.historical()
        with backfill():
            out = self.advance(host=False)
        self.assertEqual(out['granted'], [])
        self.assertNotIn('backfilled', out)

    def test_amber_never_backfills(self):
        self.historical()
        self.band = 'AMBER'
        with backfill():
            out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertNotIn('backfilled', out)


class Concurrency(Base):
    def test_an_overtaker_in_flight_holds_the_cap_across_advances(self):
        self.blocker, _ = self.running('blocker', 'build-workspace', [SHARED], 40)   # 8 slots
        self.head = self.enqueue('head', 'build-crate', [SHARED])                    # 4 slots: excl-only beside 8 + 4 + 4
        self.docker_a = self.enqueue('docker', 'container-suite', ['docker'])
        crate = self.enqueue('crate', 'build-crate', [LOCAL])      # compatible too: 8 + 4 + 4 slots, and 4 + 4 + 4 for the head
        with backfill():
            self.assertEqual(self.advance()['backfilled'], [self.docker_a])
            self.claim(self.docker_a)                                      # the overtaker now runs: still in flight
            again = self.advance()
        self.assertEqual(again['granted'], [])
        self.assertEqual(again['backfilled'], [])
        self.assertEqual(self.state(crate), 'QUEUED')
        with backfill(cap=2):
            self.assertEqual(self.advance()['backfilled'], [crate])

    def test_two_interleaved_pollers_grant_at_most_the_cap_and_never_twice(self):
        self.historical()
        crate = self.enqueue('crate', 'build-crate', [LOCAL + '-2'])
        inner = {}

        def poller_a_gate():
            inner['b'] = self.advance(memory_fn=lambda: (True, 'ok'))     # poller B runs its probe and final pass inside A's gate
            return True, 'ok'
        with backfill():
            outer = self.advance(memory_fn=poller_a_gate)
        self.assertEqual(inner['b']['granted'], [self.docker_a])
        self.assertEqual(outer['granted'], [])
        self.assertEqual(len(self.receipts('granted')), 1)
        self.assertEqual([self.state(q) for q in (self.docker_a, self.docker_b, crate)], ['GRANTED', 'QUEUED', 'QUEUED'])

    def test_probe_pass_asks_the_gate_and_the_final_pass_grants(self):
        self.historical()
        asked = []
        with backfill():
            out = self.advance(memory_fn=lambda: asked.append(1) or (True, 'ok'))
        self.assertEqual(asked, [1])
        self.assertEqual(out['backfilled'], [self.docker_a])
        self.assertEqual(self.receipts('granted')[0]['memory_gate'], 'admitted')


class ShippedOff(Base):
    def test_shipped_off_with_a_cap_of_one(self):
        self.assertIs(SHIPPED_BACKFILL_ENABLED, False)
        self.assertEqual(SHIPPED_BACKFILL_MAX, 1)
        self.assertEqual(da.RUNNER_PROTOCOL, 2)

    def test_off_mode_writes_one_rate_limited_shadow_receipt_and_grants_nothing(self):
        self.historical()
        asked = []
        with backfill(enabled=False):
            out = self.advance(memory_fn=lambda: asked.append(1) or (True, 'ok'))
            self.advance(now=self.now + timedelta(minutes=5))
        self.assertEqual(asked, [], 'a shadow candidate never costs a memory-gate run')
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['backfill_shadow'], [self.docker_a])
        self.assertEqual(out['backfilled'], [])
        self.assertEqual(out['reason'], 'budget: excl_key overlap with p6-verify-2')
        self.assertEqual([self.state(q) for q in (self.head, self.docker_a, self.docker_b, self.local)], ['QUEUED'] * 4)
        shadow = self.receipts('backfill-shadow')
        self.assertEqual(len(shadow), 1)
        self.assertEqual((shadow[0]['id'], shadow[0]['blocked_id'], shadow[0]['blocker_id'], shadow[0]['excl_key']),
                         (self.docker_a, self.head, self.blocker, SHARED))
        self.assertIn('disk_left', shadow[0]['math'])
        self.assertFalse(self.receipts('backfill', 'granted'))
        with backfill(enabled=False):
            self.advance(now=self.now + timedelta(minutes=11))
        self.assertEqual(len(self.receipts('backfill-shadow')), 2)


class Budget(unittest.TestCase):
    def test_excl_clash_is_structured_and_the_reason_is_unchanged(self):
        host = {'ram': 128 * GIB, 'used': 20 * GIB, 'cores': 16, 'load1': 0.0, 'time': None, 'window_s': 2.0, 'rows': {}, 'rows_prev': {},
                'workloads': [], 'band': 'GREEN'}
        crate = dict(decl('build-crate', [SHARED]), label='a')
        holders = [da.budget_holder('reservation', crate, None, 11, granted_jobs=4),
                   da.budget_holder('granted', dict(crate, label='a'), None)]
        out = da.budget_decision(host, holders, da.budget_request(decl('build-crate', [SHARED, 'docker']), 10 * GIB, 'new'),
                                 {'max_concurrent': 3})
        self.assertIn('excl_key overlap with a', out['resource_reasons'])
        self.assertEqual(out['excl_clash'], [{'label': 'a', 'kind': 'reservation', 'keys': [SHARED]},
                                             {'label': 'a', 'kind': 'granted', 'keys': [SHARED]}])


class Monitor(Base):
    def classify(self, now, free=400 * GIB):
        entries = list(self.entries().values())
        stamps = {r['id']: r.get('runner') for r in self.receipts('granted')}
        snap = {'now': now, 'wall': now.timestamp(), 'entries': entries, 'problems': [], 'free_bytes': free,
                'mem': {'metrics': {'pressure_level': 1, 'swap_io_mib_s': 0.0, 'compressor_ratio': 0.2, 'load_per_core': 0.4}, 'latched': False},
                'window': [], 'claims': {self.blocker: {'status': 'alive', 'idle_s': 0}}, 'protocol': da.RUNNER_PROTOCOL, 'stamps': stamps,
                'reservations': 1}
        return mon.classify(snap, mon.default_limits(da, ma))

    def test_a_fresh_backfill_grant_is_healthy_busy(self):
        self.historical()
        with backfill():
            self.advance()
        verdict = self.classify(self.now + timedelta(seconds=30))
        self.assertEqual(verdict['primary'], 'healthy-busy', verdict)
        self.assertNotIn('failed-continuation', verdict['causes'])
        self.assertNotIn('outdated-caller', verdict['causes'])

    def test_an_unclaimed_backfill_grant_falls_under_the_existing_failed_continuation_timer(self):
        self.historical()
        with backfill():
            self.advance()
        entry = self.entries()[self.docker_a]
        verdict = self.classify(self.now + timedelta(seconds=mon.escalate_s(entry) + 1))
        self.assertEqual(verdict['causes'].get('failed-continuation'), '%s GRANTED %d min without a claim' % (
            self.docker_a, (mon.escalate_s(entry) + 1) // 60))


class EndToEnd(Base):
    """Real throwaway waiter and holder processes through the real claim path (memory gate stubbed admitting, host sample injected)."""

    def claim_real(self, qid, wrapper):
        entry = self.entries()[qid]
        declared = {k: entry[k] for k in ('class', 'mem_bytes', 'cpu_slots', 'test_threads', 'excl_keys')}
        return da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ['cargo', 'test', entry['label']], str(self.worktree),
                              lambda: 400 * GIB, pid=wrapper.pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                              declared=declared, host_fn=self.host, grant_environ={})

    def test_a_compatible_later_job_runs_while_the_head_waits_then_the_head_starts(self):
        self.historical()
        with backfill():
            self.assertEqual(self.advance(memory_fn=lambda: (True, 'ok'))['backfilled'], [self.docker_a])
            docker_wrapper = self.entries()[self.docker_a]['waiter']['pid']
            claimed = self.claim_real(self.docker_a, next(p for p in self.procs if p.pid == docker_wrapper))
            self.assertTrue(claimed['ok'], claimed)
            self.assertEqual([self.state(q) for q in (self.blocker, self.head, self.docker_a)], ['CLAIMED', 'QUEUED', 'CLAIMED'])
            self.assertEqual(self.advance()['granted'], [], 'the cap holds while the overtaker runs')
            self.blocker_proc.kill()
            self.blocker_proc.wait()
            out = self.advance(memory_fn=lambda: (True, 'ok'))
            self.assertEqual(out['granted'][:1], [self.head])
            head_wrapper = self.entries()[self.head]['waiter']['pid']
            claimed = self.claim_real(self.head, next(p for p in self.procs if p.pid == head_wrapper))
            self.assertTrue(claimed['ok'], claimed)
        self.assertEqual([self.state(q) for q in (self.blocker, self.head, self.docker_a)], ['RELEASED', 'CLAIMED', 'CLAIMED'])


class KeyResolution(unittest.TestCase):
    """New keys follow cargo's own target-dir order and are realpath-canonical (resolve_workload with an injected env, home and cwd)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'home'
        self.work = self.root / 'repo/crates/wt'
        self.work.mkdir(parents=True)
        (self.home / '.cargo').mkdir(parents=True)

    def key(self, env=None, argv=None, cwd=None):
        fields, error = da.resolve_workload('build-crate', None, None, None, env=env or {}, home=str(self.home), cwd=str(cwd or self.work),
                                            argv=argv)
        self.assertIsNone(error)
        return fields['excl_keys'][0]

    def config(self, directory, text, name='config.toml'):
        (directory / '.cargo').mkdir(parents=True, exist_ok=True)
        (directory / '.cargo' / name).write_text(text)

    def test_cargo_precedence_cli_then_env_then_nearest_config_then_cargo_home(self):
        self.config(self.home, '[build]\ntarget-dir = "home-target"\n')
        self.assertEqual(self.key(), da.CARGO_KEY + str(self.home / 'home-target'), 'relative to the parent of the .cargo directory')
        self.config(self.root / 'repo', '[build]\ntarget-dir = "repo-target"\n')
        self.assertEqual(self.key(), da.CARGO_KEY + str(self.root / 'repo/repo-target'))
        self.config(self.work, '[build]\ntarget-dir = "/abs/nearest"\n')
        self.assertEqual(self.key(), da.CARGO_KEY + '/abs/nearest', 'the config nearest to cwd wins')
        self.assertEqual(self.key({'CARGO_BUILD_TARGET_DIR': 'bt'}), da.CARGO_KEY + str(self.work / 'bt'), 'the env form beats every file')
        self.assertEqual(self.key({'CARGO_BUILD_TARGET_DIR': 'bt', 'CARGO_TARGET_DIR': '/ct'}), da.CARGO_KEY + '/ct')
        argv = ['cargo', 'test', '--target-dir', 'cli', '--', '--target-dir', 'ignored']
        self.assertEqual(self.key({'CARGO_TARGET_DIR': '/ct'}, argv), da.CARGO_KEY + str(self.work / 'cli'))
        self.assertEqual(self.key({}, ['cargo', 'build', '--target-dir=/eq']), da.CARGO_KEY + '/eq')

    def test_cargo_home_and_legacy_config_and_the_default(self):
        self.assertEqual(self.key(), da.CARGO_KEY + str(self.work / 'target'))
        cargo_home = self.root / 'ch'
        cargo_home.mkdir()
        (cargo_home / 'config.toml').write_text('[build]\ntarget-dir = "/from-cargo-home"\n')
        self.assertEqual(self.key({'CARGO_HOME': str(cargo_home)}), da.CARGO_KEY + '/from-cargo-home')
        self.config(self.root / 'repo', '[build]\ntarget-dir = "/legacy"\n', name='config')
        self.config(self.root / 'repo', '[build]\ntarget-dir = "/toml"\n')
        self.assertEqual(self.key(), da.CARGO_KEY + '/legacy', 'cargo reads the extensionless file when both exist')

    def test_heavy_run_shaped_argv_and_non_cargo_jobs(self):
        heavy = ['/bin/sh', '-c', 'touch "$1" && shift && exec "$@"', 'heavy-run', '/tmp/proof', 'cargo', 'nextest', 'run',
                 '--target-dir', '/hr']
        self.assertEqual(self.key({}, heavy), da.CARGO_KEY + '/hr')
        self.assertEqual(self.key({}, ['make', '--target-dir', '/nope']), da.CARGO_KEY + str(self.work / 'target'))

    def test_symlinks_trailing_slashes_and_dot_dot_are_canonical(self):
        real = self.root / 'real-target'
        real.mkdir()
        link = self.root / 'link-target'
        link.symlink_to(real)
        for spelling in (str(link), str(link) + '/', str(self.root / 'x/../link-target')):
            self.assertEqual(self.key({'CARGO_TARGET_DIR': spelling}), da.CARGO_KEY + str(real), spelling)

    @unittest.skipUnless(sys.platform == 'darwin' and os.path.realpath('/tmp') == '/private/tmp', 'macOS /tmp alias')
    def test_tmp_and_private_tmp_are_one_key(self):
        self.assertEqual(self.key({'CARGO_TARGET_DIR': '/tmp/cae-target-x'}), da.CARGO_KEY + '/private/tmp/cae-target-x')
        self.assertTrue(da.excl_keys_clash(da.CARGO_KEY + '/tmp/cae-target-x', da.CARGO_KEY + '/private/tmp/cae-target-x'))


class KeyIdentity(Base):
    """Stored keys (pre-realpath spellings) and new keys compare by identity at decision time, so queued entries are protected too."""

    def aliases(self):
        real = Path(self.temp.name).resolve() / 'shared-target'
        real.mkdir()
        link = Path(self.temp.name).resolve() / 'shared-link'
        link.symlink_to(real)
        return da.CARGO_KEY + str(real), da.CARGO_KEY + str(link) + '/'

    def test_excl_identity(self):
        canonical, stored = self.aliases()
        self.assertTrue(da.excl_keys_clash(canonical, stored))
        self.assertFalse(da.excl_keys_clash(canonical, LOCAL))
        self.assertEqual(da.excl_identity('docker'), ['docker'])
        self.assertTrue(da.excl_keys_clash(da.CARGO_KEY + '/nonexistent/a/../b', da.CARGO_KEY + '/nonexistent/b'))
        for key in (canonical, stored, LOCAL, 'docker'):
            self.assertIn(key, da.excl_identity(key), 'the raw string always stays a token: string equality is never lost')

    def test_a_directory_created_or_removed_between_two_stats_still_clashes(self):
        """The race the single-form identity had: one side stats before cargo creates the target (or after cargo clean removes it)."""
        target = Path(self.temp.name).resolve() / 'raced-target'
        key = da.CARGO_KEY + str(target)
        link = Path(self.temp.name).resolve() / 'raced-link'
        link.symlink_to(target)          # dangling until the directory exists
        alias = da.CARGO_KEY + str(link)
        before, before_alias = da.excl_identity(key), da.excl_identity(alias)
        target.mkdir()
        after, after_alias = da.excl_identity(key), da.excl_identity(alias)
        self.assertTrue(any(t.startswith('cargo-target-inode:') for t in after))
        self.assertFalse(any(t.startswith('cargo-target-inode:') for t in before))
        for a, b in ((before, after), (before_alias, after), (before, after_alias), (before_alias, after_alias)):
            self.assertTrue(set(a) & set(b), (a, b))
        target.rmdir()
        removed = da.excl_identity(key)
        self.assertTrue(set(after) & set(removed))
        self.assertTrue(set(after_alias) & set(removed))

    def test_two_different_existing_directories_stay_distinct(self):
        root = Path(self.temp.name).resolve()
        (root / 'one').mkdir()
        (root / 'two').mkdir()
        self.assertFalse(da.excl_keys_clash(da.CARGO_KEY + str(root / 'one'), da.CARGO_KEY + str(root / 'two')))
        holders = [da.budget_holder('reservation', dict(decl('build-crate', [da.CARGO_KEY + str(root / 'one')]), label='a'), None, 11,
                                    granted_jobs=4)]
        out = da.budget_decision(self.host(), holders, da.budget_request(decl('build-crate', [da.CARGO_KEY + str(root / 'two')]), 10 * GIB,
                                                                          'b'), {'max_concurrent': 3})
        self.assertEqual(out['excl_clash'], [])

    @unittest.skipUnless(sys.platform == 'darwin', 'case-insensitive default volume')
    def test_a_missing_directory_compares_case_insensitively_on_macos(self):
        self.assertTrue(da.excl_keys_clash(da.CARGO_KEY + '/Nonexistent/Target', da.CARGO_KEY + '/nonexistent/target'))

    def test_budget_clash_sees_an_alias_and_reports_the_request_spelling(self):
        canonical, stored = self.aliases()
        crate = dict(decl('build-crate', [stored]), label='old')
        holders = [da.budget_holder('reservation', crate, None, 11, granted_jobs=4)]
        out = da.budget_decision(self.host(), holders, da.budget_request(decl('build-crate', [canonical]), 10 * GIB, 'new'), {'max_concurrent': 3})
        self.assertIn('excl_key overlap with old', out['resource_reasons'])
        self.assertEqual(out['excl_clash'], [{'label': 'old', 'kind': 'reservation', 'keys': [canonical]}])

    def test_a_running_job_on_an_aliased_key_blocks_an_ordinary_grant(self):
        canonical, stored = self.aliases()
        self.running('old', 'build-crate', [stored])
        new = self.enqueue('new', 'build-crate', [canonical])
        out = self.advance()
        self.assertEqual(out['granted'], [])
        self.assertEqual(out['reason'], 'budget: excl_key overlap with old')
        self.assertEqual(self.state(new), 'QUEUED')

    def test_a_candidate_aliasing_the_heads_key_is_never_backfilled(self):
        canonical, stored = self.aliases()
        self.running('blocker', 'build-workspace', [SHARED], 40)
        self.enqueue('head', 'build-workspace', [SHARED, stored], 40)
        alias = self.enqueue('alias', 'build-crate', [canonical])
        other = self.enqueue('other', 'container-suite', ['docker'])
        with backfill():
            out = self.advance()
        self.assertEqual(out['backfilled'], [other])
        self.assertEqual(self.state(alias), 'QUEUED')

    def test_an_entry_holding_a_legacy_key_still_claims_with_an_unchanged_fingerprint(self):
        canonical, stored = self.aliases()
        qid = self.enqueue('legacy', 'build-crate', [stored])
        entry = self.entries()[qid]
        self.assertEqual(entry['excl_keys'], [stored], 'stored keys are never rewritten')
        salt = da._queue_salt(self.path)
        self.assertEqual(da._fingerprint(salt, entry['repo'], entry['worktree'], entry['tickets'], ['cargo', 'test', 'legacy'], entry['bytes'],
                                         da._declared(entry)), entry['fingerprint'])
        self.assertEqual(self.advance()['granted'], [qid])
        wrapper = next(p for p in self.procs if p.pid == entry['waiter']['pid'])
        claimed = da.queue_claim(self.path, qid, 'legacy', entry['bytes'], ['cargo', 'test', 'legacy'], str(self.worktree), lambda: 400 * GIB,
                                 pid=wrapper.pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                                 declared=decl('build-crate', [canonical]), host_fn=self.host, grant_environ={})
        self.assertTrue(claimed['ok'], claimed)


class HeadProtection(Base):
    def test_a_candidate_that_would_shrink_the_tier_below_the_heads_lanes_is_refused(self):
        """Wide (3 lanes) now; free - reservations - candidate falls to stable (2 lanes): blocker gone, other + candidate + head = 3."""
        self.running('blocker', 'build-workspace', [SHARED], 40)
        self.running('other', 'build-crate', [LOCAL], 10)
        head = self.enqueue('head', 'build-crate', [SHARED], 10)
        docker = self.enqueue('docker', 'container-suite', ['docker'], 20)
        with backfill():
            out = self.advance(free=260 * GIB)            # 260 - 50 - 20 = 190 GiB: stable; disk_left 180 GiB passes the floor
        self.assertEqual(out['head_reason'], 'budget: excl_key overlap with blocker')
        self.assertEqual(out['backfilled'], [])
        self.assertEqual(self.state(docker), 'QUEUED')
        entries = self.entries()
        live, _ = da.live_reservations(self.path, self.now)
        ok, math = da._protects_head(self.host(), entries[head], entries[docker], [{'label': 'blocker'}], live, [], [], self.now, 'wide',
                                     260 * GIB, 50 * GIB, 2, 3)
        self.assertFalse(ok)
        self.assertEqual((math['tier_after'], math['lanes_after'], math['disk_left']), ('stable', 2, 180 * GIB))
        with backfill():
            self.assertEqual(self.advance(free=300 * GIB)['backfilled'], [docker], '300 - 70 = 230 GiB stays wide')

    def test_the_disk_latch_is_carried_into_the_projection(self):
        self.historical()
        entries = self.entries()
        live, _ = da.live_reservations(self.path, self.now)
        args = (self.host(), entries[self.head], entries[self.docker_a], [{'label': 'p6-verify-2'}], live, [], [], self.now, 'wide',
                400 * GIB, 40 * GIB, 1, 3)
        self.assertTrue(da._protects_head(*args)[0])
        ok, math = da._protects_head(*args, latch=True)
        self.assertFalse(ok)
        self.assertEqual(math['cap'], 0, 'heavy_lane_cap now sees the latch')
        low = (self.host(), entries[self.head], entries[self.docker_a], [{'label': 'p6-verify-2'}], live, [], [], self.now, 'stable',
               110 * GIB, 40 * GIB, 1, 2)
        self.assertEqual(da._protects_head(*low, latch=True)[1]['lanes_after'], 0, '110 - 40 - 32 = 38 GiB keeps a set latch (release at 50)')
        self.assertEqual(da._protects_head(*low)[1]['lanes_after'], 0, '38 GiB is the low tier: no heavy lane')

    def test_a_candidate_whose_declared_memory_breaks_the_heads_memory_projection_is_refused(self):
        """No helper projects the SPE-117 latch from declared bytes; the fallback is the head's band and memory with the candidate charged."""
        self.running('blocker', 'build-crate', [SHARED], mem_gib=4)
        self.enqueue('head', 'build-workspace', [SHARED], 40, mem_gib=40)    # beside the blocker: 20 + 4 + 40 = 64 <= 89.6 GiB
        big = self.enqueue('big', 'container-suite', ['docker'], mem_gib=40)  # alone it fits; with the head 20 + 40 + 40 > 89.6 GiB
        small = self.enqueue('small', 'build-crate', [LOCAL], mem_gib=8)
        with backfill(cap=2):
            out = self.advance()
        self.assertEqual(out['backfilled'], [small])
        self.assertEqual(self.state(big), 'QUEUED')
        self.assertEqual(self.receipts('backfill')[0]['math']['band_after'], 'GREEN')

    def blocker_load(self, load1):
        """The blocker's measured CPU (8 slots' worth over the 2 s window) is credited only while it is counted as a holder."""
        self.historical()
        pid = self.blocker_proc.pid
        self.rows, self.rows_prev = {pid: (1, 1024, 116.0)}, {pid: (1, 1024, 100.0)}
        self.load1 = load1
        self.band_fn = lambda lpc: ('GREEN', []) if lpc is None or lpc <= ma.EXIT['load_per_core'] else ('AMBER', [])

    def test_the_head_turning_amber_once_the_blockers_load_credit_is_removed_refuses(self):
        self.blocker_load(36.0)       # (36 - 8) / 16 = 1.75 with the credit: GREEN; 36 / 16 = 2.25 without it: AMBER
        with backfill():
            out = self.advance()
        self.assertEqual(out['head_reason'], 'budget: excl_key overlap with p6-verify-2')
        self.assertEqual(out['backfilled'], [])
        self.assertEqual(self.state(self.docker_a), 'QUEUED')

    def test_the_same_shape_below_the_line_still_backfills(self):
        self.blocker_load(30.0)       # 30 / 16 = 1.875 even without the credit
        with backfill():
            self.assertEqual(self.advance()['backfilled'], [self.docker_a])

    def test_every_blocker_is_recorded_and_the_first_stays_in_the_old_fields(self):
        first, _ = self.running('blocker-a', 'build-crate', [SHARED])
        second, _ = self.running('blocker-b', 'build-crate', [LOCAL])
        self.enqueue('head', 'build-crate', [SHARED, LOCAL])
        docker = self.enqueue('docker', 'container-suite', ['docker'])
        with backfill():
            self.assertEqual(self.advance()['backfilled'], [docker])
        record = self.receipts('backfill')[0]
        self.assertEqual([b['id'] for b in record['blockers']], [first, second])
        self.assertEqual((record['blocker_id'], record['blocker_label'], record['excl_key']), (first, 'blocker-a', SHARED))
        self.assertEqual(record['blockers'][1]['keys'], [LOCAL])


class ClaimRecheck(Base):
    def claim_real(self, qid, host_fn=None, free=400 * GIB):
        entry = self.entries()[qid]
        wrapper = next(p for p in self.procs if p.pid == entry['waiter']['pid'])
        declared = {k: entry[k] for k in ('class', 'mem_bytes', 'cpu_slots', 'test_threads', 'excl_keys')}
        return da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ['cargo', 'test', entry['label']], str(self.worktree),
                              lambda: free, pid=wrapper.pid, now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                              declared=declared, host_fn=host_fn, grant_environ={})

    def test_a_backfilled_grant_whose_head_protection_broke_is_revoked_at_claim(self):
        self.historical()
        with backfill():
            self.assertEqual(self.advance()['backfilled'], [self.docker_a])
        proc = self.spawn()      # a job reserved outside the queue now takes the CPU the head needs: no longer refused by the key alone
        with da.state_lock(self.path):
            da.write_reservation(self.path, 'manual', 10 * GIB, proc.pid, da.process_token(proc.pid), self.now + timedelta(hours=1),
                                 declared=dict(decl('build-crate', [LOCAL]), created=da.iso_utc(self.now - timedelta(minutes=1))), granted_jobs=4)
        out = self.claim_real(self.docker_a, self.host)
        self.assertFalse(out['ok'])
        self.assertTrue(out['reason'].startswith('backfill-revoked: %s no longer protects the oldest waiting entry %s (head-not-excl-only)'
                                                 % (self.docker_a, self.head)), out['reason'])
        self.assertEqual(self.state(self.docker_a), 'EXPIRED')
        self.assertFalse((da.queue_dir(self.path) / 'granted' / self.docker_a).exists())
        revoked = self.receipts('backfill-revoked')
        self.assertEqual([(r['id'], r['kind'], r['blocked_id']) for r in revoked], [(self.docker_a, 'head-not-excl-only', self.head)])
        self.assertEqual(self.receipts('claim-refused')[-1]['kind'], 'backfill-revoked')
        self.assertFalse((da.reservations_dir(self.path) / 'p2556d.json').exists(), 'nothing was reserved')

    def test_a_backfilled_grant_whose_disk_headroom_shrank_is_revoked_at_claim(self):
        self.historical()
        with backfill():
            self.advance()
        out = self.claim_real(self.docker_a, self.host, free=210 * GIB)
        self.assertFalse(out['ok'])
        self.assertIn('(head-unprotected)', out['reason'])     # its own floor passes (210 - 40 - 32); the head's would not (210 - 112 < 100)
        self.assertEqual(self.state(self.docker_a), 'EXPIRED')

    def test_no_host_sample_refuses_a_backfilled_claim_but_keeps_the_grant(self):
        self.historical()
        with backfill():
            self.advance()
        self.blocker_proc.kill()       # lane 1 needs no host sample, so only the re-check can refuse
        self.blocker_proc.wait()
        da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)     # records the release; with no host sample nothing is granted
        out = self.claim_real(self.docker_a, None)
        self.assertFalse(out['ok'])
        self.assertTrue(out['reason'].startswith('backfill-unverifiable: no host sample'), out['reason'])
        self.assertEqual(self.state(self.docker_a), 'GRANTED')
        self.assertTrue(self.claim_real(self.docker_a, self.host)['ok'])

    def test_an_ordinary_grant_is_never_rechecked(self):
        head = self.enqueue('head', 'build-crate', [SHARED])
        self.enqueue('later', 'build-crate', [SHARED])
        self.assertEqual(self.advance()['granted'], [head])
        out = self.claim_real(head, None)      # no host sample: lane 1 needs none, and no earlier entry waits
        self.assertTrue(out['ok'], out)
        self.assertFalse(self.receipts('backfill-revoked'))


class BackfillWindow(Base):
    def test_a_backfill_grant_gets_the_shorter_window_and_an_in_order_grant_does_not(self):
        self.historical()
        with backfill():
            self.advance()
        grant = self.entries()[self.docker_a]['grant']
        self.assertEqual(set(grant), {'at', 'expires'})
        self.assertEqual(da._parse_time(grant['expires']) - da._parse_time(grant['at']), timedelta(seconds=da.BACKFILL_GRANT_S))
        self.assertEqual(self.receipts('granted')[0]['expires'], grant['expires'])
        self.assertEqual(da.BACKFILL_GRANT_S, 600)
        self.blocker_proc.kill()
        self.blocker_proc.wait()
        with backfill():
            self.advance()
        head = self.entries()[self.head]['grant']
        self.assertEqual(da._parse_time(head['expires']) - da._parse_time(head['at']), timedelta(seconds=da.QUEUE_GRANT_S))

    def test_the_shorter_window_lapses_and_frees_the_in_flight_counter(self):
        self.historical()
        with backfill():
            self.advance()
            early = self.advance(now=self.now + timedelta(seconds=da.BACKFILL_GRANT_S - 1))
            self.assertEqual((early['expired'], self.state(self.docker_a)), ([], 'GRANTED'))
            late = self.advance(now=self.now + timedelta(seconds=da.BACKFILL_GRANT_S + 1))
        self.assertEqual(late['expired'], [self.docker_a])
        self.assertEqual(late['backfilled'], [self.docker_b])

    def test_monitor_and_recovery_read_the_actual_window(self):
        self.historical()
        with backfill():
            self.advance()
        entry = self.entries()[self.docker_a]
        self.assertEqual(mon.grant_window_s(entry), da.BACKFILL_GRANT_S)
        self.assertEqual(mon.escalate_s(entry), int(da.BACKFILL_GRANT_S * mon.GRANT_ESCALATE_FRACTION))
        verdict = Monitor.classify(self, self.now + timedelta(seconds=mon.escalate_s(entry) - 1))
        self.assertNotIn('failed-continuation', verdict['causes'])
        self.assertNotIn('unclaimed-handoff', verdict['causes'])
        verdict = Monitor.classify(self, self.now + timedelta(seconds=mon.escalate_s(entry) + 1))
        self.assertIn('failed-continuation', verdict['causes'])
        env = unittest.mock.Mock(token_fn=da.waiter_token)
        self.assertEqual(rec._evaluate(entry, env, self.now + timedelta(seconds=rec.NOTICE_S + 1)), (False, ['waiter-alive']))


class Lows(Base):
    def test_off_mode_shadow_judges_a_later_candidate_beside_the_earlier_one(self):
        self.running('blocker', 'build-crate', [SHARED])                  # 4 slots
        self.enqueue('head', 'build-crate', [SHARED])                     # 4
        first = self.enqueue('first', 'build-crate', [LOCAL])             # 4: 4 + 4 + 4 for the head
        wide = self.enqueue('wide', 'build-workspace', [LOCAL + '-2'], 40, cpus=9)   # alone 4 + 9; beside `first` 4 + 4 + 9 > 16
        with backfill(enabled=False, cap=2):
            off = self.advance()
        self.assertEqual(off['backfill_shadow'], [first])
        self.assertEqual(self.receipts('backfill-shadow')[0]['memory_gate'], 'not-asked')
        self.assertEqual(self.state(wide), 'QUEUED')
        with backfill(cap=2):
            self.assertEqual(self.advance()['backfilled'], [first], 'the enabled pass agrees with the shadow')

    def test_one_waiter_ps_per_process_per_pass(self):
        self.historical()
        asked = {}

        def counting(pid):
            asked[pid] = asked.get(pid, 0) + 1
            return da.waiter_token(pid)
        with backfill(enabled=False, cap=3):
            self.advance(waiter_token_fn=counting)
        self.assertTrue(asked)
        self.assertEqual(max(asked.values()), 1, asked)

    def test_callers_report_the_backfill_revision_from_text(self):
        with tempfile.TemporaryDirectory() as root:
            for name, body in (('old', 'RUNNER_PROTOCOL = 2\n'), ('rev9', 'RUNNER_PROTOCOL = 2\nBACKFILL_ENABLED = False\n'),
                               ('hard', 'RUNNER_PROTOCOL = 2\nBACKFILL_ENABLED = False\nBACKFILL_GRANT_S = 600\n'),
                               ('on', 'RUNNER_PROTOCOL = 2\nBACKFILL_ENABLED = True\nBACKFILL_GRANT_S = 600\n')):
                (Path(root) / name / 'scripts').mkdir(parents=True)
                (Path(root) / name / 'scripts/disk-admission.py').write_text(body + 'raise SystemExit("must not run")\n')
            rows = {r['checkout']: r['backfill'] for r in mon.inventory(root)}
        self.assertEqual(rows, {'old': 'absent', 'rev9': 'off', 'hard': 'off-hardened', 'on': 'on-hardened'})


class Requeue(Base):
    """A wrapper whose backfilled grant was revoked at claim, or lapsed unclaimed, re-queues the same job at its original FIFO position
    (queue enqueue --requeue-of, SPE-176): a fresh id, never a reused one, ordered behind everything queued before the original."""
    claim_real = ClaimRecheck.claim_real

    def revoke_docker_a(self):
        self.historical()
        with backfill():
            self.assertEqual(self.advance()['backfilled'], [self.docker_a])
        out = self.claim_real(self.docker_a, self.host, free=210 * GIB)     # the head's floor would no longer hold: revoked
        self.assertTrue(out['reason'].startswith('backfill-revoked'), out['reason'])

    def requeue(self, qid, session=SESSION, waiter='entry', again=True, now=None):
        entry = self.entries()[qid]
        pid = entry['waiter']['pid'] if waiter == 'entry' else waiter
        return da.queue_enqueue(self.path, label=entry['label'], tickets=['HORO-1'], repo='horonom/x', worktree=str(self.worktree),
                                harness='claude', session_id=session, nbytes=entry['bytes'], argv=['cargo', 'test', entry['label']],
                                now=now or self.now + timedelta(seconds=1), again=again, requeue_of=qid,
                                declared={k: entry[k] for k in ('class', 'mem_bytes', 'cpu_slots', 'test_threads', 'excl_keys') if k in entry},
                                waiter_pid=pid, ancestor_fn=(lambda p: True) if pid else None)

    def waiting_order(self):
        return [e['id'] for e in da._queue_entries(self.path)[0] if e['state'] in ('QUEUED', 'ELIGIBLE')]

    def test_a_revoked_grant_is_replaced_at_its_original_position_with_a_fresh_id(self):
        self.revoke_docker_a()
        before = set(self.entries())
        out = self.requeue(self.docker_a)
        self.assertTrue(out['ok'], out)
        new = out['id']
        self.assertNotIn(new, before)
        self.assertGreater(new, max(before))                     # ids are never reused
        self.assertEqual((out['requeue_of'], out['evidence'], out['order']), (self.docker_a, 'backfill-revoked', self.entries()[self.docker_a]['seq']))
        self.assertIn('position 2', out['reason'])
        self.assertEqual(self.state(self.docker_a), 'EXPIRED')    # the lost entry stays as it was
        self.assertEqual(self.waiting_order(), [self.head, new, self.docker_b, self.local])   # behind the head, ahead of later entries
        record = [r for r in self.receipts('enqueued') if r['id'] == new]
        self.assertEqual([(r['requeue_of'], r['evidence'], r['requeues']) for r in record], [(self.docker_a, 'backfill-revoked', 1)])
        self.assertEqual(sum(1 for e in self.entries().values() if e.get('requeue_of') == self.docker_a), 1)

    def test_the_replacement_is_granted_in_order_and_never_misread_as_a_backfilled_grant(self):
        self.revoke_docker_a()
        new = self.requeue(self.docker_a)['id']
        self.blocker_proc.kill()
        self.blocker_proc.wait()
        with backfill():
            out = self.advance()
        self.assertEqual(out['granted'][:2], [self.head, new])    # ahead of docker_b, which was queued after the original
        self.assertEqual(self.state(self.docker_b), 'QUEUED')
        # docker_b waits with a lower seq than the replacement: by seq the replacement would look like a backfilled grant and its claim
        # would need a host sample. By FIFO position nothing waits ahead of it, so it is an ordinary grant.
        out = self.claim_real(new, None)
        self.assertTrue(out['ok'], out)

    def test_an_in_order_grant_made_before_the_replacement_existed_is_not_treated_as_a_backfill(self):
        self.revoke_docker_a()
        self.blocker_proc.kill()
        self.blocker_proc.wait()
        with backfill():
            granted = self.advance()['granted']
        self.assertIn(self.docker_b, granted)
        self.assertTrue(self.requeue(self.docker_a)['ok'])
        out = self.claim_real(self.docker_b, None)               # the replacement waits ahead of it but was created after its grant
        self.assertTrue(out['ok'], out)

    def test_a_backfilled_grant_that_lapsed_unclaimed_is_replaced(self):
        self.historical()
        later = self.now + timedelta(seconds=da.BACKFILL_GRANT_S + 1)
        with backfill():
            self.advance()
            self.assertIn(self.docker_a, self.advance(now=later)['expired'])
        out = self.requeue(self.docker_a, now=later + timedelta(seconds=1))
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['evidence'], 'backfill-lapsed')

    def test_an_ordinary_expiry_is_never_replaced(self):
        head = self.enqueue('head', 'build-crate', [SHARED])
        waited = self.enqueue('waited', 'build-crate', [LOCAL])
        self.assertEqual(self.advance()['granted'][:1], [head])
        self.advance(now=self.now + timedelta(hours=25))          # an in-order grant lapses; a wait limit passes
        self.assertEqual((self.state(head), self.state(waited)), ('EXPIRED', 'EXPIRED'))
        count = len(self.entries())
        for qid in (head, waited):
            out = self.requeue(qid, now=self.now + timedelta(hours=26))
            self.assertFalse(out['ok'])
            self.assertIn('did not lose a backfilled grant', out['reason'])
        self.assertEqual(len(self.entries()), count)

    def test_a_repeated_requeue_returns_the_live_replacement_and_never_duplicates(self):
        self.revoke_docker_a()
        first = self.requeue(self.docker_a)
        again = self.requeue(self.docker_a)
        self.assertEqual((again['id'], again.get('existing')), (first['id'], True))
        self.advance(now=self.now + timedelta(hours=25))          # the replacement's own wait lapses: an ordinary expiry
        self.assertEqual(self.state(first['id']), 'EXPIRED')
        out = self.requeue(self.docker_a, now=self.now + timedelta(hours=26))
        self.assertFalse(out['ok'])
        self.assertIn('already re-queued as %s' % first['id'], out['reason'])
        self.assertEqual(sum(1 for e in self.entries().values() if e.get('requeue_of') == self.docker_a), 1)

    def test_only_the_same_session_and_waiter_may_take_the_position(self):
        self.revoke_docker_a()
        other = self.requeue(self.docker_a, session='1c2d3e4f-aaaa-bbbb-cccc-1234567890ab')
        self.assertFalse(other['ok'])
        self.assertIn('different job or session', other['reason'])
        stranger = self.requeue(self.docker_a, waiter=self.spawn().pid)
        self.assertFalse(stranger['ok'])
        self.assertIn('different waiter', stranger['reason'])
        self.assertFalse(self.requeue(self.docker_a, again=False)['ok'])
        self.assertTrue(self.requeue(self.docker_a)['ok'])

    def test_a_waiterless_handoff_entry_is_never_requeued(self):
        qid = self.enqueue('handoff', 'build-crate', [SHARED], waiter=False)
        self.advance(now=self.now + timedelta(hours=25))
        out = self.requeue(qid, waiter=None, now=self.now + timedelta(hours=26))
        self.assertFalse(out['ok'])
        self.assertIn('different waiter', out['reason'])

    def test_the_chain_is_capped(self):
        self.revoke_docker_a()
        with da.state_lock(self.path):
            entry = self.entries()[self.docker_a]
            entry['requeues'] = da.BACKFILL_REQUEUE_MAX
            da._write_queue_entry(self.path, entry)
        out = self.requeue(self.docker_a)
        self.assertFalse(out['ok'])
        self.assertIn('re-queue limit of 3', out['reason'])

    def test_the_evidence_survives_one_receipt_rotation(self):
        self.revoke_docker_a()
        receipts = da.queue_dir(self.path) / 'receipts.jsonl'
        os.replace(receipts, receipts.with_name('receipts.jsonl.1'))
        out = self.requeue(self.docker_a)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['evidence'], 'backfill-revoked')

    def test_a_claimed_backfill_job_is_never_expired_by_the_short_window_nor_requeued(self):
        self.historical()
        with backfill():
            self.advance()
        self.assertTrue(self.claim_real(self.docker_a, self.host)['ok'])
        out = self.advance(now=self.now + timedelta(seconds=da.BACKFILL_GRANT_S + 60))
        self.assertNotIn(self.docker_a, out['expired'])
        self.assertEqual(self.state(self.docker_a), 'CLAIMED')   # revoke happens only before the claim: a running job is never stranded
        count = len(self.entries())
        out = self.requeue(self.docker_a)                         # the live entry of the same job is returned, nothing new is queued
        self.assertEqual((out['id'], out.get('existing')), (self.docker_a, True))
        self.assertEqual(len(self.entries()), count)


if __name__ == '__main__':
    unittest.main()
