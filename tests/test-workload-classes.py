#!/usr/bin/env python3
"""Optional workload classes and estimates (SPE-150 PR3): recorded and validated, never used to admit."""
from importlib.util import module_from_spec, spec_from_file_location
import contextlib
import io
import json
import os
import subprocess
import tempfile
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
QueueBase = load('admission_queue_tests', 'tests/test-admission-queue.py').QueueBase
GIB = 1024 ** 3
SESSION = '0b1d2e3f-aaaa-bbbb-cccc-1234567890ab'
ARGV = ['cargo', 'nextest', 'run']
# Computed with the pre-change _fingerprint on origin/main; a legacy entry's digest must never move.
GOLDEN_FINGERPRINT = 'dec58023c2f59ba046db90ba0ff56f52a6821491bec81d69086f872b6ff56698'


class Resolve(unittest.TestCase):
    def resolve(self, name=None, mem=None, cpus=None, nbytes=None, env=None, home='/nonexistent-home', **kw):
        return da.resolve_workload(name, mem, cpus, nbytes, env={} if env is None else env, home=home, cwd='/work/wt', **kw)

    def test_nothing_declared_is_the_legacy_meaning_and_records_nothing(self):
        self.assertEqual(self.resolve(), ({}, None))

    def test_class_defaults_come_from_the_one_table(self):
        fields, error = self.resolve('build-workspace', nbytes=40 * GIB)
        self.assertIsNone(error)
        self.assertEqual((fields['class'], fields['mem_bytes'], fields['cpu_slots'], fields['test_threads']),
                         ('build-workspace', 16 * GIB, 8, 8))
        crate, _ = self.resolve('build-crate')
        self.assertEqual((crate['mem_bytes'], crate['cpu_slots']), (6 * GIB, 4))
        suite, _ = self.resolve('container-suite')
        self.assertEqual((suite['mem_bytes'], suite['excl_keys']), (12 * GIB, ['docker']))
        light, _ = self.resolve('light')
        self.assertNotIn('mem_bytes', light)
        self.assertEqual(light['excl_keys'], [])

    def test_estimates_can_be_raised_but_never_lowered(self):
        raised, error = self.resolve('build-crate', mem=8, cpus=6, nbytes=20 * GIB)
        self.assertIsNone(error)
        self.assertEqual((raised['mem_bytes'], raised['cpu_slots']), (8 * GIB, 6))
        for kwargs in (dict(mem=5), dict(cpus=3), dict(nbytes=9 * GIB)):
            fields, error = self.resolve('build-crate', **kwargs)
            self.assertIsNone(fields)
            self.assertIn('never lowered', error, kwargs)

    def test_unknown_class_fails_closed(self):
        for name in ('build-everything', '', 'BUILD-CRATE', 'legacy'):
            fields, error = self.resolve(name)
            self.assertIsNone(fields)
            self.assertIn('unknown workload class', error)

    def test_bad_numbers_are_refused(self):
        for kwargs in (dict(mem=0), dict(mem=-1), dict(mem=float('nan')), dict(mem=float('inf')), dict(mem=True), dict(mem=10 ** 9),
                       dict(cpus=0), dict(cpus=True), dict(cpus=10 ** 6), dict(cpus=2.5)):
            self.assertIsNotNone(self.resolve('light', **kwargs)[1], kwargs)

    def test_excl_key_comes_from_the_target_dir_without_executing_anything(self):
        env_fields, _ = self.resolve('build-crate', env={'CARGO_TARGET_DIR': '/t/isolated/../mine'})
        self.assertEqual(env_fields['excl_keys'], ['cargo-target:/t/mine'])
        relative, _ = self.resolve('build-crate', env={'CARGO_TARGET_DIR': 'out'})
        self.assertEqual(relative['excl_keys'], ['cargo-target:/work/wt/out'])
        default, _ = self.resolve('build-crate')
        self.assertEqual(default['excl_keys'], ['cargo-target:/work/wt/target'])
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / '.cargo').mkdir()
            (Path(home) / '.cargo/config.toml').write_text('[build]\ntarget-dir = "/shared/cargo-target"\n')
            shared, _ = self.resolve('build-workspace', home=home)
            self.assertEqual(shared['excl_keys'], ['cargo-target:/shared/cargo-target'])
            (Path(home) / '.cargo/config.toml').write_text('this is [not toml')
            self.assertEqual(self.resolve('build-workspace', home=home)[0]['excl_keys'], ['cargo-target:/work/wt/target'])
        with unittest.mock.patch.object(subprocess, 'run', side_effect=AssertionError('executed')):
            self.resolve('build-crate')

    def test_owner_and_created_are_recorded(self):
        fields, _ = self.resolve('light', session_id=SESSION, ticket='SPE-150')
        self.assertEqual(fields['owner'], {'uid': os.getuid(), 'session_id': SESSION, 'ticket': 'SPE-150'})
        self.assertTrue(da._valid_declared(fields))


