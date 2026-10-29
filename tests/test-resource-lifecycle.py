#!/usr/bin/env python3
"""Resource lifecycle (SPE-130): synthetic disposable fixtures only; the real machine's storage is never read.

Negative controls: active, unique-evidence, credential-bearing, symlink-escaping, foreign-owned, protected and
unverifiable resources are never classified A and never removed, and a resource that becomes active or changes
identity between manifest and apply is skipped.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/resource-lifecycle.py"
spec = importlib.util.spec_from_file_location("resource_lifecycle", SCRIPT)
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)

SELF = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def receipt(root, session=SELF, **extra):
    (Path(root) / rl.OWNER_RECEIPT).write_text(json.dumps({"session_id": session, "reproducible": True, **extra}))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.handles = (set(), True)
        self.patch(rl, "open_paths", lambda: self.handles)
        self.patch(rl.os, "getloadavg", lambda: (0.0, 0.0, 0.0))   # tests never depend on the host's real load
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()

    def patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def make(self, name, kind_files=("CACHEDIR.TAG",), data=b"x" * 5000):
        d = self.root / name
        d.mkdir(parents=True)
        for f in kind_files:
            (d / f).write_text("c")
        (d / "blob").write_bytes(data)
        return d

    def classify(self, path, kind="cargo_target", session=SELF):
        ctx = rl.make_ctx(session, self.sessions)
        rec = rl.measure(path, kind, ctx)
        return rl.classify(rec, ctx), rec

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = rl.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()


class ClassificationTests(Fixture):
    def test_owned_reproducible_idle_target_is_class_a(self):
        d = self.make("t")
        receipt(d, private=True)
        (cls, why), rec = self.classify(d)
        self.assertEqual((cls, why), ("A", ["owned-reproducible-idle"]))
        self.assertIsInstance(rec["allocated_bytes"], int)
        self.assertIsInstance(rec["logical_bytes"], int)

    def test_cargo_target_without_a_private_claim_is_never_a(self):
        d = self.make("t")
        receipt(d)                                  # owned, but not claimed private to the owner
        cls, why = self.classify(d)[0]
        self.assertEqual(cls, "B")
        self.assertIn("owned-but-not-claimed-private-needs-ask", why)

    def test_the_shared_target_is_never_a_even_with_a_private_claim(self):
        shared = rl.HOME / ".cargo/shared-target"
        ctx = rl.make_ctx(SELF, self.sessions)
        rec = {"path": str(shared), "kind": "cargo_target", "markers": [], "scan_complete": True,
               "receipt": {"session_id": SELF, "reproducible": True, "private": True}}
        self.assertNotEqual(rl.classify(rec, ctx)[0], "A")

    def test_unowned_target_is_class_b_not_a(self):
        d = self.make("t")
        self.assertEqual(self.classify(d)[0][0], "B")

    def test_another_sessions_receipt_does_not_make_it_a(self):
        d = self.make("t")
        receipt(d, session=OTHER)
        self.assertEqual(self.classify(d)[0][0], "B")

    def test_open_handle_or_cwd_is_class_c_even_when_owned(self):
        d = self.make("t")
        receipt(d)
        self.handles = ({str(d / "blob")}, True)
        cls, why = self.classify(d)[0]
        self.assertEqual(cls, "C")
        self.assertIn("active:open-handle-or-cwd", why)

    def test_unknown_handles_fail_closed(self):
        d = self.make("t")
        receipt(d)
        self.handles = (set(), False)
        cls, why = self.classify(d)[0]
        self.assertEqual(cls, "C")
        self.assertIn("open-handles-unknown", why)

    def test_credential_name_blocks_even_with_a_waiver(self):
        d = self.make("s", kind_files=(".env",))
        receipt(d, evidence_free=True)
        cls, why = self.classify(d, kind="tmp_tree")[0]
        self.assertEqual(cls, "C")
        self.assertIn("marker:credential-name", why)

    def test_evidence_name_blocks_unless_the_owner_waives_it(self):
        d = self.make("s", kind_files=("qa-evidence.json",))
        receipt(d)
        self.assertEqual(self.classify(d, kind="tmp_tree")[0][0], "C")
        receipt(d, evidence_free=True)
        self.assertEqual(self.classify(d, kind="tmp_tree")[0][0], "A")

    def test_keep_marker_is_never_waivable(self):
        d = self.make("s", kind_files=(rl.KEEP_MARKER,))
        receipt(d, evidence_free=True)
        self.assertEqual(self.classify(d, kind="tmp_tree")[0][0], "C")

    def test_escaping_symlink_blocks(self):
        outside = self.root / "outside"
        outside.mkdir()
        d = self.make("t")
        receipt(d)
        (d / "link").symlink_to(outside)
        cls, why = self.classify(d)[0]
        self.assertEqual(cls, "C")
        self.assertIn("marker:escaping-symlink", why)

    def test_symlink_root_is_class_c(self):
        real = self.make("real")
        link = self.root / "link"
        link.symlink_to(real)
        self.assertIn("root-is-symlink", self.classify(link)[0][1])

    def test_protected_root_is_class_c(self):
        ctx = rl.make_ctx(SELF, self.sessions)
        rec = {"path": str(Path.home()), "kind": "cargo_target", "markers": [], "scan_complete": True, "receipt": None}
        self.assertIn("protected-root", rl.classify(rec, ctx)[1])

    def test_unmeasured_scan_fails_closed(self):
        ctx = rl.make_ctx(SELF, self.sessions)
        d = self.make("t")
        rec = rl.measure(d, "cargo_target", ctx)
        rec["scan_complete"] = False
        self.assertEqual(rl.classify(rec, ctx)[0], "C")

    def test_unknown_kind_is_class_c(self):
        d = self.make("u")
        receipt(d)
        self.assertEqual(self.classify(d, kind="unknown")[0][0], "C")

    def test_live_session_scratch_is_c_and_dead_session_scratch_is_b(self):
        live_id = "33333333-3333-3333-3333-333333333333"
        started = int((time.time() - rl.etime_seconds(os.getpid())) * 1000)
        (self.sessions / "a.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": live_id, "startedAt": started}))
        proj = self.root / "claude-1/proj"
        live_dir, dead_dir = proj / live_id, proj / OTHER
        live_dir.mkdir(parents=True)
        dead_dir.mkdir(parents=True)
        self.patch(rl, "session_from_path", lambda p: Path(p).name)
        self.assertEqual(self.classify(live_dir, kind="session_scratch")[0][0], "C")
        cls, why = self.classify(dead_dir, kind="session_scratch")[0]
        self.assertEqual(cls, "B")
        self.assertIn("may-hold-unique-evidence", why)

    def test_reused_pid_does_not_count_as_a_live_session(self):
        started = int((time.time() - 10 ** 6) * 1000)
        (self.sessions / "a.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": OTHER, "startedAt": started}))
        self.assertEqual(rl.live_sessions(self.sessions), {})

    def test_worktree_states(self):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        def git(*a, cwd):
            subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=env)
        main = self.root / "main"
        git("init", "-q", "-b", "main", str(main), cwd=self.root)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x", cwd=main)
        git("remote", "add", "origin", str(remote), cwd=main)
        git("push", "-q", "-u", "origin", "main", cwd=main)
        wt = self.root / "wt"
        git("worktree", "add", "-q", "-b", "topic", str(wt), cwd=main)
        self.assertEqual(rl.worktree_state(wt), "unpushed")           # no upstream yet
        git("push", "-q", "-u", "origin", "topic", cwd=wt)
        self.assertEqual(rl.worktree_state(wt), "clean-reconciled")
        (wt / "f").write_text("dirty")
        self.assertEqual(rl.worktree_state(wt), "dirty")
        cls, why = self.classify(wt, kind="worktree")[0]
        self.assertEqual(cls, "C")
        (wt / "f").unlink()
        self.assertEqual(self.classify(wt, kind="worktree")[0][0], "B")


class ProcessTests(Fixture):
    def table(self):
        hour = 3600
        return {
            10: dict(ppid=1, age_s=5 * hour, cpu=0.0, rss_kb=100, state='Ss', comm='claude'),       # live foreign session
            20: dict(ppid=10, age_s=50 * hour, cpu=0.0, rss_kb=100, state='Ss', comm='zsh'),
            21: dict(ppid=20, age_s=50 * hour, cpu=30.0, rss_kb=2048, state='RN', comm='zsh'),      # foreign spinning test
            22: dict(ppid=20, age_s=50 * hour, cpu=25.0, rss_kb=2048, state='RN', comm='zsh'),
            30: dict(ppid=1, age_s=200 * hour, cpu=0.0, rss_kb=100, state='Ss+', comm='bash'),
            31: dict(ppid=30, age_s=200 * hour, cpu=40.0, rss_kb=500, state='R+', comm='age'),      # orphaned test process
            40: dict(ppid=1, age_s=2 * hour, cpu=0.0, rss_kb=100, state='Ss', comm='claude'),       # this session
            41: dict(ppid=40, age_s=2 * hour, cpu=0.0, rss_kb=100, state='Ss', comm='zsh'),
            42: dict(ppid=41, age_s=20 * hour, cpu=60.0, rss_kb=900, state='R', comm='cargo'),      # own leftover
            50: dict(ppid=1, age_s=10 * hour, cpu=1.0, rss_kb=100, state='S', comm='quiet'),        # below the CPU threshold
            51: dict(ppid=1, age_s=60, cpu=90.0, rss_kb=100, state='R', comm='fresh'),              # too young to be stale
        }

    def run_procs(self, extra=()):
        self.patch(rl, "process_table", self.table)
        self.patch(rl, "process_cwds", lambda pids: {p: "/work/%d" % p for p in pids})
        self.patch(rl, "live_sessions", lambda d: {"sess-foreign": 10, SELF: 40})
        out = self.root / "pstate"
        code, text = self.run_cli("procs", "--self-session", SELF, "--out", str(out), *extra)
        data = json.loads(next(out.glob("procs-*.json")).read_text())
        return code, text, {tuple(g["pids"]): g for g in data["groups"]}

    def test_groups_are_classified_by_owner_chain(self):
        code, text, groups = self.run_procs()
        self.assertEqual(code, 0)
        self.assertEqual(groups[(21, 22)]["class"], "B")          # another live session's spinning children
        self.assertEqual(groups[(21, 22)]["owner_session"], "sess-foreign")
        self.assertEqual(groups[(31,)]["class"], "C")             # orphaned: unknown owner, never automatic
        self.assertEqual(groups[(31,)]["attribution"], "orphan")
        self.assertEqual(groups[(42,)]["class"], "A")             # descended from this session
        self.assertNotIn((50,), groups)
        self.assertNotIn((51,), groups)

    def test_no_command_line_or_session_name_is_recorded(self):
        code, text, groups = self.run_procs()
        blob = json.dumps(list(groups.values())) + text
        for forbidden in ("argv", "command", "name"):
            self.assertNotIn('"%s"' % forbidden, blob)
        self.assertEqual(oct(next((self.root / "pstate").glob("procs-*.json")).stat().st_mode & 0o777), "0o600")

    def test_nothing_is_signalled(self):
        import signal
        calls = []
        self.patch(rl.os, "kill", lambda *a: calls.append(a))
        self.run_procs()
        self.assertEqual(calls, [])


class ApplyTests(Fixture):
    def inventory_manifest(self, kind="cargo_target", cls="A", names=("t",)):
        consumers = self.root / "consumers.json"
        spec = [[kind, [str(self.root / n) for n in names]]]
        consumers.write_text(json.dumps(spec))
        self.patch(rl, "default_consumers", lambda tmp=None: spec)   # apply/verify re-derive the same configured scope
        out = self.root / "state"
        code, _ = self.run_cli("inventory", "--self-session", SELF, "--consumers", str(consumers), "--out", str(out),
                               "--sessions-dir", str(self.sessions))
        self.assertEqual(code, 0)
        return next(out.glob("manifest-%s-*.json" % cls)), out

    def test_inventory_refuses_to_start_on_a_busy_host(self):
        self.patch(rl.os, "getloadavg", lambda: (10_000.0, 0, 0))
        out = self.root / "state"
        code, _ = self.run_cli("inventory", "--self-session", SELF, "--consumers", str(self.root / "none.json"),
                               "--out", str(out))
        self.assertEqual(code, 75)
        self.assertFalse(out.exists())

    def test_inventory_stops_at_its_time_budget_and_marks_the_report_partial(self):
        for n in ("a", "b"):
            self.make(n)
        consumers = self.root / "c.json"
        consumers.write_text(json.dumps([["cargo_target", [str(self.root / "a"), str(self.root / "b")]]]))
        out = self.root / "state"
        code, _ = self.run_cli("inventory", "--self-session", SELF, "--consumers", str(consumers), "--out", str(out),
                               "--sessions-dir", str(self.sessions), "--max-seconds", "-1")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(next(out.glob("inventory-*.json")).read_text())["partial"])

    def test_scan_over_its_entry_budget_fails_closed(self):
        d = self.make("big")
        for i in range(30):
            (d / ("f%d" % i)).write_text("x")
        markers, complete = rl.scan_markers(d, shallow=False, limit=10)
        self.assertFalse(complete)

    def test_inventory_writes_private_report_and_manifests(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        report = next(out.glob("inventory-*.json"))
        self.assertEqual(oct(report.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o700")
        data = json.loads(report.read_text())
        self.assertIn("upper bound", data["note"])
        self.assertIsInstance(data["free_bytes"], int)
        self.assertEqual(len(json.loads(manifest.read_text())["entries"]), 1)

    def test_apply_removes_only_class_a_and_audits_with_measured_delta(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        code, text = self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))
        self.assertEqual(code, 0)
        self.assertFalse(d.exists())
        audit = [json.loads(l) for l in (out / "gc-audit.jsonl").read_text().splitlines()]
        self.assertEqual(audit[0]["action"], "removed")
        self.assertIn("free_delta_bytes", audit[0])
        self.assertIn("ground truth", text)
        self.assertEqual(oct((out / "gc-audit.jsonl").stat().st_mode & 0o777), "0o600")

    def test_dry_run_removes_nothing(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out), "--dry-run")
        self.assertTrue(d.exists())

    def test_class_b_needs_explicit_ids_and_class_c_is_refused(self):
        d = self.make("t")                       # no receipt -> B
        manifest, out = self.inventory_manifest(cls="B")
        code, _ = self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertTrue(d.exists())
        c_dir = self.make("c")
        receipt(c_dir, private=True)
        self.handles = ({str(c_dir)}, True)
        cmanifest, _ = self.inventory_manifest(cls="C", names=("c",))
        code, _ = self.run_cli("apply", "--manifest", str(cmanifest), "--self-session", SELF, "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertTrue(c_dir.exists())

    def test_class_b_removes_only_the_named_id(self):
        a, b = self.make("a"), self.make("b")
        manifest, out = self.inventory_manifest(cls="B", names=("a", "b"))
        entry = next(e for e in json.loads(manifest.read_text())["entries"] if e["path"] == str(a))
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out), "--ids", entry["id"])
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())

    def test_resource_that_becomes_active_after_the_manifest_is_skipped(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        self.handles = ({str(d)}, True)           # a build starts using it
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))
        self.assertTrue(d.exists())
        self.assertEqual(json.loads((out / "gc-audit.jsonl").read_text().splitlines()[0])["action"], "skipped")

    def test_identity_change_after_the_manifest_is_skipped(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        d2 = self.root / "t_new"
        self.make("t_new")
        receipt(d2, private=True)
        import shutil
        shutil.rmtree(d)
        d2.rename(d)                               # same path, different directory
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))
        self.assertTrue(d.exists())

    def test_symlink_swapped_in_after_the_manifest_never_deletes_the_target(self):
        outside = self.root / "precious"
        outside.mkdir()
        (outside / "data").write_text("keep")
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        import shutil
        shutil.rmtree(d)
        d.symlink_to(outside)
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))
        self.assertEqual((outside / "data").read_text(), "keep")

    def test_removal_never_follows_a_link_inside_the_tree(self):
        outside = self.root / "precious2"
        outside.mkdir()
        (outside / "data").write_text("keep")
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        (d / "late-link").symlink_to(outside)     # appears after the manifest; revalidation sees an escaping link
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))
        self.assertEqual((outside / "data").read_text(), "keep")
        self.assertTrue(d.exists())

    def test_verify_flags_changed_entries(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        self.assertEqual(self.run_cli("verify", "--manifest", str(manifest), "--out", str(out))[0], 0)
        self.handles = ({str(d)}, True)
        self.assertEqual(self.run_cli("verify", "--manifest", str(manifest), "--out", str(out))[0], 1)

    def test_manifest_for_a_different_owner_is_refused(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        code, _ = self.run_cli("apply", "--manifest", str(manifest), "--self-session", OTHER, "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertTrue(d.exists())

    def test_a_comma_separated_owner_list_matches_either_id(self):
        d = self.make("t")
        receipt(d, session=OTHER, private=True)
        ctx = rl.make_ctx(SELF + "," + OTHER, self.sessions)
        self.assertEqual(rl.classify(rl.measure(d, "cargo_target", ctx), ctx)[0], "A")

    def edited(self, manifest, out, mutate):
        data = json.loads(manifest.read_text())
        mutate(data)
        new = out / "manifest-A-edited.json"
        fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        return new

    def test_tampered_entry_id_is_refused(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        bad = self.edited(manifest, out, lambda m: m["entries"][0].update(id="000000000000"))
        code, _ = self.run_cli("apply", "--manifest", str(bad), "--self-session", SELF, "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertTrue(d.exists())

    def test_entry_pointing_outside_the_configured_consumers_is_refused(self):
        d, other = self.make("t"), self.make("other")
        receipt(d, private=True)
        receipt(other, private=True)
        manifest, out = self.inventory_manifest()
        import hashlib
        def retarget(m):
            e = m["entries"][0]
            e["path"] = str(other)
            e["realpath"] = os.path.realpath(other)
            e["id"] = hashlib.sha256(str(other).encode()).hexdigest()[:12]
        bad = self.edited(manifest, out, retarget)
        code, _ = self.run_cli("apply", "--manifest", str(bad), "--self-session", SELF, "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertTrue(other.exists() and d.exists())

    def test_manifest_outside_the_state_dir_or_with_loose_mode_is_refused(self):
        d = self.make("t")
        receipt(d, private=True)
        manifest, out = self.inventory_manifest()
        copy = self.root / "elsewhere.json"
        copy.write_text(manifest.read_text())
        copy.chmod(0o600)
        self.assertNotEqual(self.run_cli("apply", "--manifest", str(copy), "--self-session", SELF, "--out", str(out))[0], 0)
        manifest.chmod(0o644)
        self.assertNotEqual(self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out))[0], 0)
        self.assertTrue(d.exists())

    def test_class_b_entry_modified_since_the_manifest_is_skipped(self):
        d = self.make("t")
        manifest, out = self.inventory_manifest(cls="B")
        entry_id = json.loads(manifest.read_text())["entries"][0]["id"]
        (d / "late-file").write_text("new work")
        os.utime(d, (time.time() + 5, time.time() + 5))
        self.run_cli("apply", "--manifest", str(manifest), "--self-session", SELF, "--out", str(out), "--ids", entry_id)
        self.assertTrue(d.exists())

    def test_a_root_that_contains_a_protected_root_is_class_c(self):
        ctx = rl.make_ctx(SELF, self.sessions)
        rec = {"path": str(rl.HOME.parent), "kind": "cargo_target", "markers": [], "scan_complete": True, "receipt": None}
        self.assertIn("protected-root", rl.classify(rec, ctx)[1])

    def test_unrecognised_manifest_is_refused(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"schema": 9}))
        self.assertNotEqual(self.run_cli("verify", "--manifest", str(bad))[0], 0)


if __name__ == "__main__":
    unittest.main()
