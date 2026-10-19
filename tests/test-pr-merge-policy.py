#!/usr/bin/env python3
"""PR class A/B/C policy tests for scripts/owner-merge-gate.py (SPE-116). Synthetic data, fake gh only."""
import base64
import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


base = load("base_tests", "tests/test-owner-merge-gate.py")
omg = base.omg
HEAD, OWNERS = base.HEAD, base.OWNERS
REPO = "Chisanan232/r"
ME = "chisanan232"


class Fake(base.Fake):
    """Adds CODEOWNERS reads on top of the shared fake."""

    def __init__(self, codeowners=None, **over):
        super().__init__(**over)
        self.codeowners = codeowners
        self.pr.setdefault("files", [{"path": "src/app.py"}])
        self.pr.setdefault("commits", [{"authors": [{"login": ME}]}])

    def __call__(self, *a):
        if a[0] == "api" and a[1].startswith(f"repos/{REPO}/contents/"):
            if self.codeowners is None or not a[1].endswith(".github/CODEOWNERS"):
                raise RuntimeError("404")
            return base64.b64encode(self.codeowners.encode()).decode()
        return super().__call__(*a)


def bot(login="dependabot[bot]", **over):
    f = Fake(**over)
    f.pr.update(author={"login": login, "is_bot": True}, headRefName="dependabot/npm_and_yarn/x-1.2.3",
                comments=[{"author": {"login": ME}, "body": base.REVIEW}],
                files=[{"path": "package.json"}, {"path": "package-lock.json"}],
                commits=[{"authors": [{"login": "dependabot[bot]"}]}])
    f.pr.update({k: v for k, v in over.items() if k in f.pr})
    return f


def third_party(**over):
    f = Fake(**over)
    f.pr.update(author={"login": "someone-else", "is_bot": False}, headRefName="contrib/fix")
    return f


def approval(who=ME, oid=HEAD, state="APPROVED"):
    return {"state": state, "author": {"login": who}, "commit": {"oid": oid}}


class Case(unittest.TestCase):
    def check(self, f, **kw):
        return omg.evaluate(f, REPO, 7, OWNERS, **kw)

    def refused(self, f, code, **kw):
        v, why, facts = self.check(f, **kw)
        self.assertEqual(v, "REFUSE", (v, why))
        self.assertTrue(any(code in w for w in why), (code, why))
        return facts