class Hostile(unittest.TestCase):
    GOOD = {'class': 'build-crate', 'mem_bytes': 6 * GIB, 'cpu_slots': 4, 'test_threads': 4,
            'excl_keys': ['cargo-target:/t'], 'created': '2026-10-31T00:00:00Z',
            'owner': {'uid': 501, 'session_id': SESSION, 'ticket': 'SPE-150'}}

    def test_good_record_and_absent_fields_pass(self):
        self.assertTrue(da._valid_declared(dict(self.GOOD)))
        self.assertTrue(da._valid_declared({}))

    def test_malformed_values_are_rejected(self):
        bad = {'class': [5, 'X y', '', 'a' * 40, None, True], 'mem_bytes': [0, -1, True, 1.5, '6', 2 ** 80, None],
               'cpu_slots': [0, True, 10 ** 9, '4', 1.0], 'test_threads': [0, False, 2 ** 40],
               'excl_keys': ['x', [1], ['a\nb'], ['a\0b'], [''], ['k' * 600], ['k'] * 9, None],
               'created': ['yesterday', '2026-10-31T00:00:00', 7, 'z' * 100, None],
               'owner': ['me', {'uid': True}, {'uid': -1}, {'uid': 2 ** 40}, {'uid': 1, 'session_id': 'x y'},
                         {'uid': 1, 'ticket': '../x'}, {'uid': 1, 'session_id': 5}, None, []]}
        for key, values in bad.items():
            for value in values:
                self.assertFalse(da._valid_declared(dict(self.GOOD, **{key: value})), (key, value))


