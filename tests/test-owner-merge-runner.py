#!/usr/bin/env python3
"""Unit tests for scripts/owner-merge-runner.py (SPE-118 Option A) with an injected fake gh.

No real gh, no real PR, no network. The fake is passed through the module API; the CLI has no
way to select an alternate gh executable.
"""
import contextlib
import inspect
import re
import signal
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("omr", ROOT / "scripts/owner-merge-runner.py")
omr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omr)

HEAD = "a" * 40
OTHER = "b" * 40
MERGE_SHA = "c" * 40
REPO = "Chisanan232/demo-repo"
ME = "chisanan232"


REQUIRED = ["ci", "SonarCloud Code Analysis"]


def green():
    return [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": "SonarCloud Code Analysis", "state": "SUCCESS"}]


def server_rules(names=REQUIRED, rid=11):
    return [{"type": "required_status_checks", "ruleset_id": rid,
             "parameters": {"required_status_checks": [{"context": n} for n in names]}}]


NOW = 1792497600  # 2026-10-20T12:00:00Z; every test run uses this clock


def allow_doc(**cfg):
    c = {"required_checks": REQUIRED, "reviewers": ["independent-1"]}
    c.update(cfg)
    return {"repos": {REPO: c}}


def base_pr(**over):
    pr = {"state": "OPEN", "isDraft": False, "author": {"login": ME}, "baseRefName": "main",
          "headRefName": "feature/x", "headRefOid": HEAD, "isCrossRepository": False,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "",
          "reviews": [], "statusCheckRollup": green(), "files": [{"path": "a.txt"}]}
    pr.update(over)
    return pr


class FakeGh:
    def __init__(self, pr=None, merge_rc=0, merge_err="", merged=True, later=None, user=ME,
                 rules=None, protection=(1, "HTTP 404: Not Found"), ruleset=None, jobs=None):
        self.pr = pr if pr is not None else base_pr()
        self.merge_rc, self.merge_err, self.merged = merge_rc, merge_err, merged
        self.later = later  # PR facts returned from the second full fetch (the pre-exec re-check) on
        self.user = user
        self.rules = server_rules() if rules is None else rules
        self.ruleset = (0, json.dumps({"id": 11, "bypass_actors": []})) if ruleset is None else ruleset
        self.protection = protection  # (rc, stdout-or-stderr) for the branch-protection read
        self.runs = None  # optional callable(name) -> check-runs body
        self.jobs = jobs if jobs is not None else {}  # job id -> (rc, job dict); a callable is invoked per read
        self.fetches = 0
        self.readback_base = None
        self.overrides = {}  # fetch number -> PR facts for that single PR-facts read
        self.on_fetch = None  # called with the fetch number before each PR-facts read
        self.on_ruleset = None
        self.on_merge = None
        self.calls = []
        self.views = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv == ["api", "user"]:
            return 0, json.dumps({"login": self.user}), ""
        if argv[:2] == ["pr", "view"]:
            fields = argv[6]
            if fields == omr.PR_FIELDS:
                self.fetches += 1
                if self.on_fetch:
                    self.on_fetch(self.fetches)
                pr = self.later if (self.later is not None and self.fetches >= 3) else self.pr
                pr = self.overrides.get(self.fetches, pr)
                return 0, json.dumps(pr), ""
            if fields == omr.READBACK_FIELDS:
                if self.merged:
                    return 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": MERGE_SHA},
                                          "baseRefName": self.readback_base or self.pr["baseRefName"]}), ""
                return 0, json.dumps({"state": "OPEN", "mergeCommit": None, "baseRefName": self.pr["baseRefName"]}), ""
        if argv[0] == "api" and "/check-runs?check_name=" in argv[1]:
            name = urllib.parse.unquote(argv[1].split("check_name=")[1].split("&")[0])
            body = self.runs(name) if self.runs else {"total_count": 1, "check_runs": [
                {"name": name, "head_sha": HEAD, "app": {"slug": "github-actions"}}]}
            return 0, json.dumps(body), ""
        if argv[0] == "api" and "/actions/jobs/" in argv[1]:
            job = self.jobs[argv[1].rsplit("/", 1)[1]]
            rc, body = job() if callable(job) else job
            return (0, json.dumps(body), "") if rc == 0 else (1, "", "HTTP 500")
        if argv[0] == "api" and "/rulesets/" in argv[1]:
            if self.on_ruleset:
                self.on_ruleset()
            rc, text = self.ruleset
            return (0, text, "") if rc == 0 else (1, "", text)
        if argv[0] == "api" and "/rules/branches/" in argv[1]:
            return 0, json.dumps(self.rules), ""
        if argv[0] == "api" and argv[1].endswith("/protection"):
            rc, text = self.protection
            return (0, text, "") if rc == 0 else (1, "", text)
        if argv[:2] == ["pr", "merge"]:
            if self.on_merge:
                self.on_merge()
            return self.merge_rc, "", self.merge_err
        raise AssertionError(f"unexpected argv {argv}")

    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


DROP = object()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = pathlib.Path(os.path.realpath(self.tmp.name))
        self.tmp_real = d
        self.allow = d / "allow.json"
        self.allow.write_text(json.dumps(allow_doc()))
        self.evidence = d / "evidence.json"
        self.write_evidence()
        self.audit = d / "audit" / "log.jsonl"

    def write_evidence(self, **over):
        ev = {"repo": REPO, "pr": 7, "base": "main", "head": HEAD, "verdict": "PASS", "reviewer": "independent-1",
              "reviewer_kind": "independent-agent", "reviewed_at": "2026-10-20T10:00:00Z"}
        ev.update(over)
        for k in [k for k, v in ev.items() if v is DROP]:
            del ev[k]
        self.evidence.write_text(json.dumps(ev))

    def go(self, gh, repo=REPO, pr="7", head=HEAD, dry=False, allow=None, evidence=None, clock=lambda: NOW):
        return omr.run(repo, pr, head, str(evidence or self.evidence), str(allow or self.allow),
                       str(self.audit), dry, gh, clock=clock)

    def records(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()]

    def assertRefused(self, gh, needle, **kw):
        code, rec = self.go(gh, **kw)
        self.assertEqual(code, omr.EXIT_REFUSED, rec)
        self.assertEqual(gh.merges(), [], "a refusal must never reach gh pr merge")
        self.assertIn(needle, " | ".join(rec["reasons"]))
        return rec


    def assertBlocked(self, gh, needle, **kw):
        """Like assertRefused, but also accepts the audit-failed code when the audit shares the tampered tree."""
        code, rec = self.go(gh, **kw)
        self.assertIn(code, (omr.EXIT_REFUSED, omr.EXIT_AUDIT), rec)
        self.assertEqual(gh.merges(), [])
        self.assertIn(needle, " | ".join(rec["reasons"]))


