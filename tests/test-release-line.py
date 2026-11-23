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

    def cli(self, *args, repo=REPO, policy=True, state_dir=None):
        argv = [sys.executable, "-I", str(CLI), *args, "--repo", repo, "--state-dir", str(state_dir or self.state_dir)]
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
                            (("eol", "--line", "v0.4.0"), lambda ln: ln["support"] == "eol"),
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
        body = {"schema": 1, "repo": REPO, "release_lines": entry(a)["release_lines"]}
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
        rc, out, _ = self.cli("registry", "propose", "--op", "eol", "--line", "v0.4.0", "--execute")
        self.assertEqual(rc, 0, out)

    def test_import_lists_exactly_the_gate_relevant_additions(self):
        rc, out, _ = self.cli("audit", "import", str(self.audit_file()), "--execute")
        self.assertEqual(rc, 0, out)
        adds = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]["gate_relevant_additions"]
        self.assertEqual(adds, {"branches": ["integration/v0.x", "integration/v0.4.0"], "prefixes": ["promote/"]})
        ev = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]
        scope = json.loads(pathlib.Path(ev["authority_path"]).read_text())["scope"]
        for name in ("integration/v0.x", "integration/v0.4.0", "promote/", "GATE-RELEVANT"):
            self.assertIn(name, scope)
        cand = json.loads(pathlib.Path(ev["candidate_path"]).read_text())
        self.assertEqual(entry(cand)["future_head_prefixes"], ["promote/"])

    def test_import_refuses_smuggled_branches_prefixes_and_release_segments(self):
        for over, code in (({"branches": [{"branch": "integration/evil", "version": "9"}]}, "AUDIT_CARRIES_GATE_KEYS"),
                           ({"future_head_prefixes": ["evil/"]}, "AUDIT_CARRIES_GATE_KEYS")):
            with self.subTest(code, over=list(over)):
                rc, out, _ = self.cli("audit", "import", str(self.audit_file(**over)), "--execute")
                self.assertEqual((rc, out["status"]), (1, "FAIL"))
                self.assertIn(code, self.codes(out))
                self.assertFalse(self.state_dir.exists())
        a = example()
        rl = entry(a)["release_lines"]
        rl["lines"][0]["integration_branch"] = "release/v0.x"
        rc, out, _ = self.cli("audit", "import", str(self.audit_file(release_lines=rl)), "--execute")
        self.assertEqual((rc, out["status"]), (1, "FAIL"))
        self.assertIn("RELEASE_SEGMENT", self.codes(out))
        self.assertFalse(self.state_dir.exists())

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
        lines["lines"][0]["support"] = "maybe"
        rc, out, _ = self.cli("audit", "import", str(self.audit_file(release_lines=lines)), "--execute")
        self.assertEqual(rc, 1)
        self.assertIn("INVALID_CANDIDATE", self.codes(out))
        self.assertFalse(self.state_dir.exists())


def add_successor(p, lid, version, pred, pred_version, branch=None, **over):
    e = entry(p)
    branch = branch or f"integration/{lid}"
    e["branches"].append({"branch": branch, "version": version})
    line = {"id": lid, "kind": "successor", "version": version, "integration_branch": branch,
            "predecessor": {"line": pred, "version": pred_version, "fork_ref": "v0.3.0-rc.2", "fork_sha": "3" * 40},
            "publication_base": {"branch": "main", "kind": "default"}, "support": "supported", "state": "active"}
    line.update(over)
    e["release_lines"]["lines"].append(line)
    return line


