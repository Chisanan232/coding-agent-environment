#!/usr/bin/env python3
"""Fable escalation: trigger, approval, one-per-blocker, circuit breaker and fallback. Never calls a real model."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
        # The first launch of a freshly written executable can stall for tens of seconds on macOS under load; absorb it here so it
        # cannot land inside the 30 s version probe and look like a route failure.
        subprocess.run([self.bin, "--version"], capture_output=True, timeout=300, env=dict(os.environ, FAKE_VERSION="warm"))
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


class WorkflowBase(Env):
    """Helpers to drive the real CLI the way the development loop calls it."""
    SCRIPT = str(ROOT / "scripts/fable-escalation.py")

    def cli(self, *argv, rc=0):
        env = dict(os.environ, FABLE_ESCALATION_DIR=self.dir, ASK_PENDING_DIR=self.ask)
        proc = subprocess.run([sys.executable, self.SCRIPT, *argv], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def attempt(self, approach, failure="AC3 deadlock at step 4", model="claude-opus-5-5", target="o/r#7", check="AC3 lock ordering",
                question="Which lock order removes the cycle between the cache and the index?", **extra):
        argv = ["attempt", "--target", target, "--check", check, "--session", "dev-1", "--model", model, "--approach", approach,
                "--failure", failure, "--question", question, "--non-sensitive"]
        for k, v in extra.items():
            argv += [f"--{k.replace('_', '-')}", v] if v is not True else [f"--{k.replace('_', '-')}"]
        return self.cli(*argv)

    def asked(self):
        self.attempt("reorder acquisition")
        return self.attempt("single global lock")["id"]


class WorkflowCases(WorkflowBase):
    """Controlled cases through the real CLI."""
    def test_two_distinct_failed_attempts_ask_exactly_once_across_any_number_of_later_calls(self):
        self.assertEqual(self.attempt("reorder acquisition")["action"], "CONTINUE")
        second = self.attempt("single global lock")
        self.assertEqual(second["action"], "ASK_OWNER")
        self.assertEqual(second["packet"]["question_for_fable"].count("?"), 1)
        for approach in ("lock-free index", "lock-free index", "retry with backoff"):
            self.assertEqual(self.attempt(approach)["action"], "CONTINUE")
        self.assertEqual(len([r for r in Path(self.dir, "receipts.log").read_text().splitlines() if '"offered"' in r]), 1)
        self.assertEqual(self.calls(), 0)

    def test_unset_ledger_dir_defaults_to_the_home_ledger_and_a_missing_override_is_created(self):
        # The production case: no ASK_PENDING_DIR. The offer must still reach the pending-ASK ledger (under HOME), not be skipped.
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        env = {k: v for k, v in os.environ.items() if k != "ASK_PENDING_DIR"}
        env.update(HOME=home, FABLE_ESCALATION_DIR=self.dir)
        def run(approach):
            argv = ["attempt", "--target", "o/r#7", "--check", "AC3 lock ordering", "--session", "dev-1", "--model", "claude-opus-5-5",
                    "--approach", approach, "--failure", "AC3 deadlock at step 4", "--question", "Which lock order removes the cycle?",
                    "--non-sensitive"]
            proc = subprocess.run([sys.executable, self.SCRIPT, *argv], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        run("reorder acquisition")
        ledger = run("single global lock")["packet"]["pending_ledger_item"]
        self.assertTrue(ledger and not str(ledger).startswith("not-recorded"), ledger)
        self.assertTrue(os.path.isdir(os.path.join(home, ".cache", "coding-agent-environment", "asks")))
        # An override that names a directory that does not exist yet is created, not a "No such file" failure.
        missing = os.path.join(self.tmp.name, "fresh", "asks")
        fe._ledger_packet({"id": "x1", "blocker": "b", "target": "o/r#7", "trigger": "t", "attempts": [], "question": "q?"},
                          "dev-1", missing, T0)
        self.assertTrue(os.path.isdir(missing))

    def test_an_unwritable_ledger_never_makes_a_saved_answer_look_failed(self):
        bid = self.asked()
        blocker = os.path.join(self.tmp.name, "afile")
        Path(blocker).write_text("x")
        env = dict(os.environ, FABLE_ESCALATION_DIR=self.dir, ASK_PENDING_DIR=os.path.join(blocker, "under"))
        proc = subprocess.run([sys.executable, self.SCRIPT, "answer", "--id", bid, "--decision", "approve", "--session", "dev-1",
                               "--ref", "owner said approve in chat"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.APPROVED)

    def test_consult_refuses_cleanly_on_an_approval_without_a_timestamp(self):
        bid = self.asked()
        self.cli("answer", "--id", bid, "--decision", "approve", "--session", "dev-1", "--ref", "owner said approve in chat")
        record = Path(fe._record_path(self.dir, bid))
        data = json.loads(record.read_text())
        del data["answered"]
        record.write_text(json.dumps(data))
        out = self.cli("consult", "--id", bid, "--execute", rc=2)
        self.assertIn("unreadable", out["error"])
        self.assertEqual(self.calls(), 0)

    def test_an_uncounted_attempt_says_so(self):
        out = self.attempt("wait for the runner", failure="CI_UNAVAILABLE_EXTERNAL billing limit")
        self.assertEqual(out["action"], "CONTINUE")
        self.assertIn("not counted", out["note"])
        self.assertNotIn("note", self.attempt("reorder acquisition"))

    def test_an_approval_with_a_corrupt_timestamp_continues_without_raising(self):
        bid = self.asked()
        self.cli("answer", "--id", bid, "--decision", "approve", "--session", "dev-1", "--ref", "owner said approve in chat")
        record = Path(fe._record_path(self.dir, bid))
        data = json.loads(record.read_text())
        data["answered"] = "not-a-time"
        record.write_text(json.dumps(data))
        out = self.attempt("retry with backoff")
        self.assertEqual(out["action"], "CONTINUE")
        self.assertIn("unreadable", out["reason"])
        self.assertEqual(self.calls(), 0)

    def test_same_approach_repeated_or_a_different_failure_does_not_ask(self):
        self.attempt("reorder acquisition")
        self.assertEqual(self.attempt("reorder acquisition")["action"], "CONTINUE")
        self.assertEqual(self.attempt("single global lock", failure="AC3 times out in setup")["action"], "CONTINUE")

    def test_ordinary_ci_and_queue_delays_never_ask(self):
        for failure, cause in (("CI_UNAVAILABLE_EXTERNAL billing limit", None), ("job waiting in queue delay", "queue_delay"),
                               ("runner unavailable", None), ("permission denied on merge", "missing_permission"),
                               ("ECONNRESET from provider", "external_outage")):
            for approach in ("a", "b", "c"):
                extra = {"cause": cause} if cause else {}
                self.assertEqual(self.attempt(approach + failure, failure=failure, check="AC " + failure, **extra)["action"], "CONTINUE", failure)
        self.assertFalse(Path(self.dir, "receipts.log").exists())

    def test_trigger_without_a_question_or_attestation_waits_instead_of_asking(self):
        self.attempt("a", question="")
        out = self.cli("attempt", "--target", "o/r#7", "--check", "AC3 lock ordering", "--session", "dev-1", "--model", "claude-opus-5-5",
                       "--approach", "b", "--failure", "AC3 deadlock at step 4")
        self.assertEqual(out["action"], "CONTINUE")
        self.assertIn("--question", out["next"])
        self.assertEqual(self.attempt("c")["action"], "ASK_OWNER")

    def test_review_disagreement_asks_once(self):
        out = self.cli("attempt", "--target", "o/r#8", "--check", "design review RV1", "--session", "dev-1",
                       "--question", "Which of the two queue designs keeps tenant isolation?", "--non-sensitive", "--review-unresolved",
                       "--review-position", "queue per tenant", "--review-position", "shared queue with quotas")
        self.assertEqual(out["action"], "ASK_OWNER")
        self.assertEqual(out["packet"]["trigger"], "unresolved_review_disagreement")
        again = self.cli("attempt", "--target", "o/r#8", "--check", "design review RV1", "--session", "dev-1", "--review-unresolved")
        self.assertEqual(again["action"], "CONTINUE")

    def test_approval_allows_one_consult_and_the_next_attempt_says_consult_approved_until_it_runs(self):
        bid = self.asked()
        self.cli("answer", "--id", bid, "--decision", "approve", "--ref", "owner said: approve this one", "--session", "dev-1")
        self.assertEqual(self.attempt("third try")["action"], "CONSULT_APPROVED")
        out = self.cli("consult", "--id", bid, "--execute", "--cwd", self.tmp.name)
        self.assertTrue(Path(out["advice_path"]).is_file())
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.attempt("fourth try")["action"], "CONTINUE")
        self.cli("consult", "--id", bid, "--execute", "--cwd", self.tmp.name, rc=2)
        self.assertEqual(self.calls(), 1)
        argv = json.loads(Path(self.log).read_text().splitlines()[0])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-2")

    def test_decline_and_no_reply_never_prompt_again_and_never_call(self):
        bid = self.asked()
        for approach in ("x", "y"):
            self.assertEqual(self.attempt(approach)["action"], "CONTINUE")  # no reply: still pending, no re-ask
        self.cli("answer", "--id", bid, "--decision", "decline", "--ref", "owner said: not now, skip", "--session", "dev-1")
        for approach in ("p", "q"):
            self.assertEqual(self.attempt(approach)["action"], "CONTINUE")
        self.cli("consult", "--id", bid, "--execute", "--cwd", self.tmp.name, rc=2)
        self.assertEqual(self.calls(), 0)

    def test_failed_consult_stops_all_escalation_without_repeat_prompts_or_calls(self):
        bid = self.asked()
        self.cli("answer", "--id", bid, "--decision", "approve", "--ref", "owner said: approve this one", "--session", "dev-1")
        os.environ["FAKE_RC"] = "1"
        self.cli("consult", "--id", bid, "--execute", "--cwd", self.tmp.name, rc=2)
        os.environ.pop("FAKE_RC")
        self.assertEqual(self.attempt("again")["action"], "CONTINUE")
        # a different blocker that would otherwise qualify is also held back while the breaker is open
        self.attempt("one", target="o/r#9", check="AC9 other", failure="AC9 broken")
        self.assertEqual(self.attempt("two", target="o/r#9", check="AC9 other", failure="AC9 broken")["action"], "CONTINUE")
        self.assertEqual(self.calls(), 1)

    def test_unrelated_blockers_and_work_continue_while_one_ask_is_open(self):
        self.asked()
        start = time.monotonic()
        other = self.attempt("only one try", target="o/r#10", check="AC10 other", failure="AC10 flake")
        self.assertEqual(other["action"], "CONTINUE")
        self.assertLess(time.monotonic() - start, 30)  # the call returns at once: it never waits for the owner
        self.attempt("first", target="o/r#11", check="AC11 other", failure="AC11 broken")
        self.assertEqual(self.attempt("second", target="o/r#11", check="AC11 other", failure="AC11 broken")["action"], "ASK_OWNER")
        pending = [i for i in fe._ask_pending().all_items(self.ask) if i["state"] == "PENDING"]
        self.assertEqual(len(pending), 2)  # one open request per blocker, both non-blocking


class ReviewRound2(WorkflowBase):
    def test_an_attempt_tagged_with_a_cause_is_never_counted_toward_the_trigger(self):
        self.attempt("one", cause="queue_delay")
        self.assertEqual(self.attempt("two")["action"], "CONTINUE")  # only one reasoning attempt so far
        self.assertEqual(self.attempt("three")["action"], "ASK_OWNER")

    def test_infrastructure_failure_text_is_not_kept_and_does_not_suppress_the_blocker(self):
        self.attempt("one", failure="quota exceeded on the provider")
        self.assertEqual(self.attempt("two")["action"], "CONTINUE")
        self.assertEqual(self.attempt("three")["action"], "ASK_OWNER")

    def test_concurrent_attempts_ask_exactly_once_and_none_errors(self):
        self.attempt("seed")
        results = []
        def run(i):
            results.append(self.attempt(f"concurrent approach {i}"))
        threads = [threading.Thread(target=run, args=(i,)) for i in range(5)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        self.assertEqual(sorted(r["action"] for r in results), ["ASK_OWNER"] + ["CONTINUE"] * 4)
        self.assertEqual(len([r for r in Path(self.dir, "receipts.log").read_text().splitlines() if '"offered"' in r]), 1)

    def test_a_settled_review_disagreement_cannot_trigger_later(self):
        base = ("--target", "o/r#8", "--check", "design review RV1", "--session", "dev-1")
        self.cli("attempt", *base, "--review-unresolved", "--review-position", "queue per tenant", "--review-position", "shared queue")
        self.cli("attempt", *base, "--review-resolved")
        out = self.cli("attempt", *base, "--model", "claude-opus-5-5", "--approach", "x", "--failure", "RV1 flake",
                       "--question", "Which of the two queue designs keeps tenant isolation?", "--non-sensitive")
        self.assertEqual(out["action"], "CONTINUE")

    def test_a_lost_first_display_is_recoverable_without_asking_again(self):
        asked = self.asked()
        again = self.attempt("another")
        self.assertEqual(again["action"], "CONTINUE")
        shown = self.cli("show", "--id", asked)
        self.assertEqual(shown["id"], asked)
        self.assertEqual(shown["question_for_fable"].count("?"), 1)
        self.cli("answer", "--id", asked, "--decision", "decline", "--ref", "owner said: not now, skip", "--session", "dev-1")
        self.cli("show", "--id", asked, rc=2)

    def test_a_failing_pending_ledger_still_returns_the_packet(self):
        packet = fe.offer(self.dir, case(), "sess-1", "/dev/null/not-a-dir", T0)
        self.assertTrue(packet["pending_ledger_item"].startswith("not-recorded"))
        self.assertEqual(fe.load(self.dir, packet["id"])["state"], fe.OFFERED)

    def test_non_ascii_acceptance_checks_are_distinct_blockers(self):
        self.assertNotEqual(fe.fingerprint("o/r#1", "验收一"), fe.fingerprint("o/r#1", "验收二"))

    def test_an_expired_or_breaker_blocked_approval_says_continue_not_consult(self):
        bid = self.asked()
        self.cli("answer", "--id", bid, "--decision", "approve", "--ref", "owner said: approve this one", "--session", "dev-1")
        self.assertEqual(self.attempt("next")["action"], "CONSULT_APPROVED")
        fe.trip(self.dir, "route_failure", None, T0)
        self.assertEqual(self.attempt("next2")["action"], "CONTINUE")
        fe.reset_breaker(self.dir, "route re-verified by canary today", T0)
        path = fe._record_path(self.dir, bid)
        record = json.loads(Path(path).read_text())
        record["answered"] = "2026-01-01T00:00:00Z"
        Path(path).write_text(json.dumps(record))
        out = self.attempt("next3")
        self.assertEqual(out["action"], "CONTINUE")
        self.assertIn("24h", out["reason"])


class RepoInvariants(unittest.TestCase):
    def test_the_loop_skills_call_the_attempt_command(self):
        flow = (ROOT / ".claude/skills/engineering-workflow/SKILL.md").read_text()
        loop = (ROOT / ".claude/skills/dev-impl-loop/SKILL.md").read_text()
        self.assertIn("coding-agent-environment-main-view/scripts/fable-escalation.py\" attempt", flow)  # absolute path: works from any repository
        self.assertNotIn("python3 scripts/fable-escalation.py", flow)
        for action in ("CONTINUE", "ASK_OWNER", "CONSULT_APPROVED"):
            self.assertIn(action, flow)
        self.assertEqual(loop.count("Blocker escalation to Fable"), 2)

    def test_the_claude_only_approval_text_is_dropped_from_the_codex_copy(self):
        strip = lambda s: re.sub(r"<!-- claude-only:start -->.*?<!-- claude-only:end -->\n?", "", s, flags=re.S)  # the installer's Codex transform
        for skill in ("engineering-workflow", "dev-impl-loop"):
            text = (ROOT / f".claude/skills/{skill}/SKILL.md").read_text()
            self.assertEqual(text.count("<!-- claude-only:start -->"), text.count("<!-- claude-only:end -->"))
            self.assertIn("Fable", text)  # the Claude copy carries it ...
            for word in ("AskUserQuestion", "fable-escalation", "Fable"):
                self.assertNotIn(word, strip(text), f"{skill}: {word} reaches the Codex copy")  # ... the Codex copy does not


    def test_no_scheduler_hook_or_workflow_calls_consult(self):
        for path in list((ROOT / ".github").rglob("*")) + list((ROOT / ".claude").rglob("*")):
            if path.is_file():
                self.assertNotIn("fable-escalation.py consult", path.read_text(errors="ignore"), str(path))


if __name__ == "__main__":
    unittest.main()