class HappyPath(Base):
    def test_merge_uses_exactly_one_fixed_argv_and_reads_back(self):
        gh = FakeGh()
        code, rec = self.go(gh)
        self.assertEqual(code, omr.EXIT_OK, rec)
        self.assertEqual(gh.merges(), [["pr", "merge", "7", "--repo", REPO, "--merge", "--match-head-commit", HEAD]])
        self.assertEqual((rec["outcome"], rec["class"]), ("MERGED", "A"))
        flat = [t for c in gh.calls for t in c]
        for forbidden in ("--admin", "--squash", "--rebase", "--auto", "--delete-branch", "-d"):
            self.assertNotIn(forbidden, flat)

    def test_dry_run_validates_but_never_merges(self):
        gh = FakeGh()
        code, rec = self.go(gh, dry=True)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_OK, "DRY_RUN"))
        self.assertEqual(gh.merges(), [])

    def test_class_b_bot_needs_owner_approval_at_head(self):
        pr = base_pr(author={"login": "app/dependabot"}, headRefName="dependabot/npm/x")
        self.assertRefused(FakeGh(pr), "class B needs")
        pr["reviews"] = [{"author": {"login": ME}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        code, rec = self.go(FakeGh(pr))
        self.assertEqual((code, rec["class"]), (omr.EXIT_OK, "B"))

    def test_class_b_requires_bot_branch(self):
        pr = base_pr(author={"login": "dependabot"}, headRefName="feature/x",
                     reviews=[{"author": {"login": ME}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}])
        self.assertRefused(FakeGh(pr), "provenance")

    def test_class_c_needs_nonauthor_approval_at_head(self):
        pr = base_pr(author={"login": "someone"})
        self.assertRefused(FakeGh(pr), "class C needs")
        pr["reviews"] = [{"author": {"login": "someone"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        self.assertRefused(FakeGh(pr), "class C needs")  # self-approval does not count
        pr["reviews"] = [{"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": OTHER}}]
        self.assertRefused(FakeGh(pr), "class C needs")  # stale approval
        pr["reviews"] = [{"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        code, rec = self.go(FakeGh(pr))
        self.assertEqual((code, rec["class"]), (omr.EXIT_OK, "C"))


class Refusals(Base):
    def test_wrong_expected_head(self):
        self.write_evidence(head=OTHER)  # isolate the head pin from the evidence check
        self.assertRefused(FakeGh(), "observed head differs", head=OTHER)

    def test_head_moved_between_check_and_exec(self):
        self.assertRefused(FakeGh(later=base_pr(headRefOid=OTHER)), "changed before execution")

    def test_full_recheck_before_exec_catches_every_stale_fact(self):
        bad_roll = green() + [{"__typename": "CheckRun", "name": "late", "status": "COMPLETED", "conclusion": "FAILURE"}]
        cases = {"changes requested": base_pr(reviews=[{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED",
                                                         "commit": {"oid": HEAD}}]),
                 "check failed": base_pr(statusCheckRollup=bad_roll),
                 "required check vanished": base_pr(statusCheckRollup=green()[:1]),
                 "not clean": base_pr(mergeStateStatus="BLOCKED"),
                 "draft": base_pr(isDraft=True), "closed": base_pr(state="CLOSED"),
                 "class changed": base_pr(author={"login": "stranger"})}
        for name, later in cases.items():
            gh = FakeGh(later=later)
            code, rec = self.go(gh)
            self.assertEqual(code, omr.EXIT_REFUSED, name)
            self.assertEqual(gh.merges(), [], name)
            self.assertIn("changed before execution", " ".join(rec["reasons"]), name)
            self.assertEqual(gh.fetches, 3, name)

    def test_server_side_protection_required_by_default(self):
        self.assertRefused(FakeGh(rules=[{"type": "pull_request"}]), "no server-side required status checks")
        self.assertRefused(FakeGh(rules=[], protection=(0, json.dumps({"enforce_admins": {"enabled": True}, "required_status_checks": None}))),
                           "no server-side required status checks")

    def test_server_side_set_must_cover_every_allowlisted_required_check(self):
        self.assertRefused(FakeGh(rules=server_rules(["ci"])), "SonarCloud Code Analysis is not required server-side")
        self.assertRefused(FakeGh(rules=server_rules(["other"])), "is not required server-side")
        gh = FakeGh(rules=server_rules(REQUIRED + ["extra-job"]))  # a superset is fine
        self.assertEqual(self.go(gh)[0], omr.EXIT_OK)

    def test_empty_or_malformed_server_side_definitions_refuse(self):
        bad_rules = [server_rules([]), [{"type": "required_status_checks", "ruleset_id": 11}],
                     [{"type": "required_status_checks", "ruleset_id": 11, "parameters": {"required_status_checks": "ci"}}],
                     [{"type": "required_status_checks", "ruleset_id": 11,
                       "parameters": {"required_status_checks": [{"integration_id": 1}]}}],
                     [{"type": "required_status_checks", "ruleset_id": 11,
                       "parameters": {"required_status_checks": [{"context": ""}]}}],
                     [{"type": "required_status_checks", "ruleset_id": "11",
                       "parameters": {"required_status_checks": [{"context": "ci"}]}}],
                     [{"type": "required_status_checks", "ruleset_id": True,
                       "parameters": {"required_status_checks": [{"context": "ci"}]}}], ["x"]]
        for rules in bad_rules:
            self.assertRefused(FakeGh(rules=rules), "malformed" if rules == ["x"] else "empty or malformed")
        for prot in ({"required_status_checks": {"contexts": [], "checks": []}}, {"required_status_checks": {}},
                     {"required_status_checks": "ci"}, {"required_status_checks": {"contexts": "ci"}},
                     {"required_status_checks": {"contexts": [1]}}, {"required_status_checks": {"checks": [{"app_id": 1}]}},
                     [1]):
            self.assertRefused(FakeGh(rules=[], protection=(0, json.dumps(prot))), "")

    def test_branch_protection_contexts_and_checks_are_parsed_and_unioned_with_rulesets(self):
        prot = {"enforce_admins": {"enabled": True}, "required_status_checks": {"contexts": ["ci"], "checks": [{"context": "SonarCloud Code Analysis", "app_id": 1}]}}
        code, rec = self.go(FakeGh(rules=[], protection=(0, json.dumps(prot))))
        self.assertEqual(code, omr.EXIT_OK)
        self.assertEqual(rec["server_side"]["contexts"], sorted(REQUIRED))
        mixed = FakeGh(rules=server_rules(["ci"]), protection=(0, json.dumps(
            {"enforce_admins": {"enabled": True}, "required_status_checks": {"contexts": ["SonarCloud Code Analysis"]}})))
        self.assertEqual(self.go(mixed)[0], omr.EXIT_OK)

    def test_server_side_read_error_fails_closed(self):
        gh = FakeGh(rules=[], protection=(1, "HTTP 403: Resource not accessible"))
        code, _ = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_ERROR, []))

    def test_unreadable_protection_refuses_by_default_and_is_waived_only_by_accept(self):
        gh = FakeGh(protection=(1, "HTTP 403: Resource not accessible"))
        self.assertRefused(gh, "enforce_admins is unverified")
        self.allow.write_text(json.dumps(allow_doc(accept_bypass_actors=True)))
        code, rec = self.go(FakeGh(protection=(1, "HTTP 403: Resource not accessible")))
        self.assertEqual((code, rec["server_side"]["protection"]), (omr.EXIT_OK, "unreadable"))

    def test_every_bypass_actor_type_refuses_unless_accepted(self):
        for typ in ("User", "Team", "OrganizationAdmin", "RepositoryRole", "EnterpriseOwner", "EnterpriseRole",
                    "DeployKey", "Integration", "SomethingNew", None):
            actor = {"actor_type": typ, "actor_id": 1}
            gh = FakeGh(ruleset=(0, json.dumps({"id": 11, "bypass_actors": [actor]})))
            self.assertRefused(gh, "allows bypass")
        self.allow.write_text(json.dumps(allow_doc(accept_bypass_actors=True)))
        gh = FakeGh(ruleset=(0, json.dumps({"id": 11, "bypass_actors": [{"actor_type": "User", "actor_id": 5}]})))
        code, rec = self.go(gh)
        self.assertEqual((code, rec["server_side"]["bypass"]), (omr.EXIT_OK, "accepted"))

    def test_empty_bypass_list_is_the_only_none(self):
        code, rec = self.go(FakeGh())
        self.assertEqual((code, rec["server_side"]["bypass"], rec["server_side"]["waivers"]), (omr.EXIT_OK, "none", []))
        self.assertEqual(rec["server_side"]["enforce_admins"], "n/a")

    def test_accepting_with_nothing_to_waive_records_none(self):
        self.allow.write_text(json.dumps(allow_doc(accept_bypass_actors=True)))
        code, rec = self.go(FakeGh())
        self.assertEqual((rec["server_side"]["bypass"], rec["server_side"]["waivers"]), ("none", []))

    def test_exercised_waivers_are_recorded_truthfully_never_none(self):
        self.allow.write_text(json.dumps(allow_doc(accept_bypass_actors=True)))
        ok_prot = {"enforce_admins": {"enabled": True}}
        cases = {
            "protection unreadable": (FakeGh(protection=(1, "HTTP 403: nope")), "protection-waiver-accepted",
                                      "unverified-accepted", ["protection_unreadable"]),
            "enforce_admins false": (FakeGh(protection=(0, json.dumps({"enforce_admins": {"enabled": False}}))),
                                     "protection-waiver-accepted", "false-accepted", ["enforce_admins_false"]),
            "ruleset detail unreadable": (FakeGh(ruleset=(1, "HTTP 403")), "unreadable-accepted", "n/a",
                                          ["bypass_unreadable"]),
            "ruleset bypass_actors malformed": (FakeGh(ruleset=(0, json.dumps({"bypass_actors": "x"}))),
                                                "unreadable-accepted", "n/a", ["bypass_unreadable"]),
            "bypass actors present": (FakeGh(ruleset=(0, json.dumps({"bypass_actors": [
                {"actor_type": "User", "actor_id": 1}, {"actor_type": "Team", "actor_id": 2}]}))), "accepted", "n/a",
                ["bypass_actors_present:Team,User"]),
            "two rulesets with bypass actors": (FakeGh(rules=server_rules(REQUIRED, 11) + server_rules(["ci"], 12),
                                              ruleset=(0, json.dumps({"bypass_actors": [{"actor_type": "User"}]}))),
                                       "accepted", "n/a", ["bypass_actors_present:User"]),
        }
        for name, (gh, bypass, admins, waivers) in cases.items():
            code, rec = self.go(gh)
            ss = rec["server_side"]
            self.assertEqual(code, omr.EXIT_OK, name)
            self.assertNotEqual(ss["bypass"], "none", name)
            self.assertEqual((ss["bypass"], ss["enforce_admins"], ss["waivers"]), (bypass, admins, waivers), name)
            self.assertEqual(self.records()[-1]["server_side"]["waivers"], waivers, name)

    def test_enforce_admins_must_be_true_on_classic_protection(self):
        prot = lambda ea: (0, json.dumps({"enforce_admins": ea, "required_status_checks": {"contexts": REQUIRED}}))
        for ea in ({"enabled": False}, {}, None, "true", {"enabled": "true"}):
            self.assertRefused(FakeGh(rules=[], protection=prot(ea)), "enforce_admins")
        self.assertEqual(self.go(FakeGh(rules=[], protection=prot({"enabled": True})))[0], omr.EXIT_OK)
        self.allow.write_text(json.dumps(allow_doc(accept_bypass_actors=True)))
        code, rec = self.go(FakeGh(rules=[], protection=prot({"enabled": False})))
        self.assertEqual((code, rec["server_side"]["enforce_admins"], rec["server_side"]["bypass"]),
                         (omr.EXIT_OK, "false-accepted", "protection-waiver-accepted"))
        self.assertEqual(rec["server_side"]["waivers"], ["enforce_admins_false"])

    def test_enforce_admins_checked_even_when_rulesets_also_cover(self):
        prot = (0, json.dumps({"enforce_admins": {"enabled": False}}))
        self.assertRefused(FakeGh(protection=prot), "enforce_admins")

    def test_unreadable_or_malformed_bypass_metadata_refuses_by_default(self):
        for ruleset in ((1, "HTTP 403"), (0, json.dumps({"id": 11})), (0, json.dumps({"bypass_actors": "x"})),
                        (0, json.dumps({"bypass_actors": ["x"]})), (0, json.dumps([])), (0, "null")):
            self.assertRefused(FakeGh(ruleset=ruleset), "unreadable or malformed")

    def test_accept_bypass_actors_waives_unreadable_bypass_metadata(self):
        self.allow.write_text(json.dumps(allow_doc(accept_bypass_actors=True)))
        for ruleset in ((1, "HTTP 403"), (0, json.dumps({"id": 11}))):
            code, rec = self.go(FakeGh(ruleset=ruleset))
            self.assertEqual((code, rec["server_side"]["bypass"]), (omr.EXIT_OK, "unreadable-accepted"), ruleset)

    def test_server_side_protection_explicit_waiver(self):
        self.allow.write_text(json.dumps(allow_doc(require_server_side_protection=False)))
        code, rec = self.go(FakeGh(rules=[]))
        self.assertEqual((code, rec["server_side"]), (omr.EXIT_OK, {"waived": True}))
        self.allow.write_text(json.dumps(allow_doc(require_server_side_protection="false")))
        self.assertRefused(FakeGh(rules=[]), "invalid")

    def test_missing_checks(self):
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=[])), "required check ci is missing")

    def test_each_required_check_must_be_present(self):
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=green()[:1])), "required check SonarCloud")
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=green()[1:])), "required check ci is missing")
        # only an unrelated green check: no required name present
        other = [{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=other)), "required check")

    def test_extra_unnamed_failing_check_refuses(self):
        roll = green() + [{"__typename": "CheckRun", "name": "extra", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "check extra is FAILURE")
        roll = green() + [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "check ci is FAILURE")  # duplicate name

    def test_malformed_rollup_entries_refuse(self):
        for bad in ({}, {"name": "x"}, {"name": "x", "status": "COMPLETED"}, {"status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"name": "", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"__typename": "StatusContext", "state": "SUCCESS"}, {"__typename": "StatusContext", "context": "x"},
                    {"__typename": "CheckRun", "name": "x", "status": 5}, "string", None):
            self.assertRefused(FakeGh(base_pr(statusCheckRollup=green() + [bad])), "malformed check entry")

    def test_allowlist_repo_config_must_name_required_checks_and_reviewers(self):
        for cfg in ({"required_checks": [], "reviewers": ["a"]}, {"reviewers": ["a"]}, {"required_checks": ["ci"]},
                    {"required_checks": ["ci"], "reviewers": []}, {"required_checks": [""], "reviewers": ["a"]},
                    {"required_checks": "ci", "reviewers": ["a"]}, {"required_checks": ["ci"], "reviewers": ["a"], "x": 1}, []):
            self.allow.write_text(json.dumps({"repos": {REPO: cfg}}))
            self.assertRefused(FakeGh(), "invalid")

    def test_unresolved_changes_requested(self):
        rv = [{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}}]
        self.assertRefused(FakeGh(base_pr(reviews=rv)), "CHANGES_REQUESTED")
        self.assertRefused(FakeGh(base_pr(reviewDecision="CHANGES_REQUESTED")), "CHANGES_REQUESTED")

    def test_changes_requested_superseded_by_later_approval_is_not_blocking(self):
        rv = [{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}},
              {"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        code, _ = self.go(FakeGh(base_pr(reviews=rv)))
        self.assertEqual(code, omr.EXIT_OK)

    def test_not_clean(self):
        for mss in ("BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "UNKNOWN"):
            self.assertRefused(FakeGh(base_pr(mergeStateStatus=mss)), "mergeStateStatus")
        self.assertRefused(FakeGh(base_pr(mergeable="CONFLICTING")), "mergeable")

    def test_wrong_author_is_class_c_without_approval(self):
        self.assertRefused(FakeGh(base_pr(author={"login": "stranger"})), "class C needs")

    def test_repo_not_allowlisted(self):
        self.allow.write_text(json.dumps({"repos": {"Other/repo": allow_doc()["repos"][REPO]}}))
        self.assertRefused(FakeGh(), "not on the allowlist")
        self.allow.write_text(json.dumps({"repos": [REPO]}))  # the old list format is not accepted
        self.assertRefused(FakeGh(), "allowlist must map")

    def test_allowlist_missing_or_empty_is_refused(self):
        self.assertRefused(FakeGh(), "allowlist", allow=self.tmp_real / "nope.json")
        self.allow.write_text(json.dumps({"repos": {}}))
        self.assertRefused(FakeGh(), "allowlist must map")

    def test_missing_pr_fields_fail_closed(self):
        for key in ("state", "isDraft", "author", "headRefOid", "mergeable", "mergeStateStatus", "reviews",
                    "statusCheckRollup", "files", "reviewDecision", "baseRefName"):
            pr = base_pr()
            del pr[key]
            self.assertRefused(FakeGh(pr), "missing or malformed")

    def test_evidence_head_mismatch_fail_and_missing(self):
        self.write_evidence(head=OTHER)
        self.assertRefused(FakeGh(), "evidence head")
        self.write_evidence(verdict="FAIL")
        self.assertRefused(FakeGh(), "verdict")
        self.write_evidence(pr=8)
        self.assertRefused(FakeGh(), "evidence pr")
        self.write_evidence(repo="Other/repo")
        self.assertRefused(FakeGh(), "evidence repo mismatch")
        self.assertRefused(FakeGh(), "evidence missing", evidence=self.tmp_real / "none.json")
        self.evidence.write_text("not json")
        self.assertRefused(FakeGh(), "unparseable")

    def test_evidence_requires_every_bound_field_with_the_right_type(self):
        for key in ("repo", "pr", "head", "verdict", "reviewer", "reviewed_at", "reviewer_kind"):
            self.write_evidence(**{key: DROP})
            self.assertRefused(FakeGh(), "evidence")
        for key, bad in (("reviewer", 5), ("reviewer", ""), ("reviewed_at", 123), ("reviewer_kind", ["x"]),
                         ("repo", None), ("pr", True), ("pr", 7.5), ("pr", [7])):
            self.write_evidence(**{key: bad})
            self.assertRefused(FakeGh(), "evidence")

    def test_evidence_pr_may_be_int_or_digit_string(self):
        self.write_evidence(pr="7")
        self.assertEqual(self.go(FakeGh(), dry=True)[0], omr.EXIT_OK)

    def test_evidence_reviewer_must_be_allowlisted_for_the_repo(self):
        self.write_evidence(reviewer="someone-else")
        self.assertRefused(FakeGh(), "not in the allowlisted reviewers")
        self.write_evidence(reviewer="Independent-1")  # exact match only
        self.assertRefused(FakeGh(), "not in the allowlisted reviewers")

    def test_evidence_kind_and_timestamp_validated(self):
        self.write_evidence(reviewer_kind="self")
        self.assertRefused(FakeGh(), "reviewer_kind")
        for ts in ("yesterday", "2026-10-20 10:00:00", "2026-13-40T99:00:00Z", "2999-01-01T00:00:00Z"):
            self.write_evidence(reviewed_at=ts)
            self.assertRefused(FakeGh(), "reviewed_at")

    def test_class_a_still_requires_evidence(self):
        gh = FakeGh()
        code, rec = self.go(gh, evidence=self.tmp_real / "absent.json")
        self.assertEqual((rec["class"], code, gh.merges()), (None, omr.EXIT_REFUSED, []))

    def test_bad_syntax_never_reaches_gh(self):
        cases = [dict(repo="nope"), dict(repo="a/b;rm -rf"), dict(repo="a/b c"), dict(repo="a/../b"),
                 dict(repo="a/b\n"), dict(pr="0"), dict(pr="-1"), dict(pr="7 --admin"), dict(pr="7\n"),
                 dict(pr="$(id)"), dict(head="A" * 40), dict(head="a" * 39), dict(head="a" * 41),
                 dict(head=HEAD + " --admin"), dict(head=HEAD + "\n"), dict(head="`id`".ljust(40, "a"))]
        for kw in cases:
            gh = FakeGh()
            code, rec = self.go(gh, **kw)
            self.assertEqual(code, omr.EXIT_REFUSED, kw)
            self.assertEqual(gh.calls, [], f"gh must not be started for {kw}")

    def test_shell_metacharacters_rejected_in_every_arg(self):
        for meta in (";", "&&", "|", "$(x)", "`x`", ">", "<", "'", '"', "\\", "*", "{a,b}", "\t"):
            for kw in (dict(repo=f"a/b{meta}"), dict(pr=f"7{meta}"), dict(head=HEAD[:-1] + meta)):
                gh = FakeGh()
                code, _ = self.go(gh, **kw)
                self.assertEqual((code, gh.calls), (omr.EXIT_REFUSED, []), (meta, kw))


class AllowlistTrust(Base):
    def test_shipped_example_is_never_trusted_by_default_path(self):
        ex = ROOT / "docs/examples/owner-merge-runner-allowlist.example.json"
        self.assertTrue(ex.exists())
        self.assertNotEqual(os.path.realpath(omr.DEFAULT_ALLOWLIST), os.path.realpath(ex))
        self.assertRefused(FakeGh(), "git worktree", allow=ex)  # the repo copy is refused even if pointed at

    def test_symlink_refused(self):
        link = self.tmp_real / "link.json"
        link.symlink_to(self.allow)
        self.assertRefused(FakeGh(), "non-symlink", allow=link)

    def test_group_or_world_writable_file_or_dir_refused(self):
        for mode in (0o664, 0o666, 0o646):
            os.chmod(self.allow, mode)
            self.assertBlocked(FakeGh(), "writable")
        os.chmod(self.allow, 0o644)
        os.chmod(self.tmp.name, 0o777)
        try:
            self.assertBlocked(FakeGh(), "writable")
        finally:
            os.chmod(self.tmp.name, 0o700)

    def test_inside_git_worktree_refused(self):
        wt = self.tmp_real / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /nowhere")
        f = wt / "allow.json"
        f.write_text(json.dumps(allow_doc()))
        self.assertRefused(FakeGh(), "git worktree", allow=f)

    def test_foreign_owner_refused(self):
        real = os.geteuid
        omr.os.geteuid = lambda: real() + 1
        try:
            self.assertBlocked(FakeGh(), "not owned")
        finally:
            omr.os.geteuid = real

    def test_group_writable_ancestor_refused(self):
        sub = self.tmp_real / "a" / "b"
        sub.mkdir(parents=True)
        f = sub / "allow.json"
        f.write_text(json.dumps(allow_doc()))
        os.chmod(sub.parent, 0o775)
        self.assertBlocked(FakeGh(), "writable", allow=f)
        os.chmod(sub.parent, 0o700)
        self.assertEqual(self.go(FakeGh(), dry=True, allow=f)[0], omr.EXIT_OK)

    def test_symlinked_ancestor_refused(self):
        real = self.tmp_real / "real"
        real.mkdir()
        (real / "allow.json").write_text(json.dumps(allow_doc()))
        link = self.tmp_real / "linkdir"
        link.symlink_to(real)
        self.assertRefused(FakeGh(), "symlink", allow=link / "allow.json")

    def test_sticky_root_owned_writable_ancestor_is_the_only_writable_exception(self):
        real_lstat = os.lstat
        target = str(self.tmp_real.parent)

        def fake(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == target:
                vals = list(st)
                vals[0] = stat.S_IFDIR | 0o1777
                vals[4] = 0
                return os.stat_result(vals)
            return st
        omr.os.lstat = fake
        try:
            self.assertEqual(omr.dir_chain_problems(str(self.tmp_real)), [])
            def fake2(path, *a, **k):
                st = fake(path, *a, **k)
                if str(path) == target:
                    vals = list(st)
                    vals[0] = stat.S_IFDIR | 0o777  # writable, not sticky
                    return os.stat_result(vals)
                return st
            omr.os.lstat = fake2
            self.assertTrue(any("writable" in p for p in omr.dir_chain_problems(str(self.tmp_real))))
        finally:
            omr.os.lstat = real_lstat

    def test_allowlist_is_read_through_the_checked_descriptor(self):
        opened, real_open = [], os.open

        def spy(path, flags, *a, **k):
            opened.append((str(path), flags))
            return real_open(path, flags, *a, **k)
        omr.os.open = spy
        try:
            self.assertEqual(self.go(FakeGh(), dry=True)[0], omr.EXIT_OK)
        finally:
            omr.os.open = real_open
        flags = [f for p, f in opened if p == str(self.allow)]
        self.assertEqual(len(flags), 1)
        self.assertTrue(flags[0] & os.O_NOFOLLOW)
        src = (ROOT / "scripts/owner-merge-runner.py").read_text()
        body = src[src.index("def read_trusted_file"):src.index("def load_allowlist")]
        self.assertNotIn("open(path", body.replace("os.open(path", ""))  # no second open by pathname

    def test_fifo_or_directory_allowlist_refused(self):
        fifo = self.tmp_real / "fifo.json"
        os.mkfifo(fifo)
        # O_NONBLOCK is not used by the runner, so refuse before reading: opening a FIFO read-only blocks
        # only without a writer; open a writer thread-free by testing the directory case instead.
        d = self.tmp_real / "dir.json"
        d.mkdir()
        self.assertRefused(FakeGh(), "allowlist", allow=d)

    def test_trusted_file_is_accepted(self):
        code, _ = self.go(FakeGh(), dry=True)
        self.assertEqual(code, omr.EXIT_OK)


class ClassCAssociation(Base):
    def pr_with(self, assoc, **over):
        return base_pr(author={"login": "someone"}, reviews=[
            {"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": assoc, "commit": {"oid": HEAD}}], **over)

    def test_untrusted_associations_refused(self):
        for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", None):
            self.assertRefused(FakeGh(self.pr_with(assoc)), "OWNER, MEMBER or COLLABORATOR")

    def test_trusted_associations_accepted(self):
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            code, _ = self.go(FakeGh(self.pr_with(assoc)))
            self.assertEqual(code, omr.EXIT_OK, assoc)

    def test_cross_repository_needs_authenticated_user_approval(self):
        self.assertRefused(FakeGh(self.pr_with("OWNER", isCrossRepository=True)), "cross-repository")
        pr = self.pr_with("OWNER", isCrossRepository=True)
        pr["reviews"].append({"author": {"login": ME}, "state": "APPROVED", "authorAssociation": "OWNER",
                              "commit": {"oid": HEAD}})
        code, _ = self.go(FakeGh(pr))
        self.assertEqual(code, omr.EXIT_OK)


class EvidenceBindingAndAge(Base):
    def test_base_branch_required_and_bound(self):
        self.write_evidence(base=DROP)
        self.assertRefused(FakeGh(), "evidence field base")
        self.write_evidence(base="release")
        self.assertRefused(FakeGh(), "evidence base branch")
        self.write_evidence(base=5)
        self.assertRefused(FakeGh(), "evidence field base")

    def test_base_branch_change_at_the_recheck_refuses(self):
        gh = FakeGh(later=base_pr(baseRefName="release"))
        code, rec = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("changed before execution", " ".join(rec["reasons"]))
        self.assertIn("base branch changed since the first read", " ".join(rec["reasons"]))

    def test_final_read_runs_the_full_decision_on_both_passes(self):
        """Read 2 closes the first pass, read 4 is the last read before the merge; each must be fully clean."""
        bad_roll = green() + [{"__typename": "CheckRun", "name": "security", "status": "COMPLETED", "conclusion": "FAILURE"}]
        no_sonar = [green()[0]]
        cases = {
            "changes requested": base_pr(reviews=[{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED",
                                                    "commit": {"oid": HEAD}}]),
            "failed check": base_pr(statusCheckRollup=bad_roll),
            "required check gone": base_pr(statusCheckRollup=no_sonar),
            "draft": base_pr(isDraft=True), "closed": base_pr(state="CLOSED"),
            "not clean": base_pr(mergeStateStatus="BLOCKED"), "conflicting": base_pr(mergeable="CONFLICTING"),
            "malformed check": base_pr(statusCheckRollup=green() + [{"name": "x"}]),
            "class changed": base_pr(author={"login": "stranger"}),
            "review decision": base_pr(reviewDecision="CHANGES_REQUESTED"),
        }
        for read in (2, 4):
            for name, pr in cases.items():
                gh = FakeGh()
                gh.overrides = {read: pr}
                code, rec = self.go(gh)
                self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []), (read, name))
                self.assertEqual(gh.fetches, read, (read, name))

    def test_base_flip_during_ruleset_detail_read_refuses(self):
        gh = FakeGh()
        gh.on_ruleset = lambda: gh.pr.update(baseRefName="release")
        code, rec = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("changed while server-side state was being read", " ".join(rec["reasons"]))

    def test_base_change_between_reads_with_new_signed_evidence_for_new_base_refuses(self):
        self.allow.write_text(json.dumps(dict(allow_doc(), evidence_key_file=str(self.tmp_real / "k.key"))))
        key = b"SYNTHETIC-TEST-KEY-0123456789abcdef-NOT-A-SECRET"
        (self.tmp_real / "k.key").write_bytes(key)
        os.chmod(self.tmp_real / "k.key", 0o600)

        def signed(base):
            self.write_evidence(base=base)
            ev = json.loads(self.evidence.read_text())
            ev["hmac"] = omr.evidence_mac(key, ev)
            self.evidence.write_text(json.dumps(ev))
        signed("main")
        gh = FakeGh(later=base_pr(baseRefName="release"))
        gh.on_fetch = lambda n: signed("release") if n == 3 else None  # attacker re-signs for the new base
        code, rec = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("base branch changed since the first read", " ".join(rec["reasons"]))

    def test_evidence_changed_between_passes_refuses_even_with_same_base(self):
        gh = FakeGh()
        gh.on_fetch = lambda n: self.write_evidence(reviewed_at="2026-10-20T11:00:00Z") if n == 3 else None
        code, rec = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("evidence file changed since the first read", " ".join(rec["reasons"]))

    def test_head_flip_during_protection_reads_refuses(self):
        gh = FakeGh()
        gh.on_ruleset = lambda: gh.pr.update(headRefOid=OTHER)
        self.assertEqual(self.go(gh)[0], omr.EXIT_REFUSED)
        self.assertEqual(gh.merges(), [])

    def test_protection_is_read_for_the_pinned_base(self):
        gh = FakeGh()
        self.assertEqual(self.go(gh)[0], omr.EXIT_OK)
        reads = [c[1] for c in gh.calls if c[0] == "api" and c[1].startswith("repos/")]
        self.assertTrue(reads and all("/main" in r or "/rulesets/" in r for r in reads), reads)

    def test_protection_reads_follow_a_non_main_pinned_base(self):
        self.write_evidence(base="release/1.x")
        gh = FakeGh(base_pr(baseRefName="release/1.x"))
        self.assertEqual(self.go(gh)[0], omr.EXIT_OK)
        reads = [c[1] for c in gh.calls if c[0] == "api" and c[1].startswith("repos/")]
        self.assertIn(f"repos/{REPO}/rules/branches/release/1.x", reads)
        self.assertIn(f"repos/{REPO}/branches/release/1.x/protection", reads)

    def test_readback_with_changed_base_is_an_incident_with_its_own_exit_code(self):
        gh = FakeGh()
        gh.readback_base = "release"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"], rec["base"]), (omr.EXIT_BASE_MISMATCH, "UNVERIFIED", "main"))
        self.assertNotIn(code, (omr.EXIT_OK, omr.EXIT_UNVERIFIED))
        self.assertIn("POSSIBLE MERGE TO UNREVIEWED BASE", err.getvalue())
        self.assertEqual(rec["incident"], "POSSIBLE MERGE TO UNREVIEWED BASE")
        final = self.records()[-1]
        self.assertEqual(final["incident"], "POSSIBLE MERGE TO UNREVIEWED BASE")
        self.assertIn("POSSIBLE MERGE TO UNREVIEWED BASE", " ".join(final["reasons"]))
        self.assertEqual(len(gh.merges()), 1)

    def test_not_merged_readback_keeps_the_plain_unverified_code(self):
        code, rec = self.go(FakeGh(merged=False))
        self.assertEqual((code, "incident" in rec), (omr.EXIT_UNVERIFIED, False))

    def test_first_read_with_different_base_refuses(self):
        self.assertRefused(FakeGh(base_pr(baseRefName="release")), "evidence base branch")

    def test_default_max_age_is_24_hours_and_future_refuses(self):
        self.write_evidence(reviewed_at="2026-10-19T12:00:00Z")  # exactly 24h
        self.assertEqual(self.go(FakeGh(), dry=True)[0], omr.EXIT_OK)
        self.write_evidence(reviewed_at="2026-10-19T11:59:59Z")
        self.assertRefused(FakeGh(), "older than max_evidence_age_hours")
        self.write_evidence(reviewed_at="2026-10-20T12:01:01Z")
        self.assertRefused(FakeGh(), "in the future")

    def test_configured_age_is_honoured_and_bounded(self):
        self.allow.write_text(json.dumps(allow_doc(max_evidence_age_hours=1)))
        self.write_evidence(reviewed_at="2026-10-20T10:30:00Z")
        self.assertRefused(FakeGh(), "older than")
        self.allow.write_text(json.dumps(allow_doc(max_evidence_age_hours=168)))
        self.write_evidence(reviewed_at="2026-10-14T12:00:00Z")
        self.assertEqual(self.go(FakeGh(), dry=True)[0], omr.EXIT_OK)
        for bad in (0, -1, 169, 1.5, "24", True):
            self.allow.write_text(json.dumps(allow_doc(max_evidence_age_hours=bad)))
            self.assertRefused(FakeGh(), "invalid")


class SignedEvidence(Base):
    KEY = b"SYNTHETIC-TEST-KEY-0123456789abcdef-NOT-A-SECRET"

    def setUp(self):
        super().setUp()
        self.keyfile = self.tmp_real / "keys" / "evidence.key"
        self.keyfile.parent.mkdir(mode=0o700)
        self.put_key(self.KEY)

    def put_key(self, data, mode=0o600):
        self.keyfile.write_bytes(data)
        os.chmod(self.keyfile, mode)

    def enable(self, **cfg):
        doc = allow_doc(**cfg)
        doc["evidence_key_file"] = str(self.keyfile)
        self.allow.write_text(json.dumps(doc))

    def sign(self, key=None, **over):
        self.write_evidence(**over)
        ev = json.loads(self.evidence.read_text())
        ev["hmac"] = omr.evidence_mac(key or self.KEY, ev)
        self.evidence.write_text(json.dumps(ev))

    def test_valid_signature_accepted_and_recorded(self):
        self.enable()
        self.sign()
        code, rec = self.go(FakeGh())
        self.assertEqual((code, rec["signed_evidence"]), (omr.EXIT_OK, True))

    def test_signature_required_by_default_when_key_configured(self):
        self.enable()
        self.write_evidence()  # no hmac
        self.assertRefused(FakeGh(), "signature missing or invalid")

    def test_tampered_wrong_key_and_malformed_signatures_refuse(self):
        self.enable()
        self.sign()
        ev = json.loads(self.evidence.read_text())
        for mutate in (lambda e: e.update(head=OTHER), lambda e: e.update(reviewer="independent-1 "),
                       lambda e: e.update(verdict="PASS ", reviewed_at="2026-10-20T11:00:00Z"),
                       lambda e: e.update(extra="x"), lambda e: e.update(hmac="0" * 64),
                       lambda e: e.update(hmac=None), lambda e: e.update(hmac=5), lambda e: e.pop("hmac")):
            bad = json.loads(json.dumps(ev))
            mutate(bad)
            self.evidence.write_text(json.dumps(bad))
            code, rec = self.go(FakeGh())
            self.assertEqual(code, omr.EXIT_REFUSED, bad)
        self.sign(key=b"a-different-synthetic-key-of-enough-length!!")
        self.assertRefused(FakeGh(), "signature missing or invalid")

    def test_explicit_waiver_allows_unsigned(self):
        self.enable(require_signed_evidence=False)
        self.write_evidence()
        code, rec = self.go(FakeGh())
        self.assertEqual((code, rec["signed_evidence"]), (omr.EXIT_OK, False))

    def test_unsigned_by_default_without_key_file(self):
        code, rec = self.go(FakeGh())
        self.assertEqual((code, rec["signed_evidence"]), (omr.EXIT_OK, False))

    def test_require_signed_without_key_file_is_invalid_config(self):
        self.allow.write_text(json.dumps(allow_doc(require_signed_evidence=True)))
        self.assertRefused(FakeGh(), "invalid")

    def test_key_file_must_be_trusted(self):
        self.enable()
        self.sign()
        for mode in (0o644, 0o640, 0o604, 0o660):
            os.chmod(self.keyfile, mode)
            self.assertRefused(FakeGh(), "readable or writable by group or others")
        os.chmod(self.keyfile, 0o600)
        self.put_key(b"short")
        self.assertRefused(FakeGh(), "unusable length")
        self.put_key(self.KEY)
        link = self.keyfile.parent / "link.key"
        link.symlink_to(self.keyfile)
        doc = allow_doc()
        doc["evidence_key_file"] = str(link)
        self.allow.write_text(json.dumps(doc))
        self.assertRefused(FakeGh(), "non-symlink")
        doc["evidence_key_file"] = str(self.tmp_real / "absent.key")
        self.allow.write_text(json.dumps(doc))
        self.assertRefused(FakeGh(), "evidence key file")
        doc["evidence_key_file"] = "relative.key"
        self.allow.write_text(json.dumps(doc))
        self.assertRefused(FakeGh(), "absolute path")

    def test_key_file_in_git_worktree_or_loose_ancestor_refused(self):
        self.enable()
        self.sign()
        wt = self.tmp_real / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /nowhere")
        k = wt / "k.key"
        k.write_bytes(self.KEY)
        os.chmod(k, 0o600)
        doc = allow_doc()
        doc["evidence_key_file"] = str(k)
        self.allow.write_text(json.dumps(doc))
        self.assertRefused(FakeGh(), "git worktree")
        self.enable()
        os.chmod(self.keyfile.parent, 0o770)
        self.assertBlocked(FakeGh(), "writable")
        os.chmod(self.keyfile.parent, 0o700)

    def test_key_material_never_appears_in_audit_stdout_or_stderr(self):
        self.enable()
        self.sign()
        out, err = io.StringIO(), io.StringIO()
        gh = FakeGh(merge_rc=1, merge_err="permission denied")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            omr.main(["--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", str(self.evidence)],
                     gh=gh, allowlist=str(self.allow), audit=str(self.audit), clock=lambda: NOW)
            self.evidence.write_text("{}")
            omr.main(["--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", str(self.evidence)],
                     gh=FakeGh(), allowlist=str(self.allow), audit=str(self.audit), clock=lambda: NOW)
        blob = out.getvalue() + err.getvalue() + self.audit.read_text()
        self.assertNotIn(self.KEY.decode(), blob)
        self.assertNotIn(omr.evidence_mac(self.KEY, {"a": 1}), blob)
        self.assertNotIn("hmac", self.audit.read_text())

    def test_sign_script_round_trip_and_refusals(self):
        spec = importlib.util.spec_from_file_location("oms", ROOT / "scripts/owner-merge-evidence-sign.py")
        oms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oms)
        self.enable()
        self.write_evidence()
        out = self.tmp_real / "signed.json"
        self.assertEqual(oms.main(["--key-file", str(self.keyfile), "--in", str(self.evidence), "--out", str(out)]), 0)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual(self.go(FakeGh(), evidence=out)[0], omr.EXIT_OK)
        self.assertEqual(oms.main(["--key-file", str(self.keyfile), "--in", str(self.evidence), "--out", str(out)]), 4)  # no overwrite
        os.chmod(self.keyfile, 0o644)
        with contextlib.redirect_stderr(io.StringIO()) as e:
            rc = oms.main(["--key-file", str(self.keyfile), "--in", str(self.evidence), "--out", str(self.tmp_real / "x.json")])
        self.assertEqual(rc, 2)
        self.assertNotIn(self.KEY.decode(), e.getvalue())


class ProtectionErrorClassification(Base):
    def test_only_a_real_http_404_status_means_absent(self):
        for err in ("gh: Not Found (HTTP 404)", "HTTP 404: Not Found (https://api.github.com/repos/x/branches/main/protection)",
                    "gh: Branch not protected (HTTP 404)"):
            code, rec = self.go(FakeGh(protection=(1, err)), dry=True)
            self.assertEqual((code, rec["server_side"]["protection"]), (omr.EXIT_OK, "absent"), err)

    def test_status_token_must_be_complete(self):
        for err in ("HTTP 4040: Not Found", "HTTP 404abc: Not Found", "gh: Not Found (HTTP 4040)", "HTTP 404x",
                    "HTTP 40: x", "HTTP  404", "HTTP 404_", "HTTP 4040: nope (HTTP 404)", "HTTP 404abc (HTTP 404)"):
            self.assertIsNone(omr.http_status(err), err)
            self.assertRefused(FakeGh(protection=(1, err)), "enforce_admins is unverified")
        for err, want in (("gh: Not Found (HTTP 404)", "404"), ("HTTP 404: Not Found", "404"), ("HTTP 404", "404"),
                          ("HTTP 403: x (HTTP 404)", "403")):
            self.assertEqual(omr.http_status(err), want, err)

    def test_other_ambiguous_or_unparseable_errors_are_unreadable_and_refuse(self):
        errs = ("HTTP 403: Resource not accessible by integration (https://api.github.com/repos/x/branches/release-404/protection)",
                "gh: Resource not accessible (HTTP 403) repos/x/branches/release-404/protection",
                "HTTP 403 Not Found? branches/404/protection", "HTTP 500: Internal error 404", "HTTP 401 Bad credentials 404",
                "Not Found", "404", "branch release-404 missing", "HTTP 40", "dial tcp: lookup api.github.com ... 404", "",
                "HTTP 403: forbidden (HTTP 404)")
        for err in errs:
            self.assertRefused(FakeGh(protection=(1, err)), "enforce_admins is unverified")

    def test_branch_names_containing_404_do_not_confuse_the_classifier(self):
        self.write_evidence(base="release-404")
        pr = base_pr(baseRefName="release-404")
        self.assertRefused(FakeGh(pr, protection=(1, "HTTP 403: nope repos/x/branches/release-404/protection")),
                           "enforce_admins is unverified")
        code, rec = self.go(FakeGh(pr, protection=(1, "gh: Not Found (HTTP 404)")), dry=True)
        self.assertEqual((code, rec["server_side"]["protection"]), (omr.EXIT_OK, "absent"))

    def test_unreadable_protection_without_ruleset_checks_stays_an_error(self):
        gh = FakeGh(rules=[], protection=(1, "HTTP 403: nope 404"))
        self.assertEqual((self.go(gh)[0], gh.merges()), (omr.EXIT_ERROR, []))


class MalformedInputFailsClosed(Base):
    def audited_refusal(self, gh, code_expected=None, **kw):
        code, rec = self.go(gh, **kw)
        self.assertEqual(gh.merges(), [])
        self.assertEqual(code, omr.EXIT_REFUSED if code_expected is None else code_expected, rec)
        self.assertTrue(self.audit.exists(), "an audit record must exist")
        return rec

    def test_signature_shape_is_validated_before_compare(self):
        key = SignedEvidence.KEY
        keyfile = self.tmp_real / "k.key"
        keyfile.write_bytes(key)
        os.chmod(keyfile, 0o600)
        self.allow.write_text(json.dumps(dict(allow_doc(), evidence_key_file=str(keyfile))))
        good = omr.evidence_mac(key, json.loads(json.dumps({
            "repo": REPO, "pr": 7, "base": "main", "head": HEAD, "verdict": "PASS", "reviewer": "independent-1",
            "reviewer_kind": "independent-agent", "reviewed_at": "2026-10-20T10:00:00Z"})))
        for bad in ("é" * 64, good.upper(), good[:-1], good + "0", "", " " * 64, "0" * 63 + "g"):
            self.write_evidence(hmac=bad)
            self.audit_refused = self.audited_refusal(FakeGh())

    def test_malformed_review_elements_refuse_with_an_audit_record(self):
        for reviews in (["malformed"], [None], [5], [[]], [{"author": "x", "state": "APPROVED"}],
                        [{"author": None, "state": "APPROVED"}], [{"author": {"login": 5}, "state": "APPROVED"}],
                        [{"author": {"login": "a"}, "state": 5}], [{"author": {"login": "a"}, "state": "APPROVED", "commit": "x"}],
                        [{"state": "APPROVED"}], {"a": 1}, "reviews"):
            rec = self.audited_refusal(FakeGh(base_pr(reviews=reviews)))
            self.assertIn(rec["outcome"], ("REFUSED",), reviews)

    def test_unexpected_exception_at_every_stage_is_audited_and_never_merges(self):
        def boom(*a, **k):
            raise ZeroDivisionError("injected")
        stages = ["load_allowlist", "repo_config", "load_key", "decide", "load_evidence", "check_server_protection",
                  "summarize_checks", "evaluate"]
        keyfile = self.tmp_real / "k2.key"
        keyfile.write_bytes(SignedEvidence.KEY)
        os.chmod(keyfile, 0o600)
        self.allow.write_text(json.dumps(dict(allow_doc(), evidence_key_file=str(keyfile))))
        self.write_evidence()
        ev = json.loads(self.evidence.read_text())
        ev["hmac"] = omr.evidence_mac(SignedEvidence.KEY, ev)
        self.evidence.write_text(json.dumps(ev))
        for name in stages:
            real = getattr(omr, name)
            setattr(omr, name, boom)
            try:
                gh = FakeGh()
                code, rec = self.go(gh)
            finally:
                setattr(omr, name, real)
            self.assertEqual((code, gh.merges(), rec["outcome"]), (omr.EXIT_INTERNAL, [], "ERROR"), (name, rec["reasons"]))
            self.assertEqual(self.records()[-1]["outcome"], "ERROR", name)
            self.assertIn("unexpected ZeroDivisionError", " ".join(rec["reasons"]), name)

    def test_unexpected_exception_from_gh_before_exec_is_audited(self):
        class Boom(FakeGh):
            def __call__(self, argv):
                if argv[:2] == ["pr", "view"] and self.fetches >= 1:
                    raise KeyError("injected")
                return super().__call__(argv)
        gh = Boom()
        code, rec = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_INTERNAL, []))

    def test_second_pass_exception_and_pending_write_exception_do_not_merge(self):
        calls = {"n": 0}
        real = omr.decide

        def second_boom(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 3:  # read 1, then read 2 (first pass end), then the second pass
                raise ZeroDivisionError("injected")
            return real(*a, **k)
        omr.decide = second_boom
        try:
            gh = FakeGh()
            code, _ = self.go(gh)
        finally:
            omr.decide = real
        self.assertEqual((code, gh.merges()), (omr.EXIT_INTERNAL, []))
        real_w, state = omr.write_audit, {"n": 0}

        def pending_boom(path, record):
            state["n"] += 1
            if state["n"] == 1:
                raise ValueError("injected")
            return real_w(path, record)
        omr.write_audit = pending_boom
        try:
            gh = FakeGh()
            code, rec = self.go(gh)
        finally:
            omr.write_audit = real_w
        self.assertEqual((code, gh.merges()), (omr.EXIT_INTERNAL, []))
        self.assertEqual(self.records()[-1]["outcome"], "ERROR")

    def test_pending_record_still_precedes_exec_and_post_merge_exception_is_flagged(self):
        class Boom(FakeGh):
            def __call__(self, argv):
                if argv[:2] == ["pr", "view"] and argv[6] == omr.READBACK_FIELDS:
                    raise KeyError("injected")
                return super().__call__(argv)
        gh = Boom()
        code, rec = self.go(gh)
        self.assertEqual((code, len(gh.merges())), (omr.EXIT_INTERNAL, 1))
        recs = self.records()
        self.assertEqual([r["outcome"] for r in recs], ["PENDING", "ERROR"])
        self.assertIn("may already be merged", " ".join(recs[-1]["reasons"]))
        self.assertTrue(recs[-1]["merge_attempted"])


def inspect_source(name):
    return inspect.getsource(getattr(omr, name))


class Redaction(Base):
    CASES = [  # (text, fragments that must not survive)
        ("HTTP 401: Authorization: Basic c3ludGhldGljOnBhc3N3b3Jk", ["c3ludGhldGljOnBhc3N3b3Jk", "Basic c3"]),
        ("authorization=Token abcdefgh12345 more", ["abcdefgh12345", "more"]),
        ('password="my secret pass phrase" tail', ["my secret", "pass phrase", "phrase"]),
        ("password='a b c d' tail", ["a b c", "c d"]),
        ("secret = 'two words here'", ["two words", "words here"]),
        ("token=abc def ghi", ["abc", "def", "ghi"]),
        ("api_key: sk-synthetic 12 34", ["sk-synthetic", "12 34"]),
        ("X-Api-Key=synthetic-value-1", ["synthetic-value-1"]),
        ("apikey = \"quoted key with spaces\"", ["quoted key", "with spaces"]),
        ("Authorization: Bearer abcdefghijklmnop", ["abcdefghijklmnop"]),
        ("got Basic dXNlcjpwYXNz back", ["dXNlcjpwYXNz"]),
        ("passwd: s3cr3t value", ["s3cr3t", "value"]),
        ("access_token=zzz yyy", ["zzz", "yyy"]),
        ("ghp_" + "A1b2C3d4" * 3, ["A1b2C3d4"]),
        ("github_pat_" + "x1" * 12, ["x1x1x1"]),
    ]

    def test_whole_credential_values_are_redacted(self):
        for text, gone in self.CASES:
            out = omr.scrub(text)
            self.assertIn("[redacted]", out, text)
            for frag in gone:
                self.assertNotIn(frag, out, (text, frag))

    def test_head_shas_and_plain_text_are_not_redacted(self):
        self.assertEqual(omr.scrub(HEAD), HEAD)
        self.assertEqual(omr.scrub("PR is a draft"), "PR is a draft")

    def test_gh_error_is_stage_status_and_category_only(self):
        self.assertEqual(omr.gh_error("HTTP 403: whatever", "merge"), "gh failure stage=merge status=403 category=denied")
        self.assertEqual(omr.gh_error("HTTP 401", "pr-view"), "gh failure stage=pr-view status=401 category=denied")
        self.assertEqual(omr.gh_error("gh: Not Found (HTTP 404)", "protection-read"),
                         "gh failure stage=protection-read status=404 category=not-found")
        self.assertEqual(omr.gh_error("HTTP 502: Bad gateway", "rules-read"),
                         "gh failure stage=rules-read status=502 category=error")
        self.assertEqual(omr.gh_error("HTTP 4040: x", "merge"), "gh failure stage=merge status=unknown category=unreadable")
        self.assertEqual(omr.gh_error("", "merge"), "gh failure stage=merge status=unknown category=unreadable")
        body = inspect_source("gh_error").split('"""')[2]  # code only, not the docstring
        for forbidden in ("sha256", "hashlib", "scrub(", "excerpt", "[:"):
            self.assertNotIn(forbidden, body)

    HOSTILE = [
        'HTTP 403: Bearer "SYNq1 SYNq2" and SYNtail',
        'HTTP 403: password="SYNa\\"SYNb" SYNtail2',
        "HTTP 500: Authorization: Basic SYNdXNlcjpwYXNz\nSYNsecondline token=SYNtok",
        "HTTP 403: ghp_SYN" + "A" * 24 + " github_pat_SYN" + "b" * 24,
        "HTTP 401: api_key='SYN k e y' x-api-key: SYNxk\nline3 SYNline3 secret=\"SYN s s\"",
        "SYNnostatus Bearer SYNb64== password SYNpw",
        "HTTP 404: Not Found SYNpath/repos/SYNowner/SYNrepo\n\n\tSYNtabbed",
    ]
    LEAKS = ["SYN", "Bearer", "Basic", "Authorization", "password", "api_key", "token=", "secret=", "ghp_", "github_pat_"]

    def run_path(self, path, hostile):
        class Gh(FakeGh):
            def __call__(inner, argv):
                if path == "pr-view" and argv[:2] == ["pr", "view"]:
                    return 1, "", hostile
                if path == "rules-read" and argv[0] == "api" and "/rules/branches/" in argv[1]:
                    return 1, "", hostile
                return super().__call__(argv)
        if path == "protection-read":
            gh = Gh(rules=[], protection=(1, hostile))
        elif path == "merge":
            gh = Gh(merge_rc=1, merge_err=hostile)
        else:
            gh = Gh()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            omr.main(["--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", str(self.evidence)],
                     gh=gh, allowlist=str(self.allow), audit=str(self.audit), clock=lambda: NOW)
        return self.audit.read_text() + out.getvalue() + err.getvalue()

    def test_hostile_stderr_appears_nowhere_on_any_gh_failure_path(self):
        for path in ("pr-view", "rules-read", "protection-read", "merge"):
            for hostile in self.HOSTILE:
                if self.audit.exists():
                    self.audit.unlink()
                blob = self.run_path(path, hostile)
                status = re.search(r"HTTP (\d{3})", hostile)
                if not (path == "protection-read" and status and status.group(1) == "404"):  # a real 404 is "absent"
                    self.assertIn(f"stage={path}", blob, (path, hostile))
                    self.assertIn(f"status={status.group(1) if status else 'unknown'}", blob, (path, hostile))
                for leak in self.LEAKS:
                    self.assertNotIn(leak, blob, (path, hostile, leak))
                for word in re.findall(r"[A-Za-z0-9_+/=.-]{3,}", hostile):
                    if word in ("403", "401", "404", "500", "HTTP", "Not", "Found", "line3", "and"):
                        continue
                    self.assertNotIn(word, blob, (path, hostile, word))


class FifoPaths(Base):
    @contextlib.contextmanager
    def guard(self, seconds=10):
        def alarm(*_):
            raise AssertionError("runner hung on a FIFO")
        old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def fifo(self, name):
        path = self.tmp_real / name
        os.mkfifo(path, 0o600)
        return path

    def test_allowlist_fifo_is_refused_promptly(self):
        with self.guard():
            gh = FakeGh()
            code, rec = self.go(gh, allow=self.fifo("allow.fifo"))
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("not a regular", " ".join(rec["reasons"]))
        self.assertTrue(self.audit.exists())

    def test_key_file_fifo_is_refused_promptly(self):
        fifo = self.fifo("key.fifo")
        self.allow.write_text(json.dumps(dict(allow_doc(), evidence_key_file=str(fifo))))
        with self.guard():
            gh = FakeGh()
            code, rec = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("not a regular", " ".join(rec["reasons"]))

    def test_evidence_fifo_is_refused_promptly(self):
        with self.guard():
            gh = FakeGh()
            code, rec = self.go(gh, evidence=self.fifo("ev.fifo"))
        self.assertEqual((code, gh.merges()), (omr.EXIT_REFUSED, []))
        self.assertIn("not a regular", " ".join(rec["reasons"]))

    def test_audit_fifo_blocks_the_merge_promptly(self):
        os.makedirs(self.audit.parent)
        os.mkfifo(self.audit, 0o600)
        with self.guard():
            gh = FakeGh()
            code, rec = self.go(gh)
        self.assertEqual(gh.merges(), [])
        self.assertNotEqual(code, omr.EXIT_OK)

    def test_device_files_are_refused_not_read(self):
        with self.guard():
            code, rec = self.go(FakeGh(), evidence=pathlib.Path("/dev/zero"))
        self.assertEqual(code, omr.EXIT_REFUSED)


class Outcomes(Base):
    def test_denial_recorded_not_retried(self):
        gh = FakeGh(merge_rc=1, merge_err="GraphQL: Merge Without Review denied: permission denied")
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_DENIED, "DENIED"))
        self.assertEqual(len(gh.merges()), 1)
        self.assertEqual(self.records()[-1]["outcome"], "DENIED")

    def test_other_gh_failure_is_error_not_retried(self):
        gh = FakeGh(merge_rc=1, merge_err="network unreachable")
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_ERROR, "ERROR"))
        self.assertEqual(len(gh.merges()), 1)

    def test_not_merged_after_success_is_unverified(self):
        gh = FakeGh(merged=False)
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_UNVERIFIED, "UNVERIFIED"))

    def test_exit_codes_are_distinct(self):
        codes = {omr.EXIT_OK, omr.EXIT_REFUSED, omr.EXIT_DENIED, omr.EXIT_ERROR, omr.EXIT_UNVERIFIED, omr.EXIT_AUDIT,
                 omr.EXIT_BASE_MISMATCH, omr.EXIT_INTERNAL, omr.EXIT_USAGE}
        self.assertEqual(len(codes), 9)

    def test_gh_unavailable_is_error(self):
        def broken(argv):
            return 1, "", "boom"
        code, rec = self.go(broken)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_ERROR, "ERROR"))

    def test_audit_unwritable_blocks_merge(self):
        gh = FakeGh()
        blocker = self.tmp_real / "file"
        blocker.write_text("x")
        code, rec = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(blocker / "log"), False, gh,
                               clock=lambda: NOW)
        self.assertNotEqual(code, omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])


