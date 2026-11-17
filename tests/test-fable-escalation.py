#!/usr/bin/env python3
"""Fable escalation: trigger, approval, one-per-blocker, circuit breaker and fallback. Never calls a real model."""
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("fable_escalation", ROOT / "scripts/fable-escalation.py")
fe = module_from_spec(spec)
spec.loader.exec_module(fe)

T0 = datetime(2026, 11, 17, 9, 0, 0, tzinfo=timezone.utc)
FABLE_OK = {"is_error": False, "result": "Conclusion: split the lock.", "total_cost_usd": 0.9, "duration_ms": 41000,
            "modelUsage": {"claude-fable-5-2": {}}}
# A disposable stand-in for the claude CLI: prints the scenario file's stdout and exits with its code.
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys
if "--version" in sys.argv:
    print(os.environ.get("FAKE_VERSION", "2.1.325 (Claude Code)")); sys.exit(0)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
print(os.environ["FAKE_OUT"]); sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


def case(**over):
    base = {"target": "o/r#7", "acceptance_check": "AC3 lock ordering test passes", "blocker": "deadlock under AC3",
            "attempts": [{"model": "claude-opus-5-5", "approach": "reorder acquisition", "failure": "AC3 deadlock at step 4"},
                         {"model": "claude-opus-5-5", "approach": "single global lock", "failure": "AC3 deadlock at step 4"}],
            "question": "Which lock order removes the cycle between the cache and the index?", "cause": "none",
            "inputs_non_sensitive": True, "spent": {"rework_rounds": 2, "elapsed_s": 3600, "usd": 3.1}}
    base.update(over)
    return base


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "fable")
        os.makedirs(os.path.join(self.dir, "advice"), mode=0o700)
        self.ask = os.path.join(self.tmp.name, "asks")
        os.makedirs(self.ask, mode=0o700)
        self.bin = os.path.join(self.tmp.name, "claude")
        Path(self.bin).write_text(FAKE_CLAUDE)
        os.chmod(self.bin, 0o755)
        self.log = os.path.join(self.tmp.name, "calls.log")
        self.env = {"FABLE_CLAUDE_BIN": self.bin, "FAKE_LOG": self.log, "FAKE_OUT": json.dumps(FABLE_OK)}
        self.saved = {k: os.environ.get(k) for k in [*self.env, *fe.API_ENV, "FAKE_RC", "FAKE_VERSION"]}
        for k in (*fe.API_ENV, "FAKE_RC", "FAKE_VERSION"):
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def calls(self):
        return len(Path(self.log).read_text().splitlines()) if os.path.exists(self.log) else 0

    def offered(self, **over):
        return fe.offer(self.dir, case(**over), "sess-1", self.ask, T0)

    def approved(self, **over):
        packet = self.offered(**over)
        fe.answer(self.dir, packet["id"], "approve", "owner said: approve the Fable consult", "sess-1", self.ask, T0)
        return packet["id"]


