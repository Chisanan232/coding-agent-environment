#!/usr/bin/env python3
"""End-to-end test of the SPE-108 operator scripts in a synthetic HOME (never the real one).

Order under test: install (no overlay) -> build overlay -> install -> retire legacy sections -> drift.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "scripts/spe108-live"

LEGACY = (
    "# title\n\n## Commit Policy\n\n1. old rule\n\n---\n\n## Pull Request Policy\nx\n\n## Git Worktree Workflow\nx\n\n"
    "## Delegation Model\nx\n\n## Zed Private Rule\n\n12. only in the live file\nplain bullet only here\n\n---\n\n"
    "## CodeGraph\nlive line extra\n"
    "<!-- coding-agent-environment:engineering-workflow:start -->\n## Merge Policy\nOLD merge text\n\n"
    "## Decision Delegation\nold\n<!-- coding-agent-environment:engineering-workflow:end -->\n"
)


class LiveScripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()  # macOS /var is a symlink and the script refuses symlinked parents
        self.home = base / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.live = self.home / ".claude/CLAUDE.md"
        self.live.write_text(LEGACY)
        self.repo = base / "repo"
        subprocess.run(["git", "clone", "-q", "--local", str(ROOT), str(self.repo)], check=True)
        # The clone's origin/main must equal HEAD (the script's freshness rule); mirror the working tree in.
        subprocess.run(["rsync", "-a", "--exclude", ".git", f"{ROOT}/", f"{self.repo}/"], check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "snapshot", "--allow-empty"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        self.env = {**os.environ, "HOME": str(self.home)}

    def run_script(self, *args, ok=True):
        done = subprocess.run(list(args), capture_output=True, text=True, env=self.env, cwd=self.repo)
        if ok:
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def install(self):
        return self.run_script("bash", "scripts/profile-install.sh", "--claude-skills")

    def test_install_build_install_retire_leaves_one_copy_of_each_policy(self):
        retire = str(self.repo / "scripts/spe108-live/retire-sections.py")
        build = str(self.repo / "scripts/spe108-live/build-overlay.py")
        refused = self.run_script(sys.executable, retire, ok=False)
        self.assertNotEqual(refused.returncode, 0)  # no managed span with the replacements yet
        self.install()
        self.run_script(sys.executable, build, "--repo", str(self.repo))
        overlay = self.home / ".config/coding-agent-environment/local/baseline/claude/CLAUDE.private.md"
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)
        again = self.run_script(sys.executable, build, "--repo", str(self.repo), ok=False)
        self.assertIn("already exists", again.stderr + again.stdout)
        self.install()
        text = self.live.read_text()
        self.assertEqual(text.count("\n## Merge Policy\n"), 1)
        self.assertNotIn("OLD merge text", text)
        digest = hashlib.sha256(self.live.read_bytes()).hexdigest()
        mode_before = self.live.stat().st_mode & 0o777
        dry = self.run_script(sys.executable, retire, "--repo", str(self.repo))
        self.assertIn("legacy sections 4", dry.stdout)
        self.assertEqual(self.live.read_text(), text)  # dry run changes nothing
        wrong = self.run_script(sys.executable, retire, "--apply", "--expect-sha256", "0" * 64, ok=False)
        self.assertNotEqual(wrong.returncode, 0)
        self.run_script(sys.executable, retire, "--apply", "--expect-sha256", digest)
        after = self.live.read_text()
        for gone in ("## Commit Policy", "## Pull Request Policy", "## Git Worktree Workflow"):
            self.assertNotIn(gone, after)
        self.assertEqual(after.count("\n## Delegation Model\n"), 1)  # only the managed span's copy remains
        self.assertIn("12. only in the live file", after)  # live-only rule survived outside the legacy blocks
        self.assertIn("live line extra", overlay.read_text())
        self.assertEqual(self.live.stat().st_mode & 0o777, mode_before)
        backups = list((self.home / ".claude/backups").glob("spe-108-retire-*/CLAUDE.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(hashlib.sha256(backups[0].read_bytes()).hexdigest(), digest)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        second = self.run_script(sys.executable, retire, ok=False)
        self.assertNotEqual(second.returncode, 0)  # nothing left to retire

    def test_retire_refuses_a_marker_inside_a_legacy_section(self):
        self.install()
        with self.live.open("a") as handle:
            handle.write("\n## Commit Policy\n<!-- coding-agent-environment:private-overlay:start -->\nx\n")
        done = self.run_script(sys.executable, str(self.repo / "scripts/spe108-live/retire-sections.py"), ok=False)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
