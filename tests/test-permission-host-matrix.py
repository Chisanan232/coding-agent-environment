#!/usr/bin/env python3
"""SPE-119: corpus hygiene, static expectations and the pure parts of scripts/permission-host-matrix.py. Never starts claude."""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hm = load("hm", "scripts/permission-host-matrix.py")
cr = load("cr", "scripts/candidate-rollout.py")
wsp, pr = cr.wsp, cr.pr
DOC = json.loads((ROOT / "docs/permission-corpus/routine-command-corpus.json").read_text())
CORPUS = DOC["commands"]
CLASSES = {"AUTO_SAFE", "AUTO_WITH_RESOURCE_ADMISSION", "OWNER_ASK", "DENY_OR_HOST_PROTECTED", "UNKNOWN_REQUIRES_REVIEW"}
FAMILIES = {"inspect", "git-ro", "gh-ro", "dev", "source-control", "wrapper", "owner-ask", "protected"}
ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+ )+")


def candidate_permissions():
    """What the owner's file would hold after the canary asks and the SPE-119 candidate, starting from the baseline asks."""
    baseline = json.loads((ROOT / ".claude/settings.json").read_text())["permissions"]
    live = {"permissions": {"ask": list(baseline["ask"]), "allow": []}}  # the repository baseline stands in for the owner's existing asks
    merged, _ = cr.merge(live, wsp.candidate("personal", asks_only=True))
    merged, _ = cr.merge(merged, wsp.proxy_scoped_candidate())
    return merged["permissions"]


def simple(cmd):
    """The single command the matcher sees, or None for compound shapes the static emulation cannot judge."""
    cmd = ENV_PREFIX.sub("", cmd)
    return None if re.search(r"[;&|<>$`(]", cmd) else cmd


class CorpusHygiene(unittest.TestCase):
    def test_schema_ids_and_classes(self):
        ids = [c["id"] for c in CORPUS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(CORPUS), 150)
        for c in CORPUS:
            self.assertIn(c["class"], CLASSES, c["id"])
            self.assertEqual(c["expect"], {"AUTO_SAFE": "allow", "AUTO_WITH_RESOURCE_ADMISSION": "allow", "OWNER_ASK": "ask",
                                           "DENY_OR_HOST_PROTECTED": "refuse", "UNKNOWN_REQUIRES_REVIEW": "measure"}[c["class"]], c["id"])

    def test_every_founder_listed_family_is_covered(self):
        self.assertEqual({c["family"] for c in CORPUS}, FAMILIES)
        heads = " ".join(c["cmd"] for c in CORPUS)
        for needle in ("cat ", "sed -n", "rg ", "jq ", "git blame", "git ls-tree", "git cat-file", "gh pr view", "gh api graphql", "cargo clippy", "pytest", "go vet",
                       "xcodebuild", "lefthook", "rtk proxy", "CARGO_TARGET_DIR=", "git worktree add", "git push origin HEAD", "| head"):
            self.assertIn(needle, heads, needle)

    def test_no_real_paths_hosts_or_secrets_and_only_fixture_writes(self):
        blob = json.dumps(DOC)
        for bad in ("/Users/", "/home/", "bulls23", "ghp_", "AKIA", "BEGIN PRIVATE", "Bearer "):
            self.assertNotIn(bad, blob, bad)
        for c in CORPUS:
            self.assertNotRegex(c["cmd"], r">\s*/(?!dev/null)", c["id"])
            self.assertNotIn("--output=/", c["cmd"], c["id"])
            if "gcloud" in c["cmd"]:
                self.assertIn("zz-nonexistent-probe-project", c["cmd"], c["id"])
            if "--repo" in c["cmd"] or "repos/" in c["cmd"]:
                self.assertIn("zz-nonexistent-probe-repo", c["cmd"], c["id"])