class Trigger(Env):
    def test_two_distinct_opus_attempts_with_same_failure_is_eligible(self):
        v = fe.evaluate(case(), self.dir)
        self.assertTrue(v["eligible"], v)
        self.assertEqual(v["trigger"], "two_distinct_attempts_same_failure")

    def test_one_attempt_or_the_same_approach_twice_is_not_a_trigger(self):
        one = case(attempts=case()["attempts"][:1])
        same = case(attempts=[case()["attempts"][0], dict(case()["attempts"][0])])
        for c in (one, same):
            v = fe.evaluate(c, self.dir)
            self.assertFalse(v["eligible"])
            self.assertTrue(any("no trigger" in r for r in v["reasons"]))

    def test_different_failures_are_not_the_same_acceptance_failure(self):
        a = case()["attempts"]
        a[1]["failure"] = "AC3 times out in setup"
        self.assertFalse(fe.evaluate(case(attempts=a), self.dir)["eligible"])

    def test_non_opus_attempts_do_not_count(self):
        a = [dict(x, model="claude-haiku-5-5") for x in case()["attempts"]]
        self.assertFalse(fe.evaluate(case(attempts=a), self.dir)["eligible"])

    def test_unresolved_review_disagreement_triggers_with_two_positions(self):
        review = {"unresolved_architecture_disagreement": True, "positions": ["queue per tenant", "shared queue with quotas"]}
        v = fe.evaluate(case(attempts=[], review=review), self.dir)
        self.assertEqual(v["trigger"], "unresolved_review_disagreement")
        self.assertTrue(v["eligible"], v)
        one = dict(review, positions=["queue per tenant"])
        self.assertFalse(fe.evaluate(case(attempts=[], review=one), self.dir)["eligible"])

    def test_excluded_causes_never_trigger(self):
        for cause in ("queue_delay", "ci_unavailable", "missing_permission", "external_outage"):
            v = fe.evaluate(case(cause=cause), self.dir)
            self.assertFalse(v["eligible"], cause)
            self.assertEqual(v["on_ineligible"], "continue independent work")

    def test_failure_text_naming_infrastructure_is_excluded_even_when_cause_says_none(self):
        for text in ("CI_UNAVAILABLE_EXTERNAL billing limit", "permission denied on merge", "ECONNRESET from provider"):
            a = [dict(x, failure=text) for x in case()["attempts"]]
            v = fe.evaluate(case(attempts=a), self.dir)
            self.assertFalse(v["eligible"], text)

    def test_sensitive_or_unattested_input_and_bad_questions_are_refused(self):
        self.assertFalse(fe.evaluate(case(inputs_non_sensitive=False), self.dir)["eligible"])
        self.assertFalse(fe.evaluate({k: v for k, v in case().items() if k != "inputs_non_sensitive"}, self.dir)["eligible"])
        for q in ("What should I do about it", "Which order? And why does the cache lock?", "Which order?\nAlso the index?"):
            self.assertFalse(fe.evaluate(case(question=q), self.dir)["eligible"], q)

    def test_evaluate_writes_nothing(self):
        before = sorted(os.listdir(self.dir))
        fe.evaluate(case(), self.dir)
        self.assertEqual(sorted(os.listdir(self.dir)), before)


class ApprovalAndDedup(Env):
    def test_offer_carries_blocker_attempts_and_one_question_and_records_the_pending_item(self):
        packet = self.offered()
        self.assertEqual(len(packet["attempts_made"]), 2)
        self.assertEqual(packet["question_for_fable"].count("?"), 1)
        self.assertIn("usage credits stay off", packet["route"])
        self.assertFalse(packet["pending_ledger_item"].startswith("not-recorded"), packet)
        self.assertEqual(self.calls(), 0)

    def test_offer_makes_no_model_call_and_consult_before_approval_is_refused(self):
        packet = self.offered()
        with self.assertRaisesRegex(fe.EscalationError, "OFFERED"):
            fe.consult(self.dir, packet["id"], True, self.tmp.name, T0)
        self.assertEqual(self.calls(), 0)

    def test_second_offer_for_the_same_blocker_is_refused_even_with_more_attempts_or_new_wording(self):
        self.offered()
        more = case(blocker="reworded deadlock summary",
                    attempts=case()["attempts"] + [{"model": "claude-opus-5-5", "approach": "lock-free index", "failure": "AC3 deadlock at step 4"}])
        with self.assertRaisesRegex(fe.EscalationError, "one offer and one consult per blocker"):
            fe.offer(self.dir, more, "sess-2", self.ask, T0)

    def test_two_sessions_share_one_pending_item(self):
        a = self.offered()
        ap = fe._ask_pending()
        self.assertEqual(len([i for i in ap.all_items(self.ask) if i["state"] == ap.PENDING]), 1)
        self.assertEqual(a["pending_ledger_item"], ap.all_items(self.ask)[0]["id"])

    def test_decline_closes_the_blocker_and_never_reoffers_or_consults(self):
        packet = self.offered()
        fe.answer(self.dir, packet["id"], "decline", "owner said: not now, skip it", "sess-1", self.ask, T0)
        self.assertEqual(fe.load(self.dir, packet["id"])["state"], fe.DECLINED)
        with self.assertRaises(fe.EscalationError):
            fe.consult(self.dir, packet["id"], True, self.tmp.name, T0)
        with self.assertRaises(fe.EscalationError):
            self.offered()
        self.assertEqual(self.calls(), 0)

    def test_no_reply_leaves_the_blocker_offered_and_runs_nothing(self):
        packet = self.offered()
        self.assertEqual(fe.load(self.dir, packet["id"])["state"], fe.OFFERED)
        self.assertEqual(self.calls(), 0)

    def test_answer_needs_a_reference_and_only_applies_to_an_offered_blocker(self):
        packet = self.offered()
        with self.assertRaises(fe.EscalationError):
            fe.answer(self.dir, packet["id"], "approve", "", "sess-1", self.ask, T0)
        fe.answer(self.dir, packet["id"], "approve", "owner said: approve this one", "sess-1", self.ask, T0)
        with self.assertRaises(fe.EscalationError):
            fe.answer(self.dir, packet["id"], "decline", "owner said: changed my mind", "sess-1", self.ask, T0)

    def test_answer_text_is_stored_only_as_a_hash(self):
        bid = self.approved()
        self.assertNotIn("approve the Fable consult", Path(fe._record_path(self.dir, bid)).read_text())

    def test_state_files_are_private(self):
        bid = self.approved()
        self.assertEqual(stat.S_IMODE(os.stat(fe._record_path(self.dir, bid)).st_mode), 0o600)