class Audit(Base):
    def test_audit_written_0600_append_only_and_clean(self):
        self.go(FakeGh())
        self.go(FakeGh(base_pr(isDraft=True)))
        self.assertEqual(stat.S_IMODE(os.stat(self.audit).st_mode), 0o600)
        recs = self.records()
        self.assertEqual([r["outcome"] for r in recs], ["PENDING", "MERGED", "REFUSED"])
        for k in ("ts", "repo", "pr", "expected_head", "observed_head", "checks", "decision", "reasons", "outcome"):
            self.assertIn(k, recs[0])
        self.assertEqual(recs[1]["checks"]["success"], 2)

    def test_no_secret_shaped_content_in_audit(self):
        secret = "ghp_" + "A1b2C3d4" * 5
        gh = FakeGh(merge_rc=1, merge_err=f"permission denied token={secret} Bearer abcdef123456")
        self.go(gh)
        text = self.audit.read_text()
        self.assertNotIn(secret, text)
        self.assertNotIn("abcdef123456", text)
        self.assertNotRegex(text, omr.SECRET_RE)

    def test_audit_symlink_refused_no_merge(self):
        os.makedirs(self.audit.parent)
        victim = self.tmp_real / "victim.txt"
        victim.write_text("keep")
        self.audit.symlink_to(victim)
        gh = FakeGh()
        code, rec = self.go(gh)
        self.assertEqual(gh.merges(), [])
        self.assertEqual(victim.read_text(), "keep")
        self.assertNotEqual(code, omr.EXIT_OK)

    def test_audit_hardlink_wrong_mode_or_dir_refused(self):
        os.makedirs(self.audit.parent)
        other = self.tmp_real / "other.jsonl"
        other.write_text("")
        os.chmod(other, 0o600)
        os.link(other, self.audit)  # nlink == 2
        gh = FakeGh()
        self.assertNotEqual(self.go(gh)[0], omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])
        os.unlink(self.audit)
        self.audit.write_text("")
        os.chmod(self.audit, 0o644)
        gh = FakeGh()
        self.assertNotEqual(self.go(gh)[0], omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])
        os.chmod(self.audit, 0o600)
        os.chmod(self.audit.parent, 0o777)
        gh = FakeGh()
        try:
            self.assertNotEqual(self.go(gh)[0], omr.EXIT_OK)
        finally:
            os.chmod(self.audit.parent, 0o700)
        self.assertEqual(gh.merges(), [])

    def test_audit_symlinked_directory_refused(self):
        real = self.tmp_real / "realaudit"
        real.mkdir()
        link = self.tmp_real / "linkaudit"
        link.symlink_to(real)
        gh = FakeGh()
        code, _ = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(link / "log.jsonl"), False, gh)
        self.assertEqual((gh.merges(), code == omr.EXIT_OK), ([], False))

    def test_pending_record_fsynced_before_exec(self):
        gh, real = FakeGh(), os.fsync

        def spy(fd):
            gh.calls.append(["FSYNC"])
            return real(fd)
        omr.os.fsync = spy
        try:
            self.assertEqual(self.go(gh)[0], omr.EXIT_OK)
        finally:
            omr.os.fsync = real
        names = [c[0] if c[0] == "FSYNC" else " ".join(c[:2]) for c in gh.calls]
        self.assertLess(names.index("FSYNC"), names.index("pr merge"))

    def test_short_writes_are_looped_into_one_complete_line(self):
        real = os.write
        omr.os.write = lambda fd, data: real(fd, bytes(data[:3]))
        try:
            self.assertEqual(self.go(FakeGh())[0], omr.EXIT_OK)
        finally:
            omr.os.write = real
        recs = self.records()  # every line parses as complete JSON
        self.assertEqual([r["outcome"] for r in recs], ["PENDING", "MERGED"])

    def test_zero_progress_write_raises_and_blocks_merge(self):
        real = os.write
        omr.os.write = lambda fd, data: 0
        gh = FakeGh()
        try:
            code, rec = self.go(gh)
        finally:
            omr.os.write = real
        self.assertEqual(gh.merges(), [])
        self.assertNotEqual(code, omr.EXIT_OK)
        with self.assertRaises(omr.AuditError):
            omr.os.write = lambda fd, data: 0
            try:
                omr.write_all(1, b"abc")
            finally:
                omr.os.write = real

    def test_pending_failure_refuses_before_exec(self):
        blocker = self.tmp_real / "file"
        blocker.write_text("x")
        gh = FakeGh()
        with contextlib.redirect_stderr(io.StringIO()):
            code, rec = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(blocker / "log"), False, gh,
                               clock=lambda: NOW)
        self.assertEqual(gh.merges(), [])
        self.assertIn("refusing before execution", " ".join(rec["reasons"]))

    def test_final_record_failure_exits_with_distinct_code_and_stderr_line(self):
        gh = FakeGh()
        gh.on_merge = lambda: os.chmod(self.audit, 0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, rec = self.go(gh)
        self.assertEqual(len(gh.merges()), 1)
        self.assertEqual(code, omr.EXIT_AUDIT)
        self.assertNotEqual(code, omr.EXIT_OK)
        self.assertIn("final audit record", err.getvalue())
        self.assertIn("MERGED", err.getvalue())

    def test_scrub_redacts_common_token_shapes(self):
        for s in ("github_pat_" + "x" * 30, "ghs_" + "y" * 30, "password=hunter2", "Authorization: abc"):
            self.assertIn("[redacted]", omr.scrub(s))


class ArgvFixedness(Base):
    def test_check_argv_allows_only_fixed_shapes(self):
        ok = omr.merge_argv(REPO, "7", HEAD)
        self.assertEqual(omr.check_argv(ok), "merge")
        self.assertEqual(omr.check_argv(["api", "user"]), "user")
        self.assertEqual(omr.check_argv(omr.rules_argv(REPO, "main")), "protection")
        self.assertEqual(omr.check_argv(omr.protection_argv(REPO, "release/1.x")), "protection")
        bad = [ok + ["--admin"], ok + ["--squash"], ok[:-2], [x for x in ok if x != "--merge"],
               ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", HEAD],
               ["pr", "merge", "7", "--repo", REPO, "--merge", "--match-head-commit", "x" * 40],
               ["api", "repos/a/b/branches/main"], ["api", "repos/a/b/rules/branches/../x"],
               ["api", f"repos/{REPO}/branches/main/protection", "-X", "DELETE"],
               ["pr", "close", "7", "--repo", REPO], ["api", "user", "-X", "POST"],
               ["pr", "view", "7", "--repo", REPO, "--json", "body"], ["api", "repos/a/b/pulls/7/merge"]]
        for argv in bad:
            with self.assertRaises(RuntimeError, msg=argv):
                omr.check_argv(argv)

    def test_runner_wrapper_blocks_off_shape_calls_before_gh(self):
        gh = FakeGh()
        with self.assertRaises(RuntimeError):
            omr.Runner(gh).call(["pr", "merge", "7", "--repo", REPO, "--admin"])
        self.assertEqual(gh.calls, [])

    def test_subprocess_gh_is_shell_free_with_fixed_argv(self):
        src = (ROOT / "scripts/owner-merge-runner.py").read_text()
        self.assertIn("shell=False", src)
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        for forbidden in ("--admin", "--squash", "--rebase", "--auto", "--delete-branch"):
            self.assertNotIn(f'"{forbidden}"', src)

    def test_real_subprocess_path_with_fake_executable_in_tmp(self):
        bin_path = self.tmp_real / "fake-gh"
        log = self.tmp_real / "argv.log"
        script = ("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done >> LOG\nprintf -- '---\\n' >> LOG\n"
                  "case \"$1 $2\" in 'api user') echo '{\"login\":\"chisanan232\"}';; esac\nexit 0\n")
        bin_path.write_text(script.replace("LOG", f"'{log}'"))
        bin_path.chmod(0o755)
        code, rc_out, _ = omr.SubprocessGh(str(bin_path))(["api", "user"])
        self.assertEqual((code, json.loads(rc_out)["login"]), (0, "chisanan232"))
        self.assertEqual(log.read_text().split(), ["api", "user", "---"])

    def test_cli_has_no_gh_path_allowlist_or_audit_override(self):
        for flag in ("--gh-path", "--allowlist", "--audit-log"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts/owner-merge-runner.py"), flag, "/tmp/x",
                                "--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", "x"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, omr.EXIT_USAGE, flag)

    def test_default_paths_are_fixed_under_home_dot_claude_not_the_repo(self):
        home = os.path.expanduser("~")
        self.assertEqual(omr.DEFAULT_ALLOWLIST, os.path.join(home, ".claude", "owner-merge-runner-allowlist.json"))
        self.assertEqual(omr.DEFAULT_AUDIT, os.path.join(home, ".claude", "audit", "owner-merge-runner.jsonl"))
        self.assertFalse(str(ROOT) in omr.DEFAULT_ALLOWLIST)
        self.assertFalse((ROOT / "docs/owner-merge-runner-allowlist.json").exists())

    def test_main_uses_injected_gh_and_returns_code(self):
        gh = FakeGh()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = omr.main(["--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", str(self.evidence),
                       "--dry-run"], gh=gh, allowlist=str(self.allow), audit=str(self.audit), clock=lambda: NOW)
        self.assertEqual(rc, omr.EXIT_OK)


JOB_ID = "99"


def never_ran(**over):
    job = {"name": "ci", "head_sha": HEAD, "status": "completed", "conclusion": "failure", "steps": [],
           "runner_id": 0, "runner_name": ""}
    job.update(over)
    return (0, job)


def unavailable(verdict="FAILURE", url=None, name="ci"):
    return [{"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": verdict,
             "detailsUrl": f"https://github.com/{REPO}/actions/runs/5/job/{JOB_ID}" if url is None else url},
            {"__typename": "StatusContext", "context": "SonarCloud Code Analysis", "state": "SUCCESS"}]


class LocalEquivalentCi(Base):
    """Founder LOCAL_EQUIVALENT_CI exception: only for required checks GitHub reports as never started, covered
    by pinned local evidence. An executed failure (CI_FAILED) is never bypassed."""

    LOCAL = {"classification": "LOCAL_EQUIVALENT_PASS", "head": HEAD, "run_at": "2026-10-20T11:00:00Z",
             "checks": [{"name": "ci", "exit_code": 0, "tests": 12}]}

    def setUp(self):
        super().setUp()
        self.enable()

    def enable(self, on=True, **local_over):
        self.allow.write_text(json.dumps(allow_doc(local_equivalent_ci=on)))
        local = dict(self.LOCAL, **local_over)
        for k in [k for k, v in local.items() if v is DROP]:
            del local[k]
        self.write_evidence(local_ci=local)

    def gh(self, rollup=None, job=None, **kw):
        return FakeGh(base_pr(statusCheckRollup=rollup if rollup is not None else unavailable()),
                      jobs={JOB_ID: job or never_ran()}, **kw)

    def test_never_started_job_with_local_evidence_is_approved_and_not_labelled_ci_pass(self):
        gh = self.gh()
        code, rec = self.go(gh, dry=True)
        self.assertEqual(code, omr.EXIT_OK, rec)
        self.assertEqual(rec["checks"]["ci_class"], "CI_UNAVAILABLE_EXTERNAL")
        self.assertEqual(rec["checks"]["local_equivalent"], ["ci"])

    def test_green_ci_stays_ci_pass(self):
        code, rec = self.go(FakeGh(), dry=True)
        self.assertEqual(code, omr.EXIT_OK, rec)
        self.assertEqual((rec["checks"]["ci_class"], rec["checks"]["local_equivalent"]), ("CI_PASS", []))

    def test_real_merge_runs_once_and_audit_records_the_exception(self):
        gh = self.gh()
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_OK, "MERGED"), rec)
        self.assertEqual(len(gh.merges()), 1)
        self.assertEqual(self.records()[0]["checks"]["ci_class"], "CI_UNAVAILABLE_EXTERNAL")  # the PENDING record

    def test_disabled_in_allowlist_refuses(self):
        self.enable(on=False)
        self.assertRefused(self.gh(), "check ci is FAILURE")

    def test_no_local_ci_block_refuses(self):
        self.write_evidence()
        self.assertRefused(self.gh(), "check ci is FAILURE")

    def test_executed_failure_is_never_exempted(self):
        for job in (never_ran(steps=[{"name": "run tests"}]), never_ran(runner_id=17), never_ran(runner_name="GitHub Actions 3"),
                    never_ran(head_sha=OTHER), never_ran(name="other"), never_ran(status="in_progress"),
                    (0, {"name": "ci"})):
            with self.subTest(job=job):
                self.assertRefused(self.gh(job=job), "check ci is FAILURE")

    def test_only_not_started_verdicts_qualify(self):
        for verdict in ("TIMED_OUT", "SKIPPED", "NEUTRAL", "ACTION_REQUIRED", "IN_PROGRESS", "QUEUED"):
            with self.subTest(verdict=verdict):
                self.assertRefused(self.gh(rollup=unavailable(verdict)), f"check ci is {verdict}")
        for verdict in ("FAILURE", "STARTUP_FAILURE", "CANCELLED"):
            with self.subTest(verdict=verdict):
                code, rec = self.go(self.gh(rollup=unavailable(verdict)), dry=True)
                self.assertEqual(code, omr.EXIT_OK, rec)

    def test_details_url_must_be_a_job_of_this_repo(self):
        for url in (None, "", "https://github.com/other/repo/actions/runs/5/job/99",
                    f"https://github.com/{REPO}/actions/runs/5", f"https://evil.example/{REPO}/actions/runs/5/job/99",
                    f"https://github.com/{REPO}/actions/runs/5/job/99/../1", f"https://github.com/{REPO}/actions/runs/5/job/99?x=1"):
            with self.subTest(url=url):
                roll = unavailable(url="" if url is None else url)
                if url is None:
                    del roll[0]["detailsUrl"]
                self.assertRefused(self.gh(rollup=roll, job=never_ran()), "check ci is FAILURE")

    def test_a_non_actions_check_run_of_the_same_name_blocks_the_exemption(self):
        def run(slug, **over):
            r = {"name": "ci", "head_sha": HEAD, "app": {"slug": slug}}
            r.update(over)
            return r
        cases = {"integration": [run("github-actions"), run("evil-checks-app")], "only integration": [run("evil-checks-app")],
                 "other head": [run("github-actions", head_sha=OTHER)], "other name": [run("github-actions", name="x")],
                 "no app": [run("github-actions", app=None)], "empty": []}
        for label, runs in cases.items():
            with self.subTest(label):
                gh = self.gh()
                gh.runs = lambda name, runs=runs: {"total_count": len(runs), "check_runs": runs}
                self.assertRefused(gh, "check ci is FAILURE")
        gh = self.gh()
        gh.runs = lambda name: {"total_count": 150, "check_runs": [run("github-actions")]}  # truncated listing
        self.assertRefused(gh, "check ci is FAILURE")

    def test_second_evaluation_cannot_change_the_ci_class_unnoticed(self):
        gh = FakeGh(base_pr(), jobs={JOB_ID: never_ran()})
        gh.later = base_pr(statusCheckRollup=unavailable())  # green on the first pass, unavailable from the third read on
        code, rec = self.go(gh)
        self.assertEqual(code, omr.EXIT_REFUSED, rec)
        self.assertIn("changed before execution", " | ".join(rec["reasons"]))
        self.assertEqual(gh.merges(), [])

    def test_status_context_is_never_exempted(self):
        roll = [{"__typename": "StatusContext", "context": "ci", "state": "FAILURE"}, unavailable()[1]]
        self.assertRefused(self.gh(rollup=roll), "check ci is FAILURE")

    def test_local_evidence_must_pass_at_this_head_and_be_fresh(self):
        cases = {"exit": dict(checks=[{"name": "ci", "exit_code": 1, "tests": 12}]),
                 "bool exit": dict(checks=[{"name": "ci", "exit_code": False, "tests": 1}]),
                 "head": dict(head=OTHER), "class": dict(classification="CI_PASS"),
                 "stale": dict(run_at="2026-10-01T00:00:00Z"), "future": dict(run_at="2026-10-21T00:00:00Z"),
                 "bad time": dict(run_at="yesterday"), "other check": dict(checks=[{"name": "lint", "exit_code": 0, "tests": 1}]),
                 "dup": dict(checks=[{"name": "ci", "exit_code": 0, "tests": 1}] * 2),
                 "empty": dict(checks=[]), "extra key": dict(note="x"), "missing key": dict(run_at=DROP),
                 "entry extra": dict(checks=[{"name": "ci", "exit_code": 0, "tests": 1, "x": 1}]),
                 "neg tests": dict(checks=[{"name": "ci", "exit_code": 0, "tests": -1}])}
        for label, over in cases.items():
            with self.subTest(label):
                self.enable(**over)
                code, rec = self.go(self.gh())
                self.assertNotEqual(code, omr.EXIT_OK, rec)
                self.assertTrue(code == omr.EXIT_REFUSED, rec)

    def test_missing_required_check_is_never_exempted(self):
        self.assertRefused(self.gh(rollup=[unavailable()[1]]), "required check ci is missing")

    def test_unrelated_or_unlisted_failures_still_block(self):
        roll = unavailable() + [{"__typename": "CheckRun", "name": "gitleaks", "status": "COMPLETED", "conclusion": "FAILURE",
                                 "detailsUrl": f"https://github.com/{REPO}/actions/runs/5/job/98"}]
        self.assertRefused(self.gh(rollup=roll, job=never_ran()), "check gitleaks is FAILURE")

    def test_one_executed_duplicate_of_the_check_blocks_the_whole_check(self):
        roll = unavailable() + [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE",
                                 "detailsUrl": f"https://github.com/{REPO}/actions/runs/6/job/98"}]
        gh = FakeGh(base_pr(statusCheckRollup=roll), jobs={JOB_ID: never_ran(), "98": never_ran(steps=[{"name": "t"}])})
        self.assertRefused(gh, "check ci is FAILURE")

    def test_job_read_failure_or_odd_shape_is_an_error_and_never_merges(self):
        for job in ((1, None), (0, ["x"])):
            with self.subTest(job=job):
                gh = self.gh(job=job)
                code, rec = self.go(gh)
                self.assertEqual(code, omr.EXIT_ERROR, rec)
                self.assertEqual(gh.merges(), [])

    def test_job_state_change_between_passes_refuses(self):
        state = {"n": 0}

        def flip():
            state["n"] += 1
            return never_ran() if state["n"] <= 1 else never_ran(steps=[{"name": "run tests"}])

        gh = self.gh(job=flip)
        code, rec = self.go(gh)
        self.assertEqual(code, omr.EXIT_REFUSED, rec)
        self.assertEqual(gh.merges(), [])

    def test_exception_does_not_relax_review_or_other_guards(self):
        self.write_evidence(local_ci=dict(self.LOCAL), verdict="FAIL")
        self.assertRefused(self.gh(), "verdict is not PASS")
        self.enable()
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=unavailable(), isDraft=True), jobs={JOB_ID: never_ran()}), "draft")
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=unavailable(), mergeStateStatus="BLOCKED"),
                                  jobs={JOB_ID: never_ran()}), "mergeStateStatus")

    def test_signed_evidence_covers_the_local_block(self):
        key = self.tmp_real / "k"
        key.write_bytes(b"k" * 40)
        key.chmod(0o600)
        doc = allow_doc(local_equivalent_ci=True, require_signed_evidence=True)
        doc["evidence_key_file"] = str(key)
        self.allow.write_text(json.dumps(doc))
        ev = json.loads(self.evidence.read_text())
        ev["hmac"] = omr.evidence_mac(b"k" * 40, ev)
        self.evidence.write_text(json.dumps(ev))
        code, rec = self.go(self.gh(), dry=True)
        self.assertEqual(code, omr.EXIT_OK, rec)
        ev["local_ci"]["checks"][0]["tests"] = 13  # tampering after signing
        self.evidence.write_text(json.dumps(ev))
        self.assertRefused(self.gh(), "signature")

    def test_invalid_config_value_is_refused(self):
        self.allow.write_text(json.dumps(allow_doc(local_equivalent_ci="yes")))
        self.assertRefused(self.gh(), "allowlist config")

    def test_check_argv_job_shape(self):
        self.assertEqual(omr.check_argv(["api", f"repos/{REPO}/actions/jobs/99"]), "job")
        sha = "a" * 40
        self.assertEqual(omr.check_argv(["api", f"repos/{REPO}/commits/{sha}/check-runs?check_name=ci&per_page=100"]), "check-runs")
        for bad in (["api", f"repos/{REPO}/commits/{sha}/check-runs?check_name=ci&per_page=100&x=1"],
                    ["api", f"repos/{REPO}/commits/{sha}/check-runs?check_name=a b&per_page=100"],
                    ["api", f"repos/{REPO}/commits/main/check-runs?check_name=ci&per_page=100"]):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                omr.check_argv(bad)
        for bad in (["api", f"repos/{REPO}/actions/jobs/9x"], ["api", f"repos/{REPO}/actions/jobs/99/logs"],
                    ["api", "repos/../x/actions/jobs/99"], ["api", f"repos/{REPO}/actions/jobs/99", "-X", "POST"],
                    ["api", "-X", "POST", f"repos/{REPO}/actions/jobs/99"]):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                omr.check_argv(bad)


