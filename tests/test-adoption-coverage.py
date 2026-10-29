#!/usr/bin/env python3
"""Adoption coverage report: synthetic projects and sessions only; the real machine is never read."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/adoption-coverage.py"
spec = importlib.util.spec_from_file_location("adoption_coverage", SCRIPT)
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)


def make_project(root, name, claude=None, agents=None, git_dir=True):
    base = Path(root) / name
    (base / ".claude").mkdir(parents=True)
    if git_dir:
        (base / ".git").mkdir()
    if claude is not None:
        (base / ".claude/CLAUDE.md").write_text(claude)
    if agents is not None:
        (base / "AGENTS.md").write_text(agents)
    return base


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_clean_project_is_adopted(self):
        p = make_project(self.tmp.name, "clean", claude="# Notes\nUse the global rules.\n")
        row = ac.classify(p)
        self.assertEqual(row["status"], "ADOPTED")
        self.assertEqual(row["checkout"], "main checkout")

    def test_legacy_commit_format_is_flagged_with_line_number_only(self):
        p = make_project(self.tmp.name, "legacy", claude="intro\nFormat: `<emoji> <scope>: summary`\n")
        row = ac.classify(p)
        self.assertEqual(row["status"], "OVERRIDE_REVIEW")
        self.assertEqual(row["files"][".claude/CLAUDE.md"]["legacy_format"], [2])
        self.assertNotIn("summary", json.dumps(row))

    def test_parenthesised_scope_is_not_legacy(self):
        p = make_project(self.tmp.name, "new", claude="Format: `<emoji> (<scope>): summary`\n")
        self.assertEqual(ac.classify(p)["status"], "ADOPTED")

    def test_squash_override_is_flagged_for_review_not_rewritten(self):
        p = make_project(self.tmp.name, "squashy", agents="Merges are squash merges here.\n")
        before = (p / "AGENTS.md").read_text()
        self.assertEqual(ac.classify(p)["status"], "OVERRIDE_REVIEW")
        self.assertEqual((p / "AGENTS.md").read_text(), before)

    def test_squash_prohibitions_are_not_overrides_but_permissions_are(self):
        never = make_project(self.tmp.name, "never", claude="Merge commit only - never squash or rebase.\nSquash merge is disabled.\n")
        permit = make_project(self.tmp.name, "permit", claude="| Merge | **Squash** for feature PRs; merge commits are disabled |\n")
        self.assertEqual(ac.classify(never)["status"], "ADOPTED")
        self.assertEqual(ac.classify(permit)["files"][".claude/CLAUDE.md"]["merge_override"], [1])

    def test_branch_format_three_part_flagged_four_part_not(self):
        three = make_project(self.tmp.name, "three", claude="Branch: `<phase>/<ticket-id>/<short_summary>`\n")
        four = make_project(self.tmp.name, "four", claude="Branch: `<release-or-phase>/<ticket>/<type>/<short_summary>`\n")
        self.assertEqual(ac.classify(three)["files"][".claude/CLAUDE.md"]["old_branch_format"], [1])
        self.assertEqual(ac.classify(four)["status"], "ADOPTED")

    def test_squash_phrased_with_a_negation_elsewhere_is_still_a_permission(self):
        p = make_project(self.tmp.name, "sq", agents="Merge strategy: squash all PRs (no merge commits).\n")
        self.assertEqual(ac.classify(p)["files"]["AGENTS.md"]["merge_override"], [1])

    def test_symlink_leaving_the_checkout_is_not_read(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("squash everything <emoji> <scope>: x\n")
        p = make_project(self.tmp.name, "linked")
        (p / "CLAUDE.md").symlink_to(outside)
        info = ac.classify(p)["files"]["CLAUDE.md"]
        self.assertEqual(info["skipped"], "symlink leaves the checkout")
        self.assertEqual(info["merge_override"] + info["legacy_format"], [])

    def test_missing_checkout_and_missing_files(self):
        self.assertEqual(ac.classify(Path(self.tmp.name) / "nope")["status"], "NOT_FOUND")
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(ac.classify(empty)["status"], "NO_PROJECT_FILE")

    def test_linked_worktree_is_labelled(self):
        p = make_project(self.tmp.name, "wt", claude="x\n", git_dir=False)
        (p / ".git").write_text("gitdir: /elsewhere\n")
        self.assertEqual(ac.classify(p)["checkout"], "linked worktree")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def record(self, name, pid, cwd, started_ms):
        (self.dir / name).write_text(json.dumps(
            {"pid": pid, "cwd": cwd, "startedAt": started_ms, "status": "idle", "name": "private"}))

    def test_stale_live_session_needs_reload_and_current_does_not(self):
        now = time.time()
        # Own pid with a start time near this process's real start counts as live.
        started = int((now - self._elapsed()) * 1000)
        self.record("a.json", os.getpid(), "/proj/a/sub", started)
        newer = ac.sessions(self.dir, {"*": 0, "/proj/a": now})  # instruction file changed after start
        older = ac.sessions(self.dir, {"*": 0, "/proj/a": now - 10**6})
        self.assertEqual(newer[0]["reload"], "RELOAD_NEEDED")
        self.assertEqual(older[0]["reload"], "current")

    def test_main_end_to_end_and_malformed_map(self):
        import contextlib
        import io
        proj = make_project(self.tmp.name, "proj", claude="Branch: `<phase>/<ticket-id>/<short_summary>`\n")
        mapping = Path(self.tmp.name) / "map.json"
        mapping.write_text(json.dumps({"P": [str(proj)]}))
        glob = Path(self.tmp.name) / "global.md"
        glob.write_text("g\n")
        sess = Path(self.tmp.name) / "sessions"
        sess.mkdir()
        started = int((time.time() - ac.elapsed_seconds(os.getpid())) * 1000)
        (sess / "a.json").write_text(json.dumps({"pid": os.getpid(), "cwd": str(proj / "sub"), "startedAt": started}))
        os.utime(proj / ".claude/CLAUDE.md", (time.time() + 5, time.time() + 5))  # edited after the session started
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ac.main(["--map", str(mapping), "--global-file", str(glob), "--sessions-dir", str(sess), "--json"])
        result = json.loads(out.getvalue())
        self.assertEqual(result["projects"]["P"][0]["status"], "OVERRIDE_REVIEW")
        self.assertEqual([x["reload"] for x in result["sessions"]], ["RELOAD_NEEDED"])
        for bad in ('{"P": "/abs"}', '["x"]', '{"P": []}'):
            mapping.write_text(bad)
            with self.assertRaises(SystemExit):
                ac.main(["--map", str(mapping), "--sessions-dir", str(sess)])

    def test_etime_formats(self):
        import subprocess
        from unittest import mock
        for text, want in (("05:07", 307), ("02:03:04", 7384), ("3-02:03:04", 266584)):
            with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=text + "\n")):
                self.assertEqual(ac.elapsed_seconds(1), want)
        self.assertIsNotNone(ac.elapsed_seconds(os.getpid()))
        self.assertIsNone(ac.elapsed_seconds(2**22 + 12345))

    def test_reused_pid_and_dead_pid_are_ignored(self):
        self.record("reused.json", os.getpid(), "/x", int((time.time() - 10**6) * 1000))
        self.record("dead.json", 2**22 + 12345, "/x", int(time.time() * 1000))
        self.assertEqual(ac.sessions(self.dir, {"*": 0}), [])

    def test_report_never_contains_session_name(self):
        self.record("a.json", os.getpid(), "/x", int((time.time() - self._elapsed()) * 1000))
        self.assertNotIn("private", json.dumps(ac.sessions(self.dir, {"*": 0})))

    @staticmethod
    def _elapsed():
        return ac.elapsed_seconds(os.getpid())


if __name__ == "__main__":
    unittest.main()