class Records(QueueBase):
    def declared(self, name='build-crate', **kw):
        fields, error = da.resolve_workload(name, kw.get('mem'), kw.get('cpus'), None, env={}, home='/nonexistent-home',
                                            cwd=str(self.worktree), session_id=SESSION, ticket='HORO-2070', now=self.now)
        self.assertIsNone(error)
        return fields

    def claim(self, qid, declared=None, nbytes=20 * GIB, pid=None):
        return da.queue_claim(self.path, qid, 'nda-horo-2070', nbytes, ARGV, str(self.worktree), lambda: 400 * GIB,
                              pid=pid or os.getpid(), now=self.now, memory_fn=lambda: (True, 'ok'),
                              exec_fn=lambda program, args: None, declared=declared)

    def test_fingerprint_without_declaration_is_unchanged(self):
        salt = b'a' * 64
        self.assertEqual(da._fingerprint(salt, 'horonom/nda', '/tmp/wt', ['B-2', 'A-1'], ['cargo', 'test', '--', 'x'], 21474836480),
                         GOLDEN_FINGERPRINT)
        self.assertEqual(da._fingerprint(salt, 'horonom/nda', '/tmp/wt', ['B-2', 'A-1'], ['cargo', 'test', '--', 'x'], 21474836480,
                                         da._declared({})), GOLDEN_FINGERPRINT)
        self.assertNotEqual(da._fingerprint(salt, 'horonom/nda', '/tmp/wt', ['B-2', 'A-1'], ['cargo', 'test', '--', 'x'], 21474836480,
                                            ('build-crate', 6 * GIB, 4, 4)), GOLDEN_FINGERPRINT)

    def test_entry_with_and_without_class_round_trips(self):
        legacy = self.enqueue('HORO-2070', argv=ARGV)
        classed = self.enqueue('HORO-2071', argv=ARGV, declared=self.declared())
        self.assertTrue(legacy['ok'] and classed['ok'], (legacy, classed))
        entries = self.entries()
        for key in da.WORKLOAD_FIELDS:
            if key != 'created':
                self.assertNotIn(key, entries[legacy['id']])
        got = entries[classed['id']]
        self.assertEqual((got['class'], got['mem_bytes'], got['cpu_slots'], got['owner']['ticket']), ('build-crate', 6 * GIB, 4, 'HORO-2070'))
        self.assertEqual(got['version'], 1)
        self.assertEqual(got['created'], da.iso_utc(self.now))

    def test_legacy_entry_is_still_claimable_and_its_reservation_stays_legacy(self):
        qid = self.enqueue('HORO-2070', argv=ARGV)['id']
        self.advance()
        out = self.claim(qid)
        self.assertTrue(out['ok'], out)
        record = json.loads((self.path.parent / 'reservations/nda-horo-2070.json').read_text())
        for key in da.WORKLOAD_FIELDS:
            self.assertNotIn(key, record)

    def test_classed_entry_claims_and_the_reservation_carries_the_fields(self):
        declared = self.declared()
        qid = self.enqueue('HORO-2070', argv=ARGV, declared=declared)['id']
        self.advance()
        out = self.claim(qid, declared)
        self.assertTrue(out['ok'], out)
        record = da._read_reservation(self.path.parent / 'reservations/nda-horo-2070.json')
        self.assertEqual((record['class'], record['mem_bytes'], record['cpu_slots']), ('build-crate', 6 * GIB, 4))
        self.assertEqual(da.declared_summary([record])[0]['class'], 'build-crate')

    def test_claim_with_a_different_class_or_estimate_is_refused_and_keeps_the_grant(self):
        declared = self.declared()
        qid = self.enqueue('HORO-2070', argv=ARGV, declared=declared)['id']
        self.advance()
        for other in (None, {}, self.declared('build-workspace'), self.declared(mem=8), self.declared(cpus=6)):
            out = self.claim(qid, other)
            self.assertFalse(out['ok'], other)
            self.assertIn('different workload class or estimate', out['reason'])
        self.assertEqual(self.entries()[qid]['state'], 'GRANTED')

    def test_claiming_a_legacy_entry_with_a_class_is_refused(self):
        qid = self.enqueue('HORO-2070', argv=ARGV)['id']
        self.advance()
        out = self.claim(qid, self.declared())
        self.assertFalse(out['ok'])
        self.assertIn('different workload class or estimate', out['reason'])

    def test_hostile_entry_and_reservation_files_are_problems_never_deleted(self):
        proc = self.holder('held')
        res = self.path.parent / 'reservations/held.json'
        record = json.loads(res.read_text())
        qid = self.enqueue('HORO-2070', argv=ARGV, declared=self.declared())['id']
        path = self.path.parent / 'queue' / (qid + '.json')
        entry = json.loads(path.read_text())
        for key, value in (('mem_bytes', True), ('cpu_slots', 2 ** 90), ('class', 'X Y'), ('excl_keys', 'x'), ('owner', {'uid': True})):
            path.write_text(json.dumps(dict(entry, **{key: value})))
            entries, problems = da._queue_entries(self.path)
            self.assertEqual(entries, [], key)
            self.assertIn('queue-entry-unreadable', problems[0])
            self.assertTrue(path.exists())
        path.write_text(json.dumps(entry))
        res.write_text(json.dumps(dict(record, mem_bytes=-5)))
        live, problems = da.live_reservations(self.path, self.now)
        self.assertEqual(live, [])
        self.assertIn('reservation-unreadable', problems[0])
        self.assertTrue(res.exists())
        self.assertIsNotNone(proc.pid)

    def test_direct_reserve_records_fields_and_malformed_ones_are_refused(self):
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        refused = da.reserve('job', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared={'mem_bytes': True})
        self.assertFalse(refused['admit'])
        self.assertIn('malformed', refused['reason'])
        self.assertFalse(da.reserve('job', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now,
                                    declared={'bogus': 1})['admit'])
        ok = da.reserve('job', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared=self.declared())
        self.assertTrue(ok['admit'], ok)
        record = da._read_reservation(self.path.parent / 'reservations/job.json')
        self.assertEqual(record['class'], 'build-crate')