class IntegrityTests(Base):
    """Registry integrity rules, version grammar, tag formats (review findings 2, 5, 6, 7)."""

    def invalid(self, mutate, code):
        p = example()
        mutate(p)
        self.install(p)
        rc, out, _ = self.cli("registry", "validate")
        self.assertEqual((rc, out["status"]), (3, "FAIL"), out)
        self.assertIn(code, self.codes(out))

    def test_converged_into_self_and_cycles_are_rejected_not_recursed(self):
        def selfref(p):
            entry(p)["release_lines"]["lines"][1].update(state="converged", converged_into="v0.4.0")

        def cycle(p):
            add_successor(p, "v0.5.0", "0.5.0", "v0.4.0", "0.4.0", state="converged", converged_into="v0.4.0")
            entry(p)["release_lines"]["lines"][1].update(state="converged", converged_into="v0.5.0")
        self.invalid(selfref, "BAD_STATE")
        self.invalid(cycle, "BAD_STATE")

    def test_converged_into_must_not_be_retired(self):
        def mutate(p):
            add_successor(p, "v0.5.0", "0.5.0", "v0.4.0", "0.4.0", state="retired")
            entry(p)["release_lines"]["lines"][1].update(state="converged", converged_into="v0.5.0")
        self.invalid(mutate, "BAD_STATE")

    def test_tag_format_placeholders_and_ambiguity(self):
        for tf, cf in (("v{version}{version}", "v{version}-rc.{n}"), ("v{version}-{n}", "v{version}-rc.{n}"),
                       ("v{version}", "v{version}-rc.{n}.{n}"), ("v{version}", "v{version}{n}"), ("v{version}", "v{version}-{x}-{n}")):
            with self.subTest(tf=tf, cf=cf):
                def mutate(p, tf=tf, cf=cf):
                    entry(p)["release_lines"].update(tag_format=tf, candidate_format=cf)
                self.invalid(mutate, "BAD_FIELD")

    def test_two_part_versions_for_mvp_products_and_frozen_detection(self):
        def mvp(p):
            rl = entry(p)["release_lines"]
            rl.update(tag_format="MVP-{version}", candidate_format="MVP-{version}-rc.{n}")
            rl["lines"][0]["major"] = 2
            rl["lines"][1].update(version="2.1")
            rl["lines"][1]["predecessor"].update(version="2.0")
            entry(p)["branches"][1]["version"] = "2.1"
            entry(p)["branches"].append({"branch": "integration/mvp-5.0", "version": "5.0"})
            rl["lines"].append({"id": "mvp-5", "kind": "version", "version": "5.0", "integration_branch": "integration/mvp-5.0",
                                "publication_base": {"branch": "main", "kind": "default"}, "support": "supported"})
            return p
        self.install(mvp(example()))
        self.assertEqual(self.cli("registry", "validate")[0], 0)
        self.make_repo(tags=[("MVP-2.0-rc.1", "integration/v0.x")], extra_branches=("integration/mvp-5.0",))
        rc, out, _ = self.cli("route", "--kind", "feature", "--version", "2.2", "--clone", self.clone)
        self.assertEqual(next(s for s in out["steps"] if s["step"] == "route")["evidence"]["branch"], "integration/v0.4.0")
        rc, out, _ = self.cli("route", "--kind", "feature", "--version", "5.0", "--clone", self.clone)
        self.assertEqual(next(s for s in out["steps"] if s["step"] == "route")["evidence"]["branch"], "integration/mvp-5.0")
        subprocess.run(["git", "-C", self.clone, "tag", "MVP-2.0", "origin/integration/v0.x"], check=True, env=self.env)
        rc, out, _ = self.cli("route", "--kind", "feature", "--version", "2.2", "--clone", self.clone)
        self.assertEqual(next(s for s in out["steps"] if s["step"] == "route")["evidence"]["branch"], "integration/v0.x")

    def test_integrity_rules(self):
        cases = {
            "DUPLICATE_BRANCH": lambda p: add_successor(p, "v0.5.0", "0.5.0", "v0.4.0", "0.4.0", branch="integration/v0.4.0"),
            "DUPLICATE_VERSION": lambda p: add_successor(p, "other", "0.4.0", "v0", "0.3.0"),
            "DEFAULT_BASE_LISTED": lambda p: entry(p)["branches"].append({"branch": "main", "version": "m"}),
            "BAD_PREDECESSOR": lambda p: entry(p)["release_lines"]["lines"][1]["predecessor"].update(version="0.4.0"),
        }
        for code, mutate in cases.items():
            with self.subTest(code):
                self.invalid(mutate, code)

    def test_default_base_listed_fails_even_when_gate_accepts(self):
        def mutate(p):
            entry(p)["branches"].append({"branch": "main", "version": "m"})
        self.invalid(mutate, "DEFAULT_BASE_LISTED")

    def test_unreadable_tag_list_is_refused_not_treated_as_empty(self):
        spec = importlib.util.spec_from_file_location("rlmod", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(mod.Refusal) as cm:
                mod.tag_names(d)
        self.assertEqual(cm.exception.code, "TAG_LIST_FAILED")

    def test_default_branch_cannot_be_an_integration_branch(self):
        def mutate(p):
            entry(p)["release_lines"]["lines"][0]["integration_branch"] = "master"
            entry(p)["branches"].append({"branch": "master", "version": "m"})
        self.invalid(mutate, "PROTECTED_BRANCH")

    def test_predecessor_cycle_and_unrooted_chain(self):
        def cycle(p):
            add_successor(p, "a", "0.6.0", "b", "0.5.0")
            add_successor(p, "b", "0.7.0", "a", "0.6.0")
        def unrooted(p):
            entry(p)["release_lines"]["lines"].append({"id": "mvp-5", "kind": "version", "version": "5.0", "integration_branch": "integration/v0.x",
                                                       "publication_base": {"branch": "main", "kind": "default"}, "support": "supported"})
            entry(p)["release_lines"]["lines"][-1]["integration_branch"] = "integration/mvp-5.0"
            entry(p)["branches"].append({"branch": "integration/mvp-5.0", "version": "5.0"})
            add_successor(p, "v5.1", "5.1", "mvp-5", "5.0")
        self.invalid(cycle, "BAD_PREDECESSOR")
        self.invalid(unrooted, "BAD_PREDECESSOR")

    def test_major_with_predecessor_and_bad_fork_refs(self):
        self.invalid(lambda p: entry(p)["release_lines"]["lines"][0].update(predecessor={"line": "v0.4.0"}), "BAD_FIELD")
        for ref in ("-x", "a..b", "a\nb", "a b", "x.lock", "", "a/"):
            with self.subTest(ref=ref):
                self.invalid(lambda p, ref=ref: entry(p)["release_lines"]["lines"][1]["predecessor"].update(fork_ref=ref), "BAD_PREDECESSOR")
        self.invalid(lambda p: entry(p)["release_lines"]["lines"][1]["predecessor"].update(fork_sha="G" * 40), "BAD_PREDECESSOR")

    def test_unexpected_shape_gives_an_envelope_not_a_traceback(self):
        self.install({"schema": 1, "repos": {REPO: []}})
        rc, out, err = self.cli("registry", "validate")
        self.assertEqual((rc, out["status"]), (3, "FAIL"))
        self.assertIn("UNEXPECTED_SHAPE", self.codes(out))
        self.assertNotIn("Traceback", err)

    def test_repo_name_is_validated_for_every_command(self):
        self.install(example())
        for repo in ("host.example/Owner/repo", "norepo", "a/b/c", "--x/y"):
            for args in (("registry", "validate"), ("audit", "observe", "--clone", self.tmp.name)):
                with self.subTest(repo=repo, args=args[:2]):
                    rc, out, _ = self.cli(*args, repo=repo)
                    self.assertEqual((rc, out), (2, None))


class RouteClone(Base):
    """route must refuse rather than guess when the clone cannot answer (review finding 1)."""

    def route(self, clone, kind="feature", version="0.5.0"):
        self.install(example())
        return self.cli("route", "--kind", kind, "--version", version, "--clone", clone)

    def test_missing_and_non_git_clone_are_refused(self):
        for clone in (os.path.join(self.tmp.name, "nowhere"), self.tmp.name):
            with self.subTest(clone=clone):
                rc, out, _ = self.route(clone)
                self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
                self.assertIn("NOT_A_CLONE", self.codes(out))

    def test_branch_missing_from_clone_is_refused(self):
        self.make_repo(tags=[("v0.3.0-rc.1", "integration/v0.x")])
        for ref in ("refs/remotes/origin/integration/v0.x", "refs/heads/integration/v0.x"):
            subprocess.run(["git", "-C", self.clone, "update-ref", "-d", ref], check=True, env=self.env)
        rc, out, _ = self.route(self.clone)
        self.assertEqual((rc, out["status"]), (1, "BLOCKED"))
        self.assertIn("BRANCH_NOT_IN_CLONE", self.codes(out))

    def test_fetched_remote_ref_beats_a_stale_local_head(self):
        self.make_repo()
        env = dict(self.env, GIT_AUTHOR_DATE=DATE, GIT_COMMITTER_DATE=DATE, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.x")
        g = lambda *a: subprocess.run(["git", "-C", self.clone, *a], check=True, env=env, capture_output=True)
        g("checkout", "-q", "integration/v0.x")
        (pathlib.Path(self.clone) / "b.txt").write_text("b\n")
        g("add", ".")
        g("commit", "-q", "-m", "work")
        g("tag", "v0.3.0-rc.1")
        g("push", "-q", "origin", "integration/v0.x")
        g("checkout", "-q", "main")
        g("branch", "-f", "integration/v0.x", "main")  # local head is now stale, behind the fetched origin ref
        rc, out, _ = self.route(self.clone)
        self.assertEqual(rc, 0, out)
        self.assertEqual(next(s for s in out["steps"] if s["step"] == "route")["evidence"]["branch"], "integration/v0.4.0")


class ProposalSafety(Base):
    """State directory confinement, modes and bounded input (review findings 3, 8, 13, 14)."""

    def propose(self, *extra, state_dir=None):
        return self.cli("registry", "propose", "--op", "eol", "--line", "v0.4.0", "--execute", *extra, state_dir=state_dir)

    def test_state_dir_inside_owner_config_is_refused(self):
        self.install(example())
        before = sorted(p.name for p in self.local.iterdir())
        rc, out, _ = self.propose(state_dir=self.local / "state")
        self.assertEqual(rc, 1)
        self.assertIn("STATE_DIR_UNSAFE", self.codes(out))
        self.assertEqual(sorted(p.name for p in self.local.iterdir()), before)

    def test_symlinked_state_or_proposals_dir_is_refused(self):
        self.install(example())
        target = pathlib.Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        (self.state_dir).mkdir()
        (self.state_dir / "proposals").symlink_to(target)
        rc, out, _ = self.propose()
        self.assertEqual(rc, 1)
        self.assertIn("STATE_DIR_UNSAFE", self.codes(out))
        link = pathlib.Path(self.tmp.name) / "linked-state"
        link.symlink_to(target)
        rc, out, _ = self.propose(state_dir=link)
        self.assertIn("STATE_DIR_UNSAFE", self.codes(out))
        self.assertEqual(list(target.iterdir()), [])

    def test_modes_are_forced_and_loose_preexisting_files_do_not_survive(self):
        self.install(example())
        rc, out, _ = self.propose()
        ev = next(s for s in out["steps"] if s["step"] == "emit")["evidence"]
        d = pathlib.Path(ev["candidate_path"]).parent
        os.chmod(d, 0o777)
        for p in (ev["candidate_path"], ev["authority_path"]):
            os.chmod(p, 0o666)
        rc, out, _ = self.propose()
        self.assertEqual(rc, 0, out)
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        for p in (ev["candidate_path"], ev["authority_path"]):
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertEqual([x.name for x in d.iterdir() if x.name.endswith(".tmp")], [])

    def test_noop_propose_emits_no_packet(self):
        p = example()
        entry(p)["release_lines"]["lines"][1].update(state="retired")
        self.install(p)
        rc, out, _ = self.cli("registry", "propose", "--op", "retire", "--line", "v0.4.0", "--execute")
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        self.assertIn("NO_OP", self.codes(out))
        self.assertFalse(self.state_dir.exists())
        p = example()
        entry(p)["release_lines"]["lines"][1].update(support="eol")
        self.install(p)
        rc, out, _ = self.cli("registry", "propose", "--op", "eol", "--line", "v0.4.0", "--execute")
        self.assertIn("NO_OP", self.codes(out))
        self.assertFalse(self.state_dir.exists())

    def test_retire_or_eol_of_a_line_with_active_dependents_is_refused(self):
        p = example()
        add_successor(p, "v0.5.0", "0.5.0", "v0.4.0", "0.4.0")
        self.install(p)
        for op, line in (("retire", "v0.4.0"), ("eol", "v0.4.0"), ("eol", "v0")):
            with self.subTest(op=op, line=line):
                rc, out, _ = self.cli("registry", "propose", "--op", op, "--line", line, "--execute")
                self.assertEqual(rc, 1)
                self.assertIn("HAS_ACTIVE_DEPENDENTS", self.codes(out))
        self.assertFalse(self.state_dir.exists())

    def test_add_successor_refuses_an_inactive_predecessor(self):
        for mutate in (lambda ln: ln.update(support="eol"), lambda ln: ln.update(state="retired")):
            p = example()
            mutate(entry(p)["release_lines"]["lines"][1])
            self.install(p)
            rc, out, _ = self.cli("registry", "propose", "--op", "add-successor", "--version", "0.5.0", "--predecessor", "v0.4.0",
                                  "--predecessor-version", "0.4.0", "--fork-ref", "v0.4.0-rc.1", "--fork-sha", "2" * 40, "--execute")
            self.assertEqual(rc, 1)
            self.assertIn("PREDECESSOR_NOT_ACTIVE", self.codes(out))

    def audit_with(self, data):
        path = pathlib.Path(self.tmp.name) / "a.json"
        path.write_bytes(data)
        return path

    def test_audit_file_must_be_small_and_regular(self):
        a = example()
        body = json.dumps({"schema": 1, "repo": REPO, "release_lines": entry(a)["release_lines"]}).encode()
        rc, out, _ = self.cli("audit", "import", str(self.audit_with(body + b" " * (1 << 20))))
        self.assertEqual(rc, 3)
        self.assertIn("too large", json.dumps(out))
        fifo = pathlib.Path(self.tmp.name) / "fifo.json"
        os.mkfifo(fifo)
        rc, out, _ = self.cli("audit", "import", str(fifo))
        self.assertEqual(rc, 3)
        self.assertIn("regular file", json.dumps(out))
        real = self.audit_with(body)
        link = pathlib.Path(self.tmp.name) / "link.json"
        link.symlink_to(real)
        self.assertEqual(self.cli("audit", "import", str(link))[0], 3)
        self.assertEqual(self.cli("audit", "import", str(real))[0], 0)


if __name__ == "__main__":
    unittest.main()
