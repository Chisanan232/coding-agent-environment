#!/usr/bin/env python3
"""Disk-pressure admission core refuses unsafe work and fails closed."""
from importlib.util import module_from_spec, spec_from_file_location
import io
from datetime import datetime, timedelta, timezone
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_admission', ROOT / 'scripts/disk-admission.py')
da = module_from_spec(spec)
spec.loader.exec_module(da)
GIB = 1024 ** 3


class Tiers(unittest.TestCase):
    def test_tier_boundaries(self):
        cases = [(0, 'emergency'), (20 * GIB - 1, 'emergency'), (20 * GIB, 'low'),
                 (100 * GIB - 1, 'low'), (100 * GIB, 'stable'), (200 * GIB - 1, 'stable'),
                 (200 * GIB, 'wide'), (10 ** 15, 'wide')]
        for free, expected in cases:
            self.assertEqual(da.tier(free), expected, free)
        # Negative control: a decimal-GB threshold would misplace this value.
        self.assertEqual(da.tier(100 * 10 ** 9), 'low')


class Decisions(unittest.TestCase):
    def test_heavy_refused_below_100_gib(self):
        for free in (0, 19 * GIB, 50 * GIB, 100 * GIB - 1):
            admit, _, reason = da.decide('heavy', free, {})
            self.assertFalse(admit, free)
            self.assertIn('refused', reason)
        # Negative control: at the boundary heavy work is admitted.
        for free in (100 * GIB, 150 * GIB, 500 * GIB):
            self.assertTrue(da.decide('heavy', free, {})[0], free)

    def test_light_allowed_low_refused_emergency(self):
        for free in (20 * GIB, 60 * GIB, 100 * GIB, 300 * GIB):
            self.assertTrue(da.decide('light', free, {})[0], free)
        # Negative control: below the emergency line light work is refused too.
        for free in (0, 10 * GIB, 20 * GIB - 1):
            admit, _, reason = da.decide('light', free, {})
            self.assertFalse(admit, free)
            self.assertIn('emergency', reason)

    def test_unknown_weight_is_rejected(self):
        with self.assertRaises(ValueError):
            da.decide('medium', 500 * GIB, {})

    def test_latch_set_below_20_held_until_50(self):
        admit, state, _ = da.decide('light', 19 * GIB, {})
        self.assertFalse(admit)
        self.assertTrue(state['latch'])
        # Held through recovery, then released exactly at 50 GiB.
        for free in (20 * GIB, 49 * GIB, 50 * GIB - 1):
            self.assertTrue(da.decide('light', free, state)[1]['latch'], free)
        self.assertFalse(da.decide('light', 50 * GIB, state)[1]['latch'])
        # Negative control: without a prior latch 20..<50 GiB never latches.
        self.assertFalse(da.decide('light', 30 * GIB, {})[1]['latch'])

    def test_latch_refuses_light_below_50(self):
        latched = {'latch': True}
        for free in (25 * GIB, 49 * GIB):
            admit, _, reason = da.decide('light', free, latched)
            self.assertFalse(admit, free)
            self.assertIn('latched', reason)
        # Heavy work just below the latch release point is refused by the latch itself.
        admit, _, reason = da.decide('heavy', 50 * GIB - 1, latched)
        self.assertFalse(admit)
        self.assertIn('latched', reason)
        # Negative control: the same free space without a latch admits light work.
        self.assertTrue(da.decide('light', 25 * GIB, {})[0])
        self.assertTrue(da.decide('light', 50 * GIB, latched)[0])


