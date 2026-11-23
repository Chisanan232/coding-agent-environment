#!/usr/bin/env python3
"""Tests for the SPE-183 forward-port commands in scripts/release-line.py: plan, run, status.

Reuses the SPE-182 harness: disposable bare origin + clone, a fake `gh` (extended here with `pr view`) that performs real merges into
the bare repo and logs every call, and a `git` wrapper that logs every git call the tool makes. No network.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rl_train_tests", ROOT / "tests/test-release-line-train.py")
train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(train)
base = train.base
REPO = base.REPO
REAL_GIT = train.REAL_GIT

FAKE_GH = train.FAKE_GH.replace('else:\n    sys.exit(1)', '''elif a[:2] == ["pr", "view"]:
    pr = next((p for p in st["prs"] if p["number"] == int(a[2])), None)
    if pr is None:
        sys.exit(1)
    print(json.dumps({{"number": pr["number"], "state": pr["state"], "baseRefName": pr["baseRefName"],
                      "mergeCommit": ({{"oid": pr["mergeOid"]}} if pr.get("mergeOid") else None)}}))
else:
    sys.exit(1)''')
assert '"view"' in FAKE_GH


class FP(train.TrainBase):
    def setUp(self):
        super().setUp()
        (self.bin / "gh").write_text(FAKE_GH.format(py=sys.executable, git=REAL_GIT, date=base.DATE))
        self.env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.x",
                         "GIT_AUTHOR_DATE": base.DATE, "GIT_COMMITTER_DATE": base.DATE})
        train.TrainTests.two_successors_registry(self, [])  # v0 -> v0.4.0 -> v0.5.0, installed

    def merge_fix(self, fname, text="fixed\n", extra_files=(), onto="integration/v0.x"):
        """A GitHub-style --no-ff merge commit on `onto` whose second parent carries the fix commit(s)."""
        self.w("fetch", "-q", "--tags", "origin")
        self.w("checkout", "-q", "-B", "fix/" + fname, "origin/" + onto)
        for name in (fname, *extra_files):
            (self.work / name).write_text(text if name == fname else name + "\n")
            self.w("add", ".")
            self.w("commit", "-q", "-m", f"fix {name}")
        self.w("checkout", "-q", "-B", onto, "origin/" + onto)
        self.w("merge", "-q", "--no-ff", "-m", "Merge pull request #99 from o/fix\n\nFix " + fname, "fix/" + fname)
        self.w("push", "-q", "origin", onto)
        return self.w("rev-parse", "HEAD")

    def fp(self, cmd, *extra, execute=False):
        return self.cli("forward-port", cmd, "--clone", str(self.work), *extra, *(["--execute"] if execute else []))

    def targets(self, out):
        return {s["step"].split(":")[2]: s for s in out["steps"] if s["step"].startswith("target:")}

    def prs(self):
        return [c for c in self.calls() if c.startswith("pr create")]

    def pushes(self):
        return [x for x in self.git_log.read_text().splitlines() if " push " in f" {x} "]

    def trailers_of(self, ref):
        return {m.group(1): m.group(2) for m in re.finditer(r"^Release-Line-([A-Za-z-]+): (.*)$", self.w("log", "-1", "--format=%B", ref), re.M)}

    def assert_no_force_push(self):
        self.assertTrue(self.pushes())
        for x in self.pushes():
            self.assertRegex(x, r"^(?:--no-pager )?(?:-c \S+ )*-C \S+ push origin [0-9a-f]{40}:refs/heads/\S+$")
            self.assertNotRegex(x, r"--force|--delete|--mirror| -f |:refs/heads/\+| \+")

    def clean_clone(self):
        self.assertEqual(self.w("status", "--porcelain"), "")


class PlanAndRun(FP):
    def test_plan_run_idempotent_and_merge_cycle(self):
        m = self.merge_fix("fix.txt")
        calls0, pushes0 = len(self.calls()), len(self.pushes())
        rc, out, _ = self.fp("plan", "--merge", m)
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"), out)
        t = self.targets(out)
        self.assertEqual({k: v["state"] for k, v in t.items()}, {"v0.4.0": "pending", "v0.5.0": "pending"})
        self.assertEqual(t["v0.4.0"]["evidence"]["key"], f"fp:{REPO}:{m}:v0.4.0")
        self.assertEqual(t["v0.4.0"]["evidence"]["head"], f"fwdport/v0.4.0/{m[:12]}")
        self.assertEqual(self.heads("fwdport/"), [])
        self.assertEqual(len(self.pushes()), pushes0)
        self.assertEqual(self.prs(), [])
        self.assertEqual(next(s for s in out["steps"] if s["step"] == f"source:{m[:12]}")["evidence"]["line"], "v0")
        self.clean_clone()

        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"), out)
        self.assertEqual(self.heads("fwdport/"), [f"refs/heads/fwdport/v0.4.0/{m[:12]}", f"refs/heads/fwdport/v0.5.0/{m[:12]}"])
        head = self.remote(f"refs/heads/fwdport/v0.4.0/{m[:12]}")
        self.assertEqual(self.trailers_of(head), {
            "Source": m, "Source-Line": "v0", "Target-Line": "v0.4.0", "Kind": "forward-port",
            "Reason": f"maintenance change {m[:12]} from v0 applies to v0.4.0"})
        self.assertEqual(self.w("rev-parse", f"{head}^"), self.remote("refs/heads/integration/v0.4.0"))  # one commit on the target tip
        self.assertIn("fix.txt", self.w("show", "--name-only", "--format=", head))
        self.assertIn("(cherry picked from commit " + m + ")", self.w("log", "-1", "--format=%B", head))
        prs = self.prs()
        self.assertEqual(len(prs), 2)
        self.assertTrue(prs[0].startswith(f"pr create --repo {REPO} --base integration/v0.4.0 --head fwdport/v0.4.0/{m[:12]} --title "), prs[0])
        self.assertEqual(self.states(out)["pr:v0.4.0"], "CREATED")
        self.clean_clone()
        self.assertTrue((self.state_dir / "journal.jsonl").exists())
        self.assertEqual(oct((self.state_dir / "journal.jsonl").stat().st_mode & 0o777), "0o600")

        # idempotent re-run: nothing pushed, no new PR
        pushes1 = len(self.pushes())
        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"))
        self.assertEqual({k: v["evidence"]["code"] for k, v in self.targets(out).items()}, {"v0.4.0": "PR_OPEN", "v0.5.0": "PR_OPEN"})
        self.assertEqual(len(self.pushes()), pushes1)
        self.assertEqual(len(self.prs()), 2)
        rc, out, _ = self.fp("status", "--merge", m)
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"))

        # merge through the normal gate: ported, complete, and the merge commit is never a source again (loop prevention)
        self.gate_merge(1)
        mc = self.remote("refs/heads/integration/v0.4.0")
        self.gate_merge(2)
        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual({v["evidence"]["code"] for v in self.targets(out).values()}, {"PORTED"})
        self.assertEqual({v["state"] for v in self.targets(out).values()}, {"forward-port"})
        acc = next(s for s in out["steps"] if s["step"].startswith("accounting:"))
        self.assertEqual((acc["state"], acc["evidence"]["complete"]), ("COMPLETE", True))
        rc, out, _ = self.fp("plan", "--merge", mc)
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        self.assertIn("LOOP_PREVENTED", self.codes(out))
        self.assertEqual(self.targets(out), {})
        self.assertEqual(len(self.prs()), 2)
        self.assert_no_force_push()
        self.assertFalse([c for c in self.calls() if "workflow" in c])

    def test_stale_head_is_refreshed_by_merging_the_target_in(self):
        m = self.merge_fix("fix.txt")
        self.assertEqual(self.fp("run", "--merge", m, execute=True)[1]["status"], "DEFERRED")
        head_ref = f"refs/heads/fwdport/v0.4.0/{m[:12]}"
        old = self.remote(head_ref)
        newer = self.commit("integration/v0.4.0", "later.txt")
        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        self.assertEqual(rc, 0, out)
        new = self.remote(head_ref)
        self.assertNotEqual(new, old)
        self.assertEqual(self.w("merge-base", "--is-ancestor", old, new) or "ok", "ok")
        self.assertEqual(subprocess.run([REAL_GIT, "-C", str(self.bare), "merge-base", "--is-ancestor", newer, new], env=self.genv).returncode, 0)
        self.assertEqual(len(self.prs()), 2)
        self.assert_no_force_push()

    def test_existing_head_without_the_port_is_a_conflict(self):
        m = self.merge_fix("fix.txt")
        self.branch(f"fwdport/v0.4.0/{m[:12]}", "main")  # squatting on the head name, not a port of this source
        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        t = self.targets(out)
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertEqual((t["v0.4.0"]["state"], t["v0.4.0"]["evidence"]["code"]), ("conflict", "CONFLICT_EXISTING"))
        self.assertEqual(t["v0.5.0"]["state"], "pending")  # independent target still proceeded
        self.assertEqual(len(self.prs()), 1)


class Equivalence(FP):
    def test_patch_id_content_and_trailer_equivalence(self):
        m = self.merge_fix("fix.txt", "fixed\n")
        # v0.4.0 got the identical change independently: same patch-id
        self.w("fetch", "-q", "origin")
        self.w("checkout", "-q", "-B", "integration/v0.4.0", "origin/integration/v0.4.0")
        (self.work / "fix.txt").write_text("fixed\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", "own fix")
        self.w("push", "-q", "origin", "integration/v0.4.0")
        rc, out, _ = self.fp("plan", "--merge", m)
        t = self.targets(out)
        self.assertEqual((t["v0.4.0"]["state"], t["v0.4.0"]["evidence"]["code"]), ("equivalent", "EQUIVALENT_PATCH"))
        # v0.5.0: same final content through two commits: no patch-id match, identical tree after the replay
        self.w("checkout", "-q", "-B", "integration/v0.5.0", "origin/integration/v0.5.0")
        for text in ("draft\n", "fixed\n"):
            (self.work / "fix.txt").write_text(text)
            self.w("add", ".")
            self.w("commit", "-q", "-m", "fix in two steps")
        self.w("push", "-q", "origin", "integration/v0.5.0")
        out = self.fp("plan", "--merge", m)[1]
        self.assertEqual(self.targets(out)["v0.5.0"]["evidence"]["code"], "EQUIVALENT_CONTENT")
        self.assertEqual((out["status"], next(s for s in out["steps"] if s["step"].startswith("accounting:"))["evidence"]["complete"]), ("PASS", True))
        self.assertEqual(self.heads("fwdport/"), [])

    def test_reviewed_trailer_records_are_terminal(self):
        m = self.merge_fix("fix.txt")
        for branch, kind in (("integration/v0.4.0", "equivalent"), ("integration/v0.5.0", "not-applicable")):
            self.w("fetch", "-q", "origin")
            self.w("checkout", "-q", "-B", branch, "origin/" + branch)
            self.w("commit", "-q", "--allow-empty", "-m", f"Record {kind}\n\nRelease-Line-Source: {m}\nRelease-Line-Source-Line: v0\n"
                   f"Release-Line-Target-Line: x\nRelease-Line-Kind: {kind}\nRelease-Line-Reason: reviewed")
            self.w("push", "-q", "origin", branch)
        rc, out, _ = self.fp("status", "--merge", m)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual({k: v["state"] for k, v in self.targets(out).items()}, {"v0.4.0": "equivalent", "v0.5.0": "not-applicable"})
        self.assertEqual(self.targets(out)["v0.5.0"]["evidence"]["reason"], "reviewed")

    def test_contained_after_integration(self):
        m = self.merge_fix("fix.txt")
        self.w("fetch", "-q", "origin")
        self.w("checkout", "-q", "-B", "integration/v0.4.0", "origin/integration/v0.4.0")
        self.w("merge", "-q", "--no-ff", "-m", "integrate", "origin/integration/v0.x")
        self.w("push", "-q", "origin", "integration/v0.4.0")
        out = self.fp("plan", "--merge", m)[1]
        self.assertEqual(self.targets(out)["v0.4.0"]["evidence"]["code"], "CONTAINED")
        self.assertEqual(self.targets(out)["v0.5.0"]["state"], "pending")


class Conflicts(FP):
    def test_conflict_leaves_clean_state_and_pushes_nothing_for_that_target(self):
        self.w("fetch", "-q", "origin")
        self.w("checkout", "-q", "-B", "integration/v0.4.0", "origin/integration/v0.4.0")
        (self.work / "feature-a.txt").write_text("diverged on the successor\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", "diverge")
        self.w("push", "-q", "origin", "integration/v0.4.0")
        m = self.merge_fix("feature-a.txt", "fixed on the predecessor\n")
        pushes0 = len(self.pushes())
        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        t = self.targets(out)
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertEqual((t["v0.4.0"]["state"], t["v0.4.0"]["evidence"]["code"], t["v0.4.0"]["evidence"]["paths"]),
                         ("conflict", "BLOCKED_CONFLICT", ["feature-a.txt"]))
        # v0.5.0 forked before the divergence: independent, still ported
        self.assertEqual(t["v0.5.0"]["state"], "pending")
        self.assertEqual(self.heads("fwdport/"), [f"refs/heads/fwdport/v0.5.0/{m[:12]}"])
        self.assertEqual(len(self.pushes()) - pushes0, 1)
        self.clean_clone()
        self.assertEqual(self.w("ls-files", "-u"), "")
        self.assertFalse((self.work / ".git/MERGE_HEAD").exists())
        self.assert_no_force_push()


class Targets(FP):
    def with_lines(self, **updates):
        p = json.loads(self.policy_path.read_text())
        for ln in base.entry(p)["release_lines"]["lines"]:
            if ln["id"] in updates:
                ln.update(updates[ln["id"]])
        self.install(p)

    def test_never_targets_eol_converged_or_publication_bases(self):
        self.with_lines(**{"v0.4.0": {"support": "eol"}, "v0.5.0": {"state": "converged", "converged_into": "v0"}})
        m = self.merge_fix("fix.txt")
        rc, out, _ = self.fp("run", "--merge", m, execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        ex = next(s for s in out["steps"] if s["step"].startswith("excluded:"))["evidence"]["lines"]
        self.assertEqual(ex, [{"line": "v0.4.0", "reason": "EOL"}, {"line": "v0.5.0", "reason": "STATE_CONVERGED"}])
        self.assertEqual((self.heads("fwdport/"), self.prs()), ([], []))

    def test_source_on_a_publication_base_targets_lines_but_never_the_base(self):
        self.w("checkout", "-q", "main")
        direct = self.commit("main", "direct-on-main.txt")
        out = self.fp("plan", "--merge", direct)[1]
        src = next(s for s in out["steps"] if s["step"].startswith("source:"))["evidence"]
        self.assertEqual(src["line"], "base:main")
        self.assertEqual(set(self.targets(out)), {"v0", "v0.4.0", "v0.5.0"})
        self.assertFalse([s for s in out["steps"] if s["step"].startswith("target:") and s["evidence"]["branch"] in ("main", "master")])
        rc, out, _ = self.fp("run", "--merge", direct, execute=True)
        self.assertTrue(all(h.startswith("refs/heads/fwdport/") for h in self.heads("fwdport/")))
        bases = {c.split("--base ")[1].split()[0] for c in self.prs()}
        self.assertEqual(bases, {"integration/v0.x", "integration/v0.4.0", "integration/v0.5.0"})

    def test_a_source_on_the_chain_tip_has_no_targets(self):
        tip = self.commit("integration/v0.5.0", "tip.txt")
        out = self.fp("plan", "--merge", tip)[1]
        self.assertEqual((out["status"], self.targets(out)), ("PASS", {}))
        acc = next(s for s in out["steps"] if s["step"].startswith("accounting:"))
        self.assertTrue(acc["evidence"]["complete"])


class SourceDetection(FP):
    def test_second_parent_range_is_inspected(self):
        m = self.merge_fix("fix.txt", extra_files=("fix2.txt",))
        out = self.fp("plan", "--merge", m)[1]
        ev = next(s for s in out["steps"] if s["step"].startswith("source:"))["evidence"]
        self.assertEqual(ev["range"], f"{self.w('rev-parse', m + '^1')}..{self.w('rev-parse', m + '^2')}")
        self.assertEqual(len(ev["range_commits"]), 2)
        # a trailer on any commit of M^1..M^2 (not on M itself) makes M a non-source
        self.w("fetch", "-q", "origin")
        self.w("checkout", "-q", "-B", "fix/looped", "origin/integration/v0.x")
        (self.work / "looped.txt").write_text("x\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", f"port\n\nRelease-Line-Source: {m}\nRelease-Line-Kind: forward-port")
        self.w("checkout", "-q", "-B", "integration/v0.x", "origin/integration/v0.x")
        self.w("merge", "-q", "--no-ff", "-m", "Merge pull request #100 from o/looped", "fix/looped")
        self.w("push", "-q", "origin", "integration/v0.x")
        looped = self.w("rev-parse", "HEAD")
        out = self.fp("plan", "--merge", looped)[1]
        self.assertIn("LOOP_PREVENTED", self.codes(out))
        self.assertEqual(self.targets(out), {})

    def test_integration_merge_is_not_a_source(self):
        self.w("fetch", "-q", "origin")
        self.w("checkout", "-q", "-B", "integration/v0.x", "origin/integration/v0.x")
        self.w("merge", "-q", "--no-ff", "-m", "converge", "origin/integration/v0.4.0")
        self.w("push", "-q", "origin", "integration/v0.x")
        out = self.fp("plan", "--merge", self.w("rev-parse", "HEAD"))[1]
        self.assertIn("INTEGRATION_MERGE", self.codes(out))
        self.assertEqual((out["status"], self.targets(out)), ("PASS", {}))

    def test_pr_and_range_inputs(self):
        m1 = self.merge_fix("one.txt")
        m2 = self.merge_fix("two.txt")
        self.set_gh(prs=[{"number": 7, "state": "MERGED", "headRefName": "fix/one", "baseRefName": "integration/v0.x", "mergeOid": m1, "url": "u"},
                         {"number": 8, "state": "OPEN", "headRefName": "fix/x", "baseRefName": "integration/v0.x", "url": "u"}], bare=str(self.bare))
        out = self.cli("forward-port", "plan", "--clone", str(self.work), "--pr", "7")[1]
        self.assertEqual(out["idempotency_key"], f"fp:{REPO}:{m1}:*")
        self.assertEqual(next(s for s in out["steps"] if s["step"].startswith("source:"))["evidence"]["pr"], 7)
        self.assertEqual(set(self.targets(out)), {"v0.4.0", "v0.5.0"})
        rc, out, _ = self.cli("forward-port", "plan", "--clone", str(self.work), "--pr", "8")
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("PR_NOT_MERGED", self.codes(out))
        first = self.w("rev-parse", m1 + "^1")
        out = self.fp("plan", "--range", f"{first}..{m2}")[1]
        self.assertEqual(len([s for s in out["steps"] if s["step"].startswith("source:") and s["state"] == "SOURCE"]), 2)
        self.assertEqual(len([x for x in out["steps"] if x["step"].startswith("target:")]), 4)  # two sources x two targets, accounted separately


class Cli(FP):
    def test_usage_and_registry_refusals(self):
        self.assertEqual(self.cli("forward-port", "plan", "--clone", str(self.work))[0], 2)
        self.assertEqual(self.cli("forward-port", "plan", "--merge", "a", "--pr", "1", "--clone", str(self.work))[0], 2)
        self.assertEqual(self.cli("forward-port", "plan", "--merge", "-x", "--clone", str(self.work))[0], 2)
        self.assertEqual(self.cli("forward-port", "plan", "--merge", "HEAD")[0], 2)  # no --clone
        rc, out, _ = self.fp("plan", "--merge", "0" * 40)
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("BAD_SOURCE", self.codes(out))
        p = json.loads(self.policy_path.read_text())
        base.entry(p)["release_lines"]["audit"] = {"status": "PENDING", "source": None, "observed_sha256": None}
        self.install(p)
        self.assertIn("AUDIT_PENDING", self.codes(self.fp("plan", "--merge", "HEAD")[1]))

    def test_template_is_inert_and_safe(self):
        text = (ROOT / "docs/templates/workflows/release-line.yml").read_text()
        self.assertIn("RELEASE_LINES_JSON", text)
        self.assertIn("NOT_RUN", text)
        self.assertIn("forward-port run", text)
        for banned in ("workflow_dispatch", "gh pr merge", "gh release", "git tag", "gh workflow", "git push --force", "--admin"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
