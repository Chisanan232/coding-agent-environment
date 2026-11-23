#!/usr/bin/env python3
"""Tests for the SPE-182 successor train in scripts/release-line.py: successor ensure, guard, train integrate/converge.

Disposable bare origin + clone, a fake `gh` on PATH that keeps JSON state, performs real merges into the bare repo and logs every
call, and a `git` wrapper on PATH that logs every git call the tool makes. No network.
"""
import copy
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rl_base_tests", ROOT / "tests/test-release-line.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

REAL_GIT = shutil.which("git")
REPO = base.REPO
DATE = base.DATE
FAKE_GH = """#!{py}
import json, os, subprocess, sys
S = os.environ["FAKE_GH_STATE"]
st = json.load(open(S))
a = sys.argv[1:]
open(S + ".calls", "a").write(" ".join(a) + "\\n")
opt = lambda n: a[a.index(n) + 1] if n in a else None
if a[:2] == ["pr", "list"]:
    print(json.dumps([p for p in st["prs"] if opt("--head") in (None, p["headRefName"]) and opt("--base") in (None, p["baseRefName"])]))
elif a[:2] == ["pr", "create"]:
    n = len(st["prs"]) + 1
    url = "https://example.invalid/pull/%d" % n
    st["prs"].append({{"number": n, "state": "OPEN", "headRefName": opt("--head"), "baseRefName": opt("--base"), "url": url, "body": opt("--body")}})
    json.dump(st, open(S, "w"))
    print(url)
elif a[:2] == ["pr", "merge"]:
    pr = next(p for p in st["prs"] if p["number"] == int(a[2]))
    work = S + ".merge"
    env = dict(os.environ, GIT_AUTHOR_DATE="{date}", GIT_COMMITTER_DATE="{date}", GIT_AUTHOR_NAME="gate", GIT_AUTHOR_EMAIL="g@e.x",
               GIT_COMMITTER_NAME="gate", GIT_COMMITTER_EMAIL="g@e.x")
    g = lambda *x: subprocess.run(["{git}", *x], cwd=work, env=env, check=True, capture_output=True)
    subprocess.run(["{git}", "clone", "-q", st["bare"], work], check=True, capture_output=True, env=env)
    g("checkout", "-q", pr["baseRefName"])
    g("merge", "-q", "--no-ff", "-m", "Merge pull request #%d" % pr["number"], "origin/" + pr["headRefName"])
    g("push", "-q", "origin", pr["baseRefName"])
    subprocess.run(["rm", "-rf", work])
    pr["state"] = "MERGED"
    json.dump(st, open(S, "w"))
else:
    sys.exit(1)
"""
GIT_WRAPPER = """#!/bin/sh
printf '%s\\n' "$*" >> "$GIT_LOG"
exec {git} "$@"
"""