class State(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission/state.json'

    def write(self, free, latch):
        with da.state_lock(self.path):
            return da.write_state(self.path, free, latch)

    def test_state_written_atomically_with_timestamp(self):
        self.write(36 * GIB, True)
        record = json.loads(self.path.read_text())
        self.assertEqual(record['version'], 1)
        self.assertEqual((record['free_bytes'], record['tier'], record['latch']), (36 * GIB, 'low', True))
        self.assertTrue(record['timestamp'].endswith('Z'))
        from datetime import datetime
        datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        # A failed replace leaves the previous complete record and no temp file behind.
        with mock.patch.object(da.os, 'replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                self.write(500 * GIB, False)
        self.assertEqual(json.loads(self.path.read_text()), record)
        self.assertEqual(sorted(entry.name for entry in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])

    def test_corrupt_state_fails_closed(self):
        self.assertEqual(da.read_state(self.path), {})
        self.write(150 * GIB, False)
        self.assertEqual(da.read_state(self.path)['latch'], False)
        bad = ['', '{not json', '[]', 'null', json.dumps({'version': 2, 'latch': False}),
               json.dumps({'version': 1, 'latch': 'no', 'free_bytes': 1, 'tier': 'low', 'timestamp': 't'}),
               json.dumps({'version': 1, 'latch': False, 'free_bytes': 1, 'tier': 'huge', 'timestamp': 't'})]
        for content in bad:
            self.path.write_text(content)
            with self.assertRaises(da.StateError, msg=content):
                da.read_state(self.path)

    def test_symlinked_state_path_refused(self):
        target = Path(self.temp.name) / 'elsewhere.json'
        target.write_text('{}')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.symlink_to(target)
        with self.assertRaises(da.StateError):
            da.read_state(self.path)
        with self.assertRaises(da.StateError):
            self.write(10 * GIB, True)
        self.assertEqual(target.read_text(), '{}')
        # A redirected directory is refused as well.
        self.path.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(self.temp.name)
        with self.assertRaises(da.StateError):
            self.write(10 * GIB, True)


class Volume(unittest.TestCase):
    @staticmethod
    def stats():
        return mock.Mock(f_bavail=1000, f_frsize=4096, f_bfree=9999)

    def test_darwin_reads_data_volume(self):
        with mock.patch.object(da.os.path, 'exists', return_value=True), \
                mock.patch.object(da.os, 'statvfs', return_value=self.stats()) as statvfs:
            self.assertEqual(da.read_free_bytes(), 1000 * 4096)
        statvfs.assert_called_once_with('/System/Volumes/Data')
        # Negative control: without the data volume it falls back to root.
        with mock.patch.object(da.os.path, 'exists', return_value=False), \
                mock.patch.object(da.os, 'statvfs', return_value=self.stats()) as statvfs:
            da.read_free_bytes()
        statvfs.assert_called_once_with('/')

    def test_unreadable_volume_fails_closed(self):
        with mock.patch.object(da.os, 'statvfs', side_effect=PermissionError('denied')):
            with self.assertRaises(da.VolumeError):
                da.read_free_bytes()


class Transactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission/state.json'

    def test_concurrent_checks_serialize_state_writes(self):
        inside, peak, guard = [0], [0], threading.Lock()

        def measure():
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.01)
            with guard:
                inside[0] -= 1
            return 150 * GIB

        # Negative control: the detector does see overlap when nothing serializes.
        workers = [threading.Thread(target=measure) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertGreater(peak[0], 1)
        inside[0], peak[0], results = 0, 0, []
        workers = [threading.Thread(target=lambda: results.append(da.evaluate('heavy', self.path, measure)))
                   for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result['admit'] for result in results))
        self.assertEqual(da.read_state(self.path)['free_bytes'], 150 * GIB)
        self.assertEqual(sorted(entry.name for entry in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])


class Cli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission/state.json'

    def run_main(self, argv, free):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = da.main(argv, free_fn=lambda: free, state_path=self.path)
        return code, out.getvalue()

    def test_cli_rejects_free_bytes_override(self):
        home = {**os.environ, 'HOME': self.temp.name}
        script = str(ROOT / 'scripts/disk-admission.py')
        for extra in (['--free-bytes', str(900 * GIB)], ['--state', '/tmp/x'], ['--state-path=/tmp/x'], ['--bogus']):
            result = subprocess.run([sys.executable, script, 'check', '--weight', 'heavy', *extra],
                                    capture_output=True, text=True, env=home)
            self.assertEqual(result.returncode, 2, extra)
            self.assertEqual(result.stdout, '')
        # An unknown weight and an abbreviated flag are usage errors too.
        for argv in (['check', '--weight', 'medium'], ['check', '--wei', 'heavy'], ['status']):
            result = subprocess.run([sys.executable, script, *argv], capture_output=True, text=True, env=home)
            self.assertEqual(result.returncode, 2, argv)
        self.assertFalse((Path(self.temp.name) / '.cache').exists())

    def test_refusal_exit_code_is_77(self):
        code, text = self.run_main(['check', '--weight', 'heavy'], 36 * GIB)
        self.assertEqual(code, 77)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn('tier=low', text)
        self.assertIn('36.0 GiB', text)
        # Negative control: the same request is admitted with enough space.
        self.assertEqual(self.run_main(['check', '--weight', 'heavy'], 150 * GIB)[0], 0)
        self.assertEqual(self.run_main(['check', '--weight', 'light'], 36 * GIB)[0], 0)

    def test_status_json_reports_tier_and_latch(self):
        code, text = self.run_main(['status', '--json'], 10 * GIB)
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual((report['tier'], report['free_bytes'], report['latch']), ('emergency', 10 * GIB, True))
        self.assertIn('timestamp', report)
        code, text = self.run_main(['status', '--json'], 30 * GIB)
        self.assertTrue(json.loads(text)['latch'])

    def test_unreadable_volume_and_corrupt_state_refuse_with_distinct_reasons(self):
        def broken():
            raise da.VolumeError('gone')
        self.assertFalse(da.evaluate('light', self.path, broken)['admit'])
        self.assertIn('volume-unreadable', da.evaluate('light', self.path, broken)['reason'])
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{corrupt')
        result = da.evaluate('light', self.path, lambda: 500 * GIB)
        self.assertFalse(result['admit'])
        self.assertIn('state-corrupt', result['reason'])
        self.assertTrue(result['latch'])
        # Rewritten as latched; recovery at 50 GiB or more admits again.
        self.assertTrue(da.evaluate('light', self.path, lambda: 500 * GIB)['admit'])

    def test_check_line_reports_exact_free_bytes(self):
        code, text = self.run_main(['check', '--weight', 'heavy'], 36 * GIB + 12345)
        self.assertEqual(code, 77)
        self.assertIn('bytes=%d' % (36 * GIB + 12345), text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_run_passes_injected_state_path_and_free_space(self):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = da.run(['check', '--weight', 'heavy'], state_path=self.path, free_fn=lambda: 36 * GIB)
        self.assertEqual(code, 77)
        self.assertTrue(self.path.exists())
        self.assertIn('tier=low', out.getvalue())


class Sources(unittest.TestCase):
    @staticmethod
    def failing():
        raise da.VolumeError('down')

    def test_glomeris_free_bytes_cross_checked_with_statvfs(self):
        self.assertEqual(da.measure_free_bytes(lambda: 40 * GIB, lambda: 38 * GIB), 38 * GIB)
        # The smaller reading wins whichever source reports it.
        self.assertEqual(da.measure_free_bytes(lambda: 38 * GIB, lambda: 40 * GIB), 38 * GIB)
    def test_glomeris_unavailable_falls_back_to_statvfs(self):
        self.assertEqual(da.measure_free_bytes(self.failing, lambda: 38 * GIB), 38 * GIB)
        self.assertEqual(da.measure_free_bytes(lambda: 38 * GIB, self.failing), 38 * GIB)
        # Unparsable output or a missing binary is unavailable, not a crash.
        with mock.patch.object(da.subprocess, 'run', return_value=mock.Mock(stdout='not json')):
            with mock.patch.dict(da.__dict__, {'account_home': lambda: Path(tempfile.gettempdir()) / 'absent-home'}):
                with self.assertRaises(da.VolumeError):
                    da.read_glomeris_free()

    def test_both_sources_failing_refuses(self):
        with self.assertRaises(da.VolumeError):
            da.measure_free_bytes(self.failing, self.failing)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'admission/state.json'
            result = da.evaluate('light', path, lambda: da.measure_free_bytes(self.failing, self.failing))
        self.assertFalse(result['admit'])
        self.assertIn('volume-unreadable', result['reason'])


class Hardening(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'

    def test_deleted_state_below_50_gib_is_latched(self):
        self.assertTrue(da.evaluate('light', self.path, lambda: 150 * GIB)['admit'])
        self.path.unlink()
        result = da.evaluate('light', self.path, lambda: 30 * GIB)
        self.assertFalse(result['admit'])
        self.assertIn('state-missing', result['reason'])
        self.assertTrue(result['latch'])
        # Still held at 49 GiB, released by fresh free space at 50 GiB.
        self.assertFalse(da.evaluate('light', self.path, lambda: 49 * GIB)['admit'])
        self.assertTrue(da.evaluate('light', self.path, lambda: 50 * GIB)['admit'])
        # Negative control: a missing record with plenty of space is not latched.
        self.path.unlink()
        self.assertTrue(da.evaluate('light', self.path, lambda: 60 * GIB)['admit'])

    def test_forged_unlatched_state_refused(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        forged = {'version': 1, 'timestamp': '2026-01-01T00:00:00Z', 'free_bytes': 5 * GIB,
                  'tier': 'emergency', 'latch': False}
        for record in (forged, dict(forged, tier='wide')):
            self.path.write_text(json.dumps(record))
            result = da.evaluate('light', self.path, lambda: 30 * GIB)
            self.assertFalse(result['admit'], record)
            self.assertIn('state-corrupt', result['reason'])
            self.assertTrue(result['latch'])
        # Negative control: a self-consistent unlatched record is trusted.
        self.path.write_text(json.dumps(dict(forged, free_bytes=150 * GIB, tier='stable')))
        self.assertTrue(da.evaluate('light', self.path, lambda: 30 * GIB)['admit'])

    def test_default_state_path_ignores_home_environment(self):
        import pwd
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with mock.patch.dict(os.environ, {'HOME': self.temp.name}):
            path = da.default_state_path()
        self.assertEqual(path, account / da.STATE_RELATIVE)
        self.assertFalse(str(path).startswith(self.temp.name))

    def test_default_state_lives_outside_resource_lock_namespace(self):
        relative = da.STATE_RELATIVE
        self.assertNotIn('aa-qa', relative)
        self.assertEqual(relative, '.cache/coding-agent-environment/admission/state.json')

    def test_stuck_lock_holder_refuses_without_hanging(self):
        import fcntl
        self.path.parent.mkdir(parents=True)
        holder = os.open(str(self.path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        with mock.patch.object(da, 'LOCK_TIMEOUT', 0.3):
            result = da.evaluate('light', self.path, lambda: 500 * GIB)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(result['admit'])
        self.assertIn('state-lock-timeout', result['reason'])
        # Negative control: once the holder lets go the same call admits.
        fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertTrue(da.evaluate('light', self.path, lambda: 500 * GIB)['admit'])

    def test_fifo_state_refused_without_hanging(self):
        self.path.parent.mkdir(parents=True)
        os.mkfifo(self.path)
        results = []
        worker = threading.Thread(target=lambda: results.append(da.evaluate('light', self.path, lambda: 500 * GIB)),
                                  daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'evaluate hung on a FIFO')
        self.assertFalse(results[0]['admit'])
        self.assertIn('state-not-regular', results[0]['reason'])
        # The FIFO is left alone rather than replaced.
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.path).st_mode))

    def test_symlinked_ancestor_of_state_directory_refused(self):
        home = Path(self.temp.name).resolve() / 'home'
        elsewhere = Path(self.temp.name).resolve() / 'elsewhere'
        (home / '.cache').mkdir(parents=True)
        elsewhere.mkdir()
        (home / '.cache/coding-agent-environment').symlink_to(elsewhere)
        path = home / da.STATE_RELATIVE
        with mock.patch.object(da, 'account_home', return_value=home):
            result = da.evaluate('light', path, lambda: 500 * GIB)
            self.assertFalse(result['admit'])
            self.assertIn('state-untrusted', result['reason'])
            self.assertEqual(list(elsewhere.iterdir()), [])
            # Negative control: the same layout without the symlink works.
            (home / '.cache/coding-agent-environment').unlink()
            self.assertTrue(da.evaluate('light', path, lambda: 500 * GIB)['admit'])

    def test_stale_temp_file_does_not_block_writes(self):
        self.path.parent.mkdir(parents=True)
        stale = self.path.parent / ('.state.json.%d.tmp' % os.getpid())
        stale.write_text('leftover from a crashed writer')
        self.assertTrue(da.evaluate('light', self.path, lambda: 500 * GIB)['admit'])
        self.assertEqual(stale.read_text(), 'leftover from a crashed writer')

    def test_state_and_directory_are_synced_and_temp_is_exclusive(self):
        synced, flags = [], {}
        real_open, real_fsync = os.open, os.fsync

        def spy_open(target, mode, *args):
            flags[os.path.basename(target)] = mode
            return real_open(target, mode, *args)

        def spy_fsync(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            return real_fsync(descriptor)

        with mock.patch.object(da.os, 'open', spy_open), mock.patch.object(da.os, 'fsync', spy_fsync):
            self.assertTrue(da.evaluate('light', self.path, lambda: 500 * GIB)['admit'])
        # One fsync for the file contents, one for the directory entry of the rename.
        self.assertEqual(sorted(synced), [False, True])
        temporary = [mode for name, mode in flags.items() if name.endswith('.tmp')]
        self.assertEqual(len(temporary), 1)
        self.assertTrue(temporary[0] & os.O_EXCL and temporary[0] & os.O_NOFOLLOW)
        self.assertTrue(flags['state.json.lock'] & os.O_NOFOLLOW)

    def test_enospc_through_evaluate_refuses_and_keeps_previous_state(self):
        import errno
        self.assertTrue(da.evaluate('light', self.path, lambda: 500 * GIB)['admit'])
        before = self.path.read_text()
        with mock.patch.object(da.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            result = da.evaluate('light', self.path, lambda: 500 * GIB)
        self.assertFalse(result['admit'])
        self.assertIn('state-unwritable', result['reason'])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(sorted(entry.name for entry in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])

    def test_existing_state_directory_is_tightened_to_0700(self):
        self.path.parent.mkdir(parents=True, mode=0o755)
        os.chmod(self.path.parent, 0o755)
        self.assertTrue(da.evaluate('light', self.path, lambda: 500 * GIB)['admit'])
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_symlinked_lock_file_refused(self):
        self.path.parent.mkdir(parents=True)
        target = Path(self.temp.name) / 'victim'
        target.write_text('untouched')
        os.symlink(target, str(self.path) + '.lock')
        result = da.evaluate('light', self.path, lambda: 500 * GIB)
        self.assertFalse(result['admit'])
        self.assertIn('state-unwritable', result['reason'])
        self.assertEqual(target.read_text(), 'untouched')
        self.assertFalse(self.path.exists())

    def test_unexpected_exception_exits_77(self):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with mock.patch.object(da, 'evaluate', side_effect=RuntimeError('boom')), redirect_stdout(out):
            self.assertEqual(da.run(['check', '--weight', 'light']), 77)
        self.assertIn('internal error', out.getvalue())
        # Usage errors stay argparse's SystemExit(2), still non-zero for callers.
        with self.assertRaises(SystemExit) as raised, mock.patch('sys.stderr', io.StringIO()):
            da.run(['check'])
        self.assertEqual(raised.exception.code, 2)


class Reservations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def reserve_file(self, label, pid=None, token=None, nbytes=10 * GIB, hours=1):
        pid = pid or os.getpid()
        token = token or da.process_token(pid)
        with da.state_lock(self.path):
            da.write_reservation(self.path, label, nbytes, pid, token, self.now + timedelta(hours=hours))
        return self.path.parent / 'reservations' / (label + '.json')

    def live(self):
        with da.state_lock(self.path):
            return da.live_reservations(self.path, self.now)

    def test_live_reservation_is_counted(self):
        file = self.reserve_file('job-a')
        live, problems = self.live()
        self.assertEqual([(r['label'], r['bytes']) for r in live], [('job-a', 10 * GIB)])
        self.assertEqual(problems, [])
        self.assertTrue(file.exists())
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(file.parent.stat().st_mode & 0o777, 0o700)

    def test_dead_pid_reservation_is_pruned(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        token = da.process_token(child.pid)
        file = self.reserve_file('job-dead', child.pid, token)
        self.assertEqual(len(self.live()[0]), 1)
        child.kill()
        child.wait()
        self.assertIsNone(da.process_token(child.pid))
        live, problems = self.live()
        self.assertEqual((live, problems), ([], []))
        self.assertFalse(file.exists())

    def test_pid_reuse_token_mismatch_is_pruned(self):
        file = self.reserve_file('job-reused', token='Mon Jan  1 00:00:00 2001')
        self.assertTrue(file.exists())
        live, _ = self.live()
        self.assertEqual(live, [])
        self.assertFalse(file.exists())

    def test_expired_reservation_is_pruned_and_only_own_files(self):
        expired = self.reserve_file('job-old', hours=-1)
        fresh = self.reserve_file('job-new')
        foreign = expired.parent / 'notes.json'
        foreign.write_text('{"unrelated": true}')
        stranger = expired.parent / 'readme.txt'
        stranger.write_text('keep')
        live, problems = self.live()
        self.assertEqual([r['label'] for r in live], ['job-new'])
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.exists())
        # Foreign files are reported or ignored, never deleted.
        self.assertTrue(foreign.exists() and stranger.exists())
        self.assertEqual(len(problems), 1)
        self.assertIn('notes.json', problems[0])

    def take(self, label, nbytes, free, pid=None, **extra):
        return da.reserve(label, nbytes, pid or os.getpid(), self.path, lambda: free, now=self.now, **extra)

    def test_over_capacity_reservation_refused(self):
        refused = self.take('job-big', 60 * GIB, 150 * GIB)
        self.assertFalse(refused['admit'])
        self.assertIn('over-capacity', refused['reason'])
        self.assertFalse((self.path.parent / 'reservations/job-big.json').exists())
        # Negative control: leaving exactly the 100 GiB floor is admitted.
        admitted = self.take('job-fits', 50 * GIB, 150 * GIB)
        self.assertTrue(admitted['admit'], admitted)
        self.assertEqual(admitted['reserved_bytes'], 50 * GIB)

    def test_second_heavy_lane_refused_until_release(self):
        self.assertTrue(self.take('job-one', 10 * GIB, 300 * GIB)['admit'])
        second = self.take('job-two', 10 * GIB, 300 * GIB)
        self.assertFalse(second['admit'])
        self.assertIn('lane-cap', second['reason'])
        self.assertTrue(da.release('job-one', self.path)['admit'])
        self.assertTrue(self.take('job-two', 10 * GIB, 300 * GIB)['admit'])
        # A host below the stable tier has no heavy lane at all.
        self.assertIn('refused', self.take('job-low', 1 * GIB, 60 * GIB)['reason'])

    def test_live_reservations_reduce_remaining_headroom(self):
        with mock.patch.dict(da.HEAVY_LANES, {'wide': 3}):
            self.assertTrue(self.take('job-a', 40 * GIB, 200 * GIB)['admit'])
            self.assertTrue(self.take('job-b', 40 * GIB, 200 * GIB)['admit'])
            third = self.take('job-c', 40 * GIB, 200 * GIB)
        self.assertFalse(third['admit'])
        self.assertIn('over-capacity', third['reason'])
        self.assertEqual(third['reserved_bytes'], 80 * GIB)

    def test_concurrent_reserve_serialization(self):
        results = []
        with mock.patch.dict(da.HEAVY_LANES, {'wide': 8}):
            workers = [threading.Thread(target=lambda n=n: results.append(self.take('job-%d' % n, 30 * GIB, 200 * GIB)))
                       for n in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(len(results), 8)
        # 200 - 3 * 30 = 110 >= 100 but a fourth would leave 80: exactly three fit.
        self.assertEqual(sum(1 for result in results if result['admit']), 3)
        live, problems = self.live()
        self.assertEqual((len(live), problems), (3, []))
        self.assertEqual(sorted(entry.name for entry in (self.path.parent / 'reservations').iterdir()),
                         sorted(record['label'] + '.json' for record in live))

    def test_label_ownership_dead_pid_and_invalid_labels(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertTrue(self.take('shared', 10 * GIB, 300 * GIB, pid=child.pid)['admit'])
        taken = self.take('shared', 10 * GIB, 300 * GIB)
        self.assertIn('label-in-use', taken['reason'])
        # The owner may renew its own label.
        self.assertTrue(self.take('shared', 20 * GIB, 300 * GIB, pid=child.pid)['admit'])
        done = subprocess.run([sys.executable, '-c', 'pass'])
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        self.assertIn('process-not-running', self.take('ghost', 1 * GIB, 300 * GIB, pid=gone.pid)['reason'])
        for bad in ('../escape', 'a/b', '', '.hidden', 'x' * 65):
            result = self.take(bad, 1 * GIB, 300 * GIB)
            self.assertIn('invalid-request', result['reason'], bad)
            self.assertIn('invalid-request', da.release(bad, self.path)['reason'], bad)
        self.assertFalse((self.path.parent / 'escape.json').exists())
        self.assertEqual(da.release('missing', self.path)['reason'], 'no reservation named missing')

    def test_unreadable_reservation_file_blocks_new_reservations_and_is_kept(self):
        self.assertTrue(self.take('job-ok', 10 * GIB, 300 * GIB)['admit'])
        self.assertTrue(da.release('job-ok', self.path)['admit'])
        stray = self.path.parent / 'reservations/stray.json'
        stray.write_text('not json')
        refused = self.take('job-next', 10 * GIB, 300 * GIB)
        self.assertFalse(refused['admit'])
        self.assertIn('reservation-unreadable', refused['reason'])
        self.assertEqual(stray.read_text(), 'not json')
        # Negative control: once the operator removes it, reservations work again.
        stray.unlink()
        self.assertTrue(self.take('job-next', 10 * GIB, 300 * GIB)['admit'])



class Snapshots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def shot(self, free, source='glomeris'):
        return da.snapshot(self.path, lambda: (free, source), now=self.now)

    def test_snapshot_contract_and_file(self):
        record = self.shot(250 * GIB)
        self.assertEqual(sorted(record), sorted(['schema', 'ts', 'free_bytes', 'tier', 'latch',
                                                 'heavy_lanes_allowed', 'active_heavy_jobs', 'reserved_bytes',
                                                 'protection', 'blockers', 'source']))
        self.assertEqual((record['schema'], record['ts']), (1, '2026-10-16T12:00:00Z'))
        self.assertNotIn('age_s', json.loads((self.path.parent / 'snapshot.json').read_text()))
        self.assertEqual((record['free_bytes'], record['tier'], record['latch']), (250 * GIB, 'wide', False))
        self.assertEqual((record['heavy_lanes_allowed'], record['blockers'], record['source']), (1, [], 'glomeris'))
        self.assertEqual(record['protection'], 'advisory-not-wired')
        file = self.path.parent / 'snapshot.json'
        self.assertEqual(json.loads(file.read_text()), record)
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.shot(80 * GIB, 'statvfs')['source'], 'statvfs')

    def test_snapshot_heavy_lanes_by_tier_and_latch(self):
        self.assertEqual(self.shot(150 * GIB)['heavy_lanes_allowed'], 1)
        self.assertEqual(self.shot(99 * GIB)['heavy_lanes_allowed'], 0)
        self.assertEqual(self.shot(100 * GIB)['heavy_lanes_allowed'], 1)
        wide = self.shot(500 * GIB)
        self.assertEqual((wide['tier'], wide['heavy_lanes_allowed']), ('wide', 1))
        # Latched: no lane, with the reason listed, even once free space reads as stable again.
        self.shot(10 * GIB)
        held = self.shot(40 * GIB)
        self.assertEqual((held['latch'], held['heavy_lanes_allowed']), (True, 0))
        self.assertIn('latched', held['blockers'][0])
        low = self.shot(70 * GIB)
        self.assertTrue(any('below stable' in b or 'latched' in b for b in low['blockers']))

    def test_snapshot_counts_reservations_and_reports_blockers(self):
        self.shot(300 * GIB)
        self.assertTrue(da.reserve('job-a', 40 * GIB, os.getpid(), self.path, lambda: 300 * GIB, now=self.now)['admit'])
        record = self.shot(300 * GIB)
        self.assertEqual((record['active_heavy_jobs'], record['reserved_bytes']), (1, 40 * GIB))
        # An unreadable reservation is surfaced as a blocker rather than silently ignored.
        (self.path.parent / 'reservations/bad.json').write_text('{')
        self.assertIn('reservation-unreadable', ' '.join(self.shot(300 * GIB)['blockers']))
        # An unreadable volume publishes a closed snapshot instead of a stale optimistic one.
        def broken():
            raise da.VolumeError('gone')
        closed = da.snapshot(self.path, broken, now=self.now)
        self.assertEqual((closed['tier'], closed['free_bytes'], closed['heavy_lanes_allowed'], closed['source']),
                         (None, None, 0, None))
        self.assertIn('volume-unreadable', closed['blockers'][0])

    def test_snapshot_freshness_rule(self):
        record = self.shot(300 * GIB)
        at = lambda seconds: self.now + timedelta(seconds=seconds)
        self.assertTrue(da.snapshot_is_fresh(record, at(0)))
        self.assertTrue(da.snapshot_is_fresh(record, at(120)))
        # Negative controls: just past the limit, far in the future, and malformed.
        self.assertFalse(da.snapshot_is_fresh(record, at(121)))
        self.assertFalse(da.snapshot_is_fresh(record, at(-60)))
        self.assertFalse(da.snapshot_is_fresh({'ts': 'yesterday'}, at(0)))
        self.assertFalse(da.snapshot_is_fresh({}, at(0)))
        self.assertEqual(da.snapshot_age_s(record, at(45)), 45)


class FreshnessCli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'

    def check(self, *extra):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = da.main(['snapshot', '--check-fresh', *extra], free_fn=lambda: 300 * GIB, state_path=self.path)
        return code, out.getvalue()

    def publish(self, age_seconds):
        da.snapshot(self.path, lambda: (300 * GIB, 'glomeris'),
                    now=datetime.now(timezone.utc) - timedelta(seconds=age_seconds))

    def test_fresh_snapshot_exits_0(self):
        self.publish(10)
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.check('--max-age', '60')[0], 0)

    def test_stale_snapshot_exits_77(self):
        self.publish(300)
        code, text = self.check()
        self.assertEqual(code, 77)
        self.assertIn('stale', text)

    def test_missing_snapshot_exits_77(self):
        code, text = self.check()
        self.assertEqual(code, 77)
        self.assertIn('unusable', text)
        self.assertFalse(self.path.parent.exists())

    def test_unreadable_or_forged_snapshot_exits_77(self):
        self.publish(10)
        file = self.path.parent / 'snapshot.json'
        for content in ('', '{not json', '[]', json.dumps({'schema': 2, 'ts': '2026-10-16T12:00:00Z'}),
                        json.dumps({'schema': 1, 'ts': 'yesterday'}), json.dumps({'schema': 1})):
            file.write_text(content)
            self.assertEqual(self.check()[0], 77, content)
        file.unlink()
        os.symlink(self.path, file)
        self.assertEqual(self.check()[0], 77)

    def test_max_age_cannot_relax_the_120_second_rule(self):
        self.publish(500)
        for relaxed in ('99999', '121', '0', '-5'):
            code, text = self.check('--max-age', relaxed)
            self.assertEqual(code, 77, relaxed)
            self.assertIn('--max-age', text)
        self.assertEqual(self.check()[0], 77)
        # Negative control: a fresh snapshot passes with the limit at its maximum.
        self.publish(10)
        self.assertEqual(self.check('--max-age', '120')[0], 0)


class ReservationHardening(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'admission/state.json'
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def take(self, label, nbytes=10 * GIB, free=300 * GIB, pid=None, **extra):
        return da.reserve(label, nbytes, pid or os.getpid(), self.path, lambda: free, now=self.now, **extra)

    def test_label_with_trailing_newline_is_rejected(self):
        for label in ('ok\n', 'ok\n\n', '\nok'):
            self.assertIn('invalid-request', self.take(label)['reason'], repr(label))
            self.assertIn('invalid-request', da.release(label, self.path)['reason'], repr(label))
        self.assertTrue(self.take('ok')['admit'])

    def test_garbled_or_naive_expiry_is_a_problem_not_a_crash(self):
        self.assertTrue(self.take('job-a')['admit'])
        self.assertTrue(da.release('job-a', self.path)['admit'])
        directory = self.path.parent / 'reservations'
        token = da.process_token(os.getpid())
        base = {'version': 1, 'label': 'job-x', 'bytes': GIB, 'pid': os.getpid(), 'start_token': token}
        for expires in ('2026-10-16T12:00:00', 'soon', '', '2026-13-45T99:00:00Z'):
            (directory / 'job-x.json').write_text(json.dumps(dict(base, expires=expires)))
            with da.state_lock(self.path):
                live, problems = da.live_reservations(self.path, self.now)
            self.assertEqual(live, [], expires)
            self.assertIn('reservation-unreadable', ' '.join(problems), expires)
            refused = self.take('job-y')
            self.assertFalse(refused['admit'], expires)
            self.assertIn('reservation-unreadable', refused['reason'])
        self.assertTrue((directory / 'job-x.json').exists())

    def test_reserving_for_pid_1_or_another_users_process_is_refused(self):
        refused = self.take('job-init', pid=1)
        self.assertFalse(refused['admit'])
        self.assertIn('process-not-owned', refused['reason'])
        self.assertFalse((self.path.parent / 'reservations/job-init.json').exists())
        # The owner check is what refuses: a stubbed foreign owner for a live pid is refused too,
        # and the same pid is admitted when it is the current user's.
        other = self.take('job-foreign', owner_fn=lambda pid: os.getuid() + 1)
        self.assertIn('process-not-owned', other['reason'])
        self.assertTrue(self.take('job-mine')['admit'])

    def test_hold_longer_than_4_hours_needs_max_hold_and_is_capped_at_24(self):
        four, day = 4 * 3600, 24 * 3600
        with mock.patch.dict(da.HEAVY_LANES, {'wide': 4}):
            self.assertTrue(self.take('job-4h', ttl=four)['admit'])
            self.assertIn('invalid-request', self.take('job-5h', ttl=four + 1)['reason'])
            self.assertTrue(self.take('job-24h', ttl=day, allow_long=True)['admit'])
        self.assertIn('invalid-request', self.take('job-25h', ttl=day + 1, allow_long=True)['reason'])
        record = json.loads((self.path.parent / 'reservations/job-4h.json').read_text())
        self.assertEqual(record['expires'], '2026-10-16T16:00:00Z')

    def test_reservation_whose_process_cannot_be_inspected_is_counted_conservatively(self):
        self.assertTrue(self.take('job-a', nbytes=40 * GIB)['admit'])
        file = self.path.parent / 'reservations/job-a.json'

        def broken(pid):
            raise da.ReservationError('ps is unavailable')
        with da.state_lock(self.path):
            live, problems = da.live_reservations(self.path, self.now, broken)
        self.assertEqual([(r['label'], r['bytes']) for r in live], [('job-a', 40 * GIB)])
        self.assertIn('reservation-unverifiable', ' '.join(problems))
        self.assertTrue(file.exists())
        # A snapshot taken while ps is broken still counts the job and reports a blocker.
        record = da.snapshot(self.path, lambda: (300 * GIB, 'glomeris'), now=self.now, token_fn=broken)
        self.assertEqual((record['active_heavy_jobs'], record['reserved_bytes']), (1, 40 * GIB))
        self.assertIn('reservation-unverifiable', ' '.join(record['blockers']))


class RefresherPlist(unittest.TestCase):
    def test_refresher_agent_is_bounded_and_installs_nothing(self):
        import plistlib
        data = plistlib.loads(da.build_refresher_plist('/usr/bin/python3', '/opt/x/disk-admission.py').encode())
        self.assertEqual(data['Label'], 'com.coding-agent-environment.admission-snapshot')
        self.assertEqual(data['ProgramArguments'], ['/usr/bin/python3', '/opt/x/disk-admission.py', 'snapshot', '--json'])
        self.assertLess(data['StartInterval'], da.STALE_AFTER_S)
        self.assertNotIn('KeepAlive', data)
        self.assertNotIn('ProcessType', data)  # Background would throttle the job that matters under pressure
        self.assertTrue(data['ProgramArguments'][0].startswith('/') and data['ProgramArguments'][1].startswith('/'))
        self.assertEqual((data['StandardOutPath'], data['StandardErrorPath']), ('/dev/null', '/dev/null'))
        self.assertTrue(data['RunAtLoad'])

    def test_cli_prints_the_plist_without_touching_state(self):
        import io, contextlib
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'admission/state.json'
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = da.main(['refresher-plist'], state_path=state)
            self.assertEqual(code, 0)
            self.assertIn('<key>StartInterval</key>', out.getvalue())
            self.assertFalse(state.parent.exists())


if __name__ == '__main__':
    unittest.main()
