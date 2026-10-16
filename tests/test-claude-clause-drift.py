#!/usr/bin/env python3
"""Clause-drift report: live-only clauses must be covered by the base or the private overlay."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_TEXT = "SYNTHETIC-PRIVATE-WORDING"


class ClauseDrift(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / ".claude").mkdir()
        self.live = self.home / ".claude/CLAUDE.md"
        self.overlay = self.home / ".config/coding-agent-environment/local/baseline/claude/CLAUDE.private.md"
        self.overlay.parent.mkdir(parents=True)

    def put_overlay(self, text: str, mode: int = 0o600) -> None:
        self.overlay.write_text(text)
        self.overlay.chmod(mode)

    def report(self) -> subprocess.CompletedProcess:
        env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(self.home))
        env.pop("CODING_AGENT_LOCAL_DIR", None)
        env.pop("CODING_AGENT_LOCAL_BASELINE_DIR", None)
        return subprocess.run(["bash", str(ROOT / "scripts/sync-check.sh"), "--claude-clauses"],
                              env=env, text=True, capture_output=True)

    def test_clause_in_neither_layer_is_flagged_without_printing_its_text(self) -> None:
        self.live.write_text(f"## Workflow Path\n\n## Synthetic Orphan Clause\n{SECRET_TEXT}\n"
                             f"7. Synthetic orphan rule {SECRET_TEXT}\n")
        result = self.report()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout.count("UNTRACKED live"), 2)
        self.assertIn("heading", result.stdout)
        self.assertIn("rule", result.stdout)
        self.assertNotIn(SECRET_TEXT, result.stdout + result.stderr)
        self.assertNotIn("Orphan", result.stdout + result.stderr)

    def test_clause_covered_by_the_private_overlay_is_not_drift(self) -> None:
        self.live.write_text("## Workflow Path\n\n## Synthetic Overlay Clause\n9. Synthetic overlay rule\n")
        self.put_overlay("## Synthetic Overlay Clause\n9. Synthetic overlay rule\n")
        result = self.report()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("overlay present", result.stdout)
        self.assertNotIn("UNTRACKED", result.stdout)
        self.overlay.unlink()
        self.assertEqual(self.report().returncode, 1)  # negative control: same live file, no overlay

    def test_overlay_keyword_missing_from_live_is_flagged_by_hash(self) -> None:
        self.live.write_text("## Workflow Path\n")
        self.put_overlay("<!-- clause-keywords: SYNTHETIC-KEYWORD-ONE, Workflow Path -->\n")
        result = self.report()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout.count("MISSING overlay keyword"), 1)
        self.assertNotIn("SYNTHETIC-KEYWORD-ONE", result.stdout)

    def test_overlay_with_loose_mode_or_symlink_is_refused(self) -> None:
        self.live.write_text("## Workflow Path\n")
        self.put_overlay("## Synthetic Overlay Clause\n", mode=0o644)
        result = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn("mode 0600", result.stdout)
        self.overlay.unlink()
        target = self.home / "elsewhere.md"
        target.write_text("x")
        target.chmod(0o600)
        self.overlay.symlink_to(target)
        result = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlinked private overlay", result.stdout)

    def test_absent_live_policy_fails_without_reading_anything_else(self) -> None:
        result = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Live policy absent", result.stdout)


if __name__ == "__main__":
    unittest.main()
