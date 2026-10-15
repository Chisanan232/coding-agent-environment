import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / ".claude/hooks"
HELPER = HOOKS / "ticket-state.py"
SPEC = importlib.util.spec_from_file_location("ticket_state", HELPER)
ticket_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ticket_state)


class SimulatedCrash(BaseException):
    pass


class TicketStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = (Path(self.temp.name) / "home").resolve()
        self.home.mkdir(mode=0o700)
        self.notes = self.home / "notes"
        self.workflow = self.home / "workflow"
        self.env = dict(os.environ, HOME=str(self.home),
                        ENGINEERING_SESSION_NOTES_DIR=str(self.notes),
                        ENGINEERING_WORKFLOW_STATE_DIR=str(self.workflow),
                        ENGINEERING_CONFIG_ENV=str(self.home / "no-config.env"))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, kind, root, action, ticket="SPE-17", *args, check=False):
        return subprocess.run(["python3", str(HELPER), kind, str(root), action, ticket, *args],
                              env=self.env, capture_output=True, text=True, check=check)

    def shell(self, script, *args, check=False):
        return subprocess.run(["bash", str(HOOKS / script), *args], env=self.env,
                              capture_output=True, text=True, check=check)

    @staticmethod
    def payload(token):
        encoded = token.removeprefix("ts1.")
        return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    @unittest.skipUnless(sys.platform == "darwin", "macOS fixed system alias")
    def test_verified_macos_var_alias_resolves_without_allowing_user_links(self):
        alias = Path("/var") / self.notes.relative_to("/private/var")
        store = ticket_state.TicketStore("notes", alias, "SPE-17")
        store.write(("Alias", "same managed directory"))
        self.assertEqual(store.root, self.notes)
        redirected = self.home / "user-link"
        redirected.symlink_to(self.notes, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            ticket_state.TicketStore("notes", redirected, "SPE-17")

    def test_shell_compatibility_delegates_notes_and_workflow_state(self):
        self.assertEqual(self.shell("session-memory.sh", "append", "SPE-17", "Decision", "Keep evidence").returncode, 0)
        self.assertIn("Keep evidence", self.shell("session-memory.sh", "read", "SPE-17", check=True).stdout)
        self.assertIn("SPE-17", self.shell("session-memory.sh", "list", check=True).stdout)
        self.assertEqual(self.shell("workflow-state.sh", "write", "SPE-17", "dev-impl-loop", "2", "5", "active").returncode, 0)
        value = json.loads(self.shell("workflow-state.sh", "read", "SPE-17", check=True).stdout)
        self.assertEqual(value["workflow"], "dev-impl-loop")
        self.assertEqual(value["status"], "active")

    def test_numeric_github_ticket_is_supported_and_traversal_is_still_refused(self):
        self.shell("workflow-state.sh", "write", "42", "post-merge-close", "4", "5", "active", check=True)
        value = json.loads(self.shell("workflow-state.sh", "read", "42", check=True).stdout)
        self.assertEqual(value["ticket"], "42")
        invalid = self.cli("workflow", self.workflow, "write", "../42", "workflow", "1", "2", "active")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse((self.home / "42.json").exists())

    def test_oversized_notes_workflow_and_revision_metadata_are_refused(self):
        self.notes.mkdir(mode=0o700)
        note = self.notes / "SPE-17.md"
        with note.open("wb") as stream:
            stream.truncate(ticket_state.MAX_FILE_BYTES + 1)
        result = self.shell("session-memory.sh", "read", "SPE-17")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("16 MiB read limit", result.stderr)
        self.assertEqual(note.stat().st_size, ticket_state.MAX_FILE_BYTES + 1)

        self.workflow.mkdir(mode=0o700)
        workflow = self.workflow / "SPE-17.json"
        with workflow.open("wb") as stream:
            stream.truncate(ticket_state.MAX_FILE_BYTES + 1)
        result = self.shell("workflow-state.sh", "read", "SPE-17")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("16 MiB read limit", result.stderr)
        self.assertEqual(workflow.stat().st_size, ticket_state.MAX_FILE_BYTES + 1)

        revisions = self.notes / ".ticket-state-revisions"
        revisions.mkdir(mode=0o700, exist_ok=True)
        revision = revisions / "SPE-17.json"
        revision.write_bytes(b"{}")
        revision.chmod(0o600)
        with revision.open("r+b") as stream:
            stream.truncate(ticket_state.MAX_FILE_BYTES + 1)
        result = self.shell("session-memory.sh", "snapshot", "SPE-17")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("16 MiB read limit", result.stderr)
        self.assertEqual(revision.stat().st_size, ticket_state.MAX_FILE_BYTES + 1)

    def test_legacy_private_permissions_are_preserved_by_read_and_snapshot(self):
        self.notes.mkdir(mode=0o700)
        path = self.notes / "SPE-17.md"
        path.write_text("legacy private notes\n")
        path.chmod(0o400)
        before = path.stat().st_mode & 0o777
        self.assertEqual(self.shell("session-memory.sh", "read", "SPE-17", check=True).stdout,
                         "legacy private notes\n")
        token = self.shell("session-memory.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        self.assertIsNone(self.payload(token)["revision"])
        self.assertEqual(path.stat().st_mode & 0o777, before)

    def test_legacy_notes_read_and_snapshot_preserve_unknown_revision_blocker(self):
        self.notes.mkdir(mode=0o700)
        path = self.notes / "SPE-17.md"
        path.write_text("legacy notes\n")
        self.assertEqual(self.shell("session-memory.sh", "read", "SPE-17", check=True).stdout, "legacy notes\n")
        token = self.shell("session-memory.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        self.assertIsNone(self.payload(token)["revision"])
        result = self.shell("session-memory.sh", "clear", "SPE-17", token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy writer revision unknown", result.stderr)
        self.assertTrue(path.exists())

    def test_legacy_workflow_read_remains_available_but_unknown_archive_is_blocked(self):
        self.workflow.mkdir(mode=0o700)
        path = self.workflow / "SPE-17.json"
        legacy = {"ticket": "SPE-17", "workflow": "old", "step": "1", "total_steps": "2",
                  "status": "active", "timestamp": "2026-10-01T00:00:00Z"}
        path.write_text(json.dumps(legacy))
        self.assertEqual(json.loads(self.shell("workflow-state.sh", "read", "SPE-17", check=True).stdout), legacy)
        token = self.shell("workflow-state.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        self.assertIsNone(self.payload(token)["revision"])
        result = self.shell("workflow-state.sh", "archive", "SPE-17", token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy writer revision unknown", result.stderr)
        self.assertTrue(path.exists())

    def test_tokenless_closeout_and_stale_snapshot_refuse_without_removal(self):
        self.shell("session-memory.sh", "append", "SPE-17", "Plan", "First", check=True)
        path = self.notes / "SPE-17.md"
        result = self.shell("session-memory.sh", "clear", "SPE-17")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(path.exists())
        token = self.shell("session-memory.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        self.shell("session-memory.sh", "append", "SPE-17", "Update", "Second", check=True)
        result = self.shell("session-memory.sh", "clear", "SPE-17", token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr)
        self.assertIn("Second", path.read_text())

    def test_same_content_workflow_write_advances_revision(self):
        class FrozenDateTime:
            @staticmethod
            def now(_timezone):
                class Stamp:
                    @staticmethod
                    def strftime(_fmt):
                        return "2026-10-15T00:00:00Z"
                return Stamp()

        store = ticket_state.TicketStore("workflow", self.workflow, "SPE-17")
        with patch.object(ticket_state, "datetime", FrozenDateTime):
            store.write(("workflow", "1", "3", "active"))
            first = store.snapshot_locked()
            first_bytes = store.source.read_bytes()
            store.write(("workflow", "1", "3", "active"))
            second = store.snapshot_locked()
        self.assertEqual(store.source.read_bytes(), first_bytes)
        self.assertNotEqual(first, second)
        self.assertEqual(self.payload(first)["revision"] + 1, self.payload(second)["revision"])

    def test_explicit_absence_snapshot_becomes_stale_after_first_writer(self):
        absence = self.shell("session-memory.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        self.assertIsNone(self.payload(absence)["fingerprint"])
        self.assertEqual(self.payload(absence)["revision"], 0)
        self.shell("session-memory.sh", "append", "SPE-17", "Created", "New generation", check=True)
        result = self.shell("session-memory.sh", "clear", "SPE-17", absence)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr)
        self.assertTrue((self.notes / "SPE-17.md").exists())

    def test_workflow_archive_requires_token_and_archives_without_overwrite(self):
        self.shell("workflow-state.sh", "write", "SPE-17", "workflow", "1", "2", "done", check=True)
        state = self.workflow / "SPE-17.json"
        no_token = self.shell("workflow-state.sh", "archive", "SPE-17")
        self.assertNotEqual(no_token.returncode, 0)
        token = self.shell("workflow-state.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        old = state.read_bytes()
        result = self.shell("workflow-state.sh", "archive", "SPE-17", token, check=True)
        self.assertIn("archived", result.stdout)
        self.assertFalse(state.exists())
        archives = list((self.workflow / "archive").glob("SPE-17_*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), old)
        self.assertEqual(self.shell("workflow-state.sh", "archive", "SPE-17", token, check=True).returncode, 0)
        self.assertEqual(len(list((self.workflow / "archive").glob("SPE-17_*.json"))), 1)

    def test_archive_collision_preserves_current_source(self):
        self.shell("session-memory.sh", "append", "SPE-17", "Plan", "Original", check=True)
        store = ticket_state.TicketStore("notes", self.notes, "SPE-17")
        token = store.snapshot_locked()
        snapshot = self.payload(token)
        archive = store.archive_path(token, snapshot["revision"])
        archive.parent.mkdir(mode=0o700, exist_ok=True)
        archive.write_text("different content")
        before = store.source.read_bytes()
        result = self.shell("session-memory.sh", "clear", "SPE-17", token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("collision", result.stderr)
        self.assertEqual(store.source.read_bytes(), before)

    def test_crash_after_durable_archive_replays_and_removes_only_matching_source(self):
        self.shell("session-memory.sh", "append", "SPE-17", "Plan", "Durable", check=True)
        store = ticket_state.TicketStore("notes", self.notes, "SPE-17")
        token = store.snapshot_locked()
        original = store.publish_archive

        def crash_after_pending(destination, pending):
            original(destination, pending)
            raise SimulatedCrash("stop after archive and pending intent are durable")

        with patch.object(store, "publish_archive", side_effect=crash_after_pending):
            with self.assertRaises(SimulatedCrash):
                store.archive_or_clear(token)
        self.assertTrue(store.source.exists())
        self.assertEqual(len(list((self.notes / "archive").glob("SPE-17_*.md"))), 1)
        self.assertEqual(store.archive_or_clear(token).startswith("[ticket-state]"), True)
        self.assertFalse(store.source.exists())
        self.assertEqual(store.publish_archive, original)

    def test_interrupted_writer_never_attests_legacy_or_partially_published_bytes(self):
        for after_replace in (False, True):
            with self.subTest(after_replace=after_replace):
                root = self.home / f"interrupted-{after_replace}"
                store = ticket_state.TicketStore("notes", root, "SPE-17")
                store.source.write_text("unique legacy state")
                original = ticket_state.atomic_bytes
                def interrupted(path, raw, mode=0o600):
                    if path == store.source:
                        if after_replace:
                            original(path, raw, mode)
                        raise SimulatedCrash()
                    return original(path, raw, mode)
                with patch.object(ticket_state, "atomic_bytes", side_effect=interrupted):
                    with self.assertRaises(SimulatedCrash):
                        store.write(("Pending", "new bytes"))
                current = store.source.read_bytes()
                token = store.snapshot_locked()
                self.assertIsNone(self.payload(token)["revision"])
                with self.assertRaisesRegex(ValueError, "revision unknown"):
                    store.archive_or_clear(token)
                self.assertEqual(store.source.read_bytes(), current)

    def test_external_replacement_is_not_adopted_by_a_fresh_snapshot(self):
        store = ticket_state.TicketStore("notes", self.notes, "SPE-17")
        store.write(("Owned", "prior state"))
        replacement = self.notes / "replacement"
        replacement.write_text("new uncoordinated unique state")
        replacement.replace(store.source)
        token = store.snapshot_locked()
        self.assertIsNone(self.payload(token)["revision"])
        with self.assertRaisesRegex(ValueError, "revision unknown"):
            store.archive_or_clear(token)
        self.assertEqual(store.source.read_text(), "new uncoordinated unique state")

    def test_uncoordinated_replacement_during_publication_is_not_committed(self):
        for identical in (False, True):
            with self.subTest(identical=identical):
                store = ticket_state.TicketStore("notes", self.home / f"publication-{identical}", "SPE-17")
                original = ticket_state.atomic_bytes
                def replaced(path, raw, mode=0o600):
                    identity = original(path, raw, mode)
                    if path == store.source:
                        replacement = store.root / "external-replacement"
                        replacement.write_bytes(raw if identical else b"new unique task state")
                        replacement.replace(path)
                    return identity
                with patch.object(ticket_state, "atomic_bytes", side_effect=replaced):
                    with self.assertRaisesRegex(ValueError, "publication replaced"):
                        store.write(("Write", "intended note"))
                token = store.snapshot_locked()
                self.assertIsNone(self.payload(token)["revision"])
                before = store.source.read_bytes()
                with self.assertRaisesRegex(ValueError, "revision unknown"):
                    store.archive_or_clear(token)
                self.assertEqual(store.source.read_bytes(), before)

    def test_matching_absence_token_completes_without_claiming_archive(self):
        store = ticket_state.TicketStore("notes", self.notes, "SPE-17")
        token = store.snapshot_locked()
        self.assertIn("verified absent", store.archive_or_clear(token))
        self.assertFalse((self.notes / "archive").exists())
        store.write(("New", "new task"))
        created = store.snapshot_locked()
        store.archive_or_clear(created)
        with self.assertRaisesRegex(ValueError, "stale"):
            store.archive_or_clear(token)

    def test_owned_process_crash_between_link_and_unlink_recovers_only_known_stage(self):
        store = ticket_state.TicketStore("notes", self.notes, "SPE-17")
        store.write(("Owned", "durable notes"))
        token = store.snapshot_locked()
        script = '''import importlib.util,os,sys
spec=importlib.util.spec_from_file_location("state",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=os.link
def crash_link(a,b):
 original(a,b);os._exit(81)
os.link=crash_link
m.TicketStore("notes",sys.argv[2],"SPE-17").archive_or_clear(sys.argv[3])
'''
        child = subprocess.run(["python3", "-c", script, str(HELPER), str(self.notes), token], env=self.env)
        self.assertEqual(child.returncode, 81)
        archive = next((self.notes / "archive").glob("SPE-17_*.md"))
        self.assertEqual(archive.stat().st_nlink, 2)
        self.assertTrue(store.source.exists())
        store.archive_or_clear(token)
        self.assertFalse(store.source.exists())
        self.assertEqual(archive.stat().st_nlink, 1)
        self.assertIn("durable notes", archive.read_text())

    def test_owned_process_crash_after_unlink_replays_with_directory_sync(self):
        store = ticket_state.TicketStore("workflow", self.workflow, "SPE-17")
        store.write(("workflow", "4", "5", "complete"))
        token = store.snapshot_locked()
        script = '''import importlib.util,os,sys,pathlib
spec=importlib.util.spec_from_file_location("state",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
store=m.TicketStore("workflow",sys.argv[2],"SPE-17");original=pathlib.Path.unlink
def crash_unlink(path,*args,**kwargs):
 result=original(path,*args,**kwargs)
 if path==store.source:os._exit(82)
 return result
pathlib.Path.unlink=crash_unlink
store.archive_or_clear(sys.argv[3])
'''
        child = subprocess.run(["python3", "-c", script, str(HELPER), str(self.workflow), token], env=self.env)
        self.assertEqual(child.returncode, 82)
        self.assertFalse(store.source.exists())
        with patch.object(ticket_state, "fsync_directory", wraps=ticket_state.fsync_directory) as synced:
            store.archive_or_clear(token)
        self.assertIn(unittest.mock.call(store.root), synced.call_args_list)

    def test_resumed_workflow_survives_prior_closeout_token(self):
        store = ticket_state.TicketStore("workflow", self.workflow, "SPE-17")
        store.write(("old-pr", "4", "5", "complete"))
        token = store.snapshot_locked()
        store.write(("resumed-pr", "1", "5", "in_progress"))
        with self.assertRaisesRegex(ValueError, "stale"):
            store.archive_or_clear(token)
        self.assertEqual(json.loads(store.source.read_text())["workflow"], "resumed-pr")

    def test_concurrent_same_ticket_appends_keep_every_writer(self):
        args = [("session-memory.sh", "append", "SPE-17", "Concurrent", f"entry-{index}")
                for index in range(16)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda call: self.shell(*call), args))
        self.assertTrue(all(result.returncode == 0 for result in results))
        content = (self.notes / "SPE-17.md").read_text()
        for index in range(16):
            self.assertIn(f"entry-{index}", content)
        self.assertEqual(self.payload(self.shell("session-memory.sh", "snapshot", "SPE-17", check=True).stdout.strip())["revision"], 16)

    def test_closeout_and_concurrent_new_append_never_lose_the_new_generation(self):
        self.shell("session-memory.sh", "append", "SPE-17", "Initial", "Keep me", check=True)
        token = self.shell("session-memory.sh", "snapshot", "SPE-17", check=True).stdout.strip()
        from threading import Barrier
        barrier = Barrier(2)

        def append():
            barrier.wait()
            return self.shell("session-memory.sh", "append", "SPE-17", "Concurrent", "Keep concurrent")

        def closeout():
            barrier.wait()
            return self.shell("session-memory.sh", "clear", "SPE-17", token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            append_result = pool.submit(append)
            closeout_result = pool.submit(closeout)
            self.assertEqual(append_result.result().returncode, 0)
            closeout_result = closeout_result.result()
        if closeout_result.returncode == 0:
            archived = list((self.notes / "archive").glob("SPE-17_*.md"))
            self.assertEqual(len(archived), 1)
            self.assertIn("Keep me", archived[0].read_text())
        else:
            self.assertIn("stale", closeout_result.stderr)
        current = self.notes / "SPE-17.md"
        self.assertTrue(current.exists())
        self.assertIn("Keep concurrent", current.read_text())

    def test_ticket_path_and_symlink_or_hardlink_files_are_refused(self):
        traversal = self.cli("notes", self.notes, "append", "../escape", "Plan", "body")
        self.assertNotEqual(traversal.returncode, 0)
        self.notes.mkdir(mode=0o700)
        target = self.home / "outside"
        target.write_text("protected")
        (self.notes / "SPE-17.md").symlink_to(target)
        result = self.cli("notes", self.notes, "append", "SPE-17", "Plan", "body")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "protected")
        (self.notes / "SPE-17.md").unlink()
        (self.notes / "SPE-17.md").hardlink_to(target)
        result = self.cli("notes", self.notes, "append", "SPE-17", "Plan", "body")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "protected")

    def test_foreign_owner_snapshot_is_refused(self):
        self.notes.mkdir(mode=0o700)
        path = self.notes / "SPE-17.md"
        path.write_text("owned file\n")
        current_uid = os.getuid()
        with patch.object(ticket_state.os, "getuid", return_value=current_uid + 1):
            with self.assertRaisesRegex(ValueError, "user-owned"):
                ticket_state.file_fingerprint(path)


if __name__ == "__main__":
    unittest.main()
