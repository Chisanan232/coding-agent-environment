#!/usr/bin/env python3
"""Shadow resource budget (SPE-150 PR4): computed and recorded beside the lane cap, never enforced."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


da = load('disk_admission', 'scripts/disk-admission.py')
queue_tests = load('admission_queue_tests', 'tests/test-admission-queue.py')
QueueBase = queue_tests.QueueBase
GIB = 1024 ** 3
RAM = 128 * GIB
SAMPLED = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
SESSION = '0b1d2e3f-aaaa-bbbb-cccc-1234567890ab'
CRATE = {'class': 'build-crate', 'mem_bytes': 6 * GIB, 'cpu_slots': 4, 'test_threads': 4}
ARGV = ['cargo', 'nextest', 'run', '--token', queue_tests.SECRET_ARG]   # what QueueBase.enqueue hashes into the entry
CAPS = {'max_concurrent': 1, 'free_bytes': 400 * GIB, 'reserved_bytes': 0, 'floor': 100 * GIB}


def host(used=20 * GIB, cores=16, load1=0.0, band='GREEN', rows=None, rows_prev=None, workloads=None, window_s=2.0, **extra):
    return dict({'ram': RAM, 'used': used, 'cores': cores, 'load1': load1, 'time': SAMPLED, 'window_s': window_s,
                 'rows': rows or {}, 'rows_prev': rows_prev or {}, 'workloads': workloads or [], 'band': band}, **extra)


def holder(label='a', record=CRATE, kind='reservation', created=None, pid=None, excl=('cargo-target:/a',), **extra):
    return da.budget_holder(kind, dict({'label': label, 'excl_keys': list(excl)}, **dict(record, **extra)), created, pid)


def request(record=CRATE, excl=('cargo-target:/b',), nbytes=20 * GIB, label='new'):
    return da.budget_request(dict(record, excl_keys=list(excl)), nbytes, label)


def decide(h=None, holders=(), req=None, caps=None):
    return da.budget_decision(h or host(), list(holders), req or request(), caps or CAPS)


class Arithmetic(unittest.TestCase):
    def test_a_lone_classed_request_would_be_admitted(self):
        out = decide()
        self.assertTrue(out['would_admit'] and out['fits_if_cap_were_2'])
        self.assertEqual((out['shadow'], out['status'], out['band'], out['count']), ('shadow: not enforced', 'ok', 'GREEN', 0))
        self.assertEqual(out['projected_bytes'], 26 * GIB)
        self.assertEqual(out['limit_bytes'], RAM * 70 // 100)
        json.dumps(out)

    def test_two_classed_workloads_fit_resources_but_the_cap_of_one_says_no(self):
        out = decide(holders=[holder()])
        self.assertEqual(out['resource_reasons'], [])
        self.assertFalse(out['would_admit'])
        self.assertTrue(out['fits_if_cap_were_2'])
        self.assertIn('max concurrent 1', out['cap_reason'])
        self.assertEqual((out['cpu_slots_used'], out['projected_bytes']), (4, (20 + 6 + 6) * GIB))
        # Only the cap differs: with it raised the same inputs would be admitted.
        self.assertTrue(decide(holders=[holder()], caps=dict(CAPS, max_concurrent=2))['would_admit'])

    def test_a_third_workload_does_not_fit_even_if_the_cap_were_2(self):
        out = decide(holders=[holder('a'), holder('b', excl=('cargo-target:/c',))])
        self.assertFalse(out['fits_if_cap_were_2'])

    def test_a_legacy_reservation_costs_the_whole_budget_and_all_cores(self):
        out = decide(holders=[holder('old', record={})])
        self.assertTrue(out['holders'][0]['legacy'])
        self.assertEqual((out['holders'][0]['charge_bytes'], out['holders'][0]['cpu_slots']), (out['limit_bytes'], 16))
        self.assertFalse(out['fits_if_cap_were_2'])
        self.assertTrue(any(r.startswith('memory:') for r in out['resource_reasons']))
        self.assertTrue(any(r.startswith('cpu:') for r in out['resource_reasons']))

    def test_a_legacy_request_needs_an_otherwise_free_host(self):
        free = decide(req=request(record={}))
        self.assertTrue(free['would_admit'])
        busy = decide(holders=[holder()], req=request(record={}))
        self.assertFalse(busy['fits_if_cap_were_2'])
        self.assertIn('legacy request needs the whole budget', busy['resource_reasons'][0])
        self.assertFalse(decide(h=host(workloads=[{'root_pid': 9, 'members': [9]}]), req=request(record={}))['fits_if_cap_were_2'])

    def test_excl_key_overlap_with_a_live_workload_refuses(self):
        out = decide(holders=[holder(excl=('cargo-target:/b',))])
        self.assertFalse(out['fits_if_cap_were_2'])
        self.assertEqual(out['resource_reasons'], ['excl_key overlap with a'])
        self.assertTrue(decide(holders=[holder(excl=('cargo-target:/other',))])['fits_if_cap_were_2'])

    def test_an_unclaimed_grant_holds_its_class_cost(self):
        out = decide(holders=[holder('grant', kind='granted')])
        self.assertEqual((out['count'], out['cpu_slots_used'], out['projected_bytes']), (1, 4, 32 * GIB))
        self.assertEqual(out['holders'][0]['measured_rss_bytes'], 0)
        self.assertEqual(out['holders'][0]['kind'], 'granted')

    def test_a_reservation_newer_than_the_sample_is_charged_in_full(self):
        rows = {100: (1, 4 * 1024 * 1024, 0.0)}            # 4 GiB resident, in KiB
        measured = decide(h=host(rows=rows), holders=[holder(pid=100, created=(SAMPLED - timedelta(minutes=5)).isoformat())])
        self.assertEqual((measured['holders'][0]['measured_rss_bytes'], measured['holders'][0]['charge_bytes']), (4 * GIB, 2 * GIB))
        self.assertTrue(measured['holders'][0]['counted_measurement'])
        for created in ((SAMPLED + timedelta(seconds=1)).isoformat(), SAMPLED.isoformat(), None):
            late = decide(h=host(rows=rows), holders=[holder(pid=100, created=created)])
            self.assertEqual((late['holders'][0]['measured_rss_bytes'], late['holders'][0]['charge_bytes']), (0, 6 * GIB), created)
            self.assertFalse(late['holders'][0]['counted_measurement'])

    def test_measured_usage_above_the_estimate_charges_nothing_extra(self):
        rows = {100: (1, 9 * 1024 * 1024, 0.0), 101: (100, 1024 * 1024, 0.0)}
        out = decide(h=host(rows=rows), holders=[holder(pid=100, created='2026-10-31T11:00:00Z')])
        self.assertEqual((out['holders'][0]['measured_rss_bytes'], out['holders'][0]['charge_bytes']), (10 * GIB, 0))

    def test_amber_halves_jobs_and_test_threads_and_uses_the_larger_headroom(self):
        amber = decide(h=host(band='AMBER'))
        self.assertEqual((amber['granted_jobs'], amber['granted_test_threads'], amber['headroom_percent']), (2, 2, 40))
        self.assertEqual(amber['limit_bytes'], RAM * 60 // 100)
        workspace = {'class': 'build-workspace', 'mem_bytes': 16 * GIB, 'cpu_slots': 8, 'test_threads': 8}
        self.assertEqual(decide(h=host(band='AMBER'), req=request(workspace))['granted_jobs'], 4)
        small = {'class': 'build-crate', 'mem_bytes': 6 * GIB, 'cpu_slots': 2, 'test_threads': 1}
        self.assertEqual(decide(h=host(band='AMBER'), req=request(small))['granted_jobs'], 2)   # min_jobs 2 holds
        green = decide()
        self.assertEqual((green['granted_jobs'], green['granted_test_threads'], green['headroom_percent']), (4, 4, 30))

    def test_red_refuses_every_request(self):
        out = decide(h=host(band='RED', band_reasons=['load per core 5.0 >= 4']))
        self.assertFalse(out['would_admit'] or out['fits_if_cap_were_2'])
        self.assertEqual(out['resource_reasons'], ['band RED: load per core 5.0 >= 4'])
        self.assertEqual((out['granted_jobs'], out['granted_test_threads']), (0, 0))

    def test_an_unmanaged_tree_adds_the_allowance_and_is_not_a_refusal(self):
        stray = {'root_pid': 900, 'members': [900, 901]}
        out = decide(h=host(workloads=[stray]))
        self.assertEqual((out['unmanaged_trees'], out['unmanaged_allowance_bytes']), (1, 8 * GIB))
        self.assertEqual(out['projected_bytes'], (20 + 8 + 6) * GIB)
        self.assertTrue(out['would_admit'])
        two = decide(h=host(workloads=[stray, {'root_pid': 910, 'members': [910]}]))
        self.assertEqual(two['projected_bytes'], (20 + 16 + 6) * GIB)

    def test_a_tree_inside_a_reservation_is_managed(self):
        rows = {100: (1, 1024, 0.0), 200: (100, 1024, 0.0)}
        out = decide(h=host(rows=rows, workloads=[{'root_pid': 200, 'members': [200]}]), holders=[holder(pid=100)])
        self.assertEqual(out['unmanaged_trees'], 0)

    def test_cpu_slots_sum_to_the_core_count(self):
        three = [holder(str(i), excl=('k%d' % i,)) for i in range(3)]
        fits = decide(holders=three, req=request(CRATE))                                   # 12 + 4 == 16
        self.assertEqual(fits['resource_reasons'], [])
        over = decide(holders=three, req=request(dict(CRATE, cpu_slots=5)))
        self.assertEqual(over['resource_reasons'], ['cpu: 12 slots in use + 5 requested > 16 cores'])

    def test_memory_headroom_boundaries_are_exact(self):
        limit = RAM * 70 // 100
        need = 6 * GIB
        self.assertTrue(decide(h=host(used=limit - need))['fits_if_cap_were_2'])           # projected == limit
        over = decide(h=host(used=limit - need + 1))                                     # one byte over
        self.assertFalse(over['fits_if_cap_were_2'])
        self.assertTrue(over['resource_reasons'][0].startswith('memory:'))
        amber_limit = RAM * 60 // 100
        self.assertTrue(decide(h=host(band='AMBER', used=amber_limit - need))['fits_if_cap_were_2'])
        self.assertFalse(decide(h=host(band='AMBER', used=amber_limit - need + 1))['fits_if_cap_were_2'])

    def test_the_byte_floor_is_unchanged_and_reported(self):
        out = decide(caps=dict(CAPS, free_bytes=119 * GIB))
        self.assertTrue(out['resource_reasons'][0].startswith('byte floor:'))
        self.assertFalse(decide(caps=dict(CAPS, free_bytes=120 * GIB - 1))['fits_if_cap_were_2'])
        self.assertTrue(decide(caps=dict(CAPS, free_bytes=120 * GIB))['fits_if_cap_were_2'])

    def test_no_heavy_lane_means_a_cap_of_zero_even_for_the_probe(self):
        out = decide(caps=dict(CAPS, max_concurrent=0))
        self.assertFalse(out['would_admit'] or out['fits_if_cap_were_2'])

    def test_the_load_is_corrected_for_admitted_work_before_banding(self):
        seen = []
        rows_prev = {100: (1, 1024, 10.0), 101: (100, 1024, 10.0)}
        rows = {100: (1, 1024, 14.0), 101: (100, 1024, 18.0)}         # 12 cpu-seconds in a 2 s window: 6 cores busy
        h = host(rows=rows, rows_prev=rows_prev, load1=10.0, window_s=2.0, band_fn=lambda lpc: (seen.append(lpc), ('GREEN', []))[1])
        decide(h=h, holders=[holder(pid=100, created='2026-10-31T11:00:00Z')])
        self.assertEqual(seen, [(10.0 - 4) / 16])                       # credited min(cpu_slots 4, measured 6)
        seen.clear()
        decide(h=host(load1=1.0, rows=rows, rows_prev=rows_prev, band_fn=lambda lpc: (seen.append(lpc), ('GREEN', []))[1]),
               holders=[holder(pid=100, created='2026-10-31T11:00:00Z')])
        self.assertEqual(seen, [0.0])                                    # never negative

    def test_state_only_call_reports_the_occupied_budget(self):
        out = da.budget_decision(host(), [holder()], None, CAPS)
        self.assertNotIn('would_admit', out)
        self.assertEqual((out['count'], out['projected_bytes']), (1, 26 * GIB))

    def test_a_missing_or_failed_host_sample_is_unavailable_never_a_guess(self):
        for bad in (None, {}, {'error': 'host-sample-unavailable: OSError'}, host(used=None)):
            out = da.budget_decision(bad, [holder()], request(), CAPS)
            self.assertEqual(out['status'], 'unavailable')
            self.assertEqual(out['shadow'], 'shadow: not enforced')
            self.assertNotIn('would_admit', out)

    def test_line_always_says_shadow_not_enforced(self):
        for out in (decide(), decide(holders=[holder()]), da.budget_decision(None, [], request(), CAPS),
                    da.budget_decision(host(), [], None, CAPS)):
            self.assertIn('shadow: not enforced', da.budget_line(out))

    def test_budget_holders_skips_the_claiming_grant_and_lapsed_grants(self):
        base = {'label': 'x', 'bytes': GIB, 'state': 'GRANTED', 'grant': {'expires': (SAMPLED + timedelta(hours=1)).isoformat()}}
        entries = [dict(base, id='q000001'), dict(base, id='q000002', label='y'),
                   dict(base, id='q000003', label='z', grant={'expires': (SAMPLED - timedelta(seconds=1)).isoformat()}),
                   dict(base, id='q000004', label='w', state='QUEUED', grant=None)]
        picked = da.budget_holders([], [], entries, SAMPLED, skip_id='q000001')
        self.assertEqual([h['label'] for h in picked], ['y'])


class Sites(QueueBase):
    def host_fn(self, **kw):
        self.calls = getattr(self, 'calls', 0)

        def fn():
            self.calls += 1
            return host(time=self.now + timedelta(seconds=1), **kw)
        return fn

    def reserve(self, label, proc, host_fn=None, declared=None, nbytes=20 * GIB):
        return da.reserve(label, nbytes, proc.pid, self.path, lambda: 400 * GIB, now=self.now, host_fn=host_fn, declared=declared)

    def spawn(self):
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        return proc

    def receipts(self):
        path = self.path.parent / 'queue/receipts.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_reserve_records_the_shadow_verdict_and_the_lane_cap_still_decides(self):
        first, second = self.spawn(), self.spawn()
        hf = self.host_fn()
        one = self.reserve('one', first, hf, declared=CRATE)
        self.assertTrue(one['admit'], one)
        self.assertEqual((one['budget']['shadow'], one['budget']['enforced'], one['budget']['site']),
                         ('shadow: not enforced', 'admit', 'reserve'))
        two = self.reserve('two', second, hf, declared=CRATE)
        plain = da.reserve('two', 20 * GIB, second.pid, self.path, lambda: 400 * GIB, now=self.now)
        self.assertFalse(two['admit'])
        self.assertTrue(two['reason'].startswith('lane-cap: 1 of 1 heavy lanes in use'))
        self.assertEqual(two['reason'], plain['reason'])
        self.assertEqual(two['budget']['enforced'], 'refuse')
        self.assertFalse(two['budget']['would_admit'])
        self.assertTrue(two['budget']['fits_if_cap_were_2'])
        self.assertEqual(two['budget']['count'], 1)

    def test_without_a_host_sample_the_result_has_no_budget_key(self):
        proc = self.spawn()
        self.assertNotIn('budget', self.reserve('one', proc))

    def test_a_red_budget_does_not_refuse_what_the_lane_check_admits(self):
        proc = self.spawn()
        out = self.reserve('one', proc, self.host_fn(band='RED', band_reasons=['memory pressure CRITICAL (level 4)']), declared=CRATE)
        self.assertTrue(out['admit'])
        self.assertFalse(out['budget']['would_admit'])
        self.assertIn('band RED', out['budget']['resource_reasons'][0])

    def test_a_failing_host_sample_changes_nothing(self):
        proc = self.spawn()

        def boom():
            raise OSError('ps exploded')
        out = self.reserve('one', proc, boom)
        self.assertTrue(out['admit'])
        self.assertEqual(out['budget']['status'], 'unavailable')
        self.assertNotIn('ps exploded', json.dumps(out['budget']))

    def test_over_capacity_refusal_keeps_its_reason_and_gains_the_budget(self):
        proc = self.spawn()
        out = da.reserve('big', 350 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, host_fn=self.host_fn())
        self.assertFalse(out['admit'])
        self.assertTrue(out['reason'].startswith('over-capacity'))
        self.assertEqual(out['budget']['enforced'], 'refuse')

    def test_advance_shows_the_budget_while_the_lane_stays_occupied(self):
        self.holder('busy')
        self.assertTrue(self.enqueue()['ok'])
        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, host_fn=self.host_fn())
        self.assertEqual(out['reason'], 'lane occupied')
        self.assertEqual(out['granted'], [])
        self.assertEqual((out['budget']['site'], out['budget']['enforced'], out['budget']['fits_if_cap_were_2']), ('advance', 'refuse', False))
        lines = [r for r in self.receipts() if r['event'] == 'shadow-budget']
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['budget']['shadow'], 'shadow: not enforced')
        self.assertNotIn('holders', lines[0]['budget'])

    def test_advance_records_a_would_admit_beside_a_free_lane_and_still_grants_as_before(self):
        self.assertTrue(self.enqueue()['ok'])
        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, host_fn=self.host_fn())
        self.assertEqual(len(out['granted']), 1)
        self.assertEqual(out['budget']['enforced'], 'admit')

    def test_advance_takes_no_host_sample_when_nothing_waits(self):
        hf = self.host_fn()
        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, host_fn=hf)
        self.assertNotIn('budget', out)
        self.assertEqual(self.calls, 0)

    def test_claim_records_the_budget_on_success_and_on_a_lane_cap_refusal(self):
        self.assertTrue(self.enqueue()['ok'])
        qid = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)['granted'][0]
        entry = self.entries()[qid]
        # An occupying reservation written directly (a direct reserve is blocked while a grant waits).
        blocker = self.spawn()
        with da.state_lock(self.path):
            da.write_reservation(self.path, 'blocker', 10 * GIB, blocker.pid, da.process_token(blocker.pid),
                                 self.now + timedelta(hours=1))
        refused = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ARGV, str(self.worktree), lambda: 400 * GIB,
                                 pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                                 host_fn=self.host_fn())
        self.assertFalse(refused['ok'])
        self.assertTrue(refused['reason'].startswith('lane-cap'))
        self.assertEqual((refused['budget']['site'], refused['budget']['enforced']), ('claim', 'refuse'))
        self.assertEqual(refused['budget']['count'], 1)                  # the grant itself does not count against itself

    def test_claim_success_carries_the_budget(self):
        self.assertTrue(self.enqueue()['ok'])
        qid = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)['granted'][0]
        entry = self.entries()[qid]
        out = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ARGV, str(self.worktree), lambda: 400 * GIB,
                             pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None,
                             host_fn=self.host_fn())
        self.assertTrue(out['ok'], out)
        self.assertEqual((out['budget']['enforced'], out['budget']['count']), ('admit', 0))
        self.assertEqual(self.entries()[qid]['state'], 'CLAIMED')

    def test_snapshot_gains_a_budget_section_only_with_a_host_sample(self):
        plain = da.snapshot(self.path, lambda: (400 * GIB, 'test'), now=self.now)
        self.assertNotIn('budget', plain)
        proc = self.spawn()
        self.assertTrue(self.reserve('job', proc, declared=CRATE)['admit'])
        shot = da.snapshot(self.path, lambda: (400 * GIB, 'test'), now=self.now, host_fn=self.host_fn())
        budget = shot['budget']
        self.assertEqual((budget['shadow'], budget['count'], budget['band']), ('shadow: not enforced', 1, 'GREEN'))
        self.assertEqual(budget['reference']['class'], 'build-crate')
        self.assertFalse(budget['reference']['would_admit'])
        self.assertTrue(budget['reference']['fits_if_cap_were_2'])
        self.assertEqual(da.read_snapshot(self.path)['budget']['count'], 1)
        self.assertEqual(shot['active_heavy_jobs'], 1)               # the enforced numbers are the old ones

    def test_status_budget_section_and_default_main_without_a_host(self):
        proc = self.spawn()
        self.assertTrue(self.reserve('job', proc, declared=CRATE)['admit'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            da.main(['status', '--json'], free_fn=lambda: 400 * GIB, state_path=self.path)
        self.assertNotIn('budget', json.loads(out.getvalue()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            da.main(['status', '--json'], free_fn=lambda: 400 * GIB, state_path=self.path, host_fn=self.host_fn())
        shown = json.loads(out.getvalue())
        self.assertEqual((shown['budget']['shadow'], shown['budget']['count']), ('shadow: not enforced', 1))
        self.assertEqual(shown['tier'], 'wide')

    def test_reserve_cli_prints_the_shadow_line_to_a_terminal_only(self):
        proc = self.spawn()
        err = io.StringIO()
        err.isatty = lambda: True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = da.main(['reserve', '--bytes', str(20 * GIB), '--pid', str(proc.pid), '--label', 'cli'], free_fn=lambda: 400 * GIB,
                         state_path=self.path, host_fn=self.host_fn())
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith('reserved '))
        self.assertIn('shadow: not enforced', err.getvalue())

    def test_no_new_state_files_are_written_by_the_shadow_budget(self):
        proc = self.spawn()
        self.assertTrue(self.reserve('job', proc, self.host_fn(), declared=CRATE)['admit'])
        names = sorted(p.name for p in self.path.parent.rglob('*') if p.is_file())
        self.assertEqual(names, sorted(['state.json', 'state.json.lock', 'job.json', 'measurements.jsonl']))


class HostSample(unittest.TestCase):
    def sample(self, t, load1=1.0):
        return {'time': t, 'mono': t, 'ram': RAM, 'pressure_level': 1, 'swap': {'total': 0, 'used': 0, 'free': 0},
                'vm': {'page_size': 16384, 'free': 1, 'active': 1000, 'wired': 500, 'compressor': 100, 'swapins': 0, 'swapouts': 0},
                'load1': load1, 'cpus': 16, 'disk_free': 400 * GIB, 'heavy_procs': {}, 'idle_procs': [], 'cpu_by_pid': {},
                'active_pids': [], 'heavy_workloads': [{'root_pid': 7, 'root_comm': 'cargo', 'procs': 1, 'rss_bytes': 5, 'cpu_s': 1.0,
                                                        'idle': False, 'member_cpu': {7: 1.0}}]}

    def test_read_host_sample_uses_two_windowed_samples_and_never_runs_ps_itself(self):
        times, slept, rows = iter([1000.0, 1002.0]), [], iter([{1: (0, 1, 1.0)}, {1: (0, 1, 3.0)}])
        out = da.read_host_sample(window_s=2.0, sleep=slept.append, sample_fn=lambda: self.sample(next(times)), rows_fn=lambda: next(rows))
        self.assertEqual(slept, [2.0])
        self.assertEqual((out['ram'], out['used'], out['cores'], out['window_s']), (RAM, (1000 + 500 + 100) * 16384, 16, 2.0))
        self.assertEqual(out['rows_prev'][1][2], 1.0)
        self.assertEqual(out['rows'][1][2], 3.0)
        self.assertEqual([w['root_pid'] for w in out['workloads']], [7])
        self.assertEqual(out['time'], datetime.fromtimestamp(1002.0, timezone.utc))
        self.assertEqual(out['band_fn'](None)[0], 'GREEN')
        self.assertEqual(out['band_fn'](4.0)[0], 'RED')
        self.assertEqual(da.budget_decision(out, [], request(), CAPS)['status'], 'ok')

    def test_failures_become_an_error_record_not_an_exception(self):
        def boom():
            raise OSError('nope')
        self.assertIn('error', da.read_host_sample(sleep=lambda s: None, sample_fn=boom, rows_fn=lambda: {}))
        times = iter([1000.0, 1002.0])
        out = da.read_host_sample(sleep=lambda s: None, sample_fn=lambda: self.sample(next(times)), rows_fn=lambda: None)
        self.assertEqual(out, {'error': 'process table unreadable'})


class Guarded(QueueBase):
    """A malformed record or sample inside the budget code must leave every admission outcome exactly as without it."""

    def spawn(self):
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        return proc

    def bad_host(self, pid=None):
        rows = {pid: 'not-a-tuple'} if pid else {}
        return lambda: host(rows=rows, ram='not-a-number', time=self.now + timedelta(seconds=1))

    def assert_unavailable(self, budget):
        self.assertEqual((budget['status'], budget['shadow']), ('unavailable', 'shadow: not enforced'))
        self.assertIn('budget-computation-failed', budget['reason'])

    def test_reserve_admit_and_refuse_are_identical_with_a_malformed_sample(self):
        first, second = self.spawn(), self.spawn()
        args = (20 * GIB, first.pid, self.path, lambda: 400 * GIB)
        admitted = da.reserve('one', *args, now=self.now, host_fn=self.bad_host(first.pid), declared=CRATE)
        self.assertTrue(admitted['admit'], admitted)
        self.assert_unavailable(admitted['budget'])
        plain = da.reserve('two', 20 * GIB, second.pid, self.path, lambda: 400 * GIB, now=self.now)
        guarded = da.reserve('two', 20 * GIB, second.pid, self.path, lambda: 400 * GIB, now=self.now, host_fn=self.bad_host(first.pid))
        self.assertFalse(guarded['admit'])
        self.assertEqual({k: v for k, v in guarded.items() if k != 'budget'}, plain)
        self.assert_unavailable(guarded['budget'])

    def test_advance_decides_the_same_with_a_malformed_sample(self):
        busy = self.holder('busy')
        self.assertTrue(self.enqueue()['ok'])
        out = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now, host_fn=self.bad_host(busy.pid))
        self.assertEqual((out['reason'], out['granted']), ('lane occupied', []))
        self.assert_unavailable(out['budget'])
        self.assertEqual([r for r in (self.path.parent / 'queue/receipts.jsonl').read_text().splitlines() if 'shadow-budget' in r], [])

    def test_claim_decides_the_same_with_a_malformed_sample(self):
        self.assertTrue(self.enqueue()['ok'])
        qid = da.queue_advance(self.path, lambda: 400 * GIB, now=self.now)['granted'][0]
        entry = self.entries()[qid]
        blocker = self.spawn()
        with da.state_lock(self.path):
            da.write_reservation(self.path, 'blocker', 10 * GIB, blocker.pid, da.process_token(blocker.pid), self.now + timedelta(hours=1))
        kw = dict(pid=os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'), exec_fn=lambda p, a: None)
        refused = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ARGV, str(self.worktree), lambda: 400 * GIB,
                                 host_fn=self.bad_host(blocker.pid), **kw)
        self.assertFalse(refused['ok'])
        self.assertTrue(refused['reason'].startswith('lane-cap'))
        self.assert_unavailable(refused['budget'])
        with da.state_lock(self.path):
            (self.path.parent / 'reservations/blocker.json').unlink()
        ok = da.queue_claim(self.path, qid, entry['label'], entry['bytes'], ARGV, str(self.worktree), lambda: 400 * GIB,
                            host_fn=self.bad_host(), **kw)
        self.assertTrue(ok['ok'], ok)
        self.assert_unavailable(ok['budget'])

    def test_malformed_holder_records_are_guarded_at_the_builder(self):
        for bad in (lambda: ([{'label': 'x'}], request()), lambda: ([da.budget_holder('orphan', {'label': 'x'}, None, 5, {'members': [{}]})], None)):
            self.assert_unavailable(da._shadow_site(host(), 'reserve', 'x', bad, 'wide', 400 * GIB, 0, True))
        self.assertEqual(da._shadow_site(host(), 'reserve', 'x', lambda: ([], request()), 'wide', 400 * GIB, 0, True)['status'], 'ok')

    def test_state_budget_with_a_malformed_record_is_unavailable_not_an_error(self):
        out = da._state_budget(host(), self.path, self.now, 'wide', 400 * GIB, [{'bytes': 1}], [], [])
        self.assert_unavailable(out)

    def test_base_exceptions_are_not_swallowed(self):
        def interrupt():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            da._shadow_site(host(), 'reserve', 'x', interrupt, 'wide', 400 * GIB, 0, True)


class Deadline(unittest.TestCase):
    def test_a_slow_sampler_marks_the_sample_unavailable_within_the_deadline(self):
        import time
        started = time.monotonic()
        out = da.read_host_sample(window_s=0, sleep=lambda s: None, sample_fn=lambda: time.sleep(2) or {}, rows_fn=lambda: {},
                                  deadline_s=0.2)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn('host-sample-timeout', out['error'])
        self.assertEqual(da.budget_decision(out, [], request(), CAPS)['status'], 'unavailable')

    def test_the_default_deadline_covers_the_window(self):
        self.assertGreater(da.SHADOW_DEADLINE_S, da.SHADOW_WINDOW_S)


class Unchanged(unittest.TestCase):
    def test_the_lane_caps_and_concurrency_constants(self):
        self.assertEqual(da.HEAVY_LANES, {'emergency': 0, 'low': 0, 'stable': 1, 'wide': 1})
        self.assertEqual(da.MAX_CONCURRENT, da.HEAVY_LANES)
        self.assertEqual(da.UNMANAGED_ALLOWANCE, 8 * GIB)
        self.assertEqual(da.HEADROOM_PERCENT, {'GREEN': 30, 'AMBER': 40})


if __name__ == '__main__':
    unittest.main()
