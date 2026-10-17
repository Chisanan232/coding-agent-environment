#!/usr/bin/env python3
"""Campaign resume coordinator records durable checkpoints and never loops."""
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import plistlib
import stat
import tempfile
import os
import subprocess
import threading
import unittest
from datetime import timedelta
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('campaign_resume', ROOT / 'scripts/campaign-resume.py')
cr = module_from_spec(spec)
spec.loader.exec_module(cr)

SESSION = '01234567-89ab-cdef-0123-456789abcdef'
NOW = datetime(2026, 10, 17, 6, 0, 0, tzinfo=timezone.utc)
PR_EVENT = {'id': 'pr0-76', 'kind': 'pr_merged', 'repo': 'Chisanan232/coding-agent-environment', 'number': 76}

KEY = 'pr_merged:Chisanan232/coding-agent-environment#76'


def make(directory, **overrides):
    options = dict(name='spe-108', session_id=SESSION, cwd='/tmp', events=[dict(PR_EVENT)], now=NOW)
    options.update(overrides)
    return cr.register(directory, options.pop('name'), options.pop('session_id'), options.pop('cwd'),
                       options.pop('events'), options.pop('now'), **options)


class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'campaigns'
        self.addCleanup(self.tmp.cleanup)

    def test_register_writes_private_valid_checkpoint(self):
        make(self.dir)
        path = self.dir / 'spe-108.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used'], record['consumed']), ('WAIT_DEPENDENCY', 0, []))

    def test_rejects_malformed_input(self):
        bad = [
            dict(name='../escape'), dict(session_id='not-a-uuid'), dict(cwd='relative/path'),
            dict(events=[]), dict(events=[{'id': 'x', 'kind': 'shell', 'cmd': 'rm -rf /'}]),
            dict(events=[dict(PR_EVENT, repo='a/b; rm')]), dict(events=[dict(PR_EVENT, number=0)]),
            dict(events=[dict(PR_EVENT), dict(PR_EVENT)]), dict(max_resumes=0), dict(cooldown_s=1),
            dict(timeout_s=cr.MAX_TIMEOUT_S + 1), dict(admission_weight='huge'), dict(admission_weight='heavy'),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(cr.CheckpointError):
                make(self.dir, **overrides)
        self.assertEqual(list(self.dir.glob('*.json')), [])

    def test_reregister_keeps_budget_and_only_unchanged_consumed_events(self):
        make(self.dir)
        record = cr.load(self.dir, 'spe-108')
        record.update(resumes_used=2, consumed=[KEY], state='WAITING_OPERATOR')
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        again = make(self.dir, events=[dict(PR_EVENT), dict(PR_EVENT, id='pr1-77', number=77)])
        self.assertEqual((again['resumes_used'], again['consumed'], again['state']), (2, [KEY], 'WAITING_OPERATOR'))
        other = make(self.dir, events=[dict(PR_EVENT, number=99)])
        self.assertEqual(other['consumed'], [])

    def test_load_refuses_symlink_and_garbage(self):
        make(self.dir)
        (self.dir / 'link.json').symlink_to(self.dir / 'spe-108.json')
        with self.assertRaises(cr.CheckpointError):
            cr.load(self.dir, 'link')
        (self.dir / 'junk.json').write_text('{not json')
        with self.assertRaises(cr.CheckpointError):
            cr.load(self.dir, 'junk')

    def test_symlinked_state_directory_is_refused(self):
        real = Path(self.tmp.name) / 'real'
        real.mkdir()
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(real)
        with self.assertRaises(cr.CheckpointError):
            make(link)

    def test_plist_has_interval_and_no_restart_loop(self):
        plist = plistlib.loads(cr.build_plist('/usr/bin/python3', '/x/campaign-resume.py').encode())
        self.assertEqual(plist['ProgramArguments'][2:], ['tick', '--execute'])
        self.assertEqual(plist['StartInterval'], 300)
        self.assertNotIn('KeepAlive', plist)
        self.assertFalse(plist['RunAtLoad'])
        path = plist['EnvironmentVariables']['PATH'].split(':')
        self.assertIn('/opt/homebrew/bin', path)
        self.assertTrue(any(entry.endswith('.local/bin') for entry in path))

    def test_cli_register_status_and_invalid_exit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cr.run(['register', '--campaign', 'c1', '--session-id', SESSION, '--cwd', '/tmp',
                           '--pr-merged', 'Chisanan232/coding-agent-environment#76'], directory=self.dir, now=NOW)
        self.assertEqual(code, 0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cr.run(['status', '--json'], directory=self.dir), 0)
        self.assertEqual(json.loads(out.getvalue())[0]['campaign'], 'c1')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cr.run(['register', '--campaign', 'c2', '--session-id', 'bad', '--cwd', '/tmp',
                                     '--not-before', '2026-10-18T00:00:00Z'], directory=self.dir, now=NOW), 2)


