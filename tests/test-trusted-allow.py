#!/usr/bin/env python3
"""Trusted ALLOW candidate, rollout guard, installer and apply wrapper (SPE-118). Scratch settings only; never reads or writes the real ~/.claude."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
HOME = "/Users/bryant"
DUMMY = "DUMMY-NOT-A-REAL-SECRET-123"


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wsp = load("wsp_ta", "scripts/workstation-policy.py")
cr = load("cr_ta", "scripts/candidate-rollout.py")
omg = load("omg_ta", "scripts/owner-merge-gate.py")
runner = load("runner_ta", "scripts/owner-merge-runner.py")
RULE = wsp.trusted_runner_rule(HOME)
RUNNER_CMD = f"{HOME}/.claude/bin/owner-merge-runner --repo o/r --pr 7 --expect-head {'a' * 40} --review-evidence /tmp/e.json"
PINNED = f"gh pr merge 7 --repo o/r --merge --match-head-commit {'a' * 40}"


BASELINE_ASKS = json.loads((ROOT / ".claude/settings.json").read_text())["permissions"]["ask"]  # the proxy and other protected asks every live file carries


def live_with_ask_first():
    return {"env": {"EXAMPLE_TOKEN": DUMMY}, "model": "m", "autoMode": {"environment": ["$defaults", "fact"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk-hook"}]}]},
            "permissions": {"allow": ["Bash(gh pr merge:*)", "Bash(rtk gh pr merge:*)", "Bash(gh pr *)", "Bash(rtk gh pr *)", "mcp__github__merge_pull_request", "Bash(ls:*)"],
                            "deny": ["Bash(rm -rf /)"], "ask": BASELINE_ASKS + [a for a in wsp.merge_scope_candidate()["permissions"]["ask"] if a not in BASELINE_ASKS]}}


class Candidate(unittest.TestCase):
    def test_exactly_one_allow_rule_and_nothing_else(self):
        cand = wsp.trusted_allow_candidate(HOME)
        self.assertEqual(set(cand), {"_scope_note", "permissions"})
        self.assertEqual(cand["permissions"], {"allow": [RULE], "deny": []})
        self.assertEqual(wsp.lint_settings(cand), [])

    def test_committed_example_equals_the_generator(self):
        self.assertEqual(json.loads((ROOT / "docs/examples/candidate-settings-trusted-allow.example.json").read_text()), wsp.trusted_allow_candidate(HOME))

    def test_the_lint_exception_is_exactly_the_runner_rule(self):
        for bad in ("Bash(/Users/bryant/.claude/bin/owner-merge-runner *)", "Bash(/Users/bryant/.claude/bin/owner-merge-runner --repo * --pr * --expect-head *)",
                    "Bash(/tmp/owner-merge-runner --repo * --pr * --expect-head * --review-evidence *)",
                    "Bash(/Users/bryant/.claude/bin/other --repo * --pr * --expect-head * --review-evidence *)",
                    "Bash(/Users/bryant/.claude/bin/owner-merge-runner --repo * --pr * --expect-head * --review-evidence * --admin)",
                    "Bash(gh pr merge * --merge --match-head-commit *)", "Bash(python3 /Users/bryant/.claude/bin/owner-merge-runner --repo * --pr * --expect-head * --review-evidence *)",
                    "Bash(*)"):
            self.assertTrue(wsp.lint_settings({"permissions": {"allow": [bad], "deny": []}}), bad)

    def test_render_flag_combinations_are_refused(self):
        self.assertEqual(wsp.main(["render-settings", "--kind", "personal", "--trusted-allow", "--merge-scope"]), 3)
        self.assertEqual(wsp.main(["render-settings", "--kind", "personal", "--trusted-allow", "--asks-only"]), 3)


class Precedence(unittest.TestCase):
    def merged(self):
        live = live_with_ask_first()
        return cr.merge(live, wsp.trusted_allow_candidate(HOME))[0]

    def test_runner_is_allowed_while_every_direct_merge_shape_still_asks(self):
        perms = self.merged()["permissions"]
        self.assertEqual(cr.pr.static_decision(RUNNER_CMD, perms), "allow")
        for cmd in wsp.MERGE_SCOPE_COMMANDS + ["rtk " + c for c in wsp.MERGE_SCOPE_COMMANDS] + [PINNED, "rtk " + PINNED]:
            self.assertEqual(cr.pr.static_decision(cmd, perms), "ask", cmd)

    def test_the_decision_is_allow_for_the_runner_route_and_ask_for_the_direct_route(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.json"
            p.write_text(json.dumps(self.merged()))
            # the runner is invoked by absolute path, so the permission matcher sees it as written (no rtk spelling is judged for it)
            self.assertEqual(cr.pr.static_decision(RUNNER_CMD, self.merged()["permissions"]), "allow")
            self.assertEqual(omg.host_permission([str(p)], PINNED), "ask")
            d_ = omg.decide("PASS", [], {"class": "A", "pr": 7, "repo": "o/r", "head": "a" * 40}, omg.host_permission([str(p)], PINNED))
            self.assertEqual((d_["outcome"], d_["asks"][0]["resolver"]), ("ASK", "host_prompt"))

    def test_other_permissions_are_untouched(self):
        live = live_with_ask_first()
        merged = self.merged()
        self.assertEqual(merged["permissions"]["ask"], live["permissions"]["ask"])
        self.assertEqual(merged["permissions"]["deny"], live["permissions"]["deny"])
        self.assertEqual(merged["permissions"]["allow"], live["permissions"]["allow"] + [RULE])
        for key in ("env", "model", "autoMode", "hooks"):
            self.assertEqual(merged[key], live[key], key)


class RolloutGuard(unittest.TestCase):
    def setUp(self):
        saved = os.environ.get("HOME")
        os.environ["HOME"] = HOME  # the guard requires the rule's home to be this user's home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", saved) if saved is not None else os.environ.pop("HOME", None))
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ta-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.cand = self.d / "cand.json"
        self.cand.write_text(json.dumps(wsp.trusted_allow_candidate(HOME)))

    def plan(self, live):
        lp = self.d / "live.json"
        lp.write_text(json.dumps(live))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cr.main(["plan", "--live", str(lp), "--candidate", str(self.cand)])
        return json.loads(buf.getvalue())

    def test_plan_on_top_of_ask_first_is_non_blocking_and_adds_one_allow(self):
        p = self.plan(live_with_ask_first())
        self.assertFalse(p["blocking"], p["compat"])
        self.assertEqual((len(p["report"]["asks_added"]), p["report"]["allow_added"]), (0, [RULE]))

    def test_plan_blocks_without_ask_first(self):
        live = live_with_ask_first()
        live["permissions"]["ask"] = list(BASELINE_ASKS)
        p = self.plan(live)
        self.assertTrue(p["blocking"])
        self.assertTrue(any("requires ASK-first" in f for f in p["compat"]))

    def test_plan_blocks_when_an_ask_or_deny_swallows_the_runner_command(self):
        for bucket, rule in (("ask", f"Bash({HOME}/.claude/bin/*)"), ("deny", f"Bash({HOME}/.claude/bin/owner-merge-runner:*)")):
            live = live_with_ask_first()
            live["permissions"][bucket].append(rule)
            p = self.plan(live)
            self.assertTrue(p["blocking"], bucket)
            self.assertTrue(any("runner command would not be allowed" in f for f in p["compat"]), bucket)

    def test_plan_blocks_a_runner_rule_for_another_home(self):
        cand = wsp.trusted_allow_candidate("/Users/someone-else")
        self.cand.write_text(json.dumps(cand))
        p = self.plan(live_with_ask_first())
        self.assertTrue(p["blocking"])
        self.assertTrue(any("not this user's home" in f for f in p["compat"]))

    def test_a_trusted_candidate_cannot_smuggle_another_allow(self):
        cand = wsp.trusted_allow_candidate(HOME)
        cand["permissions"]["allow"].append("Bash(gh pr merge:*)")
        self.cand.write_text(json.dumps(cand))
        self.assertTrue(self.plan(live_with_ask_first())["blocking"])


class Scripts(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ta-sh-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.home = self.d / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.live = self.home / ".claude/settings.json"
        self.live.write_text(json.dumps(live_with_ask_first(), indent=2))
        self.original = self.live.read_bytes()
        self.runner = self.home / ".claude/bin/owner-merge-runner"

    def sh(self, script, *args, env=None):
        e = {**os.environ, "HOME": str(self.home), "LIVE_SETTINGS": str(self.live), "BACKUP_DIR": str(self.home / ".claude/backups"), **(env or {})}
        r = subprocess.run(["bash", str(ROOT / "scripts" / script), *args], capture_output=True, text=True, env=e, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_apply_refuses_until_the_runner_is_installed_and_identical(self):
        rc, out = self.sh("trusted-allow-apply.sh", "--apply")
        self.assertNotEqual(rc, 0)
        self.assertIn("not installed", out)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.sh("install-owner-merge-runner.sh")
        self.runner.write_text(self.runner.read_text() + "\n# tampered\n")
        rc, out = self.sh("trusted-allow-apply.sh", "--apply")
        self.assertNotEqual(rc, 0)
        self.assertIn("differs", out)
        self.assertEqual(self.live.read_bytes(), self.original)

    def test_a_symlinked_or_directory_runner_path_is_refused_and_the_override_is_gone(self):
        self.runner.parent.mkdir(parents=True)
        real = self.d / "elsewhere"
        real.write_bytes((ROOT / "scripts/owner-merge-runner.py").read_bytes())
        self.runner.symlink_to(real)
        rc, out = self.sh("install-owner-merge-runner.sh", "--update")
        self.assertNotEqual(rc, 0)
        self.assertIn("symlink", out)
        rc, out = self.sh("trusted-allow-apply.sh", "--apply")
        self.assertNotEqual(rc, 0)
        self.assertIn("symlink", out)
        self.runner.unlink()
        self.runner.mkdir()
        rc, out = self.sh("install-owner-merge-runner.sh", "--update")
        self.assertNotEqual(rc, 0)
        self.assertIn("directory", out)
        self.assertEqual(self.live.read_bytes(), self.original)
        # an override variable no longer redirects either script
        other = self.d / "other-runner"
        rc, out = self.sh("install-owner-merge-runner.sh", env={"OWNER_MERGE_RUNNER_PATH": str(other)})
        self.assertFalse(other.exists())

    def test_installer_fixes_the_mode_of_an_identical_non_executable_runner(self):
        self.sh("install-owner-merge-runner.sh")
        self.runner.chmod(0o644)
        rc, out = self.sh("install-owner-merge-runner.sh")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.runner.stat().st_mode & 0o777, 0o755)

    def test_installer_installs_atomically_refuses_a_different_file_and_updates_on_request(self):
        rc, out = self.sh("install-owner-merge-runner.sh")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.runner.read_bytes(), (ROOT / "scripts/owner-merge-runner.py").read_bytes())
        self.assertEqual(self.runner.stat().st_mode & 0o777, 0o755)
        rc, out = self.sh("install-owner-merge-runner.sh")
        self.assertIn("already installed", out)
        self.runner.write_text("#!/usr/bin/env python3\nprint('x')\n")
        rc, out = self.sh("install-owner-merge-runner.sh")
        self.assertNotEqual(rc, 0)
        self.assertIn("differs", out)
        rc, out = self.sh("install-owner-merge-runner.sh", "--update")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.runner.read_bytes(), (ROOT / "scripts/owner-merge-runner.py").read_bytes())
        self.assertFalse(list((self.home / ".claude/bin").glob(".owner-merge-runner.*")))

    def test_plan_apply_rollback_end_to_end(self):
        self.sh("install-owner-merge-runner.sh")
        rc, out = self.sh("trusted-allow-apply.sh")
        self.assertEqual(rc, 0, out)
        self.assertIn("allow_added=1", out)
        self.assertIn("PLAN ONLY", out)
        self.assertEqual(self.live.read_bytes(), self.original)
        rc, out = self.sh("trusted-allow-apply.sh", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("APPLIED and VERIFIED", out)
        self.assertNotIn(DUMMY, out)
        applied = json.loads(self.live.read_text())
        live = live_with_ask_first()
        self.assertEqual(applied["permissions"]["allow"], live["permissions"]["allow"] + [wsp.trusted_runner_rule(str(self.home))])
        self.assertEqual(applied["permissions"]["ask"], live["permissions"]["ask"])
        self.assertEqual(applied["permissions"]["deny"], live["permissions"]["deny"])
        rc, out = self.sh("trusted-allow-apply.sh", "--apply")
        self.assertIn("already applied", out)
        rc, out = self.sh("trusted-allow-apply.sh", "--rollback")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.live.read_bytes(), self.original)

    def test_plan_refuses_without_ask_first(self):
        cfg = live_with_ask_first()
        cfg["permissions"]["ask"] = list(BASELINE_ASKS)
        self.live.write_text(json.dumps(cfg))
        before = self.live.read_bytes()
        self.sh("install-owner-merge-runner.sh")
        rc, out = self.sh("trusted-allow-apply.sh", "--apply")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.live.read_bytes(), before)


class Evidence(unittest.TestCase):
    def test_evidence_json_is_accepted_by_the_runner_loader_for_the_same_head_only(self):
        head = "b" * 40
        ev = omg.evidence_json("o/r", 7, "main", head, "agent-rv1")
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "e.json"
            p.write_text(json.dumps(ev))
            cfg = {"reviewers": ["agent-rv1"], "require_signed_evidence": False, "local_equivalent_ci": False, "max_evidence_age_hours": 24}
            sha, local = runner.load_evidence(str(p), head, "o/r", "7", "main", cfg, None, time.time())
            self.assertEqual(len(sha), 64)
            for kw in ({"head": "c" * 40}, {"base": "dev"}, {"pr": "8"}):
                args = {"head": head, "repo": "o/r", "pr": "7", "base": "main", **kw}
                with self.assertRaises(runner.Refused):
                    runner.load_evidence(str(p), args["head"], args["repo"], args["pr"], args["base"], cfg, None, time.time())
            with self.assertRaises(runner.Refused):  # a reviewer that is not allowlisted
                runner.load_evidence(str(p), head, "o/r", "7", "main", {**cfg, "reviewers": ["someone-else"]}, None, time.time())

    def test_evidence_json_rejects_malformed_input(self):
        for kw in ({"head": "abc"}, {"repo": "nope"}, {"pr": "0"}, {"reviewer": "x"}, {"kind": "robot"}, {"base": "bad branch"}):
            args = {"repo": "o/r", "pr": 7, "base": "main", "head": "a" * 40, "reviewer": "agent-rv1", "kind": "independent-agent", **kw}
            with self.assertRaises(ValueError):
                omg.evidence_json(**args)


if __name__ == "__main__":
    unittest.main()