class StaticExpectations(unittest.TestCase):
    PERM = candidate_permissions()

    def test_owner_ask_single_commands_hit_an_ask_rule(self):
        checked = 0
        for c in CORPUS:
            cmd = simple(c["cmd"])
            if c["class"] == "OWNER_ASK" and cmd:
                checked += 1
                self.assertEqual(pr.static_decision(cmd, self.PERM), "ask", c["id"] + " " + cmd)
        self.assertGreaterEqual(checked, 18)

    def test_host_protected_commands_are_never_statically_allowed(self):
        # outbound-data and IAM shapes have no allow rule and are left to the host classifier, which was measured to hold them
        for c in CORPUS:
            cmd = simple(c["cmd"])
            if c["class"] == "DENY_OR_HOST_PROTECTED" and cmd:
                self.assertNotEqual(pr.static_decision(cmd, self.PERM), "allow", c["id"] + " " + cmd)

    def test_routine_single_commands_never_hit_an_ask(self):
        checked = 0
        for c in CORPUS:
            cmd = simple(c["cmd"])
            if c["class"] in ("AUTO_SAFE", "AUTO_WITH_RESOURCE_ADMISSION") and cmd:
                checked += 1
                self.assertNotEqual(pr.static_decision(cmd, self.PERM), "ask", c["id"] + " " + cmd)
        self.assertGreaterEqual(checked, 90)

    def test_no_broad_wrapper_or_blanket_allow_is_introduced(self):
        for r in self.PERM["allow"]:
            self.assertNotIn(r, ("Bash(*)", "Bash", "Bash(rtk proxy *)", "Bash(python3 *)"), r)


