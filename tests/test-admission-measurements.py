#!/usr/bin/env python3
"""Admission measurement log (SPE-150 PR2): observational lines, summary evidence rules, no effect on decisions."""
from importlib.util import module_from_spec, spec_from_file_location
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_admission', ROOT / 'scripts/disk-admission.py')
da = module_from_spec(spec)
spec.loader.exec_module(da)
ORIGINAL_PROCESS_ROWS = da._process_rows
GIB = 1024 ** 3
SECRET_ARG = 'sk-test-ARGV-SECRET-0123456789'
NOW = datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)


def rows_for(pid, kib=1024):
    """A fake ps table: pid with a child and a grandchild, plus an unrelated process."""
    return {pid: (1, kib, 1.5), pid + 1: (pid, kib, 2.0), pid + 2: (pid + 1, kib, 0.5), 999999: (1, 7777, 9.0)}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'
        self.log = self.path.parent / 'measurements.jsonl'
        self.holders = []
        self.addCleanup(self.stop_holders)
        self.patch('_process_rows', lambda: rows_for(self.pid))
        self.patch('_read_pressure', lambda: 1)
        self.pid = 4242

    def patch(self, name, value):
        patcher = unittest.mock.patch.object(da, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop_holders(self):
        for proc in self.holders:
            proc.kill()
            proc.wait()

    def real_holder(self):
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        return proc

    def reserve(self, label='build-a', proc=None, **kwargs):
        proc = proc or self.real_holder()
        self.pid = proc.pid
        return da.reserve(label, 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=NOW, **kwargs)

    def lines(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class Append(Base):
    def test_reserve_appends_subtree_totals_with_a_private_file(self):
        result = self.reserve()
        self.assertTrue(result['admit'], result)
        line = self.lines()[0]
        self.assertEqual((line['event'], line['label'], line['procs'], line['rss_bytes'], line['cpu_s']),
                         ('reserve', 'build-a', 3, 3 * 1024 * 1024, 4.0))
        self.assertEqual((line['pressure'], line['concurrent'], line['free_bytes']), (1, 1, 400 * GIB))
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_existing_file_with_wider_mode_is_tightened(self):
        self.path.parent.mkdir(parents=True)
        self.log.write_text('')
        os.chmod(self.log, 0o644)
        self.reserve()
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_snapshot_and_advance_sample_each_live_holder(self):
        self.reserve()
        da.snapshot(self.path, lambda: (400 * GIB, 'injected'), now=NOW)
        da.queue_advance(self.path, lambda: 400 * GIB, now=NOW)
        self.assertEqual([line['event'] for line in self.lines()], ['reserve', 'snapshot', 'advance'])

    def test_refused_reserve_logs_nothing(self):
        result = da.reserve('build-a', 10 * GIB, 1, self.path, lambda: 5 * GIB, now=NOW)
        self.assertFalse(result['admit'])
        self.assertFalse(self.log.exists())

    def test_release_writes_end_line_with_duration_and_free_change(self):
        proc = self.real_holder()
        self.reserve(proc=proc)
        with unittest.mock.patch.object(da, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW + timedelta(seconds=90)
            da.release('build-a', self.path, lambda: 390 * GIB)
        end = self.lines()[-1]
        self.assertEqual((end['event'], end['duration_s'], end['free_delta_bytes']), ('end', 90, -10 * GIB))

    def test_release_of_missing_reservation_logs_nothing(self):
        da.release('nobody', self.path)
        self.assertFalse(self.log.exists())

    def test_no_argv_or_command_text_reaches_the_log(self):
        proc = subprocess.Popen(['/bin/sh', '-c', 'sleep 300', SECRET_ARG], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        self.assertTrue(self.reserve(proc=proc)['admit'])
        da.snapshot(self.path, lambda: (400 * GIB, 'injected'), now=NOW)
        da.release('build-a', self.path)
        text = self.log.read_text()
        self.assertEqual(len(self.lines()), 3)
        self.assertNotIn(SECRET_ARG, text)
        self.assertNotIn('sleep', text)

    def test_ps_is_asked_for_numbers_only(self):
        seen = []
        real_run = subprocess.run

        def spy(cmd, *args, **kwargs):
            seen.append(cmd)
            return real_run(cmd, *args, **kwargs)

        with unittest.mock.patch.object(da.subprocess, 'run', spy):
            ORIGINAL_PROCESS_ROWS()
        columns = [c for c in seen if c[0] == '/bin/ps'][0]
        self.assertEqual(columns, ['/bin/ps', '-axo', 'pid=,ppid=,rss=,time='])

    def test_rotation_at_one_mib_keeps_one_backup(self):
        self.path.parent.mkdir(parents=True)
        self.log.write_text('x' * (da.RECEIPT_ROTATE_BYTES + 1))
        self.reserve()
        backup = self.log.with_name('measurements.jsonl.1')
        self.assertTrue(backup.exists())
        self.assertEqual(len(self.lines()), 1)


class Isolation(Base):
    def test_log_failure_does_not_change_the_reserve_result(self):
        proc = self.real_holder()
        self.pid = proc.pid
        baseline = da.reserve('build-a', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=NOW)
        da.release('build-a', self.path)
        self.patch('_append_measurement', unittest.mock.Mock(side_effect=OSError('disk full')))
        before = da.MEASUREMENT_FAILURES
        again = da.reserve('build-a', 10 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=NOW)
        self.assertEqual(again, baseline)
        self.assertGreater(da.MEASUREMENT_FAILURES, before)

    def test_unusable_ps_never_raises_and_is_counted(self):
        self.patch('_process_rows', lambda: None)
        before = da.MEASUREMENT_FAILURES
        self.assertTrue(self.reserve()['admit'])
        self.assertEqual(da.MEASUREMENT_FAILURES, before + 1)
        self.assertFalse(self.log.exists())

    def test_log_path_as_directory_does_not_break_snapshot_or_release(self):
        self.path.parent.mkdir(parents=True)
        self.log.mkdir()
        proc = self.real_holder()
        self.assertTrue(self.reserve(proc=proc)['admit'])
        self.assertNotIn('error', da.snapshot(self.path, lambda: (400 * GIB, 'injected'), now=NOW))
        self.assertTrue(da.release('build-a', self.path)['admit'])

    def test_sampling_happens_after_the_lock_is_dropped(self):
        held = []
        self.patch('_process_rows', lambda: held.append(self.lock_is_held()) or rows_for(self.pid))
        self.reserve()
        self.assertEqual(held, [False])

    def lock_is_held(self):
        import fcntl
        descriptor = os.open(str(self.path) + '.lock', os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True
        finally:
            os.close(descriptor)


def write_runs(path, label, runs, **extra):
    """runs: list of (pid, peak_kib, concurrent, pressure); one sample line each."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        for pid, kib, concurrent, pressure in runs:
            line = dict({'v': 1, 'ts': '2026-10-31T03:00:00Z', 'event': 'snapshot', 'label': label, 'pid': pid,
                         'start_token': 'tok%d' % pid, 'rss_bytes': kib * 1024, 'cpu_s': 1.0, 'procs': 2,
                         'pressure': pressure, 'concurrent': concurrent, 'free_bytes': GIB}, **extra)
            handle.write(json.dumps(line) + '\n')


class Summary(Base):
    def test_three_solo_runs_at_pressure_one_give_an_estimate(self):
        write_runs(self.log, 'cargo-build', [(1, 100, 1, 1), (2, 300, 1, 1), (3, 200, 1, 1)])
        entry = da.measurements_summary(self.path)['classes']['cargo-build']
        self.assertEqual((entry['runs'], entry['solo_runs'], entry['max_rss_bytes']), (3, 3, 300 * 1024))
        self.assertEqual(entry['estimate_rss_bytes'], 300 * 1024)

    def test_fewer_than_three_solo_runs_is_insufficient_evidence(self):
        write_runs(self.log, 'cargo-build', [(1, 100, 1, 1), (2, 300, 1, 1)])
        entry = da.measurements_summary(self.path)['classes']['cargo-build']
        self.assertEqual(entry['estimate'], 'insufficient evidence')
        self.assertIsNone(entry['estimate_rss_bytes'])

    def test_concurrent_and_pressured_runs_do_not_count_toward_the_estimate(self):
        write_runs(self.log, 'e2e', [(1, 100, 1, 1), (2, 100, 2, 1), (3, 100, 1, 2), (4, 100, 1, None)])
        entry = da.measurements_summary(self.path)['classes']['e2e']
        self.assertEqual((entry['solo_runs'], entry['concurrent_runs'], entry['pressured_samples']), (3, 1, 1))
        self.assertEqual(entry['qualifying_solo_runs'], 1)
        self.assertEqual(entry['estimate'], 'insufficient evidence')

    def test_class_field_wins_over_label_and_legacy_lines_are_grouped_safely(self):
        write_runs(self.log, 'a-1', [(1, 10, 1, 1)], **{'class': 'rust'})
        write_runs(self.log, 'b-2', [(2, 20, 1, 1)], **{'class': 'rust'})
        classes = da.measurements_summary(self.path)['classes']
        self.assertEqual(list(classes), ['rust'])
        self.assertEqual(classes['rust']['runs'], 2)

    def test_p95_uses_per_run_peaks_and_bad_lines_are_skipped(self):
        write_runs(self.log, 'x', [(i, i * 10, 1, 1) for i in range(1, 21)])
        with open(self.log, 'a') as handle:
            handle.write('not json\n{"label": 1}\n')
        summary = da.measurements_summary(self.path)
        self.assertEqual(summary['classes']['x']['p95_rss_bytes'], 190 * 1024)
        self.assertEqual(summary['skipped_lines'], 2)

    def test_cli_states_lower_bound_and_is_read_only(self):
        write_runs(self.log, 'x', [(1, 10, 1, 1)])
        before = self.log.read_bytes()
        for flags in ([], ['--json']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = da.run(['measurements'] + flags, state_path=self.path, free_fn=lambda: 400 * GIB)
            self.assertEqual(rc, 0)
            self.assertIn('lower bounds', out.getvalue())
        self.assertEqual(self.log.read_bytes(), before)
        self.assertFalse(self.path.exists())

    def test_empty_log_reports_none(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(da.run(['measurements'], state_path=self.path), 0)
        self.assertIn('no measurements recorded', out.getvalue())


if __name__ == '__main__':
    unittest.main()
