#!/usr/bin/env python3
"""Tests for scripts/release-line.py (SPE-181a): registry validate/show/propose, route, audit observe/import.

Runs the CLI as a subprocess against a temp HOME (the owner trust file lives under it), a bare origin plus a clone with fixed
dates, and a fake `gh` stub on PATH. No network, no real gh, no real owner files.
"""
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/release-line.py"
EXAMPLE = ROOT / "docs/examples/version-branches-release-lines.example.json"
SPEC = importlib.util.spec_from_file_location("omg", ROOT / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)

REPO = "Example-Owner/example-product"
DATE = "2026-01-01T00:00:00Z"
FAKE_GH = """#!{py}
import json, os, sys
st = json.load(open(os.environ["FAKE_GH_STATE"]))
with open(os.environ["FAKE_GH_STATE"] + ".calls", "a") as f:
    f.write(" ".join(sys.argv[1:3]) + "\\n")
if st.get("fail"):
    sys.exit(1)
if sys.argv[1:3] == ["pr", "list"]:
    print(json.dumps(st["prs"]))
else:
    sys.exit(1)
"""


def example():
    return json.loads(EXAMPLE.read_text())


def entry(p):
    return p["repos"][REPO]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(os.path.realpath(self.tmp.name)) / "home"
        self.local = self.home / ".config/coding-agent-environment/local"
        self.local.mkdir(parents=True, mode=0o700)
        for d in (self.home / ".config", self.home / ".config/coding-agent-environment", self.local):
            os.chmod(d, 0o700)
        self.policy_path = self.local / "version-branches.json"
        self.bin = pathlib.Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.format(py=sys.executable))
        gh.chmod(0o755)
        self.gh_state = pathlib.Path(self.tmp.name) / "gh.json"
        self.set_gh(prs=[])
        self.state_dir = pathlib.Path(self.tmp.name) / "state"
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "SOURCE_DATE_EPOCH": "1767225600", "FAKE_GH_STATE": str(self.gh_state)}

    def set_gh(self, **state):
        self.gh_state.write_text(json.dumps(state))

    def install(self, policy):
        self.policy_path.write_text(json.dumps(policy, indent=2))
        os.chmod(self.policy_path, 0o600)

    def cli(self, *args, repo=REPO, policy=True):
        argv = [sys.executable, "-I", str(CLI), *args, "--repo", repo, "--state-dir", str(self.state_dir)]
        if policy:
            argv += ["--policy", str(self.policy_path)]
        p = subprocess.run(argv, env=self.env, capture_output=True, text=True)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = None
        return p.returncode, out, p.stderr

    def states(self, out):
        return {s["step"]: s["state"] for s in out["steps"]}

    def codes(self, out):
        return {s["state"] for s in out["steps"]}

    def make_repo(self, tags=(), extra_branches=()):
        base = pathlib.Path(self.tmp.name)
        git_env = dict(self.env, GIT_AUTHOR_DATE=DATE, GIT_COMMITTER_DATE=DATE, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.x")

        def git(cwd, *a):
            subprocess.run(["git", *a], cwd=cwd, env=git_env, check=True, capture_output=True)

        work, bare = base / "work", base / "origin.git"
        git(base, "init", "--bare", "-b", "main", str(bare))
        git(base, "clone", str(bare), str(work))
        (work / "docs").mkdir()
        (work / "docs/RELEASING.md").write_text("how\n")
        (work / "a.txt").write_text("a\n")
        git(work, "add", ".")
        git(work, "commit", "-m", "init")
        git(work, "push", "origin", "HEAD:main")
        for b in ("integration/v0.x", "integration/v0.4.0", *extra_branches):
            git(work, "branch", b)
        git(work, "tag", "v0.2.0")
        for t, branch in tags:
            git(work, "checkout", "-q", branch)
            git(work, "tag", t)
        git(work, "push", "origin", "--all")
        git(work, "push", "origin", "--tags")
        git(work, "fetch", "origin")
        self.clone = str(work)
        return self.clone


class RegistryTests(Base):
    def test_example_loads_in_real_gate_loader_with_identical_approvals(self):
        p = example()
        stripped = copy.deepcopy(p)
        del entry(stripped)["release_lines"]
        self.assertEqual(omg.load_version_policy(p), omg.load_version_policy(stripped))

    def test_validate_and_show(self):
        self.install(example())
        rc, out, _ = self.cli("registry", "validate")
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        self.assertEqual(out["schema"], 1)
        self.assertRegex(out["tool_revision"], r"^[0-9a-f]{40}$|^unknown$")
        rc, out, _ = self.cli("registry", "show")
        self.assertEqual(rc, 0)
        lines = {s["step"]: s for s in out["steps"]}
        self.assertTrue(lines["line:v0.4.0"]["evidence"]["merge_eligible"])
        self.assertEqual(lines["line:v0"]["evidence"]["integration_branch"], "integration/v0.x")

    def refused_config(self, mutate, code, repo_mutate=None):
        p = example()
        mutate(entry(p))
        self.install(p)
        rc, out, _ = self.cli("registry", "validate")
        self.assertEqual((rc, out["status"]), (3, "FAIL"), out)
        self.assertIn(code, self.codes(out))

    def test_every_validator_refusal(self):
        def line(e, i=0):
            return e["release_lines"]["lines"][i]

        cases = {
            "NOT_MERGE_ELIGIBLE": lambda e: e["branches"].pop(1),
            "MAINTENANCE_BASE_LISTED": lambda e: line(e, 0).update(publication_base={"branch": "integration/v0.4.0", "kind": "maintenance"}),
            "PROMOTE_PREFIX_MISSING": lambda e: e.update(future_head_prefixes=["v0.4.0/"]),
            "DUPLICATE_MAJOR": lambda e: e["release_lines"]["lines"].append(dict(line(e, 0), id="v0-again")),
            "BAD_STATE": lambda e: line(e, 1).update(state="paused"),
            "BAD_FIELD": lambda e: line(e, 0).update(support="maybe"),
            "BAD_SCHEMA": lambda e: e["release_lines"].update(schema=2),
            "BAD_AUDIT": lambda e: e["release_lines"].update(audit={"status": "COMPLETE"}),
            "BAD_PREDECESSOR": lambda e: line(e, 1)["predecessor"].update(fork_sha="abc"),
            "BAD_KIND": lambda e: line(e, 0).update(kind="train"),
        }
        for code, mutate in cases.items():
            with self.subTest(code):
                self.refused_config(mutate, code)

    def test_converged_needs_converged_into(self):
        self.refused_config(lambda e: e["release_lines"]["lines"][1].update(state="converged"), "BAD_STATE")

    def test_release_segment_branch_is_explained_and_gate_loader_also_rejects(self):
        def mutate(e):
            e["branches"][0]["branch"] = "release/v0.x"
            e["release_lines"]["lines"][0]["integration_branch"] = "release/v0.x"
        p = example()
        mutate(entry(p))
        self.install(p)
        rc, out, _ = self.cli("registry", "validate")
        self.assertEqual(rc, 3)
        self.assertTrue({"RELEASE_SEGMENT", "REJECTED_BY_GATE_LOADER"} <= self.codes(out))
        detail = next(s for s in out["steps"] if s["state"] == "RELEASE_SEGMENT")["evidence"]["detail"]
        self.assertIn("integration/v<major>.x", detail)

    def test_legacy_per_version_names_and_kind_version(self):
        p = example()
        e = entry(p)
        e["branches"] += [{"branch": "mvp-4.0/HORO-2046/integration", "version": "4.0.0"}, {"branch": "integration/mvp-5.0", "version": "5.0.0"}]
        e["release_lines"]["tag_format"] = "MVP-{version}"
        e["release_lines"]["lines"].append({"id": "mvp-4", "kind": "version", "version": "4.0.0", "integration_branch": "mvp-4.0/HORO-2046/integration",
                                            "publication_base": {"branch": "main", "kind": "default"}, "support": "supported"})
        self.install(p)
        self.assertEqual(self.cli("registry", "validate")[0], 0)

    def test_unregistered_repo_and_entry_without_release_lines(self):
        p = example()
        p["repos"]["Other/plain"] = {"branches": [{"branch": "integration/mvp-1.0", "version": "1.0"}]}
        self.install(p)
        for repo in ("Nobody/none", "Other/plain"):
            rc, out, _ = self.cli("registry", "validate", repo=repo)
            self.assertEqual((rc, out["status"]), (1, "BLOCKED"), repo)
            self.assertIn("UNREGISTERED", self.codes(out))

    def test_policy_outside_owner_dir_is_refused(self):
        other = pathlib.Path(self.tmp.name) / "p.json"
        other.write_text(json.dumps(example()))
        os.chmod(other, 0o600)
        argv = [sys.executable, "-I", str(CLI), "registry", "validate", "--repo", REPO, "--policy", str(other)]
        p = subprocess.run(argv, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 3)

    def test_audit_pending_blocks_mutation_but_not_reads(self):
        p = example()
        entry(p)["release_lines"]["audit"] = {"status": "PENDING", "source": None, "observed_sha256": None}
        self.install(p)
        self.assertEqual(self.cli("registry", "validate")[0], 0)
        self.assertEqual(self.cli("registry", "show")[0], 0)
        rc, out, _ = self.cli("registry", "propose", "--op", "eol", "--line", "v0", "--execute")
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("AUDIT_PENDING", self.codes(out))
        self.assertFalse(self.state_dir.exists())

    def test_propose_writes_candidate_and_digests_but_never_the_owner_file(self):
        self.install(example())
        before = self.policy_path.read_bytes()
        rc, out, _ = self.cli("registry", "propose", "--op", "add-successor", "--version", "0.5.0", "--predecessor", "v0.4.0",
                              "--predecessor-version", "0.4.0", "--fork-ref", "v0.4.0-rc.1", "--fork-sha", "2" * 40)
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"))
        self.assertFalse(self.state_dir.exists())
        rc, out, _ = self.cli("registry", "propose", "--op", "add-successor", "--version", "0.5.0", "--predecessor", "v0.4.0",
                              "--predecessor-version", "0.4.0", "--fork-ref", "v0.4.0-rc.1", "--fork-sha", "2" * 40, "--execute")
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        ev = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]
        cand_bytes = pathlib.Path(ev["candidate_path"]).read_bytes()
        self.assertEqual(ev["candidate_sha256"], hashlib.sha256(cand_bytes).hexdigest())
        self.assertEqual(ev["baseline_sha256"], hashlib.sha256(before).hexdigest())
        auth = json.loads(pathlib.Path(ev["authority_path"]).read_text())
        self.assertEqual((auth["candidate_sha256"], auth["baseline_sha256"], auth["canonical_loader"]), (ev["candidate_sha256"], ev["baseline_sha256"], "PASS"))
        self.assertEqual(oct(os.stat(ev["candidate_path"]).st_mode & 0o777), "0o600")
        cand = json.loads(cand_bytes)
        gate = omg.load_version_policy(cand)[REPO.lower()]
        self.assertIn("integration/v0.5.0", gate["branches"])
        self.assertEqual(self.policy_path.read_bytes(), before)

    def test_propose_retire_eol_converge_and_op_refusals(self):
        self.install(example())
        for args, check in ((("retire", "--line", "v0.4.0"), lambda ln: ln["state"] == "retired"),
                            (("eol", "--line", "v0"), lambda ln: ln["support"] == "eol"),
                            (("converge", "--line", "v0.4.0", "--into", "v0"), lambda ln: ln["state"] == "converged" and ln["converged_into"] == "v0")):
            with self.subTest(args[0]):
                op, _, lid, *rest = args
                rc, out, _ = self.cli("registry", "propose", "--op", op, "--line", lid, *rest, "--execute")
                self.assertEqual(rc, 0, out)
                path = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]["candidate_path"]
                lines = json.loads(pathlib.Path(path).read_text())["repos"][REPO]["release_lines"]["lines"]
                self.assertTrue(check(next(x for x in lines if x["id"] == lid)))
        for args, code in ((("retire", "--line", "nope"), "UNKNOWN_LINE"), (("retire", "--line", "v0"), "NOT_A_SUCCESSOR"),
                           (("converge", "--line", "v0.4.0"), "MISSING_FLAGS"), (("converge", "--line", "v0.4.0", "--into", "ghost"), "INVALID_CANDIDATE")):
            with self.subTest(code):
                rc, out, _ = self.cli("registry", "propose", "--op", args[0], *args[1:], "--execute")
                self.assertEqual(rc, 1)
                self.assertIn(code, self.codes(out))

    def test_usage_error_exits_2(self):
        rc, out, _ = self.cli("registry", "propose")
        self.assertEqual((rc, out), (2, None))


