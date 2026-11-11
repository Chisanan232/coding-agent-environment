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

    def receipt(self, fits, when=None, qid=None, count=1, band='GREEN'):
        self.seq += 1
        path = self.path.parent / 'queue/receipts.jsonl'
        line = {'ts': iso(when or T0 + timedelta(hours=1)), 'event': 'shadow-budget', 'id': qid or 'r%06d' % self.seq, 'label': 'x',
                'tickets': [], 'budget': {'status': 'ok', 'fits_if_cap_were_3': fits, 'count': count, 'band': band}}
        with open(path, 'a') as handle:
            handle.write(json.dumps(line) + '\n')

    def scenario(self, slow=1.1, overlapped_per_class=10, pressure=1, fits=(True, True, True, False), swap_growth=GIB, free=300 * GIB):
        """3 solo jobs per class (run RUN s, wait SOLO_WAIT s) then overlapped pairs (run RUN * slow, wait 100 s) that really run together."""
        at = T0
        for cls in CLASSES:
            for _ in range(3):
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
                         (20, 6, 2, 1))
        crate = report['classes']['build-crate']
        self.assertEqual((crate['solo_runs'], crate['overlapped_runs'], round(crate['slowdown'], 2)), (3, 10, 1.1))
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
                                    ('fits', dict(fits=(True, False, False, False)), 'fits_if_cap_were_3_share')):
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

    def test_disk_without_a_reserved_figure_or_overlap_sample_is_insufficient(self):
        self.job('build-crate', T0, 100, RUN, False)                         # a solo sample only
        self.assertEqual(self.criteria(self.report())['min_free_bytes']['verdict'], 'insufficient')

    def test_fits_share_counts_only_verdicts_made_with_a_job_running_once_per_entry_and_situation(self):
        self.receipt(True, count=0)                                          # an empty host trivially fits: not evidence
        for _ in range(125):                                                 # a polled entry repeats one verdict
            self.receipt(True, qid='q000900')
        self.receipt(False, qid='q000901')
        self.receipt(False, qid='q000901', count=2)                          # same entry, a different situation
        self.receipt(True, qid='q000900', band='AMBER')
        crit = self.criteria(self.report())['fits_if_cap_were_3_share']
        self.assertEqual((crit['value'], crit['verdict']), (0.5, 'PASS'))    # 2 of 4 distinct (entry, situation) verdicts
        self.setUp()
        self.receipt(True, count=0)
        self.assertEqual(self.criteria(self.report())['fits_if_cap_were_3_share']['verdict'], 'insufficient')

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
            for _ in range(3):
                self.job(cls, at, 600, 1000, False, swap=GIB)
                at += timedelta(hours=1)
        trial_from = at
        for cls in CLASSES:
            for _ in range(6):
                self.job(cls, at, 100, 1100, True, swap=GIB)
                at += timedelta(hours=1)
        report = da.trial_report(self.path, trial_from, at, baseline=(T0, trial_from - timedelta(seconds=1)), now=at)
        self.assertEqual((report['overlapped_jobs'], report['solo_baseline_jobs']), (12, 6))
        self.assertEqual(round(report['classes']['build-workspace']['slowdown'], 2), 1.1)

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
