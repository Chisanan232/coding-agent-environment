#!/usr/bin/env python3
"""Unit tests for scripts/owner-merge-gate.py with a fake gh runner (SPE-115).

Live behavior (the host classifier, a real merge) is covered by the acceptance runs recorded in
the ticket, not here.
"""
import importlib.util
import json
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location("omg", pathlib.Path(__file__).resolve().parents[1] / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)

HEAD = "a" * 40
OWNERS = ["Chisanan232", "horonomy", "ai-agent-assembly"]
REVIEW = f"OWNER-MERGE-REVIEW head={HEAD} verdict=PASS blocking=0 reviewer=independent-agent-1"


def green():
    return [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]


class Fake:
    """Answers the gh calls the gate makes from a mutable state dict."""

    def __init__(self, **over):
        self.pr = {"number": 7, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefOid": HEAD,
                   "author": {"login": "chisanan232"}, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                   "reviewDecision": "", "reviews": [], "isCrossRepository": False, "mergeCommit": None,
                   "comments": [{"author": {"login": "chisanan232"}, "body": REVIEW}],
                   "statusCheckRollup": green()}
        self.repo = {"nameWithOwner": "Chisanan232/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
                     "viewerPermission": "ADMIN", "owner": {"login": "Chisanan232"}}
        self.behind, self.threads, self.me, self.more_threads = 0, [], "chisanan232", False
        self.parents, self.cmp = [HEAD, "b" * 40], "ahead"
        for k, v in over.items():
            target = self.repo if k in self.repo else self.pr if k in self.pr else None
            if target is None:
                setattr(self, k, v)
            else:
                target[k] = v

    def __call__(self, *a):
        if a[:2] == ("repo", "view"):
            return json.dumps(self.repo)
        if a[:2] == ("api", "user"):
            return self.me + "\n"
        if a[:2] == ("pr", "view"):
            return json.dumps(self.pr)
        if a[0] == "api" and "/compare/" in a[1]:
            return (str(self.behind) if "behind_by" in a[3] else self.cmp) + "\n"
        if a[:2] == ("api", "graphql"):
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": self.more_threads}, "nodes": self.threads}}}}})
        if a[0] == "api" and "/commits/" in a[1]:
            return json.dumps(self.parents)
        raise AssertionError(a)


def run(fake, **kw):
    return omg.evaluate(fake, "Chisanan232/r", 7, OWNERS, **kw)