class RouteTests(Base):
    def route(self, kind, version, policy=None):
        self.install(policy or example())
        return self.cli("route", "--kind", kind, "--version", version, "--clone", self.clone)

    def target(self, out):
        return next(s for s in out["steps"] if s["step"] == "route")["evidence"]["branch"]

    def test_normal_route_goes_to_major_line(self):
        self.make_repo()
        rc, out, _ = self.route("feature", "0.3.0")
        self.assertEqual((rc, self.target(out)), (0, "integration/v0.x"))

    def test_exact_version_line_wins(self):
        self.make_repo()
        rc, out, _ = self.route("feature", "0.4.0")
        self.assertEqual(self.target(out), "integration/v0.4.0")

    def test_frozen_major_sends_features_to_successor_tip_and_fixes_to_major(self):
        self.make_repo(tags=[("v0.3.0-rc.1", "integration/v0.x")])
        rc, out, _ = self.route("feature", "0.5.0")
        self.assertEqual((rc, self.target(out)), (0, "integration/v0.4.0"))
        rc, out, _ = self.route("fix", "0.3.0")
        self.assertEqual((rc, self.target(out)), (0, "integration/v0.x"))

    def test_frozen_major_without_successor_needs_successor(self):
        self.make_repo(tags=[("v0.3.0-rc.1", "integration/v0.x")])
        p = example()
        e = entry(p)
        e["release_lines"]["lines"].pop(1)
        e["branches"].pop(1)
        rc, out, _ = self.route("feature", "0.5.0", p)
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("NEEDS_SUCCESSOR", self.codes(out))
        rc, out, _ = self.route("fix", "0.3.0", p)
        self.assertEqual((rc, self.target(out)), (0, "integration/v0.x"))

    def test_final_tag_unfreezes_the_major_line(self):
        self.make_repo(tags=[("v0.3.0-rc.1", "integration/v0.x"), ("v0.3.0", "integration/v0.x")])
        rc, out, _ = self.route("feature", "0.5.0")
        self.assertEqual(self.target(out), "integration/v0.x")

    def test_eol_and_retired_lines_refuse(self):
        self.make_repo()
        p = example()
        entry(p)["release_lines"]["lines"][1]["state"] = "retired"
        rc, out, _ = self.route("feature", "0.4.0", p)
        self.assertEqual(rc, 1)
        self.assertIn("LINE_RETIRED", self.codes(out))
        p = example()
        entry(p)["release_lines"]["lines"][0]["support"] = "eol"
        rc, out, _ = self.route("fix", "0.3.0", p)
        self.assertIn("EOL_LINE", self.codes(out))

    def test_converged_line_routes_to_its_target(self):
        self.make_repo()
        p = example()
        ln = entry(p)["release_lines"]["lines"][1]
        ln.update(state="converged", converged_into="v0")
        rc, out, _ = self.route("feature", "0.4.0", p)
        self.assertEqual(self.target(out), "integration/v0.x")

    def test_route_needs_a_clone_and_a_registered_repo(self):
        self.install(example())
        self.assertEqual(self.cli("route", "--kind", "fix", "--version", "0.3.0")[0], 2)
        self.make_repo()
        rc, out, _ = self.cli("route", "--kind", "fix", "--version", "0.3.0", "--clone", self.clone, repo="Nobody/none")
        self.assertIn("UNREGISTERED", self.codes(out))


