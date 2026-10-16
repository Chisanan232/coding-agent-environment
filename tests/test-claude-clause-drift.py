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


if __name__ == "__main__":
    unittest.main()
