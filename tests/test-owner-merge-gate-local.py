#!/usr/bin/env python3
"""Scenarios A-H for scripts/owner-merge-gate.py against disposable LOCAL git repositories.

A fake `gh` answers from real git state (head sha, behind_by, merge parents), so freshness,
head pinning, sequencing and the post-merge check are exercised against genuine commits.
Merges here are performed by the test itself, standing in for the owner running the printed command;
the gate never merges. Nothing leaves the machine and no GitHub resource is touched.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("omg", pathlib.Path(__file__).resolve().parents[1] / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)
OWNERS = ["Chisanan232"]
REPO = "Chisanan232/disposable"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True, check=True).stdout.strip()


class LocalGh:
    def __init__(self, root):
        self.repo = str(root)
        git(root, "init", "-q", "-b", "main")
        (root / "uv.lock").write_text("v1\n")
        (root / "a.txt").write_text("a\n")
        git(root, "add", "."), git(root, "commit", "-qm", "base")
        self.prs, self.calls, self.merged = {}, [], {}

    def branch(self, n, name, path, text, checks="SUCCESS", review=True, comments=None, threads=None, reviews=None):
        git(self.repo, "checkout", "-q", "-b", name, "main")
        pathlib.Path(self.repo, path).write_text(text)
        git(self.repo, "commit", "-qam", f"change {path}")
        git(self.repo, "checkout", "-q", "main")
        pr = {"branch": name, "checks": checks, "comments": list(comments or []), "threads": threads or [],
              "reviews": reviews or [], "decision": ""}
        self.prs[n] = pr
        if review:
            self.record(n)

    def head(self, n):
        return git(self.repo, "rev-parse", self.prs[n]["branch"])

    def record(self, n, reviewer="independent-agent-1"):
        self.prs[n]["comments"].append({"author": {"login": "chisanan232"}, "body": omg.evidence_line(self.head(n), reviewer)})

    def push(self, n, path, text):
        git(self.repo, "checkout", "-q", self.prs[n]["branch"])
        pathlib.Path(self.repo, path).write_text(text)
        git(self.repo, "commit", "-qam", "late change")
        git(self.repo, "checkout", "-q", "main")

    def update_from_main(self, n):
        git(self.repo, "checkout", "-q", self.prs[n]["branch"])
        git(self.repo, "merge", "-q", "--no-edit", "main")
        git(self.repo, "checkout", "-q", "main")

    def owner_merges(self, n, command):
        """What the owner's `gh pr merge` would do; refuses a pin that is not the current head."""
        sha = command.split("--match-head-commit ")[1].split()[0]
        if sha != self.head(n):
            raise AssertionError("head moved since the gate ran; GitHub would reject --match-head-commit")
        assert "--merge" in command and "--squash" not in command and "--admin" not in command
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge PR {n}", self.prs[n]["branch"])
        self.merged[n] = git(self.repo, "rev-parse", "HEAD")

    def conflicts(self, n):
        r = subprocess.run(["git", "merge-tree", "--write-tree", "main", self.prs[n]["branch"]], cwd=self.repo, env=ENV, capture_output=True)
        return r.returncode != 0

    def __call__(self, *a):
        self.calls.append(a)
        omg.read_only(a)  # the fake enforces the same read-only contract the real runner does
        if a[:2] == ("repo", "view"):
            return json.dumps({"nameWithOwner": REPO, "defaultBranchRef": {"name": "main"}, "isFork": False,
                               "viewerPermission": "ADMIN", "owner": {"login": "Chisanan232"}})
        if a[:2] == ("api", "user"):
            return "chisanan232\n"
        n = int(a[2]) if a[:2] == ("pr", "view") else None
        if n is not None:
            pr = self.prs[n]
            merged = n in self.merged
            checks = [] if pr["checks"] is None else [{"name": "ci", "status": "COMPLETED" if pr["checks"] != "PENDING" else "IN_PROGRESS",
                                                     "conclusion": "" if pr["checks"] == "PENDING" else pr["checks"]}]
            return json.dumps({"number": n, "state": "MERGED" if merged else "OPEN", "isDraft": False, "baseRefName": "main",
                               "headRefName": pr["branch"], "headRefOid": self.head(n), "author": {"login": "chisanan232"},
                               "mergeable": "CONFLICTING" if not merged and self.conflicts(n) else "MERGEABLE",
                               "mergeStateStatus": "DIRTY" if not merged and self.conflicts(n) else "CLEAN",
                               "reviewDecision": pr["decision"], "reviews": pr["reviews"], "comments": pr["comments"],
                               "statusCheckRollup": checks, "isCrossRepository": False,
                               "mergeCommit": {"oid": self.merged[n]} if merged else None})
        if a[0] == "api" and "/compare/" in a[1]:
            rng = a[1].split("/compare/")[1]
            lo, hi = rng.split("...")
            if "behind_by" in a[3]:
                return git(self.repo, "rev-list", "--count", f"{hi}..{lo}") + "\n"
            anc = subprocess.run(["git", "merge-base", "--is-ancestor", lo, hi], cwd=self.repo, env=ENV).returncode == 0
            return ("identical" if git(self.repo, "rev-parse", lo) == git(self.repo, "rev-parse", hi) else "ahead" if anc else "diverged") + "\n"
        if a[:2] == ("api", "graphql"):
            pr = next(p for k, p in self.prs.items() if f"n={k}" in " ".join(a))
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": pr["threads"]}}}}})
        if a[0] == "api" and "/commits/" in a[1]:
            sha = a[1].split("/commits/")[1]
            return json.dumps(git(self.repo, "rev-list", "--parents", "-n1", sha).split()[1:])
        raise AssertionError(a)


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gh = LocalGh(pathlib.Path(os.path.realpath(self.tmp.name)))

    def check(self, n, **kw):
        return omg.evaluate(self.gh, REPO, n, OWNERS, **kw)

    def codes(self, n, **kw):
        v, reasons, _ = self.check(n, **kw)
        return v, [r.split(":")[0] for r in reasons]

    def test_A_reviewed_green_pr_passes_and_post_merge_verifies(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n")
        v, reasons, facts = self.check(1)
        self.assertEqual((v, reasons), ("PASS", []))
        self.assertFalse(facts["formal_github_approval"])
        self.assertIn("not a GitHub approval", facts["review_kind"])
        cmd = omg.merge_command(facts)
        self.gh.owner_merges(1, cmd)
        out = omg.verify(self.gh, REPO, 1, facts["head"])
        self.assertEqual(out["verdict"], "VERIFIED")  # H: resulting merge commit, ancestry, base
        self.assertTrue(out["head_is_parent"] and out["merge_commit_on_base"])

    def test_B_failing_required_ci_refuses_and_nothing_merges(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n", checks="FAILURE")
        self.assertEqual(self.codes(1), ("REFUSE", ["CI_FAILED"]))
        self.assertEqual(git(self.gh.repo, "rev-list", "--count", "main"), "1")

    def test_C_unresolved_blocking_review_refuses(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n", threads=[{"isResolved": False, "isOutdated": False}])
        self.assertEqual(self.codes(1), ("REFUSE", ["UNRESOLVED_REVIEW_THREADS"]))
        self.gh.prs[1]["threads"] = []
        self.gh.prs[1]["reviews"] = [{"state": "CHANGES_REQUESTED", "author": {"login": "rev"}}]
        self.assertEqual(self.codes(1)[0], "REFUSE")

    def test_D_head_change_after_review_refuses_until_revalidated(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n")
        self.assertEqual(self.check(1)[0], "PASS")
        stale_cmd = omg.merge_command(self.check(1)[2])
        self.gh.push(1, "a.txt", "sneaky\n")
        self.assertEqual(self.codes(1), ("REFUSE", ["REVIEW_STALE"]))
        with self.assertRaises(AssertionError):  # the old pinned command no longer matches the head
            self.gh.owner_merges(1, stale_cmd)
        self.gh.record(1)
        self.assertEqual(self.check(1)[0], "PASS")

    def test_E_overlapping_lockfile_prs_are_strictly_sequential(self):
        self.gh.branch(1, "feat/lock-one", "uv.lock", "v2\n")
        self.gh.branch(2, "feat/lock-two", "uv.lock", "v3\n")
        # two real conflicting edits of one line: the second must not pass after the first merges
        self.assertEqual(self.check(1)[0], "PASS")
        self.gh.owner_merges(1, omg.merge_command(self.check(1)[2]))
        v, codes = self.codes(2)
        self.assertEqual(v, "REFUSE")
        self.assertIn("CONFLICTS", codes)
        self.assertIn("STALE_BRANCH", codes)

    def test_E2_non_conflicting_second_pr_is_stale_until_updated_and_rereviewed(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n")
        self.gh.branch(2, "feat/two", "uv.lock", "v2\n")
        self.gh.owner_merges(1, omg.merge_command(self.check(1)[2]))
        self.assertEqual(self.codes(2), ("REFUSE", ["STALE_BRANCH"]))
        self.gh.update_from_main(2)
        self.assertEqual(self.codes(2), ("REFUSE", ["REVIEW_STALE"]))
        self.gh.record(2)
        self.assertEqual(self.check(2)[0], "PASS")

    def test_F_unavailable_ci_is_a_bounded_exception_never_a_pass_of_checks(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n", checks=None)
        self.assertEqual(self.codes(1), ("REFUSE", ["NO_CI_EVIDENCE"]))
        record = f"OWNER-MERGE-CI-UNAVAILABLE head={self.gh.head(1)} cause=billing local_equivalent=PASS"
        self.gh.prs[1]["comments"].append({"author": {"login": "chisanan232"}, "body": record})
        self.assertEqual(self.codes(1), ("REFUSE", ["NO_CI_EVIDENCE"]))  # still refused without the explicit flag
        v, _, facts = self.check(1, ci_unavailable=True)
        self.assertEqual((v, facts["checks"], facts["ci_exception"]), ("PASS", 0, "billing"))
        self.gh.push(1, "a.txt", "late\n")
        self.gh.record(1)  # fresh review, but the CI exception record is for the old head
        self.assertEqual(self.codes(1, ci_unavailable=True), ("REFUSE", ["NO_CI_EVIDENCE"]))

    def test_G_gate_never_offers_an_alternate_route_and_makes_no_write_calls(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n")
        import contextlib
        import io
        refs_before = git(self.gh.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = omg.main(["check", "--repo", REPO, "--pr", "1", "--owners", "Chisanan232"], run=self.gh)
        out = json.loads(buf.getvalue())
        self.assertEqual((rc, out["verdict"]), (0, "PASS"))
        for flag in ("--admin", "--auto", "--squash", "--rebase", "--force"):
            self.assertNotIn(flag, buf.getvalue())
        verbs = {a[:2] for a in self.gh.calls}
        self.assertTrue(verbs <= {("repo", "view"), ("pr", "view"), ("api", "user"), ("api", "graphql")} | {c[:2] for c in self.gh.calls if c[1].startswith("repos/")})
        self.assertEqual(git(self.gh.repo, "for-each-ref", "--format=%(refname) %(objectname)"), refs_before)  # no ref moved or created

    def test_read_only_guard_rejects_writes(self):
        for call in (("pr", "merge", "1"), ("pr", "review", "1", "--approve"), ("pr", "comment", "1"), ("api", "repos/o/r/statuses/x", "-f", "state=success"),
                     ("api", "repos/o/r/pulls/1/merge", "-X", "PUT"), ("api", "graphql", "-f", "query=mutation{x}"), ("api", "-X", "POST", "repos/o/r")):
            with self.assertRaises(RuntimeError, msg=call):
                omg.read_only(call)

    def test_unknown_or_unstable_state_fails_closed(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n")
        orig = self.gh.__call__
        self.gh.prs[1]["checks"] = "SUCCESS"
        v, reasons, _ = omg.evaluate(lambda *a: (lambda o: json.dumps({**json.loads(o), "mergeStateStatus": "UNSTABLE"}) if a[:2] == ("pr", "view") else o)(orig(*a)), REPO, 1, OWNERS)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(reasons[0].startswith("MERGE_STATE_NOT_CLEAN"))

    def test_queue_report_lists_only_the_first_command_as_runnable(self):
        self.gh.branch(1, "feat/one", "a.txt", "one\n")
        self.gh.branch(2, "feat/two", "uv.lock", "v2\n")
        text = omg.queue_report(self.gh, REPO, [1, 2], OWNERS)
        self.assertIn("#1 PASS", text)
        self.assertIn("  run: gh pr merge 1", text)
        self.assertIn("re-run check", text)

    def test_evidence_line_rejects_bad_input(self):
        for bad in (("abc", "r-1"), ("a" * 40, "x y"), ("A" * 40, "reviewer1")):
            with self.assertRaises(ValueError):
                omg.evidence_line(*bad)


if __name__ == "__main__":
    unittest.main()
