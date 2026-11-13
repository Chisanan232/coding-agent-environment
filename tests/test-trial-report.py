#!/usr/bin/env python3
"""Concurrency trial report (SPE-160): read-only evidence for a third heavy lane; missing data is never a pass."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_admission', ROOT / 'scripts/disk-admission.py')
da = module_from_spec(spec)
spec.loader.exec_module(da)
GIB = 1024 ** 3
T0 = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
CLASSES = ('build-crate', 'build-workspace')
RUN = 2000                                                             # solo run seconds; 20 paired jobs then overlap 6.1 h
SOLO_WAIT = 1100                                                       # so a 1.5x slowdown is exactly no queue-to-completion gain


def iso(moment):
    return moment.isoformat().replace('+00:00', 'Z')


class Trial(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'
        (self.path.parent / 'queue').mkdir(parents=True)
        self.seq = 0
        self.cursor = T0

    def job(self, cls, created, wait_s, run_s, overlapped, pressure=1, release=True, swap=None, free=300 * GIB):
        """One queue entry plus its measurement lines (two samples; `concurrent` 2 when overlapped)."""
        self.seq += 1
        qid = 'q%06d' % self.seq
        claimed = created + timedelta(seconds=wait_s)
        entry = {'version': 1, 'id': qid, 'seq': self.seq, 'label': 'job-%d' % self.seq, 'state': 'RELEASED' if release else 'CLAIMED',
                 'tickets': ['SPE-160'], 'repo': 'r', 'worktree': '/w', 'harness': 'codex', 'owner_uid': os.getuid(),
                 'bytes': 20 * GIB, 'fingerprint': 'f' * 64, 'created': iso(created), 'wait_expires': iso(created + timedelta(days=1)),
                 'grant': {'at': iso(claimed - timedelta(seconds=5)), 'expires': iso(claimed + timedelta(hours=1))},
                 'holder': {'pid': 7, 'start_token': 't', 'claimed_at': iso(claimed)}, 'class': cls}
        if release:
            entry['release'] = {'at': iso(claimed + timedelta(seconds=run_s)), 'evidence': 'x', 'holder_alive': False}
        da.write_json_atomic(self.path.parent / 'queue' / (qid + '.json'), entry)
        for offset in (0.0, run_s - 30):
            da._append_measurement(self.path, {'label': entry['label'], 'pid': 7, 'start_token': 't', 'v': 1, 'event': 'advance',
                                               'ts': iso(claimed + timedelta(seconds=offset)), 'queue_id': qid, 'rss_bytes': GIB,
                                               'concurrent': 2 if overlapped else 1, 'pressure': pressure, 'free_bytes': free,
                                               'swap_used_bytes': swap})
        return qid

    def receipt(self, fits, when=None, qid=None, count=2, band='GREEN', reasons=(), aware=None):
        self.seq += 1
        path = self.path.parent / 'queue/receipts.jsonl'
        line = {'ts': iso(when or T0 + timedelta(hours=1)), 'event': 'shadow-budget', 'id': qid or 'r%06d' % self.seq, 'label': 'x',
                'tickets': [], 'budget': {'status': 'ok', 'fits_if_cap_were_3': fits, 'count': count, 'band': band,
                           'resource_reasons': list(reasons)}}
        if aware is not None:   # (class-aware eligible, fits as class-aware)
            line['budget'].update(class_aware_eligible=aware[0], fits_if_class_aware_3=aware[1])
        with open(path, 'a') as handle:
            handle.write(json.dumps(line) + '\n')

    def scenario(self, slow=1.1, overlapped_per_class=10, pressure=1, fits=(True,) * 9 + (False,) * 3, swap_growth=GIB, free=300 * GIB):
        """5 solo jobs per class (run RUN s, wait SOLO_WAIT s) then overlapped pairs (run RUN * slow, wait 100 s) that really run together."""
        at = T0
        for cls in CLASSES:
            for _ in range(5):
                self.job(cls, at, SOLO_WAIT, RUN, False, swap=10 * GIB, free=free)
                at += timedelta(hours=1)
        for cls in CLASSES:
            for n in range(overlapped_per_class // 2):
                for _ in range(2):
                    self.job(cls, at, 100, RUN * slow, True, pressure=pressure, swap=10 * GIB + swap_growth, free=free)
                at += timedelta(hours=1)
        for value in fits:
            self.receipt(value)

    def report(self, **kw):
        return da.trial_report(self.path, T0 - timedelta(hours=1), T0 + timedelta(days=2), now=T0 + timedelta(days=2), **kw)

    def criteria(self, report):
        return {c['name']: c for c in report['criteria']}

    def test_a_clean_trial_passes_with_the_figures_the_fixture_implies(self):
        self.scenario()
        report = self.report()
        self.assertEqual(report['verdict'], 'PASS', report['criteria'])
        self.assertEqual((report['overlapped_jobs'], report['solo_baseline_jobs'], report['peak_concurrent'], report['peak_pressure']),
                         (20, 10, 2, 1))
        crate = report['classes']['build-crate']
        self.assertEqual((crate['solo_runs'], crate['overlapped_runs'], round(crate['slowdown'], 2)), (5, 10, 1.1))
        crit = self.criteria(report)
        self.assertEqual(crit['median_queue_to_completion_ratio']['value'], round((100 + RUN * 1.1) / (SOLO_WAIT + RUN), 3))
        self.assertEqual(crit['swap_growth_bytes']['value'], GIB)
        self.assertEqual(crit['min_free_bytes']['value'], 260 * GIB)      # 300 free minus two 20 GiB reservations
        self.assertEqual(crit['fits_if_cap_were_3_share']['value'], 0.75)
        self.assertEqual(crit['pressure_samples_above_1_during_overlap']['value'], 0)
        self.assertGreater(crate['jobs_per_busy_hour'], 0)
        row = next(j for j in report['jobs'] if j['overlapped'])
        self.assertEqual((row['wait_s'], row['run_s_upper_bound'], row['queue_to_completion_s_upper_bound']), (100, RUN * 1.1, 100 + RUN * 1.1))
        self.assertEqual(row['run_s_lower_bound'], RUN * 1.1 - 30)                    # last sample is 30 s before the recorded release
        json.dumps(report)

    def test_each_threshold_can_fail_the_trial_alone(self):
        for label, kwargs, name in (('slow', dict(slow=1.7), 'worst_class_slowdown'),
                                    ('pressure', dict(pressure=2), 'pressure_samples_above_1_during_overlap'),
                                    ('swap', dict(swap_growth=3 * GIB), 'swap_growth_bytes'),
                                    ('disk', dict(free=150 * GIB), 'min_free_bytes'),
                                    ('fits', dict(fits=(True,) + (False,) * 11), 'fits_if_cap_were_3_share')):
            self.setUp()
            self.scenario(**kwargs)
            report = self.report()
            self.assertEqual(self.criteria(report)[name]['verdict'], 'FAIL', label)
            self.assertEqual(report['verdict'], 'FAIL', label)

    def test_no_queue_to_completion_gain_fails(self):
        self.scenario(slow=1.5)                                              # q2c 100 + 3000 == 1100 + 2000: no gain
        crit = self.criteria(self.report())
        self.assertEqual(crit['median_queue_to_completion_ratio']['verdict'], 'FAIL')

    def test_too_few_overlapped_jobs_or_classes_is_not_a_pass(self):
        self.scenario(overlapped_per_class=4)                                # 8 paired jobs < 20
        report = self.report()
        self.assertEqual(self.criteria(report)['overlapped_jobs']['verdict'], 'insufficient')
        self.assertEqual(report['verdict'], 'INSUFFICIENT')
        self.setUp()
        at = T0
        for _ in range(3):
            self.job('build-crate', at, 600, 1000, False, swap=GIB)
            at += timedelta(hours=1)
        for _ in range(12):
            self.job('build-crate', at, 100, 1100, True, swap=GIB)
            at += timedelta(hours=1)
        self.assertEqual(self.criteria(self.report())['overlapped_classes']['verdict'], 'insufficient')

    def test_per_class_minimum_and_unclassified_jobs(self):
        self.scenario(overlapped_per_class=8)                                # 16 jobs, 8 per class: still < 20 in total
        self.assertEqual(self.criteria(self.report())['overlapped_jobs']['verdict'], 'insufficient')
        self.setUp()
        at = T0
        for cls, pairs in (('build-crate', 8), ('build-workspace', 2)):      # 16 + 4 jobs: 20 in total, one class below 5
            for _ in range(pairs):
                for _ in range(2):
                    self.job(cls, at, 100, RUN, True)
                at += timedelta(hours=1)
        crit = self.criteria(self.report())
        self.assertEqual((crit['overlapped_jobs']['verdict'], crit['overlapped_classes']['verdict']), ('PASS', 'insufficient'))
        self.setUp()
        for n in range(10):
            for _ in range(2):
                self.job('unclassified', T0 + timedelta(hours=n), 100, RUN, True)
        self.assertEqual(self.criteria(self.report())['overlapped_classes']['verdict'], 'insufficient')

    def test_five_hours_of_overlap_are_required(self):
        at = T0
        for cls in CLASSES:
            for _ in range(5):
                for _ in range(2):
                    self.job(cls, at, 100, 600, True)                       # 10 pairs of 10 min = 1.7 h
                at += timedelta(hours=1)
        crit = self.criteria(self.report())
        self.assertEqual((crit['overlapped_jobs']['verdict'], crit['overlapped_classes']['verdict']), ('PASS', 'PASS'))
        self.assertEqual(crit['overlap_hours']['verdict'], 'insufficient')
        self.assertLess(crit['overlap_hours']['value'], 5)

    def test_disk_is_judged_on_overlap_samples_net_of_reservations_and_the_window_minimum_is_only_reported(self):
        self.scenario()
        self.job('build-crate', T0 - timedelta(minutes=30), 10, 1200, False, free=149 * GIB)   # one job, no overlap, low disk
        report = self.report()
        self.assertEqual(self.criteria(report)['min_free_bytes']['verdict'], 'PASS')
        self.assertEqual((report['window_min_free_bytes'], report['verdict']), (149 * GIB, 'PASS'))
        self.assertIn('window minimum free disk 149.0 GiB', da.format_trial_report(report))
        self.setUp()
        self.scenario(free=190 * GIB)                                        # 190 - 2 x 20 reserved = 150 < 160 although 190 >= 160
        crit = self.criteria(self.report())['min_free_bytes']
        self.assertEqual((crit['verdict'], crit['value']), ('FAIL', 150 * GIB))

    def test_disk_margin_needs_every_overlap_sample_evaluable(self):
        self.scenario()
        orphan = self.job('build-crate', T0 + timedelta(days=1, hours=2), 100, RUN, True)   # an overlap sample whose job has no bytes figure
        path = self.path.parent / 'queue' / (orphan + '.json')
        entry = json.loads(path.read_text())
        entry['bytes'] = None
        path.write_text(json.dumps(entry))
        self.assertEqual(self.criteria(self.report())['min_free_bytes']['verdict'], 'insufficient')

    def test_a_class_outside_the_workload_classes_is_not_a_declared_class(self):
        for n in range(10):
            for _ in range(2):
                self.job('made-up-class', T0 + timedelta(hours=n), 100, RUN, True)
        self.assertEqual(self.criteria(self.report())['overlapped_classes']['verdict'], 'insufficient')

    def test_disk_without_a_reserved_figure_or_overlap_sample_is_insufficient(self):
        self.job('build-crate', T0, 100, RUN, False)                         # a solo sample only
        self.assertEqual(self.criteria(self.report())['min_free_bytes']['verdict'], 'insufficient')

    def test_fits_share_counts_only_verdicts_made_with_a_job_running_once_per_entry_and_situation(self):
        self.receipt(True, count=0)                                          # an empty host trivially fits: not evidence
        for _ in range(125):                                                 # a polled entry repeats one verdict
            self.receipt(True, qid='q000900')
        self.receipt(False, qid='q000901')
        self.receipt(False, qid='q000901', count=3)                          # same entry, a different situation (3 running: not a third-lane question)
        self.receipt(True, qid='q000902', count=1)                           # one running: fits() is count + 1 <= limit, trivially true for a cap of 2
        self.receipt(True, qid='q000900', band='AMBER')
        for n in range(9):                                                   # enough distinct verdicts to judge a share at all
            self.receipt(n < 6, qid='q001%03d' % n)
        crit = self.criteria(self.report())['fits_if_cap_were_3_share']
        self.assertEqual((crit['value'], crit['verdict']), (0.667, 'PASS'))  # 8 of 12 distinct count==2 (entry, situation) verdicts
        self.setUp()
        self.receipt(True, count=0)
        self.assertEqual(self.criteria(self.report())['fits_if_cap_were_3_share']['verdict'], 'insufficient')

    def test_class_aware_policy_judges_only_eligible_verdicts_that_carry_the_field(self):
        self.receipt(True, qid='q003000')                                    # an old receipt with no class-aware field: ignored
        for n in range(6):                                                   # eligible, fits
            self.receipt(False, qid='q003%03d' % n, aware=(True, True))
        for n in range(4):                                                   # eligible, refused
            self.receipt(False, qid='q004%03d' % n, aware=(True, False), reasons=['memory: projected 91.0 GiB > 89.6 GiB'])
        for n in range(5):                                                   # another class: set aside, not counted
            self.receipt(True, qid='q005%03d' % n, aware=(False, False))
        crit = self.criteria(self.report(policy='class-aware'))
        self.assertNotIn('fits_if_cap_were_3_share', crit)
        share = crit['fits_if_class_aware_3_share']
        self.assertEqual((share['value'], share['verdict']), (0.6, 'PASS'))
        self.assertIn('5 verdicts of other classes set aside, 10 counted', share['needed'])
        self.assertEqual(self.criteria(self.report())['fits_if_cap_were_3_share']['value'], 0.375)   # the default policy still counts every verdict: 6 of 16

    def test_class_aware_policy_needs_enough_eligible_verdicts(self):
        for n in range(9):
            self.receipt(True, qid='q006%03d' % n, aware=(True, True))
        self.assertEqual(self.criteria(self.report(policy='class-aware'))['fits_if_class_aware_3_share']['verdict'], 'insufficient')

    def test_class_aware_policy_can_fail(self):
        for n in range(10):
            self.receipt(True, qid='q007%03d' % n, aware=(True, n < 3))
        crit = self.criteria(self.report(policy='class-aware'))['fits_if_class_aware_3_share']
        self.assertEqual((crit['value'], crit['verdict']), (0.3, 'FAIL'))

    def test_the_cli_takes_the_policy(self):
        self.scenario()
        for n in range(10):
            self.receipt(True, qid='q008%03d' % n, aware=(True, True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            da.run(['queue', 'trial-report', '--since', iso(T0 - timedelta(hours=1)), '--until', iso(T0 + timedelta(days=2)), '--json',
                    '--policy', 'class-aware'], state_path=self.path, free_fn=lambda: 400 * GIB)
        report = json.loads(out.getvalue())
        self.assertEqual(report['policy'], 'class-aware')
        self.assertIn('fits_if_class_aware_3_share', {c['name'] for c in report['criteria']})


    def test_missing_data_is_insufficient_never_pass(self):
        empty = self.report()
        self.assertEqual(empty['verdict'], 'INSUFFICIENT')
        self.assertEqual({c['verdict'] for c in empty['criteria']}, {'insufficient'})
        self.setUp()
        self.scenario(pressure=None, swap_growth=0, fits=())                  # no pressure readings, no verdict lines
        report = self.report()
        crit = self.criteria(report)
        self.assertEqual(crit['pressure_samples_above_1_during_overlap']['verdict'], 'insufficient')
        self.assertEqual(crit['fits_if_cap_were_3_share']['verdict'], 'insufficient')
        self.assertEqual(report['verdict'], 'INSUFFICIENT')

    def test_repolling_one_waiting_entry_counts_once_whatever_the_live_figures(self):
        for gib in (93.1, 95.8, 99.0, 96.4):                                 # same entry, same refusal kind, a different live figure each poll
            self.receipt(False, qid='q002000', reasons=['memory: projected %.1f GiB > 89.6 GiB (RAM x (1 - 30%%))' % gib])
        self.receipt(False, qid='q002000', reasons=['memory: projected 97.0 GiB > 89.6 GiB', 'cpu: 12 slots in use + 8 requested > 16 cores'])
        for n in range(9):
            self.receipt(True, qid='q002%03d' % (n + 1))
        crit = self.criteria(self.report())['fits_if_cap_were_3_share']
        self.assertEqual((crit['value'], crit['verdict']), (0.818, 'PASS'))  # 9 fits of 11: one memory verdict and one memory+cpu verdict

    def test_a_repolled_failure_cannot_drown_distinct_entries(self):
        for gib in range(90, 110):
            self.receipt(False, qid='q003000', reasons=['memory: projected %d.0 GiB > 89.6 GiB' % gib])
        for n in range(6):
            self.receipt(True, qid='q003%03d' % (n + 1))
        crit = self.criteria(self.report())['fits_if_cap_were_3_share']
        self.assertEqual(crit['verdict'], 'insufficient')                    # 7 distinct verdicts, below min_verdicts

    def test_an_unfinished_job_is_listed_but_not_counted(self):
        self.job('build-crate', T0, 100, 3600, True, release=False)
        report = self.report()
        self.assertEqual((len(report['jobs']), report['overlapped_jobs'], report['jobs'][0]['finished']), (1, 0, False))

    def test_jobs_outside_the_window_are_ignored(self):
        self.scenario()
        report = da.trial_report(self.path, T0 + timedelta(days=30), now=T0 + timedelta(days=31))
        self.assertEqual((report['jobs'], report['verdict']), ([], 'INSUFFICIENT'))

    def test_a_separate_baseline_window_supplies_the_solo_runs(self):
        at = T0
        for cls in CLASSES:
            for _ in range(5):
                self.job(cls, at, 600, 1000, False, swap=GIB)
                at += timedelta(hours=1)
        trial_from = at
        for cls in CLASSES:
            for _ in range(6):
                self.job(cls, at, 100, 1100, True, swap=GIB)
                at += timedelta(hours=1)
        report = da.trial_report(self.path, trial_from, at, baseline=(T0, trial_from - timedelta(seconds=1)), now=at)
        self.assertEqual((report['overlapped_jobs'], report['solo_baseline_jobs']), (12, 10))
        self.assertEqual(round(report['classes']['build-workspace']['slowdown'], 2), 1.1)

    def test_one_concurrent_sample_in_a_long_job_is_not_an_overlap(self):
        # q000419 in the 2026-11-12 trial: a 58-minute job with 1 of ~315 samples at concurrent 2 was counted as overlapped.
        long_id = self.job('build-crate', T0, 100, 4000, False)
        entry = json.loads((self.path.parent / 'queue' / (long_id + '.json')).read_text())
        for offset in range(60, 3900, 60):
            da._append_measurement(self.path, {'label': entry['label'], 'pid': 7, 'start_token': 't', 'v': 1, 'event': 'advance',
                                               'ts': iso(T0 + timedelta(seconds=100 + offset)), 'queue_id': long_id, 'rss_bytes': GIB,
                                               'concurrent': 2 if offset == 600 else 1, 'pressure': 1, 'free_bytes': 300 * GIB})
        short = self.job('build-crate', T0 + timedelta(seconds=700), 0, 200, True)
        rows = {j['id']: j['overlapped'] for j in self.report()['jobs']}
        self.assertEqual((rows[long_id], rows[short]), (False, True))

    def test_a_comparison_on_a_handful_of_runs_is_insufficient_not_a_fail(self):
        # 2 overlapped jobs, 1 per class, a 100x slowdown against a class baseline of short runs: not a verdict about a lane.
        for _ in range(5):
            self.job('container-suite', T0, 100, 30, False)
        self.job('container-suite', T0 + timedelta(hours=1), 100, 3000, True)
        report = self.report()
        crit = self.criteria(report)
        self.assertEqual(crit['worst_class_slowdown']['verdict'], 'insufficient')
        self.assertEqual(crit['median_queue_to_completion_ratio']['verdict'], 'insufficient')
        self.assertNotEqual(report['verdict'], 'FAIL')

    def test_a_share_on_fewer_than_ten_verdicts_is_insufficient(self):
        for n in range(9):
            self.receipt(False, qid='q003%03d' % n)
        self.assertEqual(self.criteria(self.report())['fits_if_cap_were_3_share']['verdict'], 'insufficient')
        self.receipt(False, qid='q003999')                                   # the tenth distinct verdict is enough to judge
        self.assertEqual(self.criteria(self.report())['fits_if_cap_were_3_share']['verdict'], 'FAIL')

    def test_pressure_in_a_briefly_overlapped_job_still_fails_the_trial(self):
        # A job with 40% of its samples at concurrent 2 is not "overlapped", but a pressure-2 sample inside it is still an overlap sample.
        qid = self.job('build-crate', T0, 100, 4000, False)
        entry = json.loads((self.path.parent / 'queue' / (qid + '.json')).read_text())
        for index, offset in enumerate(range(60, 600, 60)):
            da._append_measurement(self.path, {'label': entry['label'], 'pid': 7, 'start_token': 't', 'v': 1, 'event': 'advance',
                                               'ts': iso(T0 + timedelta(seconds=100 + offset)), 'queue_id': qid, 'rss_bytes': GIB,
                                               'concurrent': 2 if index < 4 else 1, 'pressure': 2 if index == 0 else 1,
                                               'free_bytes': 300 * GIB})
        report = self.report()
        self.assertFalse(next(j for j in report['jobs'] if j['id'] == qid)['overlapped'])
        self.assertEqual(self.criteria(report)['pressure_samples_above_1_during_overlap']['verdict'], 'FAIL')

    def test_a_partly_overlapped_job_does_not_join_the_solo_baseline(self):
        for _ in range(5):
            self.job('build-crate', T0, 100, 1000, False)
        qid = self.job('build-crate', T0 + timedelta(hours=1), 100, 5000, False)
        entry = json.loads((self.path.parent / 'queue' / (qid + '.json')).read_text())
        for index, offset in enumerate(range(60, 600, 60)):
            da._append_measurement(self.path, {'label': entry['label'], 'pid': 7, 'start_token': 't', 'v': 1, 'event': 'advance',
                                               'ts': iso(T0 + timedelta(hours=1, seconds=100 + offset)), 'queue_id': qid, 'rss_bytes': GIB,
                                               'concurrent': 2 if index == 0 else 1, 'pressure': 1, 'free_bytes': 300 * GIB})
        self.assertEqual(self.report()['solo_baseline_jobs'], 5)

    def test_it_writes_nothing(self):
        self.scenario()

        def snapshot():
            return sorted((str(p.relative_to(self.path.parent)), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in self.path.parent.rglob('*') if p.is_file())

        before = snapshot()
        report = self.report()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = da.run(['queue', 'trial-report', '--since', iso(T0 - timedelta(hours=1)), '--until', iso(T0 + timedelta(days=2)), '--json'],
                        state_path=self.path, free_fn=lambda: 400 * GIB)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())['verdict'], report['verdict'])
        self.assertEqual(snapshot(), before)
        self.assertFalse((self.path.parent / 'state.json').exists())      # not even the state file or lock was created

    def test_cli_text_output_and_bad_times(self):
        self.scenario()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = da.run(['queue', 'trial-report', '--since', iso(T0 - timedelta(hours=1)), '--until', iso(T0 + timedelta(days=2))],
                        state_path=self.path, free_fn=lambda: 400 * GIB)
        self.assertEqual(rc, 0)
        self.assertIn('verdict: PASS', out.getvalue())
        for argv in (['--since', 'yesterday'], ['--since', '2026-11-09T08:00:00'], ['--since', iso(T0), '--baseline-since', iso(T0)]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = da.run(['queue', 'trial-report'] + argv, state_path=self.path, free_fn=lambda: 400 * GIB)
            self.assertEqual(rc, da.REFUSED_EXIT, argv)
            self.assertFalse(json.loads(out.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