class Consult(Env):
    def test_dry_run_prints_the_command_shape_and_calls_nothing(self):
        bid = self.approved()
        out = fe.consult(self.dir, bid, False, self.tmp.name, T0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.calls(), 0)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.APPROVED)

    def test_consult_is_read_only_subscription_shaped_and_pinned_to_fable(self):
        argv = fe.consult_argv(fe.load(self.dir, self.approved()))
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-2")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--max-budget-usd"):
            self.assertIn(flag, argv)

    def test_approved_consult_runs_once_returns_advice_and_records_cost(self):
        bid = self.approved()
        out = fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.assertEqual(Path(out["advice_path"]).read_text(), FABLE_OK["result"])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.CONSULTED)
        with self.assertRaises(fe.EscalationError):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.assertEqual(self.calls(), 1)

    def test_stale_approval_is_refused(self):
        bid = self.approved()
        late = T0 + timedelta(seconds=fe.APPROVAL_TTL_S + 1)
        with self.assertRaisesRegex(fe.EscalationError, "24h"):
            fe.consult(self.dir, bid, True, self.tmp.name, late)
        self.assertEqual(self.calls(), 0)

    def test_api_credential_in_environment_refuses_the_call_without_reading_it(self):
        bid = self.approved()
        os.environ["ANTHROPIC_API_KEY"] = "x"
        with self.assertRaisesRegex(fe.EscalationError, "subscription only"):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.assertEqual(self.calls(), 0)

    def test_old_claude_version_is_a_route_failure(self):
        bid = self.approved()
        os.environ["FAKE_VERSION"] = "2.1.306 (Claude Code)"
        with self.assertRaises(fe.EscalationError):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.assertTrue(fe.breaker(self.dir)["open"])
        self.assertEqual(self.calls(), 0)


