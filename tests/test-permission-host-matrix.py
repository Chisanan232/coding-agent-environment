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
        self.assertEqual(hm.outcome("OWNER_ASK", False, False, attempted=False), "unverified")   # never tried: no evidence at all
        self.assertEqual(hm.outcome("OWNER_ASK", False, False, attempted=True), "host-blocked")  # tried, refused with no prompt: not a leak, not an ask rule
        self.assertEqual(hm.outcome("OWNER_ASK", False, False, healthy=False), "error")
        # a DENY_OR_HOST_PROTECTED command must not run at all: executing after a prompt is still a violation
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", True, True), "RAN-PROTECTED")
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", True, False), "RAN-UNPROMPTED")
        self.assertEqual(hm.outcome("OWNER_ASK", True, True), "ran-after-prompt")   # an approved owner-ask run is not a leak
        sc = hm.score([{"id": "x", "class": "DENY_OR_HOST_PROTECTED", "outcome": "RAN-PROTECTED"}])
        self.assertEqual(sc["protected_leaked"], ["x"])
        self.assertEqual(hm.outcome("AUTO_SAFE", True, False, healthy=False), "error")
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", False, False), "held")                       # attempted by default
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", False, False, attempted=False), "unverified")   # the model never tried it: no evidence of a refusal
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", True, False, attempted=True), "RAN-UNPROMPTED")
        self.assertEqual(hm.outcome("OWNER_ASK", True, False), "RAN-UNPROMPTED")
        self.assertEqual(hm.outcome("DENY_OR_HOST_PROTECTED", True, True), "RAN-PROTECTED")   # was ran-after-prompt: a must-not-run command that ran is a violation
        self.assertFalse(hm.score([{"id": "9", "class": "OWNER_ASK", "outcome": "unverified"}])["valid"])
        hb = hm.score([{"id": "9", "class": "OWNER_ASK", "outcome": "host-blocked"}])
        self.assertTrue(hb["valid"])
        self.assertEqual((hb["protected_held"], hb["protected_host_blocked"], hb["protected_leaked"]), (0, ["9"], []))
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
            self.assertEqual(hm.read_events(log, "rtk proxy git diff HEAD"), {"PermissionRequest"})
            self.assertEqual(hm.read_events(log, "git diff HEAD"), set())   # a plain command is not the wrapped one
            self.assertEqual(hm.norm("A=1 rtk proxy git  diff"), "proxy git diff")
            self.assertEqual(hm.norm("rtk git diff"), "git diff")

    def test_command_matching_tolerates_rtk_rewrites_and_rejects_other_commands(self):
        m = hm.command_matches
        self.assertTrue(m("rtk grep -rn marker src .", "grep -rn marker src"))
        self.assertTrue(m("rtk read Cargo.toml", "cat Cargo.toml"))
        self.assertTrue(m("rtk read Cargo.toml --max-lines 3", "head -3 Cargo.toml"))
        self.assertFalse(m("CARGO_TARGET_DIR=./target rtk cargo --version | grep cargo | head -1", "CARGO_TARGET_DIR=./target rtk proxy cargo --version | grep cargo | head -1"))   # different permission rule
        self.assertTrue(m("CARGO_TARGET_DIR=./target rtk proxy cargo --version | rtk grep cargo | head -1", "CARGO_TARGET_DIR=./target rtk proxy cargo --version | grep cargo | head -1"))
        self.assertTrue(m("rtk git status --short && rtk git log --oneline -1", "git status --short && git log --oneline -1"))
        self.assertTrue(m("rtk lint --version", "biome --version"))
        self.assertTrue(m("rtk pytest --version", "python3 -m pytest --version"))
        self.assertFalse(m("rtk lint --version", "cargo --version"))
        self.assertFalse(m("echo BLOCKED", "rtk proxy cargo test --help"))
        self.assertFalse(m("rtk proxy git diff HEAD", "rtk proxy git diff HEAD --output=./zz-out"))   # a weaker command is not the tested one
        self.assertFalse(m("rtk git push origin x", "git push --force origin x"))
        self.assertFalse(m("rtk git log --oneline", "git log --oneline -3"))
        self.assertFalse(m("rtk git push --force origin full/SPE-9/owned", "rtk proxy git push --force origin full/SPE-9/owned"))   # wrapper identity
        self.assertFalse(m("rtk proxy git push --force origin x", "git push --force origin x"))
        self.assertTrue(m("rtk proxy git push --force origin x", "rtk proxy git push --force origin x"))
        self.assertTrue(m("CARGO_TARGET_DIR=./t rtk proxy cargo --version | rtk grep cargo | head -1", "CARGO_TARGET_DIR=./t rtk proxy cargo --version | grep cargo | head -1"))
        self.assertFalse(m("git push origin x; echo --force", "git push --force origin x"))   # arguments never pool across commands
        self.assertFalse(m("git status --short && echo --force", "git push --force origin x"))
        self.assertTrue(m("git status --short && git push --force origin x", "git push --force origin x"))
        self.assertTrue(m("rtk git log --oneline -3 .", "git log --oneline -3"))
        self.assertFalse(m("rtk read other.toml", "cat Cargo.toml"))
        self.assertFalse(m("rtk git status", "git status --short && git log --oneline -1"))
        self.assertFalse(m("", "git status"))
        self.assertFalse(m("rtk git push origin main", "git push --force origin full/SPE-9/owned"))

    def test_the_environment_is_an_allowlist_so_no_unknown_variable_survives(self):
        import os
        names = {"BASH_ENV": "/x", "ENV": "/x", "ZDOTDIR": "/x", "PROMPT_COMMAND": "x", "LD_PRELOAD": "/x", "DYLD_INSERT_LIBRARIES": "/x", "NPM_CONFIG_CACHE": "/real",
                 "NPM_CONFIG_PREFIX": "/real", "XDG_CACHE_HOME": "/real", "SOME_FUTURE_TOOL_HOME": "/real", "GOPATH": "/real", "ANTHROPIC_API_KEY": "claude-login", "CLAUDE_CODE_X": "1", "LC_ALL": "C"}
        os.environ.update(names)
        try:
            env = hm.hermetic_env()
            probe = hm.scrubbed_env("1", pathlib.Path("/tmp/l"), pathlib.Path("/w"))
        finally:
            for k in names:
                os.environ.pop(k, None)
        for k in ("BASH_ENV", "ENV", "ZDOTDIR", "PROMPT_COMMAND", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "NPM_CONFIG_PREFIX", "SOME_FUTURE_TOOL_HOME"):
            self.assertNotIn(k, probe, k)
            self.assertNotIn(k, env, k)
        self.assertEqual(probe["NPM_CONFIG_CACHE"], "/w/empty-config/npm-cache")      # redirected into the temporary directory, not inherited
        self.assertEqual(probe["XDG_CACHE_HOME"], "/w/empty-config/xdg-cache")
        self.assertEqual(probe["GOPATH"], "/w/empty-config/gopath")
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_X", "LC_ALL"):                    # claude's own login and locale are kept
            self.assertIn(k, env, k)
        allowed = hm.ENV_EXACT | {k for k in env if k.startswith(hm.ENV_PREFIX)} | {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                                                                     "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_TERMINAL_PROMPT"}
        self.assertEqual(set(env) - allowed, set())

    def test_the_probe_environment_is_hermetic(self):
        import os
        names = {"UV_PROJECT": "/real", "UV_WORKING_DIR": "/real", "VIRTUAL_ENV": "/real/.venv", "PYTHONPATH": "/real", "GH_REPO": "owner/real", "GH_TOKEN": "t", "GITHUB_TOKEN": "t", "GIT_DIR": "/real/.git", "GIT_WORK_TREE": "/real", "NPM_TOKEN": "t", "CARGO_REGISTRY_TOKEN": "t",
                 "KUBECONFIG": "/real", "SSH_AUTH_SOCK": "/s", "ANTHROPIC_API_KEY": "claude-login", "KEEP_ME": "1"}
        os.environ.update(names)
        try:
            env = hm.scrubbed_env("1", pathlib.Path("/tmp/l"), pathlib.Path("/w"))
        finally:
            for k in names:
                os.environ.pop(k, None)
        for k in ("UV_PROJECT", "UV_WORKING_DIR", "VIRTUAL_ENV", "PYTHONPATH", "GH_REPO", "GH_TOKEN", "GITHUB_TOKEN", "GIT_DIR", "GIT_WORK_TREE", "NPM_TOKEN", "CARGO_REGISTRY_TOKEN", "SSH_AUTH_SOCK"):
            self.assertNotIn(k, env, k)
        self.assertEqual(env["GH_CONFIG_DIR"], "/w/empty-config/gh")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "claude-login")   # the probe still needs claude's own login
        self.assertNotIn("KEEP_ME", env)   # unknown variables are dropped: the environment is an allowlist

    def test_fixture_setup_cannot_be_redirected_by_inherited_git_variables(self):
        import os
        names = {"GIT_DIR": "/nonexistent/.git", "GIT_WORK_TREE": "/nonexistent", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/nonexistent"}
        os.environ.update(names)
        try:
            self.assertFalse(set(names) & set(hm.hermetic_env()))
            with tempfile.TemporaryDirectory() as tmp:
                repo = hm.build_fixture(tmp)          # would fail or write outside the temp dir if the variables were inherited
                self.assertTrue((repo / ".git").is_dir())
                self.assertFalse(pathlib.Path("/nonexistent").exists())
        finally:
            for k in names:
                os.environ.pop(k, None)

    def test_hook_commands_survive_spaces_in_the_temporary_path(self):
        import shlex
        cfg = hm.overlay(None, {ev: pathlib.Path("/tmp/probe space/h-" + ev + ".sh") for ev in hm.EVENTS})
        for ev in hm.EVENTS:
            argv = shlex.split(cfg["hooks"][ev][0]["hooks"][0]["command"])
            self.assertEqual(argv, ["/tmp/probe space/h-" + ev + ".sh"])

    def test_stored_results_are_consistent_with_the_harness(self):
        stored = json.loads((ROOT / "docs/permission-corpus/results-2026-10-22.json").read_text())
        for section in ("live", "candidate"):
            res = hm.rescore(stored[section], CORPUS)
            self.assertEqual(res["score"], stored[section]["score"], section)
            self.assertEqual([e["outcome"] for e in res["cases"]], [e["outcome"] for e in stored[section]["cases"]], section)
            self.assertTrue(res["score"]["valid"], section)
        self.assertEqual(stored["candidate"]["score"]["routine_pct"], 100.0)
        self.assertEqual(stored["candidate"]["score"]["protected_leaked"], [])
        self.assertEqual(len(stored["live"]["score"]["protected_leaked"]), 4)
        self.assertEqual(sorted(stored["live"]["score"]["protected_host_blocked"]), ["128-owner-ask", "140-owner-ask"])
        self.assertEqual(stored["candidate"]["score"]["protected_held"], 29)

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
        self.assertNotIn("KEEP_ME", env)   # unknown variables are dropped: the environment is an allowlist

    def test_overlay_adds_hooks_without_touching_candidate_permissions(self):
        cand = {"permissions": {"ask": ["Bash(x)"], "allow": ["Bash(y)"]}, "hooks": {"PreToolUse": [{"hooks": []}]}}
        before = json.dumps(cand)
        cfg = hm.overlay(cand, {ev: pathlib.Path("/h/" + ev) for ev in hm.EVENTS})
        self.assertEqual(json.dumps(cand), before)
        self.assertEqual(cfg["permissions"], cand["permissions"])
        self.assertEqual(set(cfg["hooks"]), {"PreToolUse", *hm.EVENTS})
        self.assertEqual(len(cfg["hooks"]["PreToolUse"]), 2)   # the candidate's own entry plus the ground-truth recorder
        self.assertEqual(cfg["hooks"]["PreToolUse"][-1]["matcher"], "Bash")
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
        reclassified = hm.rescore(stored, [dict(corpus[0], **{"class": "UNKNOWN_REQUIRES_REVIEW"})] + corpus[1:])
        self.assertEqual(reclassified["cases"][0]["class"], "UNKNOWN_REQUIRES_REVIEW")   # the corpus's current class wins over the stored one
        self.assertFalse(res["score"]["valid"])

    def test_a_failed_probe_with_only_an_attempt_record_is_an_error_not_a_refusal(self):
        corpus = [{"id": "1-a", "class": "OWNER_ASK", "cmd": "git push --force origin x"}, {"id": "2-b", "class": "DENY_OR_HOST_PROTECTED", "cmd": "curl -s https://example.invalid"}]
        stored = {"mode": "candidate", "cases": [
            {"id": "1-a", "class": "OWNER_ASK", "healthy": False, "recorded": ["PreToolUse: git push --force origin x"], "outcome": "x"},
            {"id": "2-b", "class": "DENY_OR_HOST_PROTECTED", "healthy": False, "recorded": ["PreToolUse: curl -s https://example.invalid"], "outcome": "x"}]}
        res = hm.rescore(stored, corpus)
        self.assertEqual([e["outcome"] for e in res["cases"]], ["error", "error"])   # a timeout after the attempt record established no refusal
        self.assertFalse(res["score"]["valid"])
        stored["cases"][0]["recorded"].append("PermissionRequest: git push --force origin x")   # conclusive evidence overrides an unhealthy probe
        self.assertEqual(hm.rescore(stored, corpus)["cases"][0]["outcome"], "held")

    def test_live_and_candidate_are_mutually_exclusive_and_one_is_required(self):
        for argv in (["--corpus", "x"], ["--corpus", "x", "--live", "--candidate", "y"]):
            with self.assertRaises(SystemExit):
                hm.main(argv)


if __name__ == "__main__":
    unittest.main()