class Gate(unittest.TestCase):
    def refused(self, fake, code, **kw):
        v, reasons, _ = run(fake, **kw)
        self.assertEqual(v, "REFUSE", reasons)
        self.assertTrue(any(r.startswith(code) for r in reasons), reasons)

    def test_reviewed_green_pr_passes_with_pinned_merge_commit_command(self):
        v, reasons, facts = run(Fake())
        self.assertEqual((v, reasons), ("PASS", []))
        cmd = omg.merge_command(facts)
        self.assertIn("--merge", cmd)
        self.assertIn(f"--match-head-commit {HEAD}", cmd)
        self.assertNotIn("--admin", cmd)
        self.assertNotIn("--squash", cmd)

    def test_failed_required_ci_refuses(self):
        self.refused(Fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")

    def test_pending_ci_waits_not_passes(self):
        v, _, _ = run(Fake(statusCheckRollup=[{"name": "t", "status": "IN_PROGRESS", "conclusion": ""}]))
        self.assertEqual(v, "WAIT")

    def test_unresolved_review_thread_refuses(self):
        self.refused(Fake(threads=[{"isResolved": False, "isOutdated": False}]), "UNRESOLVED_REVIEW_THREADS")

    def test_outstanding_request_changes_refuses(self):
        self.refused(Fake(reviews=[{"state": "CHANGES_REQUESTED", "author": {"login": "x"}}]), "CHANGES_REQUESTED")

    def test_head_changed_after_review_refuses(self):
        self.refused(Fake(headRefOid="c" * 40), "REVIEW_STALE")

    def test_missing_review_record_refuses(self):
        self.refused(Fake(comments=[]), "NO_REVIEW_EVIDENCE")

    def test_review_record_from_another_user_does_not_count(self):
        self.refused(Fake(comments=[{"author": {"login": "someone-else"}, "body": REVIEW}]), "NO_REVIEW_EVIDENCE")

    def test_review_with_blocking_findings_refuses(self):
        body = REVIEW.replace("blocking=0", "blocking=2")
        self.refused(Fake(comments=[{"author": {"login": "chisanan232"}, "body": body}]), "REVIEW_NOT_CLEAN")

    def test_author_review_is_not_independent(self):
        body = REVIEW.replace("independent-agent-1", "coordinator")
        self.refused(Fake(comments=[{"author": {"login": "chisanan232"}, "body": body}]), "REVIEW_NOT_INDEPENDENT")

    def test_stale_branch_refuses_so_overlapping_lockfile_prs_serialize(self):
        self.refused(Fake(behind=1), "STALE_BRANCH")

    def test_conflicting_pr_refuses(self):
        self.refused(Fake(mergeable="CONFLICTING", mergeStateStatus="DIRTY"), "CONFLICTS")

    def test_required_formal_review_is_never_substituted(self):
        self.refused(Fake(reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED"), "FORMAL_APPROVAL_REQUIRED")

    def test_repository_rule_block_refuses(self):
        self.refused(Fake(mergeStateStatus="BLOCKED"), "BLOCKED_BY_REPOSITORY_RULES")

    def test_untrusted_owner_refuses_before_any_gh_call(self):
        v, reasons, _ = omg.evaluate(lambda *a: self.fail("gh called"), "evil/r", 7, OWNERS)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(reasons[0].startswith("UNAUTHORIZED_REPO"))

    def test_lookalike_owner_prefix_refuses(self):
        v, reasons, _ = omg.evaluate(lambda *a: self.fail("gh called"), "Chisanan2320/r", 7, OWNERS)
        self.assertEqual(v, "REFUSE")

    def test_fork_and_cross_repository_head_refuse(self):
        self.refused(Fake(isFork=True), "FORK_REPO")
        self.refused(Fake(isCrossRepository=True), "CROSS_REPOSITORY_HEAD")

    def test_non_default_and_deployment_bases_refuse(self):
        self.refused(Fake(baseRefName="release"), "BASE_NOT_DEFAULT_OR_DEPLOY")
        self.refused(Fake(baseRefName="feature-x"), "BASE_NOT_DEFAULT_OR_DEPLOY")

    def test_draft_and_closed_refuse(self):
        self.refused(Fake(isDraft=True), "DRAFT")
        self.refused(Fake(state="MERGED"), "NOT_OPEN")

    def test_no_checks_without_exception_refuses(self):
        self.refused(Fake(statusCheckRollup=[]), "NO_CI_EVIDENCE")

    def test_ci_unavailable_needs_flag_and_head_pinned_record(self):
        record = f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW}, {"author": {"login": "chisanan232"}, "body": record}]
        self.refused(Fake(statusCheckRollup=[], comments=comments), "NO_CI_EVIDENCE")  # flag missing
        v, _, facts = run(Fake(statusCheckRollup=[], comments=comments), ci_unavailable=True)
        self.assertEqual((v, facts["ci_exception"]), ("PASS", "billing"))
        stale = [comments[0], {"author": {"login": "chisanan232"}, "body": record.replace(HEAD, "d" * 40)}]
        self.refused(Fake(statusCheckRollup=[], comments=stale), "NO_CI_EVIDENCE", ci_unavailable=True)

    def test_ci_unavailable_never_excuses_a_genuine_failure(self):
        record = f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW}, {"author": {"login": "chisanan232"}, "body": record}]
        bad = [{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.refused(Fake(statusCheckRollup=bad, comments=comments), "CI_FAILED", ci_unavailable=True)

    def test_gate_has_no_mutating_gh_verbs(self):
        src = pathlib.Path(omg.__file__).read_text()
        for verb in ('"merge"', '"approve"', "--admin", "--auto", "--squash", "--force", '"comment"', '"review"'):
            self.assertNotIn(verb, src.split("def merge_command")[0] + src.split("def main")[1], verb)


class Verify(unittest.TestCase):
    def fake(self, state="MERGED", parents=None, cmp="ahead"):
        f = Fake(parents=parents or [HEAD, "b" * 40], cmp=cmp)
        f.pr = {"state": state, "mergeCommit": {"oid": "m" * 40} if state == "MERGED" else None, "baseRefName": "main"}
        return f

    def test_merge_commit_with_reviewed_head_parent_verifies(self):
        self.assertEqual(omg.verify(self.fake(), "Chisanan232/r", 7, HEAD)["verdict"], "VERIFIED")

    def test_squash_shaped_single_parent_is_a_mismatch(self):
        self.assertEqual(omg.verify(self.fake(parents=["b" * 40]), "Chisanan232/r", 7, HEAD)["verdict"], "MISMATCH")

    def test_head_not_a_parent_is_a_mismatch(self):
        self.assertEqual(omg.verify(self.fake(parents=["c" * 40, "b" * 40]), "Chisanan232/r", 7, HEAD)["verdict"], "MISMATCH")

    def test_open_pr_is_not_merged(self):
        self.assertEqual(omg.verify(self.fake(state="OPEN"), "Chisanan232/r", 7, HEAD)["verdict"], "NOT_MERGED")


if __name__ == "__main__":
    unittest.main()