class BreakerAndFallback(Env):
    def failing(self, out, rc="0"):
        os.environ["FAKE_OUT"], os.environ["FAKE_RC"] = json.dumps(out), rc

    def test_wrong_effective_model_opens_the_breaker_and_blocks_new_offers(self):
        self.failing(dict(FABLE_OK, modelUsage={"claude-opus-5-5": {}}))
        bid = self.approved()
        with self.assertRaisesRegex(fe.EscalationError, "breaker opened"):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.assertEqual(fe.breaker(self.dir)["reason"], "model_mismatch")
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.FAILED)
        other = case(target="o/r#8", acceptance_check="AC9 other")
        v = fe.evaluate(other, self.dir)
        self.assertFalse(v["eligible"])
        self.assertTrue(any("circuit breaker open" in r for r in v["reasons"]))

    def test_fable_plus_a_fallback_model_is_a_mismatch(self):
        self.failing(dict(FABLE_OK, modelUsage={"claude-fable-5-2": {}, "claude-sonnet-5-5": {}}))
        with self.assertRaises(fe.EscalationError):
            fe.consult(self.dir, self.approved(), True, self.tmp.name, T0)
        self.assertTrue(fe.breaker(self.dir)["open"])

    def test_error_result_nonzero_exit_and_unparseable_output_all_trip_the_breaker(self):
        for out, rc in ((dict(FABLE_OK, is_error=True, result=None), "0"), (FABLE_OK, "1"), ("not json", "0")):
            self.tearDown()
            self.setUp()
            os.environ["FAKE_OUT"], os.environ["FAKE_RC"] = (out if isinstance(out, str) else json.dumps(out)), rc
            bid = self.approved()
            with self.assertRaises(fe.EscalationError):
                fe.consult(self.dir, bid, True, self.tmp.name, T0)
            self.assertEqual(fe.breaker(self.dir)["reason"], "route_failure")
            self.assertEqual(self.calls(), 1)  # one consult spent, never retried

    def test_failed_consult_is_not_retried_for_the_same_blocker(self):
        self.failing(dict(FABLE_OK, is_error=True))
        bid = self.approved()
        with self.assertRaises(fe.EscalationError):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.failing(FABLE_OK)
        with self.assertRaises(fe.EscalationError):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        self.assertEqual(self.calls(), 1)

    def test_advice_that_does_not_advance_opens_the_breaker(self):
        bid = self.approved()
        fe.consult(self.dir, bid, True, self.tmp.name, T0)
        fe.verdict(self.dir, bid, "not_advanced", {"rework_rounds": 3}, T0)
        self.assertEqual(fe.breaker(self.dir)["reason"], "no_advance")
        self.assertFalse(fe.evaluate(case(target="o/r#9", acceptance_check="AC1 new"), self.dir)["eligible"])

    def test_advancing_advice_keeps_the_breaker_closed_and_a_new_blocker_can_still_escalate(self):
        bid = self.approved()
        fe.consult(self.dir, bid, True, self.tmp.name, T0)
        fe.verdict(self.dir, bid, "advanced", {"rework_rounds": 1, "elapsed_s": 900, "usd": 1.2}, T0)
        self.assertFalse(fe.breaker(self.dir)["open"])
        self.assertTrue(fe.evaluate(case(target="o/r#9", acceptance_check="AC1 new"), self.dir)["eligible"])

    def test_open_breaker_refuses_consult_of_an_already_approved_blocker_and_only_an_owner_reset_closes_it(self):
        bid = self.approved()
        fe.trip(self.dir, "route_failure", None, T0)
        with self.assertRaisesRegex(fe.EscalationError, "breaker open"):
            fe.consult(self.dir, bid, True, self.tmp.name, T0)
        with self.assertRaises(fe.EscalationError):
            fe.reset_breaker(self.dir, "", T0)
        fe.reset_breaker(self.dir, "route re-verified by canary on 2.1.325", T0)
        self.assertFalse(fe.breaker(self.dir)["open"])

    def test_verdict_needs_a_consulted_blocker(self):
        bid = self.approved()
        with self.assertRaises(fe.EscalationError):
            fe.verdict(self.dir, bid, "advanced", {}, T0)

    def test_report_makes_no_reduction_claim_without_a_baseline(self):
        bid = self.approved()
        fe.consult(self.dir, bid, True, self.tmp.name, T0)
        fe.verdict(self.dir, bid, "advanced", {"rework_rounds": 1, "elapsed_s": 900, "usd": 1.2}, T0)
        out = fe.report(self.dir)
        self.assertEqual(out["closed"][0]["spent_at_offer"]["rework_rounds"], 2)
        self.assertIn("no control arm", out["note"])