class ClassA(Case):
    def test_owner_admin_passes(self):
        v, _, facts = self.check(Fake())
        self.assertEqual((v, facts["class"], facts["authority"]), ("PASS", "A", "repository administrator"))
        self.assertNotIn("--admin", omg.merge_command(facts))

    def test_code_owner_with_write_passes(self):
        v, _, facts = self.check(Fake(viewerPermission="WRITE", codeowners="* @chisanan232\n"))
        self.assertEqual((v, facts["authority"]), ("PASS", "verified code owner with write permission"))

    def test_code_owner_without_merge_permission_refuses(self):
        for perm in ("READ", "TRIAGE"):
            self.refused(Fake(viewerPermission=perm, codeowners="* @chisanan232\n"), "NOT_REPO_ADMIN")

    def test_code_ownership_must_cover_every_file_and_be_a_person(self):
        self.refused(Fake(viewerPermission="WRITE", codeowners="docs/ @chisanan232\n"), "NOT_REPO_ADMIN")
        self.refused(Fake(viewerPermission="WRITE", codeowners="* @Chisanan232/team\n"), "NOT_REPO_ADMIN")
        self.refused(Fake(viewerPermission="WRITE", codeowners="* @other\nsrc/ @chisanan232 @other\n*.py @other\n"), "NOT_REPO_ADMIN")
        self.refused(Fake(viewerPermission="WRITE", codeowners="src/[a]* @chisanan232\n"), "NOT_REPO_ADMIN")
        self.refused(Fake(viewerPermission="WRITE", codeowners=None), "NOT_REPO_ADMIN")

    def test_pr_editing_codeowners_cannot_grant_itself_ownership(self):
        f = Fake(viewerPermission="WRITE", codeowners="* @chisanan232\n")
        f.pr["files"] = [{"path": ".github/CODEOWNERS"}]
        self.refused(f, "NOT_REPO_ADMIN")

    def test_failing_ci_refuses(self):
        self.refused(Fake(statusCheckRollup=[{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")

    def test_stale_review_after_head_change_refuses(self):
        self.refused(Fake(headRefOid="b" * 40), "REVIEW_STALE")

    def test_unresolved_threads_refuse(self):
        self.refused(Fake(threads=[{"isResolved": False, "isOutdated": False}]), "UNRESOLVED_REVIEW_THREADS")


class Unverified(Case):
    def test_missing_or_ghost_author_fails_closed(self):
        for author in (None, {}, {"login": ""}, {"login": "ghost", "is_bot": False}):
            f = Fake()
            f.pr["author"] = author
            self.refused(f, "UNVERIFIED_AUTHOR")


class ClassC(Case):
    def test_without_owner_approval_refuses_even_with_agent_review_comment(self):
        facts = self.refused(third_party(), "THIRD_PARTY_NEEDS_OWNER_APPROVAL")
        self.assertEqual(facts["class"], "C")

    def test_other_reviewers_approval_does_not_count(self):
        self.refused(third_party(reviews=[approval("some-maintainer")]), "THIRD_PARTY_NEEDS_OWNER_APPROVAL")

    def test_valid_owner_approval_passes_without_admin(self):
        v, _, facts = self.check(third_party(reviews=[approval()]))
        self.assertEqual((v, facts["class"]), ("PASS", "C"))
        self.assertNotIn("--admin", omg.merge_command(facts))
        self.assertIn("never", facts["admin_merge"])

    def test_approval_for_an_older_head_is_stale(self):
        self.refused(third_party(reviews=[approval(oid="c" * 40)]), "OWNER_APPROVAL_STALE")

    def test_approval_without_a_commit_id_is_not_provable(self):
        self.refused(third_party(reviews=[{"state": "APPROVED", "author": {"login": ME}}]), "OWNER_APPROVAL_STALE")

    def test_later_changes_requested_or_dismissal_revokes_approval(self):
        self.refused(third_party(reviews=[approval(), approval(state="CHANGES_REQUESTED")]), "CHANGES_REQUESTED")
        self.refused(third_party(reviews=[approval(), approval(state="DISMISSED")]), "OWNER_APPROVAL_STALE")

    def test_approval_still_needs_every_other_gate(self):
        self.refused(third_party(reviews=[approval()], statusCheckRollup=[{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")
        self.refused(third_party(reviews=[approval()], threads=[{"isResolved": False}]), "UNRESOLVED_REVIEW_THREADS")

    def test_reviewer_without_merge_permission_refuses(self):
        self.refused(third_party(reviews=[approval()], viewerPermission="READ"), "NO_MERGE_PERMISSION")


class OwnerIdentity(Case):
    def org_fake(self, builder, role, **over):
        f = builder(nameWithOwner="horonomy/r", owner={"login": "horonomy"}, org_role=role, **over)
        return f

    def test_write_collaborator_is_not_the_owner_for_class_b_or_c(self):
        for builder in (third_party, bot):
            f = self.org_fake(builder, "member active", reviews=[approval()])
            v, why, _ = omg.evaluate(f, "horonomy/r", 7, OWNERS)
            self.assertEqual(v, "REFUSE", builder.__name__)
            self.assertTrue(any("OWNER_IDENTITY_NOT_VERIFIED" in w for w in why), why)

    def test_org_admin_counts_as_the_owner(self):
        f = self.org_fake(third_party, "admin active", reviews=[approval()])
        self.assertEqual(omg.evaluate(f, "horonomy/r", 7, OWNERS)[0], "PASS")

    def test_inactive_or_pending_org_admin_does_not(self):
        f = self.org_fake(third_party, "admin pending", reviews=[approval()])
        self.assertEqual(omg.evaluate(f, "horonomy/r", 7, OWNERS)[0], "REFUSE")


class ClassB(Case):
    def test_verified_bot_waits_for_real_approval_then_passes(self):
        v, why, facts = self.check(bot())
        self.assertEqual((v, facts["class"], facts["needs_approval"]), ("WAIT", "B", True))
        self.assertIn("gh pr review 7", facts["approve_command"])
        self.assertIn("--approve", facts["approve_command"])
        self.assertNotIn("merge_command", facts)
        v, _, facts = self.check(bot(reviews=[approval()]))
        self.assertEqual((v, facts["class"]), ("PASS", "B"))

    def test_approve_is_offered_only_after_other_gates_pass(self):
        v, _, facts = self.check(bot(statusCheckRollup=[{"name": "ci", "status": "IN_PROGRESS"}]))
        self.assertEqual(v, "WAIT")
        self.assertNotIn("approve_command", facts)
        v, _, facts = self.check(bot(statusCheckRollup=[{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]))
        self.assertEqual(v, "REFUSE")
        self.assertNotIn("approve_command", facts)

    def test_approval_for_older_head_is_not_enough(self):
        v, _, facts = self.check(bot(reviews=[approval(oid="c" * 40)]))
        self.assertEqual(v, "WAIT")
        self.assertTrue(facts["needs_approval"])

    def test_bot_pr_needs_the_review_record_and_green_ci(self):
        f = bot(reviews=[approval()])
        f.pr["comments"] = []
        self.refused(f, "NO_REVIEW_EVIDENCE")
        self.refused(bot(reviews=[approval()], statusCheckRollup=[]), "NO_CI_EVIDENCE")

    def test_spoofed_identities_fall_to_class_c_and_need_owner_approval(self):
        cases = {
            "human with bot-looking login": lambda f: f.pr.update(author={"login": "dependabot[bot]", "is_bot": False}),
            "unlisted bot": lambda f: f.pr.update(author={"login": "evil-updater[bot]", "is_bot": True}),
            "lookalike login": lambda f: f.pr.update(author={"login": "dependabot-helper[bot]", "is_bot": True}),
            "non-bot branch": lambda f: f.pr.update(headRefName="feature/x"),
            "human commit on bot branch": lambda f: f.pr.update(commits=[{"authors": [{"login": "dependabot[bot]"}]}, {"authors": [{"login": "mallory"}]}]),
            "workflow change": lambda f: f.pr.update(files=[{"path": "package.json"}, {"path": ".github/workflows/ci.yml"}]),
            "source change": lambda f: f.pr.update(files=[{"path": "package.json"}, {"path": "src/auth.py"}]),
            "no commit data": lambda f: f.pr.update(commits=[]),
            "cross-repository head": lambda f: f.pr.update(isCrossRepository=True),
        }
        for name, mutate in cases.items():
            f = bot()
            mutate(f)
            v, why, facts = self.check(f)
            self.assertEqual(facts.get("class") if name != "cross-repository head" else "C", "C", name)
            self.assertEqual(v, "REFUSE", (name, why))
            self.assertNotIn("approve_command", facts, name)

    def test_spoofed_bot_with_owner_approval_is_a_normal_third_party_pr(self):
        f = bot(reviews=[approval()])
        f.pr["author"] = {"login": "dependabot[bot]", "is_bot": False}
        v, _, facts = self.check(f)
        self.assertEqual((v, facts["class"]), ("PASS", "C"))

    def test_app_prefixed_login_from_gh_is_accepted(self):
        v, _, facts = self.check(bot(login="app/dependabot"))
        self.assertEqual(facts["class"], "B")


class Sequencing(Case):
    def test_only_one_bot_pr_is_runnable_at_a_time(self):
        f = bot(reviews=[approval()])
        text = omg.queue_report(f, REPO, [1, 2, 3], OWNERS)
        self.assertEqual(text.count("  run: "), 1)
        self.assertEqual(text.count("after the previous merge, re-run check"), 2)

    def test_lockfile_conflict_after_previous_merge_refuses_until_refreshed(self):
        stale = bot(reviews=[approval()], behind=1)
        self.refused(stale, "STALE_BRANCH")
        conflict = bot(reviews=[approval()], mergeable="CONFLICTING", mergeStateStatus="DIRTY")
        self.refused(conflict, "CONFLICTS")

    def test_head_change_after_refresh_requires_new_review_and_approval(self):
        f = bot(reviews=[approval()])
        f.pr["headRefOid"] = "d" * 40
        v, why, _ = self.check(f)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(any("REVIEW_STALE" in w for w in why))


class HostDenial(unittest.TestCase):
    def test_denial_is_recorded_and_no_alternate_route_is_offered(self):
        out = omg.host_denied(REPO, 7, HEAD, "[Merge Without Review]\nblocked")
        self.assertEqual((out["status"], out["action"], out["alternate_route_allowed"]), ("HARNESS_PERMISSION_BLOCK", "stop", False))
        self.assertNotIn("--admin", json.dumps(out))
        self.assertNotIn("--auto", json.dumps(out))
        self.assertNotIn("\n", out["host_message"])

    def test_cli_exit_is_nonzero_and_input_validated(self):
        self.assertEqual(omg.main(["host-denied", "--repo", REPO, "--pr", "7", "--head", HEAD, "--message", "x"]), 1)
        self.assertEqual(omg.main(["host-denied", "--repo", REPO, "--pr", "7", "--head", "nothex"]), 3)


class ReadOnly(unittest.TestCase):
    def test_new_reads_stay_inside_the_allowlist(self):
        omg.read_only(["api", f"repos/{REPO}/contents/.github/CODEOWNERS", "--jq", ".content"])
        for bad in (["pr", "review", "7", "--approve"], ["pr", "merge", "7", "--admin"], ["api", f"repos/{REPO}/pulls/7/reviews", "-X", "POST"]):
            with self.assertRaises(RuntimeError):
                omg.read_only(bad)


if __name__ == "__main__":
    unittest.main()