class TrainBase(base.Base):
    def setUp(self):
        super().setUp()
        (self.bin / "gh").write_text(FAKE_GH.format(py=sys.executable, git=REAL_GIT, date=DATE))
        (self.bin / "git").write_text(GIT_WRAPPER.format(git=REAL_GIT))
        (self.bin / "git").chmod(0o755)
        self.git_log = pathlib.Path(self.tmp.name) / "git.log"
        self.git_log.write_text("")
        self.env["GIT_LOG"] = str(self.git_log)
        self.genv = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "GIT_AUTHOR_DATE": DATE, "GIT_COMMITTER_DATE": DATE, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.x",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.x"}
        root = pathlib.Path(self.tmp.name)
        self.bare, self.work = root / "origin.git", root / "work"
        self.w("init", "-q", "--bare", "-b", "main", str(self.bare), cwd=root)
        self.w("clone", "-q", str(self.bare), str(self.work), cwd=root)
        self.w("checkout", "-q", "-B", "main")
        self.commit("main", "init.txt")
        self.set_gh(prs=[], bare=str(self.bare))
        self.branch("integration/v0.x", "main")
        self.commit("integration/v0.x", "feature-a.txt")
        self.tag("v0.3.0-rc.1", "integration/v0.x")

    # git helpers (plain env, not logged)
    def w(self, *args, cwd=None):
        p = subprocess.run([REAL_GIT, *args], cwd=cwd or self.work, env=self.genv, check=True, capture_output=True, text=True)
        return p.stdout.strip()

    def branch(self, name, start):
        self.w("checkout", "-q", "-b", name, start)
        self.w("push", "-q", "origin", name)

    def commit(self, branch, fname):
        self.w("fetch", "-q", "--tags", "origin")  # the gate and the tool move origin; always build on its tip
        if self.w("ls-remote", "--heads", "origin", branch):
            self.w("checkout", "-q", "-B", branch, "origin/" + branch)
        (self.work / fname).write_text(fname + "\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", fname)
        self.w("push", "-q", "origin", branch)
        return self.w("rev-parse", "HEAD")

    def tag(self, name, branch):
        self.w("tag", name, branch)
        self.w("push", "-q", "origin", name)

    def promote(self, version, rc):
        """Simulate promotion: merge the candidate into main and tag the final version on the merge commit."""
        self.w("checkout", "-q", "main")
        self.w("merge", "-q", "--no-ff", "-m", f"promote {version}", rc)
        self.w("tag", f"v{version}")
        self.w("push", "-q", "origin", "main", f"v{version}")

    def remote(self, ref):
        out = self.w("ls-remote", str(self.bare), ref)
        return out.split()[0] if out else None

    def heads(self, prefix=""):
        return sorted(x.split("\t")[1] for x in self.w("ls-remote", "--heads", str(self.bare)).splitlines() if x.split("\t")[1].startswith("refs/heads/" + prefix))

    def calls(self):
        p = pathlib.Path(str(self.gh_state) + ".calls")
        return p.read_text().splitlines() if p.exists() else []

    def gate_merge(self, number):
        subprocess.run([str(self.bin / "gh"), "pr", "merge", str(number), "--merge"], env=self.env, check=True, capture_output=True)

    # registry helpers
    def v0_only_policy(self, **line_updates):
        p = base.example()
        e = base.entry(p)
        e["branches"] = [e["branches"][0]]
        e["future_head_prefixes"] = ["promote/"]
        e["release_lines"]["lines"] = [e["release_lines"]["lines"][0]]
        e["release_lines"]["lines"][0].update(line_updates)
        return p

    def install_packet(self, out):
        cand = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]["candidate_path"]
        self.install(json.loads(pathlib.Path(cand).read_text()))

    def ensure(self, pred, *extra, execute=True):
        argv = ["successor", "ensure", "--predecessor", pred, "--clone", str(self.work), *extra]
        return self.cli(*argv, *(["--execute"] if execute else []))

    def run_train(self, kind, *extra, execute=True):
        return self.cli("train", kind, "--clone", str(self.work), *extra, *(["--execute"] if execute else []))

    def guard(self, base_branch, head):
        return self.cli("guard", "--clone", str(self.work), "--base", base_branch, "--head", head)


class SuccessorEnsureTests(TrainBase):
    def test_created_reused_and_no_duplicates(self):
        self.install(self.v0_only_policy())
        rc1 = self.remote("refs/tags/v0.3.0-rc.1")
        rc, out, _ = self.ensure("0.3.0", execute=False)
        self.assertEqual((rc, out["status"], self.states(out)["branch"]), (0, "NOT_RUN", "DRY_PLAN"))
        self.assertIsNone(self.remote("refs/heads/integration/v0.4.0"))
        rc, out, _ = self.ensure("0.3.0")
        self.assertEqual((rc, out["status"], self.states(out)["branch"]), (0, "PASS", "CREATED"), out)
        self.assertEqual(out["idempotency_key"], f"succ:{REPO}:0.4.0")
        self.assertEqual(self.states(out)["registry"], "PENDING_OWNER_LISTING")
        self.assertEqual(self.remote("refs/heads/integration/v0.4.0"), rc1)
        cand = json.loads(pathlib.Path(next(s for s in out["steps"] if s["step"] == "emit")["evidence"]["candidate_path"]).read_text())
        e = base.entry(cand)
        self.assertIn({"branch": "integration/v0.4.0", "version": "0.4.0"}, e["branches"])
        self.assertIn("v0.4.0/", e["future_head_prefixes"])
        pred = e["release_lines"]["lines"][-1]["predecessor"]
        self.assertEqual((pred["fork_ref"], pred["fork_sha"]), ("v0.3.0-rc.1", rc1))
        # not listed until the owner installs it; a rerun finds the branch and reuses it
        rc, out, _ = self.ensure("0.3.0")
        self.assertEqual((rc, self.states(out)["branch"]), (0, "REUSED"))
        before = self.heads()
        self.install_packet(self.ensure("0.3.0")[1])
        rc, out, _ = self.ensure("0.3.0")
        self.assertEqual((rc, self.states(out)["registry"], self.states(out)["branch"]), (0, "LISTED", "REUSED"))
        self.assertEqual(self.heads(), before)

    def test_conflict_existing_when_branch_lacks_the_fork(self):
        self.install(self.v0_only_policy())
        self.branch("integration/v0.4.0", "main")  # unrelated: does not contain rc.1
        rc, out, _ = self.ensure("0.3.0")
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("CONFLICT_EXISTING", self.codes(out))

    def test_refusals(self):
        self.install(self.v0_only_policy())
        sha = self.commit("main", "late-main.txt")  # on main only, not reachable from integration/v0.x
        rc, out, _ = self.ensure("0.3.0", "--version", "0.9.0", "--from", sha)
        self.assertIn("FORK_NOT_ON_PREDECESSOR", self.codes(out))
        rc, out, _ = self.ensure("0.7.0")
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("NO_FORK_POINT", self.codes(out))  # no rc tag for 0.7.0 on the line
        p = self.v0_only_policy()
        base.entry(p)["release_lines"]["audit"] = {"status": "PENDING", "source": None, "observed_sha256": None}
        self.install(p)
        self.assertIn("AUDIT_PENDING", self.codes(self.ensure("0.3.0")[1]))