class HarnessPureParts(unittest.TestCase):
    def test_outcome_and_score(self):
        self.assertEqual(hm.outcome("AUTO_SAFE", True, False), "ran-no-prompt")
        self.assertEqual(hm.outcome("AUTO_SAFE", False, True), "prompted")
        self.assertEqual(hm.outcome("AUTO_SAFE", False, False), "not-run")
        self.assertEqual(hm.outcome("OWNER_ASK", False, True), "held")
        self.assertEqual(hm.outcome("OWNER_ASK", False, False), "unverified")        # no prompt, no run: not evidence the ask rule worked
        self.assertEqual(hm.outcome("OWNER_ASK", False, False, healthy=False), "error")
        self.assertEqual(hm.outcome("AUTO_SAFE", True, False, healthy=False), "error")
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", False, False), "held")
        self.assertEqual(hm.outcome("OWNER_ASK", True, False), "RAN-UNPROMPTED")
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", True, True), "ran-after-prompt")
        self.assertFalse(hm.score([{"id": "9", "class": "OWNER_ASK", "outcome": "unverified"}])["valid"])
        self.assertFalse(hm.score([{"id": "9", "class": "AUTO_SAFE", "outcome": "error"}])["valid"])
        entries = [{"id": "1", "class": "AUTO_SAFE", "outcome": "ran-no-prompt"}, {"id": "2", "class": "AUTO_SAFE", "outcome": "prompted"},
                   {"id": "3", "class": "OWNER_ASK", "outcome": "held"}, {"id": "4", "class": "OWNER_ASK", "outcome": "RAN-UNPROMPTED"}]
        s = hm.score(entries)
        self.assertEqual((s["routine_total"], s["routine_ran_no_prompt"], s["routine_prompted"], s["routine_pct"]), (2, 1, 1, 50.0))
        self.assertEqual((s["protected_total"], s["protected_held"], s["protected_leaked"]), (2, 1, ["4"]))

    def test_events_for_a_different_command_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "l.jsonl"
            log.write_text(json.dumps({"label": "1", "event": "PostToolUse", "cmd": "echo BLOCKED"}) + "\n"
                           + json.dumps({"label": "1", "event": "PermissionRequest", "cmd": "rtk proxy git diff HEAD"}) + "\n" + "garbage\n")
            self.assertEqual(hm.read_events(log, "rtk proxy cargo test --help"), set())
            self.assertEqual(hm.read_events(log, "git diff HEAD"), {"PermissionRequest"})   # rtk rewrite is normalised away
            self.assertEqual(hm.norm("A=1 rtk proxy git  diff"), "git diff")

    def test_command_matching_tolerates_rtk_rewrites_and_rejects_other_commands(self):
        m = hm.command_matches
        self.assertTrue(m("rtk grep -rn marker src .", "grep -rn marker src"))
        self.assertTrue(m("rtk read Cargo.toml", "cat Cargo.toml"))
        self.assertTrue(m("rtk read Cargo.toml --max-lines 3", "head -3 Cargo.toml"))
        self.assertTrue(m("CARGO_TARGET_DIR=./target rtk cargo --version | grep cargo | head -1", "CARGO_TARGET_DIR=./target rtk proxy cargo --version | grep cargo | head -1"))
        self.assertTrue(m("rtk git status --short && rtk git log --oneline -1", "git status --short && git log --oneline -1"))
        self.assertTrue(m("rtk lint --version", "biome --version"))
        self.assertTrue(m("rtk pytest --version", "python3 -m pytest --version"))
        self.assertFalse(m("rtk lint --version", "cargo --version"))
        self.assertFalse(m("echo BLOCKED", "rtk proxy cargo test --help"))
        self.assertFalse(m("rtk read other.toml", "cat Cargo.toml"))
        self.assertFalse(m("rtk git status", "git status --short && git log --oneline -1"))
        self.assertFalse(m("", "git status"))
        self.assertFalse(m("rtk git push origin main", "git push --force origin full/SPE-9/owned"))

    def test_cloud_credentials_never_reach_the_probe_environment(self):
        import os
        os.environ.update({"AWS_SECRET_ACCESS_KEY": "x", "GOOGLE_APPLICATION_CREDENTIALS": "x", "CLOUDSDK_CORE_PROJECT": "real", "KEEP_ME": "1"})
        try:
            env = hm.scrubbed_env("1", pathlib.Path("/tmp/l"), pathlib.Path("/w"))
        finally:
            for k in ("AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CORE_PROJECT", "KEEP_ME"):
                os.environ.pop(k, None)
        for k in ("AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
            self.assertNotIn(k, env)
        self.assertEqual(env["CLOUDSDK_CONFIG"], "/w/no-gcloud")
        self.assertEqual(env["KEEP_ME"], "1")

    def test_overlay_adds_hooks_without_touching_candidate_permissions(self):
        cand = {"permissions": {"ask": ["Bash(x)"], "allow": ["Bash(y)"]}, "hooks": {"PreToolUse": [{"hooks": []}]}}
        before = json.dumps(cand)
        cfg = hm.overlay(cand, {ev: pathlib.Path("/h/" + ev) for ev in hm.EVENTS})
        self.assertEqual(json.dumps(cand), before)
        self.assertEqual(cfg["permissions"], cand["permissions"])
        self.assertEqual(set(cfg["hooks"]), {"PreToolUse", *hm.EVENTS})
        self.assertNotIn("matcher", cfg["hooks"]["PermissionRequest"][0])
        self.assertEqual(cfg["hooks"]["PostToolUseFailure"][0]["matcher"], "Bash")

    def test_fixture_is_a_disposable_repo_with_a_local_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = hm.build_fixture(tmp)
            self.assertTrue((repo / "src/lib.rs").read_text().count("marker"))
            out = subprocess.run(["git", "remote", "-v"], cwd=repo, capture_output=True, text=True).stdout
            self.assertIn(str(pathlib.Path(tmp) / "remote.git"), out)

    def test_rescore_reevaluates_stored_evidence_offline(self):
        corpus = [{"id": "1-a", "class": "AUTO_SAFE", "cmd": "biome --version"}, {"id": "2-b", "class": "OWNER_ASK", "cmd": "git push --force origin x"},
                  {"id": "3-c", "class": "OWNER_ASK", "cmd": "git push --force origin y"}]
        stored = {"mode": "candidate", "cases": [
            {"id": "1-a", "class": "AUTO_SAFE", "healthy": True, "recorded": ["PostToolUseFailure: rtk lint --version"], "outcome": "not-run"},
            {"id": "2-b", "class": "OWNER_ASK", "healthy": True, "recorded": ["PermissionRequest: rtk git push --force origin x"], "outcome": "unverified"},
            {"id": "3-c", "class": "OWNER_ASK", "healthy": True, "recorded": ["PostToolUse: echo BLOCKED"], "outcome": "held"}]}
        res = hm.rescore(stored, corpus)
        self.assertEqual([e["outcome"] for e in res["cases"]], ["ran-no-prompt", "held", "unverified"])
        self.assertFalse(res["score"]["valid"])

    def test_live_and_candidate_are_mutually_exclusive_and_one_is_required(self):
        for argv in (["--corpus", "x"], ["--corpus", "x", "--live", "--candidate", "y"]):
            with self.assertRaises(SystemExit):
                hm.main(argv)


if __name__ == "__main__":
    unittest.main()
