#!/usr/bin/env python3
"""cargo-deps-reclaim (SPE-164): attribution by the artifact's own recorded root, and every protection of `apply`."""
from importlib.util import module_from_spec, spec_from_file_location
import argparse
import contextlib
import fcntl
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('cdr', ROOT / 'scripts/cargo-deps-reclaim.py')
cdr = module_from_spec(spec)
spec.loader.exec_module(cdr)

OLD = time.time() - 3 * 86400
H = 'a' * 16          # partition 'a'
H2 = 'b' * 16         # partition 'b'


class Fixture:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.base = base
        self.live = base / 'work'                       # the allowed prefix, exists
        self.live.mkdir()
        self.target = base / 'target'
        self.deps = self.target / 'debug/deps'
        self.deps.mkdir(parents=True)
        (self.target / 'CACHEDIR.TAG').write_text('Signature: 8a477f597d28d172789f06886806bc55\n')
        for lock in ('.cargo-lock', '.cargo-build-lock'):
            (self.target / 'debug' / lock).write_text('')
        self.state = base / 'state'
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        self.prefixes = [str(self.live)]

    def crate(self, stem, digest, root, files=('rlib', 'rmeta', 'd', 'x.cgu.0.rcgu.o'), old=True):
        for suffix in files:
            name = '%s-%s.%s' % (stem, digest, suffix)
            path = self.deps / name
            if suffix == 'd':
                lines = ['# env-dep:CARGO_MANIFEST_DIR=%s' % root] if root else []
                path.write_text('\n'.join(lines + ['%s: x.rs' % name]) + '\n')
            else:
                path.write_bytes(b'x' * 8192)
            if old:
                os.utime(path, (OLD, OLD))

    def args(self, **kw):
        base = dict(deps=str(self.deps), state=str(self.state), max_seconds=60, prefix=self.prefixes, execute=False,
                    batch_gib=0.000001, max_gib=20.0, min_age_hours=24.0, reset=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def classify(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cdr.classify(self.args())

    def apply(self, free_fn=None, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cdr.apply(self.args(**kw), free_fn=free_fn or self.honest_free())
        return out.getvalue()

    def honest_free(self):
        """Free space that grows by exactly the bytes the deps directory loses (each inode counted once)."""
        def used():
            return sum({p.stat().st_ino: p.stat().st_blocks * 512 for p in self.deps.iterdir()}.values())
        start = used()
        return lambda: 10 ** 12 + start - used()

    def names(self):
        return {p.name for p in self.deps.iterdir()}


def gone_root(fx):
    return str(fx.live / 'removed-worktree/crates/x')


class ClassifyTests(unittest.TestCase):
    def test_missing_root_is_obsolete_present_root_is_kept(self):
        fx = Fixture(self)
        present = fx.live / 'live-worktree'
        present.mkdir()
        fx.crate('gone', H, gone_root(fx))
        fx.crate('live', H, str(present))
        fx.classify()
        plan = cdr.read_plan(cdr.private_state(fx.state))
        self.assertTrue(plan)
        self.assertTrue(all(e['name'].startswith('gone-') for e in plan))
        self.assertNotIn('live', ''.join(e['name'] for e in plan))

    def test_no_recorded_root_conflicting_roots_and_outside_prefix_are_kept(self):
        fx = Fixture(self)
        fx.crate('noroot', H, None)
        fx.crate('outside', H, '/elsewhere/gone/crate')
        fx.crate('conflict', H, gone_root(fx), files=('rlib', 'd'))
        extra = fx.deps / ('conflict-%s.d' % H)
        extra.write_text('# env-dep:CARGO_MANIFEST_DIR=%s\n# env-dep:CARGO_MANIFEST_DIR=%s/other\n' % (gone_root(fx), gone_root(fx)))
        fx.classify()
        self.assertEqual(cdr.read_plan(cdr.private_state(fx.state)), [])

    def test_root_under_a_prefix_that_does_not_exist_is_kept(self):
        fx = Fixture(self)
        fx.prefixes = [str(fx.base / 'unmounted')]
        fx.crate('gone', H, str(fx.base / 'unmounted/wt/crate'))
        fx.classify()
        self.assertEqual(cdr.read_plan(cdr.private_state(fx.state)), [])

    def test_executables_and_libraries_are_kept_even_when_the_root_is_gone(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx), files=('d', 'rlib', 'dylib'))
        (fx.deps / ('gone-%s' % H)).write_bytes(b'bin')           # extensionless test executable
        fx.classify()
        names = {e['name'] for e in cdr.read_plan(cdr.private_state(fx.state))}
        self.assertEqual(names, {'gone-%s.rlib' % H})

    def test_classification_resumes_by_partition_and_is_idempotent(self):
        fx = Fixture(self)
        fx.crate('one', H, gone_root(fx))
        fx.crate('two', H2, gone_root(fx))
        fx.classify()
        first = len(cdr.read_plan(cdr.private_state(fx.state)))
        fx.classify()
        self.assertEqual(len(cdr.read_plan(cdr.private_state(fx.state))), first)
        self.assertEqual(first, 6)

    def test_a_directory_that_is_not_a_cargo_target_is_refused(self):
        fx = Fixture(self)
        (fx.target / 'CACHEDIR.TAG').unlink()
        with self.assertRaises(SystemExit):
            fx.classify()

    def test_keep_marker_refuses_the_target(self):
        fx = Fixture(self)
        (fx.target / '.ca-keep').write_text('')
        with self.assertRaises(SystemExit):
            fx.classify()


class ApplyTests(unittest.TestCase):
    def prepared(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx))
        fx.classify()
        return fx

    def test_d_files_are_never_deleted(self):
        fx = self.prepared()
        fx.apply(execute=True)
        self.assertTrue((fx.deps / ('gone-%s.d' % H)).exists())

    def test_dry_run_deletes_nothing(self):
        fx = self.prepared()
        before = fx.names()
        out = fx.apply()
        self.assertEqual(fx.names(), before)
        self.assertIn('would remove 3 files', out)

    def test_execute_removes_only_the_classified_obsolete_files(self):
        fx = self.prepared()
        fx.crate('live', 'c' * 16, None)                           # unclassified: no recorded root
        keep = {n for n in fx.names() if n.startswith('live-')} | {'gone-%s.d' % H}
        out = fx.apply(execute=True)
        self.assertEqual(fx.names(), keep)
        self.assertIn('removed 3 files', out)

    def test_estimate_and_measured_change_are_reported_separately(self):
        fx = self.prepared()
        out = fx.apply(execute=True)
        self.assertIn('ESTIMATE, hard-link-deduplicated', out)
        self.assertIn('measured free-space change', out)

    def test_active_build_lock_refuses_and_deletes_nothing(self):
        fx = self.prepared()
        before = fx.names()
        fd = os.open(fx.target / 'debug/.cargo-lock', os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(SystemExit) as caught:
            fx.apply(execute=True)
        self.assertIn('build is active', str(caught.exception))
        self.assertEqual(fx.names(), before)

    def test_no_lock_file_fails_closed(self):
        fx = self.prepared()
        for lock in (fx.target / 'debug').glob('.cargo-*lock'):
            lock.unlink()
        with self.assertRaises(SystemExit):
            fx.apply(execute=True)

    def test_locks_are_released_after_apply(self):
        fx = self.prepared()
        fx.apply(execute=True)
        fd = os.open(fx.target / 'debug/.cargo-lock', os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_hard_link_outside_the_deletion_set_is_never_unlinked(self):
        fx = self.prepared()
        outside = fx.base / 'outside-link'
        os.link(fx.deps / ('gone-%s.rlib' % H), outside)           # second link the plan never saw
        fx.apply(execute=True)
        self.assertTrue((fx.deps / ('gone-%s.rlib' % H)).exists())
        self.assertEqual(outside.read_bytes(), b'x' * 8192)

    def test_hard_link_inside_the_set_is_removed_together(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx), files=('d', 'rlib'))
        os.link(fx.deps / ('gone-%s.rlib' % H), fx.deps / ('gone-%s.rmeta' % H))
        fx.classify()
        fx.apply(execute=True)
        self.assertEqual(fx.names(), {'gone-%s.d' % H})

    def test_root_that_reappears_before_apply_keeps_the_files(self):
        fx = self.prepared()
        Path(gone_root(fx)).mkdir(parents=True)
        before = fx.names()
        fx.apply(execute=True)
        self.assertEqual(fx.names(), before)

    def test_file_changed_since_classification_is_kept(self):
        fx = self.prepared()
        target = fx.deps / ('gone-%s.rlib' % H)
        os.utime(target, (OLD + 100, OLD + 100))
        fx.apply(execute=True)
        self.assertTrue(target.exists())

    def test_young_files_are_kept(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx), old=False)
        fx.classify()
        before = fx.names()
        fx.apply(execute=True)
        self.assertEqual(fx.names(), before)

    def test_keep_marker_added_after_classification_refuses(self):
        fx = self.prepared()
        (fx.deps / '.ca-keep').write_text('')
        with self.assertRaises(SystemExit):
            fx.apply(execute=True)

    def test_stop_guard_halts_when_free_space_does_not_move(self):
        fx = Fixture(self)
        for i in range(6):
            fx.crate('gone%d' % i, H, gone_root(fx), files=('rlib', 'd'))
        fx.classify()
        out = fx.apply(execute=True, free_fn=lambda: 1_000_000_000)    # frees nothing, as if a snapshot held the blocks
        self.assertIn('STOP', out)
        gone = [n for n in range(6) if not (fx.deps / ('gone%d-%s.rlib' % (n, H))).exists()]
        self.assertEqual(len(gone), 1)                  # the first batch only; the other five files were never touched

    def test_pre_existing_outside_link_keeps_the_file_at_classify_time(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx), files=('d', 'rlib'))
        os.link(fx.deps / ('gone-%s.rlib' % H), fx.base / 'outside-link')    # already there when classify runs
        fx.classify()
        self.assertEqual([e for e in cdr.read_plan(cdr.private_state(fx.state)) if e['kind'] == 'rlib'], [e for e in cdr.read_plan(cdr.private_state(fx.state))])
        self.assertEqual(cdr.eligible(cdr.read_plan(cdr.private_state(fx.state)), time.time(), 0), [])
        fx.apply(execute=True)
        self.assertTrue((fx.deps / ('gone-%s.rlib' % H)).exists())

    def test_symlinked_ancestor_is_not_proof_of_absence(self):
        fx = Fixture(self)
        (fx.live / 'moved').symlink_to(fx.base / 'unmounted-drive')            # dangling, as an unmounted volume would be
        fx.crate('gone', H, str(fx.live / 'moved/wt/crate'))
        fx.classify()
        self.assertEqual(cdr.read_plan(cdr.private_state(fx.state)), [])

    def test_dotdot_and_unnormalised_roots_are_kept(self):
        fx = Fixture(self)
        fx.crate('dd', H, str(fx.live) + '/a/../b/crate')
        fx.crate('dbl', H, str(fx.live) + '//b/crate')
        fx.classify()
        self.assertEqual(cdr.read_plan(cdr.private_state(fx.state)), [])

    def test_permission_error_on_a_component_counts_as_present(self):
        fx = Fixture(self)
        locked = fx.live / 'locked'
        locked.mkdir()
        fx.crate('gone', H, str(locked / 'wt/crate'))
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        fx.classify()
        self.assertEqual(cdr.read_plan(cdr.private_state(fx.state)), [])

    def test_prefix_unmounted_between_classify_and_apply_deletes_nothing(self):
        fx = self.prepared()
        before = fx.names()
        import shutil
        shutil.rmtree(fx.live)                                                  # the whole prefix vanishes
        fx.apply(execute=True)
        self.assertEqual(fx.names(), before)

    def test_the_default_prefixes_are_all_acceptable(self):
        for prefix in cdr.DEFAULT_PREFIXES:
            self.assertTrue(cdr.valid_prefix(prefix), prefix)

    def test_a_truncated_d_file_is_no_evidence(self):
        fx = Fixture(self)
        fx.crate('big', H, gone_root(fx), files=('rlib',))
        big = fx.deps / ('big-%s.d' % H)
        big.write_text('# env-dep:CARGO_MANIFEST_DIR=%s\n' % gone_root(fx) + '#' * (cdr.D_READ_LIMIT + 10))
        os.utime(big, (OLD, OLD))
        fx.classify()
        self.assertEqual(cdr.read_plan(cdr.private_state(fx.state)), [])

    def test_home_directory_and_its_parents_are_not_valid_prefixes(self):
        home = str(cdr.HOME)
        self.assertFalse(cdr.valid_prefix(home))
        self.assertFalse(cdr.valid_prefix(os.path.dirname(home)))
        self.assertTrue(cdr.valid_prefix(home + '/Bryant-Developments'))

    def test_malformed_plan_lines_are_skipped_not_fatal(self):
        fx = self.prepared()
        path = cdr.plan_paths(cdr.private_state(fx.state))[1]
        with open(path, 'a') as handle:
            handle.write('not json\n[]\n{"name": 5}\n{"name": "x-%s.rlib", "ino": "1", "nlink": 1, "mtime": 0, "blocks": 1, "root": "/a", "kind": "rlib"}\n' % H)
        self.assertEqual(len(cdr.read_plan(cdr.private_state(fx.state))), 3)
        fx.apply(execute=True)

    def test_classify_with_another_prefix_set_needs_reset(self):
        fx = self.prepared()
        other = fx.base / 'other-prefix'
        other.mkdir()
        fx.prefixes = [str(other)]
        with self.assertRaises(SystemExit):
            fx.classify()

    def test_progress_file_is_written_atomically_and_privately(self):
        fx = self.prepared()
        path = cdr.plan_paths(cdr.private_state(fx.state))[0]
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual([p.name for p in fx.state.iterdir() if '.tmp' in p.name], [])

    def test_the_default_free_space_probe_reads_the_deps_filesystem(self):
        fx = self.prepared()
        seen = []
        import unittest.mock
        real = cdr.free_bytes
        with unittest.mock.patch.object(cdr, 'free_bytes', lambda path: seen.append(path) or real(path)):
            with contextlib.redirect_stdout(io.StringIO()):
                cdr.apply(fx.args(execute=True, batch_gib=1.0))
        self.assertTrue(seen and all(p == str(fx.deps) for p in seen))

    def test_the_cap_is_not_overshot_by_a_large_inode_group(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx), files=('d', 'rlib'))
        for i in range(4):
            os.link(fx.deps / ('gone-%s.rlib' % H), fx.deps / ('gone-%s.link%d.rlib' % (H, i)))
        fx.classify()
        fx.apply(execute=True, batch_gib=100.0, max_gib=1 / cdr.GIB)           # cap far below the one inode group
        self.assertTrue((fx.deps / ('gone-%s.rlib' % H)).exists())

    def test_overly_broad_prefix_is_refused(self):
        fx = Fixture(self)
        fx.crate('gone', H, gone_root(fx))
        for prefix in ('/', '/Users', 'relative/path'):
            fx.prefixes = [prefix]
            with self.assertRaises(SystemExit):
                fx.classify()

    def test_max_gib_caps_even_when_the_batch_is_larger(self):
        fx = Fixture(self)
        for i in range(10):
            fx.crate('gone%d' % i, H, gone_root(fx), files=('rlib', 'd'))
        fx.classify()
        out = fx.apply(execute=True, batch_gib=100.0, max_gib=3 * 8192 / cdr.GIB)
        self.assertLessEqual(sum(1 for i in range(10) if not (fx.deps / ('gone%d-%s.rlib' % (i, H))).exists()), 3)
        self.assertIn('removed', out)

    def test_tampered_plan_names_are_ignored(self):
        fx = self.prepared()
        path = cdr.plan_paths(cdr.private_state(fx.state))[1]
        outside = fx.base / 'victim'
        outside.write_bytes(b'keep me')
        with open(path, 'a') as handle:
            handle.write(json.dumps({'name': '../victim', 'ino': outside.stat().st_ino, 'nlink': 1, 'mtime': 0, 'blocks': 8,
                                     'root': gone_root(fx), 'kind': 'rlib'}) + '\n')
        fx.apply(execute=True)
        self.assertTrue(outside.exists())

    def test_duplicate_plan_entries_are_collapsed(self):
        fx = self.prepared()
        path = cdr.plan_paths(cdr.private_state(fx.state))[1]
        lines = path.read_text()
        path.write_text(lines + lines)
        self.assertEqual(len(cdr.read_plan(cdr.private_state(fx.state))), 3)

    def test_a_vanished_file_does_not_abort_the_batch(self):
        fx = self.prepared()
        real = os.unlink
        victim = 'gone-%s.rmeta' % H

        def flaky(path, *a, **kw):
            if path == victim:
                raise FileNotFoundError(path)
            return real(path, *a, **kw)
        import unittest.mock
        with unittest.mock.patch.object(cdr.os, 'unlink', flaky):
            fx.apply(execute=True, batch_gib=1.0)
        self.assertEqual(sorted(n for n in fx.names()), sorted([victim, 'gone-%s.d' % H]))

    def test_stale_plan_is_refused_and_reset_redoes_it(self):
        fx = self.prepared()
        path = cdr.plan_paths(cdr.private_state(fx.state))[0]
        progress = json.loads(path.read_text())
        progress['done_at'] = {k: time.time() - 8 * 86400 for k in progress['done']}
        path.write_text(json.dumps(progress))
        with self.assertRaises(SystemExit):
            fx.apply(execute=True)
        with contextlib.redirect_stdout(io.StringIO()):
            cdr.classify(fx.args(reset=True))
        fx.apply(execute=True)
        self.assertEqual(fx.names(), {'gone-%s.d' % H})

    def test_stale_classification_is_refused(self):
        fx = self.prepared()
        path = cdr.plan_paths(cdr.private_state(fx.state))[0]
        progress = json.loads(path.read_text())
        progress['done_at'] = {k: time.time() - 8 * 86400 for k in progress['done']}
        path.write_text(json.dumps(progress))
        with self.assertRaises(SystemExit):
            fx.apply(execute=True)

    def test_state_directory_must_be_private(self):
        fx = self.prepared()
        os.chmod(fx.state, 0o755)
        with self.assertRaises(SystemExit):
            fx.apply()

    def test_state_of_another_deps_directory_is_refused(self):
        fx = self.prepared()
        other = fx.base / 'target2/debug/deps'
        other.mkdir(parents=True)
        (other.parent.parent / 'CACHEDIR.TAG').write_text('x')
        for lock in ('.cargo-lock',):
            (other.parent / lock).write_text('')
        with self.assertRaises(SystemExit):
            cdr.apply(fx.args(deps=str(other)))


if __name__ == '__main__':
    unittest.main()