class AuditTests(Base):
    def test_observe_reports_git_and_gh_facts_read_only(self):
        self.make_repo(tags=[("v0.3.0-rc.1", "integration/v0.x")])
        self.set_gh(prs=[{"number": 1, "baseRefName": "main", "headRefName": "promote/0.2.0", "state": "MERGED", "mergeCommit": None},
                         {"number": 2, "baseRefName": "integration/v0.x", "headRefName": "feat/x", "state": "OPEN", "mergeCommit": None},
                         {"number": 3, "baseRefName": "main", "headRefName": "feat/y", "state": "MERGED", "mergeCommit": None}])
        before = subprocess.run(["git", "-C", self.clone, "for-each-ref"], capture_output=True, text=True, env=self.env).stdout
        rc, out, _ = self.cli("audit", "observe", "--clone", self.clone, policy=False)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        ev = {s["step"]: s["evidence"] for s in out["steps"]}
        self.assertIn("integration/v0.4.0", ev["branches"]["names"])
        self.assertEqual(ev["tags"]["rc"]["examples"], ["v0.3.0-rc.1"])
        self.assertIn("v0.2.0", ev["tags"]["final"]["examples"])
        self.assertEqual(ev["doc_paths"]["paths"], ["docs/RELEASING.md"])
        self.assertEqual(ev["pr_base_counts"]["bases"], {"integration/v0.x": 1, "main": 2})
        self.assertEqual([p["number"] for p in ev["promotion_merges"]["prs"]], [1])
        after = subprocess.run(["git", "-C", self.clone, "for-each-ref"], capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(before, after)
        calls = pathlib.Path(str(self.gh_state) + ".calls").read_text().split("\n")
        self.assertEqual(set(filter(None, calls)), {"pr list"})

    def test_observe_works_offline_when_gh_fails(self):
        self.make_repo()
        self.set_gh(fail=True)
        rc, out, _ = self.cli("audit", "observe", "--clone", self.clone, policy=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.states(out)["pr_facts"], "NOT_RUN")
        self.assertEqual(self.states(out)["branches"], "OBSERVED")

    def test_observe_refuses_a_non_clone(self):
        rc, out, _ = self.cli("audit", "observe", "--clone", self.tmp.name, policy=False)
        self.assertEqual(rc, 1)
        self.assertIn("NOT_A_CLONE", self.codes(out))

    def audit_file(self, **over):
        a = example()
        body = {"schema": 1, "repo": REPO, "branches": entry(a)["branches"], "future_head_prefixes": entry(a)["future_head_prefixes"],
                "release_lines": entry(a)["release_lines"]}
        body.update(over)
        path = pathlib.Path(self.tmp.name) / "audit.json"
        path.write_text(json.dumps(body))
        return path

    def test_import_emits_candidate_that_completes_the_audit_and_unblocks_mutation(self):
        path = self.audit_file()
        rc, out, _ = self.cli("audit", "import", str(path), "--execute")  # no policy file at all: first registration
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        ev = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]
        cand = json.loads(pathlib.Path(ev["candidate_path"]).read_text())
        audit = entry(cand)["release_lines"]["audit"]
        self.assertEqual(audit, {"status": "COMPLETE", "source": "audit.json", "observed_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertFalse(self.policy_path.exists())
        self.install(cand)  # the owner installs it
        self.assertEqual(self.cli("registry", "validate")[0], 0)
        rc, out, _ = self.cli("registry", "propose", "--op", "eol", "--line", "v0", "--execute")
        self.assertEqual(rc, 0, out)

    def test_import_merges_into_an_existing_policy_without_dropping_other_repos(self):
        p = {"schema": 1, "repos": {"Other/plain": {"branches": [{"branch": "integration/mvp-1.0", "version": "1.0"}]}}}
        self.install(p)
        rc, out, _ = self.cli("audit", "import", str(self.audit_file()), "--execute")
        self.assertEqual(rc, 0, out)
        cand = json.loads(pathlib.Path(next(s for s in out["steps"] if s["step"] == "emit")["evidence"]["candidate_path"]).read_text())
        self.assertIn("Other/plain", cand["repos"])
        self.assertIn(REPO, cand["repos"])

    def test_import_rejects_bad_files_and_invalid_registries(self):
        bad = pathlib.Path(self.tmp.name) / "bad.json"
        bad.write_text("{")
        self.assertEqual(self.cli("audit", "import", str(bad))[0], 3)
        for over in ({"repo": "Else/where"}, {"schema": 2}):
            self.assertEqual(self.cli("audit", "import", str(self.audit_file(**over)))[0], 3, over)
        a = example()
        lines = entry(a)["release_lines"]
        lines["lines"][0]["integration_branch"] = "integration/unlisted"
        rc, out, _ = self.cli("audit", "import", str(self.audit_file(release_lines=lines)), "--execute")
        self.assertEqual(rc, 1)
        self.assertIn("INVALID_CANDIDATE", self.codes(out))
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
