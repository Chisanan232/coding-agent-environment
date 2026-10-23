#!/usr/bin/env python3
"""scripts/ask-first-apply.sh on scratch settings only (SPE-118). Never reads or writes the real ~/.claude."""
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ask-first-apply.sh"
sys.dont_write_bytecode = True
DUMMY = "DUMMY-NOT-A-REAL-SECRET-123"
LIVE = {
    "env": {"EXAMPLE_TOKEN": DUMMY}, "model": "m", "effortLevel": "medium",
    "permissions": {"allow": ["Bash(gh pr merge:*)", "Bash(rtk gh pr merge:*)", "Bash(gh pr *)", "Bash(rtk gh pr *)", "mcp__github__merge_pull_request", "Bash(ls:*)"],
                    "deny": ["Bash(rm -rf /)"], "ask": ["Bash(gh api *pulls/*/merge*)", "Bash(rtk gh api *pulls/*/merge*)", "Bash(gh pr merge * --admin*)", "Bash(git push --force:*)"]},
    "autoMode": {"environment": ["$defaults", "Live trusted-source fact."]},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk-hook"}]}]},
    "statusLine": {"type": "command", "command": "sl"},
}


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AskFirst(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="askfirst-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.live = self.d / "settings.json"
        self.live.write_text(json.dumps(LIVE, indent=2))
        self.original = self.live.read_bytes()
        self.backups = self.d / "backups"

    def run_script(self, *args):
        env = {**os.environ, "LIVE_SETTINGS": str(self.live), "BACKUP_DIR": str(self.backups)}
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_plan_only_changes_nothing_and_says_so(self):
        rc, out = self.run_script()
        self.assertEqual(rc, 0, out)
        self.assertIn("PLAN ONLY", out)
        self.assertIn("asks_added=35 allow_added=0 blocking=false", out)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertFalse(self.backups.exists())

    def test_apply_pins_verifies_backs_up_and_preserves_everything_else(self):
        rc, out = self.run_script("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("APPLIED and VERIFIED", out)
        applied = json.loads(self.live.read_text())
        self.assertEqual(applied["permissions"]["allow"], LIVE["permissions"]["allow"])
        self.assertEqual(applied["permissions"]["deny"], LIVE["permissions"]["deny"])
        self.assertEqual(applied["autoMode"], LIVE["autoMode"])
        for key in ("env", "model", "effortLevel", "hooks", "statusLine"):
            self.assertEqual(applied[key], LIVE[key], key)
        self.assertEqual(len(applied["permissions"]["ask"]) - len(LIVE["permissions"]["ask"]), 35)  # the two api-merge asks already existed and are not duplicated
        for ask in LIVE["permissions"]["ask"]:
            self.assertIn(ask, applied["permissions"]["ask"])
        backups = sorted(self.backups.glob("settings.*.json"))
        self.assertEqual(len(backups), 2)  # candidate-rollout's pinned backup and the extra pre-ask-first copy
        for b in backups:
            self.assertEqual(b.read_bytes(), self.original)
            self.assertEqual(stat.S_IMODE(b.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.backups / "ask-first-rollback.json").stat().st_mode), 0o600)

    def test_never_prints_settings_values(self):
        _, out = self.run_script("--apply")
        self.assertNotIn(DUMMY, out)
        _, out = self.run_script("--rollback")
        self.assertNotIn(DUMMY, out)

    def test_second_apply_is_a_no_op(self):
        self.run_script("--apply")
        after = self.live.read_bytes()
        rc, out = self.run_script("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("already applied", out)
        self.assertEqual(self.live.read_bytes(), after)

    def test_rollback_restores_the_exact_original_bytes_once(self):
        self.run_script("--apply")
        rc, out = self.run_script("--rollback")
        self.assertEqual(rc, 0, out)
        self.assertIn("ROLLED BACK", out)
        self.assertEqual(self.live.read_bytes(), self.original)
        rc, out = self.run_script("--rollback")
        self.assertNotEqual(rc, 0)
        self.assertIn("no rollback record", out)

    def test_rollback_refuses_when_live_changed_after_apply(self):
        self.run_script("--apply")
        self.live.write_text(self.live.read_text() + "\n")
        rc, out = self.run_script("--rollback")
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(self.live.read_bytes(), self.original)

    def test_missing_live_file_and_bad_argument_are_refused(self):
        self.live.unlink()
        rc, out = self.run_script()
        self.assertNotEqual(rc, 0)
        self.assertIn("live settings not found", out)
        self.live.write_text(json.dumps(LIVE))
        rc, out = self.run_script("--bogus")
        self.assertNotEqual(rc, 0)
        self.assertIn("usage", out)

    def test_after_apply_the_pinned_merge_asks_instead_of_denies_and_reads_stay_silent(self):
        """Emulator-based (the repository's prefix-rule matcher), NOT the real host: it cannot prove a prompt appears."""
        omg = load("omg_askfirst", "scripts/owner-merge-gate.py")
        pinned = omg.merge_command({"pr": 7, "repo": "o/r", "head": "a" * 40})
        self.assertEqual(omg.host_permission([str(self.live)], pinned), "allow")  # before: silent
        self.run_script("--apply")
        self.assertEqual(omg.host_permission([str(self.live)], pinned), "ask")  # after: ask, not deny
        for read in ("gh pr view 7 --json mergeable", "gh pr list --json mergeable", "gh pr checks 7", "gh pr comment 7 --body x"):
            self.assertNotIn(omg.host_permission([str(self.live)], read), ("ask", "deny"), read)
        d = omg.decide("PASS", [], {"class": "A", "pr": 7, "repo": "o/r", "head": "a" * 40}, omg.host_permission([str(self.live)], pinned))
        self.assertEqual((d["outcome"], d["asks"][0]["resolver"]), ("ASK", "host_prompt"))
        self.assertEqual(d["denies"], [])


if __name__ == "__main__":
    unittest.main()
