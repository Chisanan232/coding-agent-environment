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
ma = load('memory_admission_backfill', 'scripts/memory-admission.py')
SHIPPED_BACKFILL_ENABLED, SHIPPED_BACKFILL_MAX = da.BACKFILL_ENABLED, da.BACKFILL_MAX
da.THIRD_LANE_ENABLED = True      # the shipped lane rules: three lanes at the wide tier
da.FOURTH_LANE_ENABLED = False
GIB = 1024 ** 3
SHARED = 'cargo-target:/Users/dev/.cargo/shared-target'
LOCAL = 'cargo-target:/Users/dev/.cargo/targets/local-agent'
SESSION = '0b1d2e3f-aaaa-bbbb-cccc-1234567890ab'


def decl(cls, keys, cpus=None):
    spec = da.WORKLOAD_CLASSES[cls]
    jobs = cpus or spec['jobs']
    return {'class': cls, 'mem_bytes': (spec['mem_base_mib'] + spec['mem_per_job_mib'] * spec['jobs']) * da.MIB, 'cpu_slots': jobs,
            'test_threads': jobs, 'excl_keys': list(keys)}


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
                  'window_s': 2.0, 'rows': {}, 'rows_prev': {}, 'workloads': list(self.workloads), 'band': self.band}
        if self.band_fn:
            sample['band_fn'] = self.band_fn
        return sample

    def enqueue(self, label, cls, keys, gib=None, waiter=True, cpus=None):
        proc = self.spawn() if waiter else None
        out = da.queue_enqueue(self.path, label=label, tickets=['HORO-1'], repo='horonom/x', worktree=str(self.worktree), harness='claude',
                               session_id=SESSION, nbytes=(gib or da.WORKLOAD_CLASSES[cls]['disk_gib']) * GIB, argv=['cargo', 'test', label],
                               now=self.now, declared=decl(cls, keys, cpus), waiter_pid=proc.pid if proc else None,
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

    def running(self, label, cls, keys, gib=None):
        qid = self.enqueue(label, cls, keys, gib)
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


if __name__ == '__main__':
    unittest.main()