class ReviewFixes(Env):
    def test_non_object_json_result_trips_the_breaker_instead_of_crashing(self):
        for out in ("[]", "null", "7", json.dumps({"is_error": False, "modelUsage": {"claude-fable-5-2": {}}, "result": 5})):
            self.tearDown()
            self.setUp()
            os.environ["FAKE_OUT"] = out
            bid = self.approved()
            with self.assertRaisesRegex(fe.EscalationError, "breaker opened"):
                fe.consult(self.dir, bid, True, self.tmp.name, T0)
            self.assertTrue(fe.breaker(self.dir)["open"], out)
            self.assertEqual(fe.load(self.dir, bid)["state"], fe.FAILED)

    def test_target_case_and_check_punctuation_do_not_make_a_new_blocker(self):
        self.offered()
        for over in ({"target": "O/R#7"}, {"acceptance_check": "AC3  Lock ordering test passes."}):
            v = fe.evaluate(case(**over), self.dir)
            self.assertFalse(v["eligible"], over)

    def test_more_infrastructure_wordings_are_excluded(self):
        for text in ("HTTP 403 forbidden", "Permission to merge was denied", "503 overloaded", "runner unavailable", "EACCES on write",
                     "connection timed out", "HTTP/1.1 503"):
            a = [dict(x, failure=text) for x in case()["attempts"]]
            self.assertFalse(fe.evaluate(case(attempts=a), self.dir)["eligible"], text)

    def test_alternate_provider_variables_refuse_the_call(self):
        for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            bid = self.approved(acceptance_check="AC " + name)
            os.environ[name] = "1"
            try:
                with self.assertRaisesRegex(fe.EscalationError, "subscription only"):
                    fe.consult(self.dir, bid, True, self.tmp.name, T0)
            finally:
                del os.environ[name]
        self.assertEqual(self.calls(), 0)

    def test_attempt_text_goes_on_stdin_not_argv(self):
        argv = fe.consult_argv(fe.load(self.dir, self.approved()))
        self.assertFalse(any("deadlock at step 4" in a for a in argv))
        self.assertIn("deadlock at step 4", fe.consult_prompt(fe.load(self.dir, self.approved(acceptance_check="AC other"))))

    def test_ordinary_reasoning_failures_are_not_excluded(self):
        for text in ("test_deadlock timed out at step 4", "assert failed at line 403", "exit 503 from the lock test"):
            a = [dict(x, failure=text) for x in case()["attempts"]]
            self.assertTrue(fe.evaluate(case(attempts=a), self.dir)["eligible"], text)

    def test_prompt_reaches_the_child_on_stdin(self):
        fake = Path(self.bin)
        fake.write_text(FAKE_CLAUDE.replace('open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")',
                                            'open(os.environ["FAKE_LOG"], "a").write(sys.stdin.read().replace("\\n", " ") + "\\n")'))
        fe.consult(self.dir, self.approved(), True, self.tmp.name, T0)
        logged = Path(self.log).read_text()
        self.assertIn("deadlock at step 4", logged)
        self.assertIn("Which lock order removes the cycle", logged)

    def test_version_probe_timeout_trips_the_breaker(self):
        bid = self.approved()
        real = fe.claude_version
        fe.claude_version = lambda: (_ for _ in ()).throw(fe.subprocess.TimeoutExpired("claude", 30))
        try:
            with self.assertRaises(fe.EscalationError):
                fe.consult(self.dir, bid, True, self.tmp.name, T0)
        finally:
            fe.claude_version = real
        self.assertEqual(fe.breaker(self.dir)["reason"], "route_failure")
        self.assertEqual(self.calls(), 0)

    def test_first_breaker_reason_is_kept(self):
        fe.trip(self.dir, "route_failure", None, T0)
        fe.trip(self.dir, "no_advance", None, T0)
        self.assertEqual(fe.breaker(self.dir)["reason"], "route_failure")

    def test_unreadable_record_is_refused_not_overwritten(self):
        bid = fe.fingerprint("o/r#7", case()["acceptance_check"])
        Path(fe._record_path(self.dir, bid)).write_text("{corrupt")
        self.assertFalse(fe.evaluate(case(), self.dir)["eligible"])


class RepoInvariants(unittest.TestCase):
    def test_no_scheduler_hook_or_workflow_calls_consult(self):
        for path in list((ROOT / ".github").rglob("*")) + list((ROOT / ".claude").rglob("*")):
            if path.is_file():
                self.assertNotIn("fable-escalation.py consult", path.read_text(errors="ignore"), str(path))


if __name__ == "__main__":
    unittest.main()