class Decisions(QueueBase):
    def test_a_declared_estimate_changes_no_admission_decision(self):
        """Lane cap 1 and the byte floor read only `bytes`: a huge declared memory estimate neither blocks nor adds a lane."""
        fields, _ = da.resolve_workload('container-suite', 4000, 1000, None, env={}, home='/x', cwd='/x')
        first = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        second = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders += [first, second]
        self.assertTrue(da.reserve('one', 20 * GIB, first.pid, self.path, lambda: 400 * GIB, now=self.now, declared=fields)['admit'])
        refused = da.reserve('two', 20 * GIB, second.pid, self.path, lambda: 400 * GIB, now=self.now)
        self.assertFalse(refused['admit'])
        self.assertTrue(refused['reason'].startswith('lane-cap'))

    def test_snapshot_and_status_show_workloads_only_when_declared(self):
        plain = da.snapshot(self.path, lambda: (400 * GIB, 'test'), now=self.now)
        self.assertNotIn('workloads', plain)
        fields, _ = da.resolve_workload('build-crate', None, None, None, env={}, home='/x', cwd='/x')
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        self.assertTrue(da.reserve('job', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared=fields)['admit'])
        shot = da.snapshot(self.path, lambda: (400 * GIB, 'test'), now=self.now)
        self.assertEqual(shot['workloads'][0]['class'], 'build-crate')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            da.main(['status', '--json'], free_fn=lambda: 400 * GIB, state_path=self.path)
        self.assertEqual(json.loads(out.getvalue())['workloads'][0]['label'], 'job')

    def test_measurement_lines_carry_the_class(self):
        fields, _ = da.resolve_workload('build-crate', None, None, None, env={}, home='/x', cwd='/x')
        proc = subprocess.Popen(['/bin/sleep', '300'], stdin=subprocess.DEVNULL)
        self.holders.append(proc)
        da.reserve('job', 20 * GIB, proc.pid, self.path, lambda: 400 * GIB, now=self.now, declared=fields)
        lines, _ = da._read_measurement_lines(self.path)
        self.assertEqual([ln.get('class') for ln in lines if ln.get('label') == 'job'], ['build-crate'])


class Cli(QueueBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = da.run(list(argv), state_path=self.path, free_fn=lambda: 400 * GIB)
        return rc, out.getvalue()

    def test_enqueue_with_class_and_unknown_class(self):
        base = ['queue', 'enqueue', '--label', 'nda-x', '--ticket', 'HORO-2070', '--repo', 'horonom/nda', '--worktree',
                str(self.worktree), '--harness', 'codex', '--session-id', SESSION, '--bytes', str(20 * GIB)]
        rc, out = self.run_cli(*base, '--class', 'build-crate', '--cpus', '6', '--', *ARGV)
        self.assertEqual(rc, 0, out)
        entry = self.entries()[json.loads(out)['id']]
        self.assertEqual((entry['class'], entry['cpu_slots'], entry['owner']['session_id']), ('build-crate', 6, SESSION))
        rc, out = self.run_cli(*base, '--class', 'nope', '--', *ARGV)
        self.assertEqual(rc, 77)
        self.assertIn('unknown workload class', out)
        rc, out = self.run_cli(*base, '--class', 'build-crate', '--mem-gib', '1', '--', *ARGV)
        self.assertEqual(rc, 77)
        self.assertIn('never lowered', out)

    def test_reserve_and_run_refuse_unknown_class_before_touching_state(self):
        rc, out = self.run_cli('reserve', '--bytes', str(20 * GIB), '--pid', str(os.getpid()), '--label', 'job', '--class', 'zzz')
        self.assertEqual(rc, 77)
        rc, out = self.run_cli('run', '--bytes', str(20 * GIB), '--label', 'job', '--class', 'zzz', '--', 'true')
        self.assertEqual(rc, 77)
        self.assertFalse((self.path.parent / 'reservations').exists())


if __name__ == '__main__':
    unittest.main()
