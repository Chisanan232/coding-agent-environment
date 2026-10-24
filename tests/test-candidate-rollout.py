#!/usr/bin/env python3
"""scripts/candidate-rollout.py on scratch copies only (SPE-116). Never touches a real settings file."""
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cr", ROOT / "scripts/candidate-rollout.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
DUMMY = "DUMMY-NOT-A-REAL-SECRET-123"
LIVE = {"env": {"EXAMPLE_TOKEN": DUMMY}, "model": "m", "effortLevel": "medium",
        "permissions": {"allow": ["Bash(ls:*)", "Bash(gh pr merge:*)"], "deny": ["Bash(rm -rf /)"], "ask": ["Bash(git push --force:*)", "Bash(old ask:*)"]},
        "autoMode": {"environment": ["$defaults", "old long natural-language rule"], "allow": ["$defaults", "Authorized pull request merge: old text"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk-hook"}]}]},
        "statusLine": {"type": "command", "command": "sl"}}
BASELINE = {"permissions": {"ask": ["Bash(terraform destroy:*)", "Bash(rtk terraform destroy:*)"], "allow": ["Bash(everything:*)"]},
            "autoMode": {"allow": ["$defaults", "Authorized pull request merge: restored old text"]}}


class Rollout(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="cr-test-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.live, self.base, self.cand = self.d / "settings.json", self.d / "baseline.json", self.d / "cand.json"
        self.live.write_text(json.dumps(LIVE, indent=2))
        self.base.write_text(json.dumps(BASELINE))
        self.cand.write_text(json.dumps(cr.wsp.candidate("personal")))
        self.args = ["--live", str(self.live), "--candidate", str(self.cand), "--baseline", str(self.base)]

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = cr.main(list(argv))
            except SystemExit as e:
                return str(e.code), buf.getvalue()
        return rc, buf.getvalue()

    def plan(self, *extra):
        rc, out = self.run_cli("plan", *self.args, *extra)
        return json.loads(out)

    def test_merge_touches_only_autoMode_ask_and_the_optional_hook(self):
        merged, rep = cr.merge(LIVE, cr.wsp.candidate("personal"), BASELINE, "/x/hook.py")
        for key in ("env", "model", "effortLevel", "statusLine"):
            self.assertEqual(merged[key], LIVE[key])
        self.assertEqual(merged["permissions"]["allow"], LIVE["permissions"]["allow"])
        self.assertEqual(merged["permissions"]["deny"], LIVE["permissions"]["deny"])
        self.assertEqual(merged["autoMode"], cr.wsp.candidate("personal")["autoMode"])
        self.assertTrue(set(LIVE["permissions"]["ask"]) <= set(merged["permissions"]["ask"]))
        self.assertIn("Bash(terraform destroy:*)", merged["permissions"]["ask"])
        self.assertEqual(len(merged["permissions"]["ask"]), len(set(merged["permissions"]["ask"])))
        self.assertTrue(rep["hook_added"])
        self.assertEqual(merged["hooks"]["PreToolUse"][0], LIVE["hooks"]["PreToolUse"][0])
        self.assertEqual(LIVE["autoMode"]["allow"][1], "Authorized pull request merge: old text")  # input not mutated

    def test_asks_only_candidate_leaves_autoMode_exactly_as_it_is(self):
        cand = cr.wsp.candidate("personal", asks_only=True)
        self.assertNotIn("autoMode", cand)
        merged, rep = cr.merge(LIVE, cand)  # no baseline: only the candidate's asks are added
        self.assertEqual(merged["autoMode"], LIVE["autoMode"])
        self.assertEqual(rep["autoMode_before"], rep["autoMode_after"])
        for key in ("env", "model", "statusLine", "hooks"):
            self.assertEqual(merged[key], LIVE[key])
        self.assertEqual(merged["permissions"]["allow"], LIVE["permissions"]["allow"])
        self.assertTrue(set(LIVE["permissions"]["ask"]) <= set(merged["permissions"]["ask"]))
        self.assertTrue(rep["asks_added"])
        bare = {k: v for k, v in LIVE.items() if k != "autoMode"}  # owner reset: no autoMode block at all
        merged2, _ = cr.merge(bare, cand)
        self.assertNotIn("autoMode", merged2)

    def test_asks_only_plan_has_no_blocking_finding_and_example_matches_generator(self):
        self.cand.write_text(json.dumps(cr.wsp.candidate("personal", asks_only=True)))
        p = json.loads(self.run_cli("plan", "--live", str(self.live), "--candidate", str(self.cand))[1])
        self.assertFalse(p["blocking"])
        example = json.loads((ROOT / "docs/examples/candidate-settings-personal-asks-only.example.json").read_text())
        self.assertEqual(example, cr.wsp.candidate("personal", asks_only=True))
        self.assertEqual(cr.wsp.lint_settings(example), [])

    def test_autoMode_only_candidate_without_permissions_block_merges_and_plans(self):
        cand = {"autoMode": {"environment": ["$defaults", "Synthetic factual line for this test."]}}
        live = json.loads(json.dumps(LIVE))
        live["autoMode"] = {"environment": ["$defaults", "Old fact."]}
        self.live.write_text(json.dumps(live))
        merged, rep = cr.merge(live, cand)
        self.assertEqual(merged["autoMode"], cand["autoMode"])
        self.assertEqual(merged["permissions"], LIVE["permissions"])  # asks, allow and deny exactly as live
        self.assertEqual(rep["asks_added"], [])
        self.assertEqual(rep["asks_retired"], [])
        self.cand.write_text(json.dumps(cand))
        p = json.loads(self.run_cli("plan", "--live", str(self.live), "--candidate", str(self.cand))[1])
        self.assertFalse(p["blocking"])
        self.assertEqual(p["report"]["allow_added"], [])
        self.assertNotIn(DUMMY, json.dumps(p))

    def test_candidate_that_omits_environment_and_denies_keeps_the_live_ones(self):
        live = json.loads(json.dumps(LIVE))
        live["autoMode"] = {"environment": ["$defaults", "Live trusted-source fact."], "soft_deny": ["$defaults", "owner soft"], "hard_deny": ["$defaults", "owner hard"]}
        snapshot = json.dumps(live, sort_keys=True)
        cand = {"autoMode": {"allow": ["$defaults"]}}
        merged, rep = cr.merge(live, cand)
        self.assertEqual(merged["autoMode"]["environment"], ["$defaults", "Live trusted-source fact."])
        self.assertEqual(merged["autoMode"]["soft_deny"], ["$defaults", "owner soft"])
        self.assertEqual(merged["autoMode"]["hard_deny"], ["$defaults", "owner hard"])
        self.assertEqual(merged["autoMode"]["allow"], ["$defaults"])
        self.assertEqual(sorted(rep["autoMode_preserved"]), ["environment", "hard_deny", "soft_deny"])
        self.assertEqual(json.dumps(live, sort_keys=True), snapshot)  # live input is never mutated
        merged["autoMode"]["environment"].append("x")  # the merged copy does not alias the live list
        self.assertEqual(live["autoMode"]["environment"], ["$defaults", "Live trusted-source fact."])

    def test_candidate_that_replaces_environment_still_replaces_it_and_reports_the_drop(self):
        merged, rep = cr.merge(LIVE, {"autoMode": {"environment": ["$defaults", "New fact."]}})
        self.assertEqual(merged["autoMode"]["environment"], ["$defaults", "New fact."])
        self.assertNotIn("environment", rep["autoMode_preserved"])
        self.assertIn("old long natural-language rule", rep["dropped_from_live"]["environment"])

    def test_merged_autoMode_is_linted_so_a_preserved_live_entry_can_block(self):
        live = json.loads(json.dumps(LIVE))
        live["autoMode"] = {"environment": ["$defaults"] + [f"fact {i}" for i in range(9)]}  # too long once preserved
        self.live.write_text(json.dumps(live))
        self.cand.write_text(json.dumps({"autoMode": {"allow": ["$defaults"]}}))
        p = json.loads(self.run_cli("plan", "--live", str(self.live), "--candidate", str(self.cand))[1])
        self.assertTrue(p["blocking"])
        self.assertTrue(any("merged configuration lint" in f for f in p["compat"]), p["compat"])
        before = self.live.read_bytes()
        rc, _ = self.run_cli("apply", "--live", str(self.live), "--candidate", str(self.cand), "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))
        self.assertEqual(self.live.read_bytes(), before)

    def test_preserved_live_standing_merge_text_blocks_instead_of_slipping_through(self):
        cand = {"autoMode": {"environment": ["$defaults", "Fact."]}}  # omits allow, so the live allow text would be carried over
        merged, rep = cr.merge(LIVE, cand)
        self.assertIn("allow", rep["autoMode_preserved"])
        findings = cr.compat(LIVE, merged, cand)
        self.assertTrue(any(f.startswith("BLOCKING merged configuration lint") and "standing merge" in f for f in findings), findings)

    def test_asks_only_candidate_does_not_judge_an_untouched_live_autoMode(self):
        live = json.loads(json.dumps(LIVE))
        live["autoMode"] = {"allow": ["Custom allow rule"]}  # no $defaults, owner's own and untouched
        self.live.write_text(json.dumps(live))
        self.cand.write_text(json.dumps(cr.wsp.candidate("personal", asks_only=True)))
        p = json.loads(self.run_cli("plan", "--live", str(self.live), "--candidate", str(self.cand))[1])
        self.assertFalse(p["blocking"], p["compat"])
        self.assertFalse(any("lost $defaults" in f for f in p["compat"]))
        self.cand.write_text(json.dumps({"autoMode": {"allow": ["Custom allow rule"]}}))  # but an autoMode candidate still is judged
        p2 = json.loads(self.run_cli("plan", "--live", str(self.live), "--candidate", str(self.cand))[1])
        self.assertTrue(any("lost $defaults" in f for f in p2["compat"]), p2["compat"])

    def test_non_object_autoMode_candidate_is_refused_with_a_reason(self):
        for bad in (None, [], "x"):
            with self.assertRaises(SystemExit) as ctx:
                cr.merge(LIVE, {"autoMode": bad})
            self.assertIn("must be an object", str(ctx.exception))

    def test_owner_deny_entries_that_are_objects_are_carried_not_crashed_on(self):
        live = json.loads(json.dumps(LIVE))
        live["autoMode"] = {"hard_deny": ["$defaults", {"rule": "owner structured entry"}]}
        merged, rep = cr.merge(live, cr.wsp.candidate("personal"))
        self.assertIn({"rule": "owner structured entry"}, merged["autoMode"]["hard_deny"])
        self.assertEqual(merged["autoMode"]["hard_deny"][0], "$defaults")

    def test_structured_ask_and_allow_entries_plan_without_crashing(self):
        live = json.loads(json.dumps(LIVE))
        live["permissions"]["allow"] = live["permissions"]["allow"] + [{"a": 1}]
        live["permissions"]["ask"] = live["permissions"]["ask"] + [{"a": 1}]
        self.live.write_text(json.dumps(live))
        self.cand.write_text(json.dumps({"permissions": {"ask": [{"tool": "Bash", "pattern": "x"}]}}))
        p = json.loads(self.run_cli("plan", "--live", str(self.live), "--candidate", str(self.cand))[1])
        self.assertTrue(any("both allow and ask" in f and '"a": 1' in f for f in p["compat"]), p["compat"])
        self.assertIn({"tool": "Bash", "pattern": "x"}, cr.merge(live, json.loads(self.cand.read_text()))[0]["permissions"]["ask"])

    def test_non_ascii_live_bytes_roundtrip_exactly_and_no_temp_file_is_left(self):
        live = json.loads(json.dumps(LIVE))
        live["note"] = "café ✓ 日本"
        self.live.write_bytes((json.dumps(live, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        original = self.live.read_bytes()
        self.cand.write_text(json.dumps({"autoMode": {"allow": ["$defaults"]}}))
        args = ["--live", str(self.live), "--candidate", str(self.cand)]
        p = json.loads(self.run_cli("plan", *args)[1])
        rc, out = self.run_cli("apply", *args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertEqual(rc, 0, out)
        res = json.loads(out)
        self.assertEqual(cr.sha(self.live.read_bytes()), p["result_sha"])
        rc, _ = self.run_cli("rollback", "--live", str(self.live), "--backup", res["backup"], "--expect-backup-sha", p["live_sha"], "--expect-live-sha", p["result_sha"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_bytes(), original)
        self.assertEqual(list(self.d.glob(".rollout-*")), [])

    def test_compat_blocks_when_an_existing_ask_would_be_lost(self):
        cand = {"autoMode": {"allow": ["$defaults"]}}
        merged, _ = cr.merge(LIVE, cand)
        merged["permissions"]["ask"] = [a for a in merged["permissions"]["ask"] if a != "Bash(old ask:*)"]
        self.assertTrue(any("existing ask lost" in f and "old ask" in f for f in cr.compat(LIVE, merged, cand)))
        kept, _ = cr.merge(LIVE, cand)
        self.assertFalse(any("existing ask lost" in f for f in cr.compat(LIVE, kept, cand)))

    def test_autoMode_only_candidate_roundtrip_restores_the_exact_live_bytes(self):
        original = self.live.read_bytes()
        self.cand.write_text(json.dumps({"autoMode": {"allow": ["$defaults"]}}))
        args = ["--live", str(self.live), "--candidate", str(self.cand)]
        p = json.loads(self.run_cli("plan", *args)[1])
        self.assertFalse(p["blocking"], p["compat"])
        rc, out = self.run_cli("apply", *args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertEqual(rc, 0, out)
        res = json.loads(out)
        self.assertEqual(json.loads(self.live.read_text())["autoMode"]["environment"], LIVE["autoMode"]["environment"])
        rc, _ = self.run_cli("rollback", "--live", str(self.live), "--backup", res["backup"], "--expect-backup-sha", p["live_sha"], "--expect-live-sha", p["result_sha"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_bytes(), original)

    def test_baseline_autoMode_text_is_never_applied(self):
        merged, _ = cr.merge(LIVE, cr.wsp.candidate("personal"), BASELINE)
        self.assertNotIn("Authorized pull request merge", json.dumps(merged["autoMode"]))
        self.assertNotIn("Bash(everything:*)", merged["permissions"]["allow"])

    def test_plan_is_read_only_and_never_prints_env_values(self):
        before = self.live.read_bytes()
        rc, out = self.run_cli("plan", *self.args)
        self.assertEqual(self.live.read_bytes(), before)
        self.assertNotIn(DUMMY, out)
        self.assertNotIn("EXAMPLE_TOKEN", out)
        p = json.loads(out)
        self.assertEqual(p["live_sha"], cr.sha(before))
        self.assertNotIn("_text", p)

    def test_apply_verify_rollback_roundtrip(self):
        original = self.live.read_bytes()
        p = self.plan()
        bdir = self.d / "backups"
        rc, out = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(bdir))
        self.assertEqual(rc, 0, out)
        res = json.loads(out)
        self.assertEqual(cr.sha(self.live.read_bytes()), p["result_sha"])
        self.assertEqual(stat.S_IMODE(os.stat(res["backup"]).st_mode), 0o600)
        self.assertEqual(cr.sha(pathlib.Path(res["backup"]).read_bytes()), p["live_sha"])
        rc, out = self.run_cli("verify", "--live", str(self.live), "--expect-result-sha", p["result_sha"])
        self.assertEqual(json.loads(out)["verdict"], "VERIFIED")
        rc, out = self.run_cli("rollback", "--live", str(self.live), "--backup", res["backup"], "--expect-backup-sha", p["live_sha"], "--expect-live-sha", p["result_sha"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_bytes(), original)

    def test_apply_refuses_when_pins_do_not_match(self):
        p = self.plan()
        before = self.live.read_bytes()
        for live_sha, res_sha in (("0" * 64, p["result_sha"]), (p["live_sha"], "0" * 64)):
            rc, _ = self.run_cli("apply", *self.args, "--expect-live-sha", live_sha, "--expect-result-sha", res_sha, "--backup-dir", str(self.d / "b"))
            self.assertIn("REFUSED", str(rc))
        self.live.write_text(self.live.read_text() + " ")  # live changed after the plan
        rc, _ = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))
        self.assertEqual(self.live.read_bytes(), before + b" ")

    def test_blocking_candidate_cannot_be_applied(self):
        bad = cr.wsp.candidate("personal")
        bad["autoMode"]["allow"] = ["only mine"]
        self.cand.write_text(json.dumps(bad))
        p = self.plan()
        self.assertTrue(p["blocking"])
        before = self.live.read_bytes()
        rc, _ = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))
        self.assertEqual(self.live.read_bytes(), before)

    def test_second_apply_is_a_no_op_and_hook_is_added_once(self):
        extra = ["--hook-command", "/x/hook.py", "--fail-closed"]
        p = self.plan(*extra)
        self.run_cli("apply", *self.args, *extra, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        cfg = json.loads(self.live.read_text())
        hooks = [h for g in cfg["hooks"]["PreToolUse"] for h in g["hooks"] if h["command"] == "/x/hook.py"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["onFailure"], "block")
        again = self.plan(*extra)
        self.assertEqual(again["report"]["asks_added"], [])
        self.assertFalse(again["report"]["hook_added"])
        self.assertEqual(again["result_sha"], again["live_sha"])

    def test_compat_reports_standing_merge_allow_and_overlaps_without_blocking(self):
        p = self.plan()
        self.assertFalse(p["blocking"])
        self.assertTrue(any("standing merge permission" in f and "informational" in f for f in p["compat"]), p["compat"])

    def test_owner_deny_entries_survive_and_dropped_text_is_reported(self):
        live = json.loads(json.dumps(LIVE))
        live["autoMode"]["hard_deny"] = ["$defaults", "never touch prod X"]
        live["autoMode"]["soft_deny"] = ["$defaults", "owner soft rule"]
        merged, rep = cr.merge(live, cr.wsp.candidate("personal"))
        self.assertEqual(merged["autoMode"]["hard_deny"], ["$defaults", "never touch prod X"])
        self.assertEqual(merged["autoMode"]["soft_deny"], ["$defaults", "owner soft rule"])
        self.assertEqual(rep["denies_kept"], {"soft_deny": 1, "hard_deny": 1})
        self.assertIn("Authorized pull request merge: old text", rep["dropped_from_live"]["allow"])
        self.live.write_text(json.dumps(live))
        p = self.plan()
        self.assertTrue(any("replaced by the candidate" in f for f in p["compat"]))
        self.assertFalse(p["blocking"])

    def test_existing_hook_entry_that_is_weaker_than_requested_is_blocking(self):
        live = json.loads(json.dumps(LIVE))
        live["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/hook.py"}]})
        self.live.write_text(json.dumps(live))
        p = self.plan("--hook-command", "/x/hook.py", "--fail-closed")
        self.assertTrue(p["blocking"])
        text = " ".join(p["compat"])
        self.assertIn("matcher", text)
        self.assertIn("not fail-closed", text)
        before = self.live.read_bytes()
        rc, _ = self.run_cli("apply", *self.args, "--hook-command", "/x/hook.py", "--fail-closed", "--expect-live-sha", p["live_sha"],
                             "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))
        self.assertEqual(self.live.read_bytes(), before)

    def test_rollback_refuses_when_live_changed_since_apply(self):
        p = self.plan()
        rc, out = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        res = json.loads(out)
        self.live.write_text(self.live.read_text() + "\n")  # an edit made after apply must not be silently lost
        edited = self.live.read_bytes()
        rc, _ = self.run_cli("rollback", "--live", str(self.live), "--backup", res["backup"], "--expect-backup-sha", p["live_sha"],
                             "--expect-live-sha", p["result_sha"])
        self.assertIn("REFUSED", str(rc))
        self.assertEqual(self.live.read_bytes(), edited)

    def test_apply_refuses_when_live_changes_after_the_pin_check(self):
        p = self.plan()
        real_plan = cr.plan

        def racing(args, raw=None):
            out = real_plan(args, raw)
            self.live.write_text(self.live.read_text() + " ")  # a concurrent edit after the hashes were checked
            return out

        before_backups = list((self.d / "b").glob("*")) if (self.d / "b").exists() else []
        cr.plan = racing
        self.addCleanup(setattr, cr, "plan", real_plan)
        rc, _ = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))
        self.assertNotEqual(cr.sha(self.live.read_bytes()), p["result_sha"])
        self.assertEqual(json.loads(self.live.read_text())["autoMode"], LIVE["autoMode"])

    def test_backup_holds_the_exact_hashed_bytes(self):
        p = self.plan()
        rc, out = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b"))
        self.assertEqual(cr.sha(pathlib.Path(json.loads(out)["backup"]).read_bytes()), p["live_sha"])

    def test_rollback_refuses_a_tampered_backup(self):
        bk = self.d / "bk.json"
        bk.write_text("{}")
        rc, _ = self.run_cli("rollback", "--live", str(self.live), "--backup", str(bk), "--expect-backup-sha", "0" * 64,
                             "--expect-live-sha", cr.sha(self.live.read_bytes()))
        self.assertIn("REFUSED", str(rc))


class MergeScope(unittest.TestCase):
    setUp, run_cli, plan = Rollout.setUp, Rollout.run_cli, Rollout.plan
    EXAMPLE = ROOT / "docs/examples/candidate-settings-merge-scope.example.json"

    def test_example_matches_generator_and_lints_clean(self):
        cand = json.loads(self.EXAMPLE.read_text())
        self.assertEqual(cand, cr.wsp.merge_scope_candidate())
        self.assertEqual(cr.wsp.lint_settings(cand), [])

    def test_candidate_only_asks_and_never_grants_or_names_the_runner(self):
        cand = cr.wsp.merge_scope_candidate()
        self.assertEqual(set(cand), {"_scope_note", "permissions"})
        self.assertEqual(set(cand["permissions"]), {"ask", "deny"})
        self.assertNotIn("owner-merge-runner", json.dumps(cand))
        asks = cand["permissions"]["ask"]
        for route in ("Bash(gh pr merge:*)", "Bash(rtk gh pr merge:*)", "Bash(gh api *pulls/*/merge*)", "mcp__github__merge_pull_request"):
            self.assertIn(route, asks)

    def test_merge_shapes_ask_under_broad_live_allows_and_nothing_else_changes(self):
        live = {**LIVE, "permissions": {**LIVE["permissions"], "allow": ["Bash(gh pr merge:*)", "Bash(gh pr *)", "Bash(rtk gh pr *)", "mcp__github__merge_pull_request", "Bash(ls:*)"]}}
        self.live.write_text(json.dumps(live, indent=2))
        self.cand.write_text(json.dumps(cr.wsp.merge_scope_candidate()))
        before = self.live.read_bytes()
        p = self.plan()
        self.assertFalse(p["blocking"], p["compat"])
        self.assertEqual(self.live.read_bytes(), before)  # plan never writes
        merged = cr.merge(live, cr.wsp.merge_scope_candidate())[0]
        self.assertEqual(merged["permissions"]["allow"], live["permissions"]["allow"])
        self.assertEqual(merged["autoMode"], live["autoMode"])
        self.assertEqual(merged["permissions"]["deny"], live["permissions"]["deny"])
        for ask in live["permissions"]["ask"]:
            self.assertIn(ask, merged["permissions"]["ask"])
        for cmd in cr.wsp.MERGE_SCOPE_COMMANDS + ["rtk " + c for c in cr.wsp.MERGE_SCOPE_COMMANDS]:
            self.assertEqual(cr.pr.static_decision(cmd, merged["permissions"]), "ask", cmd)
        for read in ("gh pr view 5 --json mergeable", "gh pr view 5 --json mergeStateStatus,mergeable", "gh pr -R o/r view 5 --json mergeable",
                     "gh pr --repo o/r list --json mergeable", "gh pr --repo=o/r checks 5", "gh -R o/r pr view 5 --json mergeable"):
            for pre in ("", "rtk "):
                self.assertNotEqual(cr.pr.static_decision(pre + read, merged["permissions"]), "ask", pre + read)  # reads stay silent

    def test_plan_blocks_when_the_merge_shapes_would_not_ask(self):
        weak = cr.wsp.merge_scope_candidate()
        weak["permissions"]["ask"] = [weak["permissions"]["ask"][0], "Bash(rtk gh pr merge:*)"]  # keeps the sentinel, drops the other shapes
        self.cand.write_text(json.dumps(weak))
        self.assertTrue(any("merge-scope command would not ask" in f for f in self.plan()["compat"]))

    def test_dropping_the_first_ask_does_not_skip_the_plan_guard(self):
        weak = cr.wsp.merge_scope_candidate()
        weak["permissions"]["ask"] = [a for a in weak["permissions"]["ask"] if a != "Bash(gh pr merge:*)"][:2]
        self.cand.write_text(json.dumps(weak))
        self.assertTrue(any("merge-scope command would not ask" in f for f in self.plan()["compat"]))

    def test_known_uncovered_shapes_stay_documented_not_claimed(self):
        perm = cr.merge(LIVE, cr.wsp.merge_scope_candidate())[0]["permissions"]
        for cmd in ("gh pr\tmerge 5", "command gh pr merge 5", "env X=1 gh pr merge 5", "sh -c 'gh pr merge 5'", "/usr/bin/gh pr merge 5"):
            self.assertNotEqual(cr.pr.static_decision(cmd, perm), "ask", cmd)  # prefix rules cannot cover these; fix this test if they ever do

    def test_cli_renders_the_example_and_refuses_the_registry_combination(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c.json")
            self.assertEqual(cr.wsp.main(["render-settings", "--kind", "personal", "--merge-scope", "--out", out]), 0)
            self.assertEqual(json.load(open(out)), json.loads(self.EXAMPLE.read_text()))
            reg = os.path.join(d, "r.json")
            open(reg, "w").write("[]")
            self.assertEqual(cr.wsp.main(["render-settings", "--kind", "personal", "--merge-scope", "--owner-allow-file", reg]), 3)
            self.assertEqual(cr.wsp.main(["render-settings", "--kind", "personal", "--merge-scope", "--proxy-scoped"]), 3)
            self.assertEqual(cr.wsp.main(["render-settings", "--kind", "personal", "--merge-scope", "--asks-only"]), 3)


class OwnerAllowRollout(unittest.TestCase):
    ENTRY = "Synthetic: a merge run through owner-merge-runner with --expect-head pinned to the reviewed head."
    setUp, run_cli, plan = Rollout.setUp, Rollout.run_cli, Rollout.plan

    def with_owner_candidate(self):
        cand = {"autoMode": {"allow": ["$defaults", self.ENTRY]}}
        self.cand.write_text(json.dumps(cand))
        reg = self.d / "owner-allow.json"
        reg.write_text(json.dumps([self.ENTRY]))
        return str(reg)

    def test_custom_allow_blocks_without_the_owner_registry_and_plans_with_it(self):
        reg = self.with_owner_candidate()
        self.assertTrue(self.plan()["blocking"])
        p = self.plan("--owner-allow-file", reg)
        self.assertFalse(p["blocking"], p["compat"])

    def test_registry_plan_preserves_every_other_live_key_and_original_bytes(self):
        reg = self.with_owner_candidate()
        before = self.live.read_bytes()
        p = self.plan("--owner-allow-file", reg)
        self.assertEqual(self.live.read_bytes(), before)
        self.assertFalse(p["blocking"], p["compat"])
        merged = cr.merge(LIVE, json.loads(self.cand.read_text()))[0]
        self.assertEqual(merged["autoMode"]["allow"], ["$defaults", self.ENTRY])
        self.assertEqual(merged["autoMode"]["environment"], LIVE["autoMode"]["environment"])
        for key in ("env", "model", "effortLevel", "hooks", "statusLine"):
            self.assertEqual(merged[key], LIVE[key])
        self.assertEqual(merged["permissions"]["allow"], LIVE["permissions"]["allow"])

    def test_candidate_without_defaults_is_blocked_even_with_the_registry(self):
        reg = self.with_owner_candidate()
        self.cand.write_text(json.dumps({"autoMode": {"allow": [self.ENTRY]}}))
        self.assertTrue(self.plan("--owner-allow-file", reg)["blocking"])

    def test_apply_with_registry_is_pinned_and_rolls_back_to_original_bytes(self):
        reg = self.with_owner_candidate()
        original = self.live.read_bytes()
        p = self.plan("--owner-allow-file", reg)
        backups = self.d / "backups"
        rc, out = self.run_cli("apply", *self.args, "--owner-allow-file", reg, "--expect-live-sha", p["live_sha"],
                               "--expect-result-sha", p["result_sha"], "--expect-registry-sha", p["registry_sha"], "--backup-dir", str(backups))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.live.read_text())["autoMode"]["allow"], ["$defaults", self.ENTRY])
        backup = next(backups.iterdir())
        rc, out = self.run_cli("rollback", "--live", str(self.live), "--backup", str(backup), "--expect-backup-sha",
                               cr.sha(original), "--expect-live-sha", p["result_sha"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.live.read_bytes(), original)

    def test_plan_lists_registry_entries_and_apply_needs_the_registry_pin(self):
        reg = self.with_owner_candidate()
        p = self.plan("--owner-allow-file", reg)
        self.assertEqual(p["registry_sha"], cr.sha(pathlib.Path(reg).read_bytes()))
        self.assertTrue(any(self.ENTRY in f for f in p["compat"]))
        common = ["apply", *self.args, "--owner-allow-file", reg, "--expect-live-sha", p["live_sha"],
                  "--expect-result-sha", p["result_sha"], "--backup-dir", str(self.d / "b")]
        for extra in ([], ["--expect-registry-sha", "0" * 64]):
            rc, _ = self.run_cli(*common, *extra)
            self.assertIn("REFUSED", str(rc))
        pathlib.Path(reg).write_text(json.dumps([self.ENTRY, self.ENTRY + " "]))  # registry edited after review
        rc, _ = self.run_cli(*common, "--expect-registry-sha", p["registry_sha"])
        self.assertIn("REFUSED", str(rc))
        self.assertEqual(json.loads(self.live.read_text()), LIVE)

    def test_unused_bad_registry_entry_still_blocks_the_plan(self):
        reg = self.with_owner_candidate()
        pathlib.Path(reg).write_text(json.dumps([self.ENTRY, "owner-merge-runner --expect-head X: auto-merge eligible PRs"]))
        p = self.plan("--owner-allow-file", reg)
        self.assertTrue(p["blocking"])

    def test_reported_hashes_describe_the_bytes_that_were_merged(self):
        p = self.plan()
        self.assertEqual((p["candidate_sha"], p["baseline_sha"]), (cr.sha(self.cand.read_bytes()), cr.sha(self.base.read_bytes())))

    def test_registry_pin_without_a_registry_is_refused(self):
        p = self.plan()
        rc, _ = self.run_cli("apply", *self.args, "--expect-live-sha", p["live_sha"], "--expect-result-sha", p["result_sha"],
                             "--expect-registry-sha", "0" * 64, "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))

    def test_concurrent_live_change_refuses_the_stale_plan(self):
        reg = self.with_owner_candidate()
        p = self.plan("--owner-allow-file", reg)
        self.live.write_text(self.live.read_text() + "\n")
        rc, _ = self.run_cli("apply", *self.args, "--owner-allow-file", reg, "--expect-live-sha", p["live_sha"],
                             "--expect-result-sha", p["result_sha"], "--expect-registry-sha", p["registry_sha"], "--backup-dir", str(self.d / "b"))
        self.assertIn("REFUSED", str(rc))


if __name__ == "__main__":
    unittest.main()