class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'campaigns'
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        which = mock.patch.object(cr.shutil, 'which', return_value='/usr/local/bin/claude')
        which.start()
        self.addCleanup(which.stop)
        self.merged = lambda repo, number: 'MERGED'
        self.open = lambda repo, number: 'OPEN'

    def runner(self, result=None):
        def run(argv, cwd, timeout, log_path, on_start=None):
            self.calls.append((argv, cwd, timeout))
            return result or {'outcome': 'exited', 'code': 0}
        return run

    def tick(self, now=NOW, execute=True, pr_state=None, admit=lambda weight: True, runner=None, age=lambda session, at: 3600):
        return cr.tick(self.dir, now, execute, pr_state or self.merged, admit, runner or self.runner(), age=age)

    def receipts(self):
        path = self.dir / 'receipts.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def decisions(self):
        return [row['decision'] for row in self.receipts()]

    def test_no_event_means_no_resume_and_no_noise(self):
        make(self.dir)
        self.assertEqual(self.tick(pr_state=self.open), [])
        self.assertEqual((self.calls, self.receipts()), ([], []))

    def test_event_resumes_once_with_fixed_safe_command(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(), ['spe-108'])
        argv, cwd, timeout = self.calls[0]
        self.assertEqual(argv[:5], ['/usr/local/bin/claude', '--resume', SESSION, '-p', cr.PROMPT])
        self.assertEqual((cwd, timeout), (self.tmp.name, cr.DEFAULT_TIMEOUT_S))
        for forbidden in ('--dangerously-skip-permissions', '--permission-mode', 'bypassPermissions'):
            self.assertNotIn(forbidden, argv)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used'], record['consumed'], record['lease']),
                         ('WAIT_DEPENDENCY', 1, [KEY], None))
        self.assertEqual(self.decisions(), ['resume_started', 'resume_finished'])
        self.assertNotIn(cr.PROMPT, (self.dir / 'receipts.jsonl').read_text())

    def test_duplicate_and_stale_events_do_not_resume_again(self):
        make(self.dir, cwd=self.tmp.name)
        self.tick()
        later = NOW + timedelta(days=2)
        self.assertEqual(self.tick(now=later), [])
        self.assertEqual(len(self.calls), 1)

    def test_live_lease_blocks_a_concurrent_tick(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', lease={'pid': os.getpid(), 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls, [])

    def test_register_during_a_run_keeps_the_lease(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        lease = {'pid': os.getpid(), 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))}
        record.update(state='RUNNING', lease=lease)
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.assertEqual(make(self.dir, cwd=self.tmp.name)['lease'], lease)

    def test_crash_recovery_clears_dead_lease_without_spending_or_repeating(self):
        make(self.dir, cwd=self.tmp.name)
        dead = subprocess.Popen(['/usr/bin/true'])
        dead.wait()
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', resumes_used=1, consumed=[KEY],
                      lease={'pid': dead.pid, 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.assertEqual(self.tick(), [])
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['lease'], record['resumes_used']), ('WAIT_DEPENDENCY', None, 1))
        self.assertEqual((self.calls, self.decisions()), ([], ['crash_recovered']))

    def test_expired_lease_is_recovered_even_if_pid_is_alive(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', consumed=[KEY],
                      lease={'pid': os.getpid(), 'token': 'x', 'expires': cr.iso_utc(NOW - timedelta(seconds=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.tick()
        self.assertEqual(self.decisions(), ['crash_recovered'])

    def test_clean_exit_without_checkpoint_waits_but_failed_exit_needs_operator(self):
        make(self.dir, cwd=self.tmp.name)
        self.tick(runner=self.runner({'outcome': 'exited', 'code': 0}))
        self.assertEqual(cr.load(self.dir, 'spe-108')['state'], 'WAIT_DEPENDENCY')
        make(self.dir, name='bad', cwd=self.tmp.name)
        self.tick(runner=self.runner({'outcome': 'exited', 'code': 1}))
        self.assertEqual(cr.load(self.dir, 'bad')['state'], 'WAITING_OPERATOR')
        self.assertEqual(self.receipts()[-1]['code'], 1)

    def test_agent_reregister_during_run_is_preserved(self):
        make(self.dir, cwd=self.tmp.name)

        def agent(argv, cwd, timeout, log_path, on_start=None):
            make(self.dir, cwd=self.tmp.name, events=[dict(PR_EVENT, id='pr1-77', number=77)])
            return {'outcome': 'exited', 'code': 0}
        self.tick(runner=agent)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual([event['id'] for event in record['events']], ['pr1-77'])
        self.assertEqual(record['consumed'], [])
        self.assertIsNone(record['lease'])

    def test_admission_denied_spends_nothing_and_honours_cooldown(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(admit=lambda weight: False), [])
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['resumes_used'], record['consumed']), (0, []))
        self.assertEqual(self.decisions(), ['admission_denied'])
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=60)), [])
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=cr.DEFAULT_COOLDOWN_S + 1)), ['spe-108'])

    def test_admission_is_light_only_and_fails_closed(self):
        make(self.dir, cwd=self.tmp.name)
        seen = []
        self.tick(admit=lambda weight: seen.append(weight) or False)
        self.assertEqual(seen, ['light'])
        with mock.patch.object(cr.subprocess, 'run', side_effect=OSError('boom')):
            self.assertFalse(cr.admission_admits('light'))
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 77)):
            self.assertFalse(cr.admission_admits('light'))
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(cr.admission_admits('light'))

    def test_permission_denied_launch_spends_budget_and_needs_operator(self):
        make(self.dir, cwd=self.tmp.name)
        self.tick(runner=self.runner({'outcome': 'launch_failed', 'error': 'PermissionError'}))
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['resumes_used'], record['state']), (1, 'WAITING_OPERATOR'))
        self.assertEqual(self.receipts()[-1]['error'], 'PermissionError')
        make(self.dir, cwd=self.tmp.name, events=[dict(PR_EVENT, id='pr1-77', number=77)])
        self.tick(now=NOW + timedelta(days=1))
        self.assertEqual(len(self.calls), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cr.run(['reset', '--campaign', 'spe-108'], directory=self.dir, now=NOW + timedelta(days=1)), 0)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used']), ('WAIT_DEPENDENCY', 0))
        self.assertEqual(self.decisions()[-1], 'operator_reset')

    def test_lifetime_budget_stops_after_max_resumes(self):
        for index in range(2):
            make(self.dir, cwd=self.tmp.name, max_resumes=2, events=[dict(PR_EVENT, id='pr%d' % index, number=100 + index)])
            self.tick(now=NOW + timedelta(days=index))
        make(self.dir, cwd=self.tmp.name, max_resumes=2, events=[dict(PR_EVENT, id='pr9', number=109)])
        self.tick(now=NOW + timedelta(days=5))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((cr.load(self.dir, 'spe-108')['state'], self.decisions()[-1]), ('WAITING_OPERATOR', 'budget_exhausted'))

    def test_second_timer_wait_after_a_resume_still_fires(self):
        at = {'id': 'time0', 'kind': 'not_before', 'at': '2026-10-17T07:00:00Z'}
        make(self.dir, cwd=self.tmp.name, events=[dict(at)])
        self.tick(now=NOW + timedelta(hours=2))
        make(self.dir, cwd=self.tmp.name, events=[dict(at, at='2026-10-18T07:00:00Z')])
        self.assertEqual(self.tick(now=NOW + timedelta(days=2)), ['spe-108'])
        self.assertEqual(len(self.calls), 2)

    def test_later_campaign_is_stamped_with_its_own_claim_time(self):
        make(self.dir, name='aaa', cwd=self.tmp.name)
        make(self.dir, name='bbb', cwd=self.tmp.name)
        clock_now = [NOW]

        def slow(argv, cwd, timeout, log_path, on_start=None):
            clock_now[0] = NOW + timedelta(hours=3)
            return {'outcome': 'exited', 'code': 0}
        self.tick(now=lambda: clock_now[0], runner=slow)
        record = cr.load(self.dir, 'bbb')
        self.assertEqual(cr.parse_utc(record['last_resume_at']), NOW + timedelta(hours=3))
        self.assertEqual(cr.parse_utc(record['next_attempt_after']), NOW + timedelta(hours=3, seconds=cr.DEFAULT_COOLDOWN_S))
        self.assertEqual(cr.load(self.dir, 'aaa')['last_resume_at'], cr.iso_utc(NOW))

    def test_child_process_keeps_the_lease_alive_after_the_coordinator_dies(self):
        child = subprocess.Popen(['/bin/sleep', '30'])
        self.addCleanup(child.kill)
        dead = subprocess.Popen(['/usr/bin/true'])
        dead.wait()
        lease = {'pid': dead.pid, 'child': child.pid, 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))}
        self.assertTrue(cr.lease_live(lease, NOW))
        child.kill()
        child.wait()
        self.assertFalse(cr.lease_live(lease, NOW))

    def test_child_pid_is_recorded_in_the_lease_while_running(self):
        make(self.dir, cwd=self.tmp.name)
        leases = []

        def run(argv, cwd, timeout, log_path, on_start=None):
            on_start(os.getpid())
            leases.append(cr.load(self.dir, 'spe-108')['lease'])
            return {'outcome': 'exited', 'code': 0}
        self.tick(runner=run)
        self.assertEqual(leases[0]['child'], os.getpid())

    def test_events_are_read_outside_the_lock(self):
        make(self.dir, cwd=self.tmp.name)
        finished = []

        def slow_github(repo, number):
            # The resumed agent registers while the coordinator is inside gh; the lock must be free.
            worker = threading.Thread(target=lambda: make(self.dir, cwd=self.tmp.name))
            worker.start()
            worker.join(5)
            finished.append(not worker.is_alive())
            return 'MERGED'
        self.tick(pr_state=slow_github)
        self.assertEqual(finished, [True])

    def test_unreadable_github_is_receipted_once_per_hour(self):
        make(self.dir, cwd=self.tmp.name)
        unreadable = lambda repo, number: None
        self.tick(pr_state=unreadable)
        self.tick(now=NOW + timedelta(minutes=10), pr_state=unreadable)
        self.assertEqual(self.decisions(), ['github_unreadable'])
        self.tick(now=NOW + timedelta(hours=2), pr_state=unreadable)
        self.assertEqual(self.decisions(), ['github_unreadable'] * 2)

    def test_tick_survives_a_campaign_that_raises_and_a_deleted_checkpoint(self):
        make(self.dir, name='aaa', cwd=self.tmp.name)
        make(self.dir, name='bbb', cwd=self.tmp.name)

        def run(argv, cwd, timeout, log_path, on_start=None):
            if '/aaa' in str(log_path) or str(log_path).endswith('aaa.log'):
                (self.dir / 'aaa.json').unlink()
                return {'outcome': 'exited', 'code': 0}
            raise RuntimeError('boom')
        self.tick(runner=run)
        decisions = self.decisions()
        self.assertIn('resume_finished', decisions)
        self.assertIn('tick_error', decisions)
        finished = [row for row in self.receipts() if row['decision'] == 'resume_finished'][0]
        self.assertEqual(finished['state_after'], 'unreadable')

    def test_timeout_survives_an_already_exited_process_group(self):
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [subprocess.TimeoutExpired('x', 1), 0]
        with mock.patch.object(cr.subprocess, 'Popen', return_value=process), \
                mock.patch.object(cr.os, 'killpg', side_effect=ProcessLookupError):
            result = cr.run_claude(['claude'], self.tmp.name, 1, Path(self.tmp.name) / 'log')
        self.assertEqual(result, {'outcome': 'timeout'})

    def test_real_argv_reaches_the_claude_binary(self):
        fake = Path(self.tmp.name) / 'claude'
        out = Path(self.tmp.name) / 'argv.txt'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > ' + str(out) + '\n')
        fake.chmod(0o755)
        make(self.dir, cwd=self.tmp.name)
        with mock.patch.object(cr.shutil, 'which', return_value=str(fake)):
            self.assertEqual(self.tick(runner=cr.run_claude), ['spe-108'])
        self.assertEqual(out.read_text().splitlines(),
                         ['--resume', SESSION, '-p', cr.PROMPT, '--max-budget-usd', cr.DEFAULT_BUDGET_USD])
        self.assertEqual(cr.load(self.dir, 'spe-108')['state'], 'WAIT_DEPENDENCY')

    def test_real_launch_failure_and_timeout(self):
        script = Path(self.tmp.name) / 'noexec'
        script.write_text('#!/bin/sh\n')
        log = Path(self.tmp.name) / 'run.log'
        self.assertEqual(cr.run_claude([str(script)], self.tmp.name, 5, log), {'outcome': 'launch_failed', 'error': 'PermissionError'})
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(cr.run_claude(['/bin/sleep', '30'], self.tmp.name, 1, log), {'outcome': 'timeout'})
        self.assertEqual(cr.run_claude(['/usr/bin/true'], self.tmp.name, 5, log), {'outcome': 'exited', 'code': 0})

    def test_dry_run_launches_nothing(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(execute=False), [])
        self.assertEqual((self.calls, self.decisions()), ([], ['dry_run_would_resume']))
        self.assertEqual(cr.load(self.dir, 'spe-108')['resumes_used'], 0)

    def test_missing_cwd_or_claude_is_a_cooldown_not_a_spend(self):
        make(self.dir, cwd='/nonexistent-dir-for-test')
        self.tick()
        self.assertEqual(self.decisions(), ['cwd_missing'])
        make(self.dir, name='other', cwd=self.tmp.name)
        with mock.patch.object(cr.shutil, 'which', return_value=None):
            self.tick(now=NOW + timedelta(seconds=1))
        self.assertIn('claude_not_found', self.decisions())
        self.assertEqual(self.calls, [])

    def test_not_before_event_and_unreadable_github(self):
        make(self.dir, cwd=self.tmp.name, events=[{'id': 't0', 'kind': 'not_before', 'at': '2026-10-17T07:00:00Z'}])
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(now=NOW + timedelta(hours=2)), ['spe-108'])
        with mock.patch.object(cr.subprocess, 'run', side_effect=OSError('no gh')):
            self.assertIsNone(cr.gh_pr_state('o/r', 1))
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stdout='MERGED')):
            self.assertIsNone(cr.gh_pr_state('o/r', 1))

    def test_corrupt_checkpoint_is_reported_and_other_campaigns_still_run(self):
        make(self.dir, cwd=self.tmp.name)
        (self.dir / 'broken.json').write_text('{oops')
        self.assertEqual(self.tick(), ['spe-108'])
        self.assertIn('invalid_checkpoint', self.decisions())

    def test_done_and_operator_states_are_never_resumed(self):
        for state in ('DONE', 'WAITING_OPERATOR'):
            make(self.dir, cwd=self.tmp.name)
            record = cr.load(self.dir, 'spe-108')
            record['state'] = state
            cr.write_json_atomic(self.dir / 'spe-108.json', record)
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls, [])

    def test_reregistering_an_already_consumed_pr_does_not_resume_again(self):
        make(self.dir, cwd=self.tmp.name)
        self.tick()
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(now=NOW + timedelta(days=2)), [])
        self.assertEqual(len(self.calls), 1)

    def test_commit_compares_event_content_when_register_races_the_gh_call(self):
        make(self.dir, cwd=self.tmp.name)

        def swap_the_wait(repo, number):
            make(self.dir, cwd=self.tmp.name, events=[dict(PR_EVENT, repo='other/repo')])
            return 'MERGED'
        self.assertEqual(self.tick(pr_state=swap_the_wait), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(cr.load(self.dir, 'spe-108')['resumes_used'], 0)

    def test_invalid_name_never_touches_the_filesystem_outside_the_state_dir(self):
        for call in (lambda: make(self.dir, name='../x'), lambda: cr.reset(self.dir, '../x', NOW)):
            with self.assertRaises(cr.CheckpointError):
                call()
        self.assertFalse((self.dir.parent / 'x.lock').exists())

    def test_reset_refuses_while_a_resume_is_running(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', lease={'pid': os.getpid(), 'child': None, 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        with self.assertRaises(cr.CheckpointError):
            cr.reset(self.dir, 'spe-108', NOW)
        self.assertEqual(cr.load(self.dir, 'spe-108')['state'], 'RUNNING')

    def test_finish_leaves_a_checkpoint_it_no_longer_owns(self):
        make(self.dir, cwd=self.tmp.name)

        def takeover(argv, cwd, timeout, log_path, on_start=None):
            record = cr.load(self.dir, 'spe-108')
            record['lease']['token'] = 'someone-else'
            cr.write_json_atomic(self.dir / 'spe-108.json', record)
            return {'outcome': 'exited', 'code': 1}
        self.tick(runner=takeover)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['lease']['token']), ('RUNNING', 'someone-else'))

    def test_real_runner_records_the_child_pid_in_the_lease(self):
        fake = Path(self.tmp.name) / 'claude'
        fake.write_text('#!/bin/sh\nsleep 1\n')
        fake.chmod(0o755)
        make(self.dir, cwd=self.tmp.name)
        recorded = []
        original = cr._record_child

        def spy(directory, name, token, pid):
            original(directory, name, token, pid)
            recorded.append((cr.load(directory, name)['lease']['child'], pid))
        with mock.patch.object(cr.shutil, 'which', return_value=str(fake)), mock.patch.object(cr, '_record_child', spy):
            self.tick(runner=cr.run_claude)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0], recorded[0][1])
        self.assertGreater(recorded[0][1], 0)


    def test_cli_refuses_heavy_automatic_resume(self):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as raised:
            cr.run(['register', '--campaign', 'c', '--session-id', SESSION, '--cwd', '/tmp', '--pr-merged', 'o/r#1',
                    '--weight', 'heavy'], directory=self.dir, now=NOW)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(list(self.dir.glob('*.json')) if self.dir.exists() else [], [])


    def test_recently_active_or_missing_session_is_never_resumed(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(age=lambda session, at: 5), [])
        self.assertEqual(self.tick(age=lambda session, at: None, now=NOW + timedelta(seconds=cr.DEFAULT_COOLDOWN_S + 1)), [])
        self.assertEqual(self.decisions(), ['session_active', 'session_not_found'])
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['resumes_used'], record['consumed']), (0, []))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tick(now=NOW + timedelta(days=1)), ['spe-108'])

    def test_session_age_uses_transcript_mtime_without_reading_it(self):
        home = Path(self.tmp.name) / 'home'
        transcript = home / '.claude/projects/-some-cwd' / ('%s.jsonl' % SESSION)
        transcript.parent.mkdir(parents=True)
        transcript.write_text('SECRET-CONTENT')
        os.utime(transcript, (NOW.timestamp() - 600, NOW.timestamp() - 600))
        with mock.patch.object(cr, 'account_home', return_value=home), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('transcript must not be read')):
            self.assertEqual(cr.session_age(SESSION, NOW), 600)
            self.assertIsNone(cr.session_age('11111111-2222-3333-4444-555555555555', NOW))

    def test_receipts_and_logs_are_bounded(self):
        path = self.dir / 'receipts.jsonl'
        self.dir.mkdir(parents=True)
        path.write_text('x' * (cr.MAX_LOG_BYTES + 1))
        cr.receipt(self.dir, NOW, 'c', 'probe')
        self.assertTrue((self.dir / 'receipts.jsonl.1').exists())
        self.assertEqual(len(path.read_text().splitlines()), 1)
        make(self.dir, cwd=self.tmp.name)
        log = self.dir / 'spe-108.log'
        log.write_text('y' * (cr.MAX_LOG_BYTES + 1))
        self.tick()
        self.assertTrue((self.dir / 'spe-108.log.1').exists())


    def test_file_marker_event_fires_when_the_file_appears(self):
        marker = Path(self.tmp.name) / 'operator-done'
        make(self.dir, cwd=self.tmp.name, events=[{'id': 'f0', 'kind': 'file_exists', 'path': str(marker)}])
        self.assertEqual(self.tick(), [])
        marker.write_text('')
        self.assertEqual(self.tick(), ['spe-108'])
        self.assertEqual(self.tick(now=NOW + timedelta(days=2)), [])

    def test_file_marker_event_rejects_relative_and_traversal_paths(self):
        for path in ('relative', '/a/../etc/passwd', '/a/b/..', 5):
            with self.subTest(path=path), self.assertRaises(cr.CheckpointError):
                make(self.dir, events=[{'id': 'f0', 'kind': 'file_exists', 'path': path}])

    def test_idle_ticks_spawn_nothing(self):
        make(self.dir, cwd=self.tmp.name)
        for minute in range(0, 60, 5):
            self.tick(now=NOW + timedelta(minutes=minute), pr_state=self.open)
        self.assertEqual((self.calls, self.receipts()), ([], []))


    def test_session_that_becomes_active_during_the_admission_window_is_not_resumed(self):
        make(self.dir, cwd=self.tmp.name)
        ages = iter([3600, 5])
        self.assertEqual(self.tick(age=lambda session, at: next(ages)), [])
        self.assertEqual((self.calls, self.decisions()), ([], ['session_active']))
        self.assertEqual(cr.load(self.dir, 'spe-108')['resumes_used'], 0)

    def test_claude_config_dir_is_honoured_by_the_session_lookup(self):
        config = Path(self.tmp.name) / 'cfg'
        transcript = config / 'projects/-x' / ('%s.jsonl' % SESSION)
        transcript.parent.mkdir(parents=True)
        transcript.write_text('')
        os.utime(transcript, (NOW.timestamp() - 300, NOW.timestamp() - 300))
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config)}):
            self.assertEqual(cr.session_age(SESSION, NOW), 300)

    def test_concurrent_receipts_keep_every_line_across_rotation(self):
        self.dir.mkdir(parents=True)
        (self.dir / 'receipts.jsonl').write_text('x' * (cr.MAX_LOG_BYTES + 1))
        workers = [threading.Thread(target=lambda n=n: cr.receipt(self.dir, NOW, 'c', 'probe', n=n)) for n in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        lines = (self.dir / 'receipts.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertEqual((self.dir / 'receipts.jsonl.1').stat().st_size, cr.MAX_LOG_BYTES + 1)


if __name__ == '__main__':
    unittest.main()