def line(lid, version, pred_line, pred_version, branch, fork_sha, state="active"):
    return {"id": lid, "kind": "successor", "version": version, "integration_branch": branch,
            "predecessor": {"line": pred_line, "version": pred_version, "fork_ref": f"v{pred_version}-rc.1", "fork_sha": fork_sha},
            "publication_base": {"branch": "main", "kind": "default"}, "support": "supported", "state": state}


class TrainTests(TrainBase):
    def test_blocked_train_end_to_end(self):
        self.install(self.v0_only_policy())
        # v0.4.0 forks from the frozen v0.3.0 candidate; the owner installs the packet
        self.install_packet(self.ensure("0.3.0")[1])
        self.commit("integration/v0.4.0", "succ-1.txt")
        self.tag("v0.4.0-rc.1", "integration/v0.4.0")  # cut before the predecessor is integrated: must never be promoted
        # a late fix to the predecessor lands, rc.2 is what ships
        fix = self.commit("integration/v0.x", "late-fix.txt")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        # out of order / early: the final tag does not exist yet
        rc, out, _ = self.run_train("integrate", "--from", "v0", "--into", "v0.4.0")
        self.assertIn("NO_FINAL_TAG", self.codes(out))
        self.promote("0.3.0", "v0.3.0-rc.2")
        calls_before = len(self.calls())
        rc, out, _ = self.run_train("integrate", "--from", "v0", "--into", "v0.4.0", execute=False)
        self.assertEqual((rc, out["status"], self.states(out)["head"]), (0, "NOT_RUN", "DRY_PLAN"))
        self.assertEqual(self.heads("lineint/"), [])
        rc, out, _ = self.run_train("integrate", "--from", "v0", "--into", "v0.4.0")
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"), out)
        self.assertEqual(self.states(out)["stale_candidates"], "PROMOTION_BLOCKED")
        self.assertEqual(next(s for s in out["steps"] if s["step"] == "stale_candidates")["evidence"]["tags"], ["v0.4.0-rc.1"])
        self.assertEqual(out["idempotency_key"], f"int:{REPO}:v0:v0.4.0:v0.3.0")
        self.assertEqual(self.remote("refs/heads/lineint/0.3.0/v0.4.0"), fix)  # pinned at the promoted candidate, not at the base tip
        rc, out, _ = self.run_train("integrate", "--from", "v0", "--into", "v0.4.0")
        self.assertEqual((rc, self.states(out)["head"], self.states(out)["pr"]), (0, "REUSED", "REUSED"))
        self.assertEqual(len([c for c in self.calls()[calls_before:] if c.startswith("pr create")]), 1)
        self.assertEqual(self.heads("lineint/"), ["refs/heads/lineint/0.3.0/v0.4.0"])
        body = json.loads(self.gh_state.read_text())["prs"][0]["body"]
        self.assertIn("Release-Line-Kind: integrate", body)
        self.gate_merge(1)
        rc, out, _ = self.run_train("integrate", "--from", "v0", "--into", "v0.4.0")
        self.assertEqual((rc, out["status"], self.states(out)["integration"]), (0, "PASS", "INTEGRATED"))
        self.assertEqual(subprocess.run([REAL_GIT, "-C", str(self.bare), "merge-base", "--is-ancestor", fix, "integration/v0.4.0"],
                                        env=self.genv).returncode, 0)

        # v0.4.0 ships; v0.5.0 forks from the v0.4.0 candidate it needs (the post-integration one), not from the stale rc.1
        self.commit("integration/v0.4.0", "succ-2.txt")
        rc2 = self.w("rev-parse", "HEAD")
        self.tag("v0.4.0-rc.2", "integration/v0.4.0")
        rc, out, _ = self.ensure("0.4.0")
        self.assertEqual((rc, self.states(out)["branch"]), (0, "CREATED"), out)
        self.assertEqual(self.remote("refs/heads/integration/v0.5.0"), rc2)
        self.install_packet(out)
        self.assertEqual(self.cli("registry", "validate")[0], 0)
        rc3 = self.commit("integration/v0.4.0", "late-fix-2.txt")  # a late v0.4.0 fix after v0.5.0 forked
        self.tag("v0.4.0-rc.3", "integration/v0.4.0")
        self.promote("0.4.0", "v0.4.0-rc.3")
        # converge before the second integration is refused; so is the guard
        rc, out, _ = self.run_train("converge", "--line", "v0")
        self.assertEqual((rc, out["status"], self.states(out)["precondition"]), (1, "BLOCKED", "CONVERGE_PRECONDITION"))
        tip = self.remote("refs/heads/integration/v0.5.0")
        self.assertIn("CONVERGE_PRECONDITION", self.codes(self.guard("integration/v0.x", tip)[1]))
        self.assertEqual(self.commit("integration/v0.5.0", "succ-3.txt"), self.remote("refs/heads/integration/v0.5.0"))
        # integrate in dependency order
        rc, out, _ = self.run_train("integrate", "--from", "v0.4.0", "--into", "v0.5.0")
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"), out)
        self.assertEqual(self.remote("refs/heads/lineint/0.4.0/v0.5.0"), rc3)
        self.gate_merge(2)
        self.assertEqual(self.states(self.run_train("integrate", "--from", "v0.4.0", "--into", "v0.5.0")[1])["integration"], "INTEGRATED")
        # converge: PR from the tip successor into the major integration branch, then the converge packet
        rc, out, _ = self.run_train("converge", "--line", "v0")
        self.assertEqual((rc, out["status"], out["idempotency_key"]), (0, "DEFERRED", f"conv:{REPO}:v0"), out)
        self.assertEqual(self.run_train("converge", "--line", "v0")[1]["steps"][-1]["state"], "REUSED")
        self.assertEqual(len([c for c in self.calls() if c.startswith("pr create")]), 3)
        tip = self.remote("refs/heads/integration/v0.5.0")
        self.assertEqual(self.states(self.guard("integration/v0.x", tip)[1])["converge"], "CONVERGE_ALLOWED")
        self.gate_merge(3)
        rc, out, _ = self.run_train("converge", "--line", "v0")
        self.assertEqual((rc, out["status"], self.states(out)["convergence"]), (0, "PASS", "CONVERGED"), out)
        self.install_packet(out)
        rc, shown, _ = self.cli("registry", "show")
        self.assertEqual(rc, 0)
        steps = {s["step"]: s for s in shown["steps"]}
        self.assertEqual((steps["line:v0.4.0"]["state"], steps["line:v0.5.0"]["state"]), ("converged", "converged"))
        self.assertEqual(steps["line:v0.5.0"]["evidence"]["converged_into"], "v0")
        rc, out, _ = self.run_train("converge", "--line", "v0")  # nothing left to converge once the owner installed the packet
        self.assertIn("NO_SUCCESSORS", self.codes(out))
        # no duplicate PRs or branches across all retries; no force push; zero workflow runs
        self.assertEqual(len([c for c in self.calls() if c.startswith("pr create")]), 3)
        self.assertEqual(self.heads("lineint/"), ["refs/heads/lineint/0.3.0/v0.4.0", "refs/heads/lineint/0.4.0/v0.5.0"])
        self.assertFalse([c for c in self.calls() if "workflow" in c])
        self.assert_no_force_push()

    def assert_no_force_push(self):
        pushes = [x for x in self.git_log.read_text().splitlines() if " push " in f" {x} "]
        self.assertTrue(pushes)
        for x in pushes:
            self.assertRegex(x, r"^-C \S+ push origin [0-9a-f]{40}:refs/heads/\S+$")
            self.assertNotRegex(x, r"--force|--delete|--mirror| -f |:refs/heads/\+| \+")

    def two_successors_registry(self, tags):
        self.branch("integration/v0.4.0", "integration/v0.x")
        self.commit("integration/v0.4.0", "succ-1.txt")
        self.branch("integration/v0.5.0", "integration/v0.4.0")
        p = self.v0_only_policy()
        e = base.entry(p)
        sha = self.remote("refs/tags/v0.3.0-rc.1")
        e["branches"] += [{"branch": "integration/v0.4.0", "version": "0.4.0"}, {"branch": "integration/v0.5.0", "version": "0.5.0"}]
        e["release_lines"]["lines"] += [line("v0.4.0", "0.4.0", "v0", "0.3.0", "integration/v0.4.0", sha),
                                        line("v0.5.0", "0.5.0", "v0.4.0", "0.4.0", "integration/v0.5.0", sha)]
        self.install(p)
        for t in tags:
            self.commit("integration/v0.4.0", f"{t}.txt")
            self.tag(t, "integration/v0.4.0")

    def test_out_of_order_integrate_is_refused(self):
        self.two_successors_registry(["v0.4.0-rc.1"])
        self.commit("integration/v0.x", "late-fix.txt")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.promote("0.3.0", "v0.3.0-rc.2")
        self.promote("0.4.0", "v0.4.0-rc.1")  # v0.4.0 shipped although v0.3.0 never reached integration/v0.4.0
        rc, out, _ = self.run_train("integrate", "--from", "v0.4.0", "--into", "v0.5.0")
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("OUT_OF_ORDER", self.codes(out))
        self.assertEqual(self.heads("lineint/"), [])
        self.assertEqual([c for c in self.calls() if c.startswith("pr create")], [])

    def test_integrate_refuses_non_successor_and_conflict_is_not_pushed(self):
        self.two_successors_registry([])
        self.assertIn("NOT_SUCCESSOR", self.codes(self.run_train("integrate", "--from", "v0", "--into", "v0.5.0")[1]))
        self.w("checkout", "-q", "integration/v0.4.0")
        (self.work / "feature-a.txt").write_text("changed on the successor\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", "diverge")
        self.w("push", "-q", "origin", "integration/v0.4.0")
        self.w("checkout", "-q", "integration/v0.x")
        (self.work / "feature-a.txt").write_text("changed on the predecessor\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", "diverge predecessor")
        self.w("push", "-q", "origin", "integration/v0.x")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.promote("0.3.0", "v0.3.0-rc.2")
        rc, out, _ = self.run_train("integrate", "--from", "v0", "--into", "v0.4.0")
        self.assertIn("MERGE_CONFLICT", self.codes(out))
        self.assertEqual(self.heads("lineint/"), [])