def approval(login, oid=HEAD, state="APPROVED", assoc="COLLABORATOR"):
    return {"author": {"login": login}, "state": state, "commit": {"oid": oid}, "authorAssociation": assoc}


class IndependentReviewReadback(Base):
    """Security-sensitive merges: an APPROVED GitHub review at the exact head by a second, allowlisted identity.
    The local evidence file (even signed) is same-user tamper evidence, not this."""

    def setUp(self):
        super().setUp()
        self.allow.write_text(json.dumps(allow_doc(independent_github_reviewers=["Second-Reviewer"])))

    def check(self, reviews, ok):
        gh = FakeGh(base_pr(reviews=reviews))
        if ok:
            code, rec = self.go(gh, dry=True)
            self.assertEqual(code, omr.EXIT_OK, rec)
        else:
            self.assertRefused(gh, "allowlisted independent reviewer")

    def test_second_identity_approval_at_head_passes_case_insensitively(self):
        self.check([approval("second-reviewer")], True)

    def test_missing_wrong_stale_or_unqualified_approvals_refuse(self):
        self.check([], False)
        self.check([approval("someone-else")], False)
        self.check([approval("second-reviewer", oid=OTHER)], False)
        self.check([approval("second-reviewer", state="COMMENTED")], False)
        self.check([approval("second-reviewer", assoc="NONE")], False)
        self.check([approval("second-reviewer"), approval("second-reviewer", state="DISMISSED")], False)

    def test_reviewer_cannot_be_the_author_or_the_authenticated_user(self):
        self.allow.write_text(json.dumps(allow_doc(independent_github_reviewers=[ME])))
        self.check([approval(ME)], False)  # the PR author and the merging user is the same identity
        self.allow.write_text(json.dumps(allow_doc(independent_github_reviewers=["Second-Reviewer"])))
        gh = FakeGh(base_pr(author={"login": "second-reviewer"}, reviews=[approval("second-reviewer", assoc="OWNER")]))
        code, rec = self.go(gh)
        self.assertNotEqual(code, omr.EXIT_OK, rec)

    def test_a_later_changes_requested_from_the_reviewer_refuses(self):
        self.check([approval("second-reviewer"), approval("second-reviewer", state="CHANGES_REQUESTED")], False)

    def test_default_has_no_such_requirement_and_bad_config_refuses(self):
        self.allow.write_text(json.dumps(allow_doc()))
        code, rec = self.go(FakeGh(), dry=True)
        self.assertEqual(code, omr.EXIT_OK, rec)
        for bad in ("x", ["bad login!"], [1], ["a", None]):
            with self.subTest(bad=bad):
                self.allow.write_text(json.dumps(allow_doc(independent_github_reviewers=bad)))
                self.assertRefused(FakeGh(), "allowlist config")


if __name__ == "__main__":
    unittest.main()
