import importlib.util
import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor

HELPER = Path(__file__).resolve().parents[1] / ".claude/hooks/ticket-state.py"
BOOTSTRAP_SOURCE = (Path(__file__).resolve().parents[1]
                    / ".claude/skills/engineering-runtime/scripts/bootstrap.py")
SPEC = importlib.util.spec_from_file_location("ticket_state_context", HELPER)
ticket_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ticket_state)


class ContextStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.worktree = Path(self.temp.name) / "worktree"
        self.worktree.mkdir(mode=0o700)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(["git", "init", "-q", str(self.worktree)], env=env, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self, host="claude", release=None):
        root = self.worktree / f".{host}"
        store = ticket_state.ContextStore(root)
        state = store.initialize(host, str(self.worktree), "SPE-17", release=release,
                                 fresh_owned_scope=True)
        self.assertEqual(state["status"], "managed")
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / ".current-context.json").stat().st_mode & 0o777, 0o600)
        return store, state

    def run_bootstrap(self, host, cwd=None, **extra_env):
        host_root = Path(self.temp.name) / f".{host}"
        bootstrap = host_root / "skills/engineering-runtime/scripts/bootstrap.py"
        bootstrap.parent.mkdir(mode=0o700, parents=True)
        helper = bootstrap.with_name("ticket-state.py")
        shutil.copyfile(HELPER, helper)
        shutil.copyfile(BOOTSTRAP_SOURCE, bootstrap)
        self.assertFalse((host_root / "hooks/ticket-state.py").exists())
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(extra_env)
        result = subprocess.run([sys.executable, str(bootstrap)], cwd=cwd or self.worktree, env=env,
                                capture_output=True, text=True, check=True)
        values = {}
        for line in result.stdout.splitlines():
            key, value = shlex.split(line, posix=True)[1].split("=", 1)
            values[key] = value
        self.assertEqual((Path(values["ENGINEERING_RUNTIME"]) / "bootstrap.py").resolve(),
                         bootstrap.resolve())
        return values

    def test_inspect_absence_is_read_only_and_has_no_token(self):
        absent = self.worktree / ".claude"
        before = set(self.worktree.iterdir())
        result = ticket_state.ContextStore(absent).inspect()
        self.assertEqual(result["status"], "legacy_uncoordinated")
        self.assertNotIn("token", result)
        self.assertEqual(set(self.worktree.iterdir()), before)

        absent.mkdir(mode=0o700)
        before = set(absent.iterdir())
        result = ticket_state.ContextStore(absent).inspect()
        self.assertEqual(result["status"], "legacy_uncoordinated")
        self.assertNotIn("token", result)
        self.assertEqual(set(absent.iterdir()), before)

    def test_context_cli_initializes_and_inspects_json(self):
        root = self.worktree / ".codex"
        initialized = subprocess.run(
            ["python3", str(HELPER), "context", str(root), "initialize", "--host", "codex",
             "--worktree", str(self.worktree), "--ticket", "42", "--fresh-owned-scope"],
            capture_output=True, text=True, check=True)
        value = json.loads(initialized.stdout)
        self.assertEqual(value["status"], "managed")
        observed = subprocess.run(["python3", str(HELPER), "context", str(root), "inspect"],
                                  capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(observed.stdout)["generation"], value["generation"])

    def test_initialize_requires_exclusive_fresh_host_directory(self):
        root = self.worktree / ".claude"
        root.mkdir(mode=0o700)
        sentinel = root / "unowned-state"
        sentinel.write_text("preserve")
        with self.assertRaisesRegex(ValueError, "adoption refused"):
            ticket_state.ContextStore(root).initialize(
                "claude", str(self.worktree), "SPE-17", fresh_owned_scope=True)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(set(root.iterdir()), {sentinel})

    def test_publish_identical_tuple_advances_generation_and_rejects_stale_token(self):
        store, initial = self.initialize()
        updated = store.publish(initial["generation"], initial["token"], "SPE-17",
                                str(self.worktree))
        self.assertNotEqual(updated["generation"], initial["generation"])
        self.assertEqual(updated["ticket"], initial["ticket"])
        with self.assertRaisesRegex(ValueError, "stale"):
            store.publish(initial["generation"], initial["token"], "SPE-17",
                           str(self.worktree))

    def test_publish_refuses_active_ticket_handover_without_changing_context(self):
        store, initial = self.initialize()
        before = store.object_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "handover is unsupported"):
            store.publish(initial["generation"], initial["token"], "SPE-18",
                          str(self.worktree), release="phase8")
        self.assertEqual(store.object_path.read_bytes(), before)
        self.assertEqual(store.inspect()["ticket"], "SPE-17")

    def test_publish_reply_stays_bound_to_own_write_after_next_cas_wins(self):
        store, initial = self.initialize()
        real_context_lock = ticket_state.context_lock
        injected = {"done": False, "later": None}

        @contextlib.contextmanager
        def race_after_unlock(root):
            with real_context_lock(root):
                yield
            if not injected["done"]:
                injected["done"] = True
                observed = store.inspect()
                injected["later"] = store.publish(
                    observed["generation"], observed["token"], "SPE-17",
                    str(self.worktree), release="later")

        with unittest.mock.patch.object(ticket_state, "context_lock", race_after_unlock):
            first = store.publish(initial["generation"], initial["token"], "SPE-17",
                                  str(self.worktree), release="first")

        self.assertEqual(first["release"], "first")
        self.assertNotEqual(first["generation"], injected["later"]["generation"])
        self.assertEqual(store.inspect()["generation"], injected["later"]["generation"])

    def test_initialize_fsyncs_parent_before_publishing_and_preserves_on_failure(self):
        root = self.worktree / ".claude"
        store = ticket_state.ContextStore(root)
        real_fsync = ticket_state.fsync_directory
        calls = []

        def fail_parent_sync(path):
            calls.append(Path(path))
            if Path(path).resolve() == self.worktree.resolve():
                self.assertTrue(root.is_dir())
                self.assertFalse((root / ".current-context.json").exists())
                raise OSError("simulated parent-directory fsync failure")
            real_fsync(path)

        with unittest.mock.patch.object(ticket_state, "fsync_directory", fail_parent_sync):
            with self.assertRaisesRegex(OSError, "parent-directory fsync failure"):
                store.initialize("claude", str(self.worktree), "SPE-17",
                                 fresh_owned_scope=True)
        self.assertEqual([path.resolve() for path in calls], [self.worktree.resolve()])
        self.assertTrue(root.is_dir())
        self.assertFalse(store.object_path.exists())

    def test_initialize_reply_stays_bound_to_own_write_after_next_cas_wins(self):
        store = ticket_state.ContextStore(self.worktree / ".claude")
        real_context_lock = ticket_state.context_lock
        injected = {"done": False, "later": None}

        @contextlib.contextmanager
        def race_after_unlock(root):
            with real_context_lock(root):
                yield
            if not injected["done"]:
                injected["done"] = True
                observed = store.inspect()
                injected["later"] = store.publish(
                    observed["generation"], observed["token"], "SPE-17",
                    str(self.worktree), release="concurrent")

        with unittest.mock.patch.object(ticket_state, "context_lock", race_after_unlock):
            initialized = store.initialize("claude", str(self.worktree), "SPE-17",
                                            fresh_owned_scope=True)

        self.assertIsNone(initialized["release"])
        self.assertNotEqual(initialized["generation"], injected["later"]["generation"])
        self.assertEqual(store.inspect()["generation"], injected["later"]["generation"])

    def test_clear_writes_tombstone_and_repeat_with_current_token_is_noop(self):
        store, initial = self.initialize()
        cleared = store.clear(initial["generation"], initial["token"])
        self.assertEqual(cleared["state"], "cleared")
        self.assertIsNone(cleared["ticket"])
        self.assertIsNone(cleared["worktree"])
        self.assertIsNone(cleared["release"])
        again = store.clear(cleared["generation"], cleared["token"])
        self.assertEqual(again["generation"], cleared["generation"])
        self.assertEqual(again["token"], cleared["token"])
        # A tombstone remains authoritative even when an old marker reappears.
        (store.root / ".current-ticket").write_text("OLD-1")
        observed = store.inspect()
        self.assertEqual(observed["state"], "cleared")
        self.assertIsNone(observed["ticket"])
        replay = store.clear(cleared["generation"], cleared["token"])
        self.assertEqual(replay["generation"], cleared["generation"])

    def test_legacy_markers_block_clear_and_are_preserved(self):
        store, initial = self.initialize()
        marker = store.root / ".current-worktree"
        marker.write_text("legacy-value")
        object_before = store.object_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "legacy context markers"):
            store.clear(initial["generation"], initial["token"])
        self.assertEqual(marker.read_text(), "legacy-value")
        self.assertEqual(store.object_path.read_bytes(), object_before)

    def test_legacy_markers_block_publish_without_changing_active_context(self):
        store, initial = self.initialize()
        marker = store.root / ".current-ticket"
        marker.write_text("LEGACY-42\n")
        object_before = store.object_path.read_bytes()
        marker_before = marker.read_bytes()
        with self.assertRaisesRegex(ValueError, "legacy context markers"):
            store.publish(initial["generation"], initial["token"], "SPE-18",
                          str(self.worktree))
        self.assertEqual(store.object_path.read_bytes(), object_before)
        self.assertEqual(marker.read_bytes(), marker_before)

    def test_invalid_object_reports_unavailable_without_fallback_token(self):
        store, _initial = self.initialize()
        store.object_path.write_text('{"schema":1}')
        store.object_path.chmod(0o600)
        observed = store.inspect()
        self.assertEqual(observed["status"], "unavailable")
        self.assertNotIn("token", observed)

    def test_root_identity_swap_invalidates_context(self):
        store, _initial = self.initialize()
        moved = self.worktree / ".claude-old"
        original = store.object_path.read_bytes()
        store.root.rename(moved)
        store.root.mkdir(mode=0o700)
        store.object_path.write_bytes(original)
        store.object_path.chmod(0o600)
        observed = store.inspect()
        self.assertEqual(observed["status"], "unavailable")
        self.assertTrue((moved / ".current-context.json").exists())

    def test_claude_and_codex_context_slots_are_separate(self):
        claude, cstate = self.initialize("claude")
        codex, xstate = self.initialize("codex")
        self.assertNotEqual(cstate["scope_id"], xstate["scope_id"])
        self.assertNotEqual(claude.root / ".context-slot.lock", codex.root / ".context-slot.lock")
        self.assertEqual(claude.inspect()["status"], "managed")
        self.assertEqual(codex.inspect()["status"], "managed")

    def test_concurrent_publish_and_clear_allow_exactly_one_cas_winner(self):
        store, state = self.initialize()
        barrier = threading.Barrier(2)

        def publish():
            barrier.wait()
            return store.publish(state["generation"], state["token"], "SPE-17",
                                 str(self.worktree), release="phase8")

        def clear():
            barrier.wait()
            return store.clear(state["generation"], state["token"])

        def capture(call):
            try:
                call()
                return "success"
            except ValueError as exc:
                self.assertIn("stale", str(exc))
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(capture, (publish, clear)))
        self.assertCountEqual(outcomes, ["success", "stale"])
        self.assertNotEqual(store.inspect()["generation"], state["generation"])

    def test_concurrent_same_ticket_release_updates_allow_one_cas_winner(self):
        store, state = self.initialize()
        barrier = threading.Barrier(2)

        def publish(release):
            barrier.wait()
            return store.publish(state["generation"], state["token"], "SPE-17",
                                 str(self.worktree), release=release)

        def capture(release):
            try:
                return publish(release)
            except ValueError as exc:
                self.assertIn("stale", str(exc))
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(capture, ("phase-a", "phase-b")))
        winners = [result for result in outcomes if result != "stale"]
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0]["release"], {"phase-a", "phase-b"})
        self.assertEqual(store.inspect()["generation"], winners[0]["generation"])

    def test_git_environment_cannot_redirect_worktree_discovery(self):
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(["git", "init", "-q", str(outside)], env=clean, check=True)
        inherited = dict(os.environ, GIT_DIR=str(outside / ".git"), GIT_WORK_TREE=str(outside))
        with unittest.mock.patch.dict(os.environ, inherited, clear=True):
            store, state = self.initialize()
        self.assertEqual(state["worktree"], str(self.worktree.resolve()))
        self.assertEqual(store.inspect()["status"], "managed")

    def test_bootstrap_managed_active_is_authoritative_for_both_hosts(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                store, state = self.initialize(host, release="phase7")
                (store.root / ".current-ticket").write_text("STALE-9")
                (store.root / ".current-worktree").write_text("/stale/worktree")
                env = self.run_bootstrap(host, ENGINEERING_CURRENT_TICKET="ENV-8",
                                         ENGINEERING_CURRENT_WORKTREE="/env/worktree",
                                         ENGINEERING_CURRENT_RELEASE="old-release",
                                         CLAUDE_CURRENT_TICKET="CLAUDE-7")
                self.assertEqual(env["ENGINEERING_CONTEXT_STATUS"], "managed")
                self.assertEqual(env["ENGINEERING_CURRENT_TICKET"], "SPE-17")
                self.assertEqual(env["ENGINEERING_CURRENT_WORKTREE"], str(self.worktree.resolve()))
                self.assertEqual(env["ENGINEERING_CURRENT_RELEASE"], "phase7")
                self.assertEqual(env["ENGINEERING_CONTEXT_GENERATION"], state["generation"])
                self.assertEqual(env["ENGINEERING_CONTEXT_SCOPE_ID"], state["scope_id"])
                self.assertEqual(env["ENGINEERING_CONTEXT_CAS_TOKEN"], state["token"])

    def test_bootstrap_tombstone_suppresses_stale_environment_and_markers(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                store, state = self.initialize(host)
                cleared = store.clear(state["generation"], state["token"])
                (store.root / ".current-ticket").write_text("STALE-9")
                (store.root / ".current-worktree").write_text("/stale/worktree")
                (store.root / ".current-release").write_text("old-release")
                env = self.run_bootstrap(host, ENGINEERING_CURRENT_TICKET="ENV-8",
                                         ENGINEERING_CURRENT_WORKTREE="/env/worktree",
                                         ENGINEERING_CURRENT_RELEASE="env-release")
                self.assertEqual(env["ENGINEERING_CONTEXT_STATUS"], "managed")
                self.assertEqual(env["ENGINEERING_CONTEXT_GENERATION"], cleared["generation"])
                self.assertTrue(env["ENGINEERING_CONTEXT_CAS_TOKEN"])
                for key in ("ENGINEERING_CURRENT_TICKET", "ENGINEERING_CURRENT_WORKTREE",
                            "ENGINEERING_CURRENT_RELEASE"):
                    self.assertEqual(env[key], "")

    def test_bootstrap_malformed_context_suppresses_every_fallback(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                store, _state = self.initialize(host)
                store.object_path.write_text('{"schema":1}')
                store.object_path.chmod(0o600)
                (store.root / ".current-ticket").write_text("STALE-9")
                env = self.run_bootstrap(host, ENGINEERING_CURRENT_TICKET="ENV-8",
                                         ENGINEERING_CURRENT_WORKTREE="/env/worktree",
                                         ENGINEERING_CURRENT_RELEASE="env-release")
                self.assertEqual(env["ENGINEERING_CONTEXT_STATUS"], "unavailable")
                for key in ("ENGINEERING_CURRENT_TICKET", "ENGINEERING_CURRENT_WORKTREE",
                            "ENGINEERING_CURRENT_RELEASE", "ENGINEERING_CONTEXT_GENERATION",
                            "ENGINEERING_CONTEXT_SCOPE_ID", "ENGINEERING_CONTEXT_CAS_TOKEN"):
                    self.assertEqual(env[key], "")

    def test_bootstrap_absent_context_preserves_legacy_precedence_without_tokens(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                root = self.worktree / f".{host}"
                root.mkdir(mode=0o700)
                (root / ".current-ticket").write_text("MARKER-1")
                (root / ".current-worktree").write_text("/marker/worktree")
                (root / ".current-release").write_text("marker-release")
                env = self.run_bootstrap(host, ENGINEERING_CURRENT_TICKET="ENV-2",
                                         ENGINEERING_CURRENT_RELEASE="env-release")
                self.assertEqual(env["ENGINEERING_CONTEXT_STATUS"], "legacy_uncoordinated")
                self.assertEqual(env["ENGINEERING_CURRENT_TICKET"], "ENV-2")
                self.assertEqual(env["ENGINEERING_CURRENT_WORKTREE"], "/marker/worktree")
                self.assertEqual(env["ENGINEERING_CURRENT_RELEASE"], "env-release")
                for key in ("ENGINEERING_CONTEXT_GENERATION", "ENGINEERING_CONTEXT_SCOPE_ID",
                            "ENGINEERING_CONTEXT_CAS_TOKEN"):
                    self.assertEqual(env[key], "")

    def test_bootstrap_failed_git_discovery_never_falls_back_to_context(self):
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\nexit 128\n")
        fake_git.chmod(0o700)
        subdirectory = self.worktree / "nested"
        subdirectory.mkdir()
        original_path = os.environ.get("PATH", "")
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                store, initial = self.initialize(host)
                cleared = store.clear(initial["generation"], initial["token"])
                (store.root / ".current-ticket").write_text("STALE-9")
                env = self.run_bootstrap(
                    host, cwd=subdirectory,
                    PATH=f"{fake_bin}:{original_path}",
                    ENGINEERING_CURRENT_TICKET="ENV-8",
                    ENGINEERING_CURRENT_WORKTREE="/env/worktree",
                    ENGINEERING_CURRENT_RELEASE="env-release")
                self.assertEqual(env["ENGINEERING_CONTEXT_STATUS"], "unavailable")
                self.assertEqual(env["ENGINEERING_CONTEXT_DIR"], "")
                for key in ("ENGINEERING_CURRENT_TICKET", "ENGINEERING_CURRENT_WORKTREE",
                            "ENGINEERING_CURRENT_RELEASE", "ENGINEERING_CONTEXT_GENERATION",
                            "ENGINEERING_CONTEXT_SCOPE_ID", "ENGINEERING_CONTEXT_CAS_TOKEN"):
                    self.assertEqual(env[key], "")
                self.assertEqual(store.inspect()["generation"], cleared["generation"])


if __name__ == "__main__":
    unittest.main()