class GuardTests(TrainBase):
    def test_successor_only_commits_and_clean_heads(self):
        TrainTests.two_successors_registry(self, [])
        self.commit("integration/v0.5.0", "succ-3.txt")
        succ_tip = self.remote("refs/heads/integration/v0.4.0")
        rc, out, _ = self.guard("integration/v0.x", succ_tip)
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("SUCCESSOR_ONLY_COMMITS", self.codes(out))
        # a head that only carries predecessor work passes; so does a head into the successor that carries the predecessor's commits
        rc, out, _ = self.guard("integration/v0.x", self.remote("refs/heads/integration/v0.x"))
        self.assertEqual((rc, self.states(out)["successor_commits"]), (0, "NONE"))
        rc, out, _ = self.guard("integration/v0.4.0", self.remote("refs/heads/integration/v0.x"))
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        # commits of the second successor are successor-only for the first too (transitive)
        rc, out, _ = self.guard("integration/v0.4.0", self.remote("refs/heads/integration/v0.5.0"))
        self.assertIn("SUCCESSOR_ONLY_COMMITS", self.codes(out))

    def test_eol_base_and_publication_base_advisory(self):
        self.install(self.v0_only_policy(support="eol"))
        rc, out, _ = self.guard("integration/v0.x", self.remote("refs/heads/integration/v0.x"))
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("EOL_BASE", self.codes(out))
        self.install(self.v0_only_policy())
        rc, out, _ = self.guard("main", self.remote("refs/heads/main"))
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        step = next(s for s in out["steps"] if s["state"] == "PUBLICATION_BASE_DIRECT_CHANGE")
        self.assertTrue(step["evidence"]["advisory"])
        rc, out, _ = self.guard("unrelated", self.remote("refs/heads/main"))
        self.assertEqual((rc, self.states(out)["base"]), (0, "NOT_AN_INTEGRATION_BASE"))
        self.assertEqual(self.guard("integration/v0.x", "deadbeef")[0], 1)


if __name__ == "__main__":
    unittest.main()
