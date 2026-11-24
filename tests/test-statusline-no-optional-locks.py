#!/usr/bin/env python3
"""The statusline polls git every few seconds and kills it at a short timeout; it must never take index.lock.

A plain `git status` refreshes the index under .git/index.lock; a SIGKILL mid-refresh leaves a stale lock
that blocks the next real git write in that checkout. `--no-optional-locks` removes the lock from the poll.
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("statusline", ROOT / ".claude/statusline.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class StatuslineGitPoll(unittest.TestCase):
    def test_the_poll_passes_no_optional_locks_before_the_subcommand(self):
        seen = []

        def fake_run(args, cwd=None, timeout=0.4):
            seen.append(list(args))
            return None

        with patch.object(MODULE, "run", fake_run):
            MODULE.git_segments("/nonexistent")
        self.assertEqual(len(seen), 1)
        argv = seen[0]
        self.assertEqual(argv[0], "git")
        self.assertEqual(argv[1], "--no-optional-locks")
        self.assertEqual(argv[2], "status")

    def test_a_poll_against_a_real_repository_leaves_no_index_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            for command in (["git", "init", "-q"], ["git", "config", "user.email", "t@example.invalid"],
                            ["git", "config", "user.name", "t"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / "a.txt").write_text("a\n")
            subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
            segments = MODULE.git_segments(str(repo))
            self.assertTrue(segments, "a clean repository still yields a branch segment")
            self.assertFalse((repo / ".git" / "index.lock").exists())


if __name__ == "__main__":
    unittest.main()
