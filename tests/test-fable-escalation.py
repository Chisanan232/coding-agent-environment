#!/usr/bin/env python3
"""Fable escalation: trigger, standing authorization, shared monthly budget, one-per-blocker, circuit breaker and fallback.

Never calls a real model: every consult goes to a fake `claude` executable. Dollar figures are list-price estimates, never billed money."""
import contextlib
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
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("fable_escalation", ROOT / "scripts/fable-escalation.py")
fe = module_from_spec(spec)
spec.loader.exec_module(fe)

T0 = datetime(2026, 11, 17, 9, 0, 0, tzinfo=timezone.utc)
HOST = "a" * 64
QUESTION = "Which lock order removes the cycle between the cache and the index?"
FABLE_OK = {"is_error": False, "result": "Conclusion: split the lock.", "total_cost_usd": 0.9, "duration_ms": 41000,
            "modelUsage": {"claude-fable-5-2": {}}}
# A disposable stand-in for the claude CLI: prints the scenario file's stdout and exits with its code.
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys
if "--version" in sys.argv:
    print(os.environ.get("FAKE_VERSION", "2.1.325 (Claude Code)")); sys.exit(0)
if "--help" in sys.argv:
    print("  --setting-sources <s>\\n  --permission-mode <m>\\n  --disallowedTools, --disallowed-tools <tools...>"); sys.exit(0)
if os.environ.get("FAKE_CWD_LOG"):
    open(os.environ["FAKE_CWD_LOG"], "a").write(json.dumps({"cwd": os.getcwd(), "files": sorted(
        os.path.relpath(os.path.join(d, f), os.getcwd()) for d, _, fs in os.walk(os.getcwd()) for f in fs)}) + "\\n")
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
print(os.environ["FAKE_OUT"]); sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''
# Runs the real CLI with the two host-specific probes pinned, so the CLI tests do not depend on the machine they run on.
RUNNER = ("import sys, importlib.util as u\n"
          "s = u.spec_from_file_location('fe', sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)\n"
          f"m.host_hash = lambda: '{HOST}'; m.MANAGED_SETTINGS = '/nonexistent/managed-settings.json'; m.PROFILE_BIN = '/nonexistent/profile'\n"
          "sys.exit(m.main(sys.argv[2:]))\n")


def case(**over):
    base = {"target": "o/r#7", "acceptance_check": "AC3 lock ordering test passes", "blocker": "deadlock under AC3",
            "attempts": [{"model": "claude-opus-5-5", "approach": "reorder acquisition", "failure": "AC3 deadlock at step 4"},
                         {"model": "claude-opus-5-5", "approach": "single global lock", "failure": "AC3 deadlock at step 4"}],
            "question": QUESTION, "cause": "none",
            "inputs_non_sensitive": True, "spent": {"rework_rounds": 2, "elapsed_s": 3600, "usd": 3.1}}
    base.update(over)
    return base


def valid_auth(home, **over):
    base = {"schema": "fable-authorization/v1",
            "decision": {"ticket": "SPE-171", "date": "2026-11-25", "by": "owner", "ref": "owner decision in chat"},
            "model": "claude-fable-5-2", "billing_route": "claude.ai-max-usage-credits-off",
            "retention": {"model_improvement_setting": "off", "confirmed_on": "2026-11-25"},
            "scope": {"host_sha256": HOST, "repo_owners": ["o"], "allowed_profiles": []},
            "caps": {"per_consult_usd": 4, "monthly_usd": 40, "timezone": "Asia/Taipei"},
            "ledger_dir": os.path.join(home, ".local", "state", "coding-agent-environment", "fable-budget")}
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        self.root = os.path.join(self.home, ".config", "coding-agent-environment", "local")
        os.makedirs(self.root, mode=0o700)
        for d in (os.path.join(self.home, ".config"), os.path.join(self.home, ".config", "coding-agent-environment"), self.root):
            os.chmod(d, 0o700)
        self.auth_path = os.path.join(self.root, "fable-authorization.json")
        self.dir = os.path.join(self.tmp.name, "fable")
        os.makedirs(os.path.join(self.dir, "advice"), mode=0o700)
        self.bin = os.path.join(self.tmp.name, "claude")
        Path(self.bin).write_text(FAKE_CLAUDE)
        os.chmod(self.bin, 0o755)
        # The first launch of a freshly written executable can stall for tens of seconds on macOS under load; absorb it here so it
        # cannot land inside the 30 s version probe and look like a route failure.
        subprocess.run([self.bin, "--version"], capture_output=True, timeout=300, env=dict(os.environ, FAKE_VERSION="warm"))
        self.log = os.path.join(self.tmp.name, "calls.log")
        self.env = {"FABLE_CLAUDE_BIN": self.bin, "FAKE_LOG": self.log, "FAKE_OUT": json.dumps(FABLE_OK), "HOME": self.home}
        self.saved = {k: os.environ.get(k) for k in [*self.env, *fe.API_ENV, "FAKE_RC", "FAKE_VERSION", "FABLE_ESCALATION_DIR"]}
        for k in (*fe.API_ENV, "FAKE_RC", "FAKE_VERSION", "FABLE_ESCALATION_DIR"):
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.patched = {k: getattr(fe, k) for k in ("host_hash", "MANAGED_SETTINGS", "PROFILE_BIN")}
        fe.host_hash = lambda: HOST
        fe.MANAGED_SETTINGS = os.path.join(self.tmp.name, "no-managed-settings.json")
        fe.PROFILE_BIN = os.path.join(self.tmp.name, "no-profile-resolver")
        self.repo = self.make_repo("repo", "https://github.com/o/r.git")
        self.write_auth()
        self.ledger = valid_auth(self.home)["ledger_dir"]

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for k, v in self.patched.items():
            setattr(fe, k, v)
        self.tmp.cleanup()

    def make_repo(self, name, url):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        subprocess.run(["git", "-C", path, "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "remote", "add", "origin", url], check=True, capture_output=True)
        return path

    def write_auth(self, obj=None, mode=0o600, **over):
        Path(self.auth_path).write_text(json.dumps(obj if obj is not None else valid_auth(self.home, **over)))
        os.chmod(self.auth_path, mode)

    def calls(self):
        return len(Path(self.log).read_text().splitlines()) if os.path.exists(self.log) else 0

    def args(self, **over):
        base = {"target": "o/r#7", "acceptance_check": "AC3 lock ordering test passes", "model": "claude-opus-5-5",
                "failure": "AC3 deadlock at step 4", "question": QUESTION, "non_sensitive": True, "cwd": self.repo}
        base.update(over)
        return base

    def ready(self, directory=None, **over):
        """Two distinct Opus attempts with the same failure: returns the blocker id once the tool says CONSULT."""
        d = directory or self.dir
        fe.attempt(d, self.args(approach="reorder acquisition", **over), "sess-1", T0)
        out = fe.attempt(d, self.args(approach="single global lock", **over), "sess-1", T0)
        self.assertEqual(out["action"], "CONSULT", out)
        return out["id"]

    def two_attempts(self, directory=None, **over):
        d = directory or self.dir
        fe.attempt(d, self.args(approach="reorder acquisition", **over), "sess-1", T0)
        return fe.attempt(d, self.args(approach="single global lock", **over), "sess-1", T0)

    def consult(self, bid, at=T0, directory=None, execute=True):
        return fe.consult(directory or self.dir, bid, execute, self.repo, at)

    def month(self, at=T0):
        return json.loads(Path(self.ledger, fe.month_key(at) + ".json").read_text())

    def seed_month(self, usd, at=T0, count=None):
        """Pre-settle `usd` of this month's budget in whole $4 entries (plus one remainder entry)."""
        os.makedirs(self.ledger, mode=0o700, exist_ok=True)
        os.chmod(self.ledger, 0o700)
        chunks = [4.0] * int(usd // 4) + ([round(usd % 4, 6)] if usd % 4 else [])
        entries = [{"rid": f"seed{i}", "blocker_id": f"seed{i:012d}", "session": "seed", "reserved_usd": c, "state": "SETTLED",
                    "charged_usd": c, "basis": "seed", "reserved_at": fe.iso(at), "settled_at": fe.iso(at)} for i, c in enumerate(chunks)]
        Path(self.ledger, fe.month_key(at) + ".json").write_text(json.dumps({"month": fe.month_key(at), "entries": entries}))

    def out(self, **fields):
        os.environ["FAKE_OUT"] = json.dumps(dict(FABLE_OK, **fields))


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


class AutoConsultState(Env):
    def test_trigger_with_valid_authorization_returns_consult_exactly_once_and_repeats_are_idempotent(self):
        out = self.two_attempts()
        self.assertEqual(out["action"], "CONSULT")
        self.assertIn(f"consult --id {out['id']} --execute", out["next"])
        self.assertEqual(fe.load(self.dir, out["id"])["state"], fe.READY)
        again = fe.attempt(self.dir, self.args(approach="third idea"), "sess-2", T0)
        self.assertEqual((again["action"], again["id"]), ("CONSULT", out["id"]))
        self.assertEqual(len([r for r in Path(self.dir, "receipts.log").read_text().splitlines() if '"ready"' in r]), 1)
        self.assertEqual(self.calls(), 0)  # attempt never calls a model

    def test_consult_runs_once_then_every_later_attempt_continues(self):
        bid = self.ready()
        self.consult(bid)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.CONSULTED)
        for approach in ("fourth try", "fifth try"):
            self.assertEqual(fe.attempt(self.dir, self.args(approach=approach), "sess-1", T0)["action"], "CONTINUE")
        with self.assertRaisesRegex(fe.EscalationError, "only from READY"):
            self.consult(bid)
        self.assertEqual(self.calls(), 1)

    def test_concurrent_attempts_produce_one_consult_action_and_no_error(self):
        fe.attempt(self.dir, self.args(approach="seed"), "sess-1", T0)
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(fe.attempt(self.dir, self.args(approach=f"concurrent {i}"), "s", T0)))
                   for i in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual({r["action"] for r in results}, {"CONSULT"})  # every call sees the same READY blocker
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(len([r for r in Path(self.dir, "receipts.log").read_text().splitlines() if '"ready"' in r]), 1)

    def test_state_files_are_private(self):
        bid = self.ready()
        self.assertEqual(stat.S_IMODE(os.stat(fe._record_path(self.dir, bid)).st_mode), 0o600)

    def test_second_blocker_record_for_the_same_blocker_is_refused_even_with_more_attempts_or_new_wording(self):
        self.ready()
        more = case(blocker="reworded deadlock summary",
                    attempts=case()["attempts"] + [{"model": "claude-opus-5-5", "approach": "lock-free index", "failure": "AC3 deadlock at step 4"}])
        v = fe.evaluate(more, self.dir)
        self.assertFalse(v["eligible"])
        self.assertTrue(any("one consult per blocker" in r for r in v["reasons"]), v)

    def test_a_trigger_without_a_question_or_attestation_waits_instead_of_becoming_ready(self):
        fe.attempt(self.dir, self.args(approach="a", question=""), "s", T0)
        out = fe.attempt(self.dir, self.args(approach="b", question="", non_sensitive=False), "s", T0)
        self.assertEqual(out["action"], "CONTINUE")
        self.assertIn("--question", out["next"])
        self.assertEqual(fe.attempt(self.dir, self.args(approach="c"), "s", T0)["action"], "CONSULT")

    def test_review_disagreement_becomes_ready_once(self):
        base = {"target": "o/r#8", "acceptance_check": "design review RV1", "question": "Which of the two queue designs keeps tenant isolation?",
                "non_sensitive": True, "review_unresolved": True, "review_position": ["queue per tenant", "shared queue with quotas"],
                "cwd": self.repo}
        out = fe.attempt(self.dir, base, "dev-1", T0)
        self.assertEqual(out["action"], "CONSULT")
        self.assertEqual(fe.load(self.dir, out["id"])["trigger"], "unresolved_review_disagreement")
        self.assertEqual(fe.attempt(self.dir, dict(base, review_position=None), "dev-1", T0)["id"], out["id"])


class Authorization(Env):
    def refused_at_attempt(self, reason):
        out = self.two_attempts()
        self.assertEqual(out["action"], "CONTINUE", out)
        self.assertEqual(out["reason"], reason)
        self.assertIn("opus-architect", out["fallback"])
        self.assertFalse(os.path.exists(fe._record_path(self.dir, out["id"])))  # nothing is READY
        self.assertEqual(self.calls(), 0)

    def refused_at_consult(self, bid, reason):
        out = self.consult(bid)
        self.assertEqual((out["action"], out["reason"]), ("CONTINUE", reason), out)
        self.assertIn("opus-architect", out["fallback"])
        self.assertEqual(self.calls(), 0)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.READY)  # a refusal spends nothing and can be retried
        self.assertFalse(fe.breaker(self.dir)["open"])

    def both(self, reason, break_it):
        """The gate holds at attempt (nothing becomes READY) and again at consult (a READY blocker is refused)."""
        break_it()
        self.refused_at_attempt(reason)
        self.write_auth()
        self.tearDown_state()
        bid = self.ready()
        break_it()
        self.refused_at_consult(bid, reason)

    def tearDown_state(self):
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if os.path.isfile(path):
                os.unlink(path)
        subprocess.run(["rm", "-rf", os.path.join(self.dir, "attempts")])

    def test_missing_authorization_file(self):
        self.both("no_authorization", lambda: os.unlink(self.auth_path) if os.path.exists(self.auth_path) else None)

    def test_symlinked_group_writable_and_malformed_authorization_files_are_invalid(self):
        def symlink():
            real = os.path.join(self.root, "real.json")
            os.replace(self.auth_path, real)
            os.symlink(real, self.auth_path)
        def undo_symlink():
            if os.path.islink(self.auth_path):
                os.unlink(self.auth_path)
                os.replace(os.path.join(self.root, "real.json"), self.auth_path)
        def group_writable():
            os.chmod(self.auth_path, 0o660)
        def corrupt():
            Path(self.auth_path).write_text("{not json")
        for name, break_it in (("symlink", symlink), ("group", group_writable), ("corrupt", corrupt)):
            with self.subTest(name):
                self.write_auth()
                self.tearDown_state()
                break_it()
                self.refused_at_attempt("authorization_invalid")
                undo_symlink()

    def test_wrong_model_wrong_ticket_wrong_route_and_unknown_keys_are_invalid(self):
        bad = [valid_auth(self.home, model="claude-opus-5-5"),
               valid_auth(self.home, decision={"ticket": "SPE-999"}),
               valid_auth(self.home, decision={"by": "agent"}),
               valid_auth(self.home, billing_route="api"),
               valid_auth(self.home, extra="x"),
               valid_auth(self.home, scope={"surprise": 1}),
               valid_auth(self.home, caps={"timezone": "UTC"}),
               valid_auth(self.home, scope={"host_sha256": "short"}),
               valid_auth(self.home, ledger_dir="/etc/fable-budget"),
               valid_auth(self.home, ledger_dir="relative/dir"),
               valid_auth(self.home, caps={"per_consult_usd": -1}),
               valid_auth(self.home, caps={"per_consult_usd": True})]
        for obj in bad:
            with self.subTest(obj.get("model"), keys=sorted(obj)):
                self.write_auth(obj)
                self.tearDown_state()
                self.refused_at_attempt("authorization_invalid")

    def test_duplicate_keys_in_the_file_are_invalid(self):
        Path(self.auth_path).write_text('{"schema": "fable-authorization/v1", "schema": "fable-authorization/v1"}')
        self.refused_at_attempt("authorization_invalid")

    def test_retention_must_be_confirmed_off_before_any_consult(self):
        for setting, confirmed in (("unconfirmed", None), ("on", "2026-11-25")):
            with self.subTest(setting):
                self.write_auth(retention={"model_improvement_setting": setting, "confirmed_on": confirmed})
                self.tearDown_state()
                self.refused_at_attempt("retention_unconfirmed")
        # A blocker that became READY earlier is refused at consult too, and the fake claude is never called.
        self.write_auth()
        self.tearDown_state()
        bid = self.ready()
        self.write_auth(retention={"model_improvement_setting": "on", "confirmed_on": "2026-11-25"})
        self.refused_at_consult(bid, "retention_unconfirmed")
        self.assertFalse(os.path.exists(self.ledger) and os.listdir(self.ledger))

    def test_attempt_and_evaluate_still_run_while_retention_is_unconfirmed(self):
        self.write_auth(retention={"model_improvement_setting": "unconfirmed", "confirmed_on": None})
        self.assertTrue(fe.evaluate(case(), self.dir)["eligible"])
        fe.attempt(self.dir, self.args(approach="a"), "s", T0)
        out = fe.attempt(self.dir, self.args(approach="b"), "s", T0)
        self.assertEqual(out["reason"], "retention_unconfirmed")

    def test_file_caps_above_the_script_ceilings_are_clamped_and_lower_caps_are_kept(self):
        self.write_auth(caps={"per_consult_usd": 100, "monthly_usd": 1000})
        auth, reason = fe.load_authorization()
        self.assertIsNone(reason)
        self.assertEqual(auth["caps"], {"per_consult_usd": 4.0, "monthly_usd": 40.0})
        self.write_auth(caps={"per_consult_usd": 2, "monthly_usd": 10})
        self.assertEqual(fe.load_authorization()[0]["caps"], {"per_consult_usd": 2.0, "monthly_usd": 10.0})

    def test_scope_host_mismatch(self):
        fe.host_hash = lambda: "b" * 64
        self.refused_at_attempt("scope_host_mismatch")
        fe.host_hash = lambda: None  # unreadable host id fails closed
        self.tearDown_state()
        self.refused_at_attempt("scope_host_mismatch")

    def test_target_owner_must_be_listed(self):
        out = self.two_attempts(target="stranger/r#7")
        self.assertEqual(out["reason"], "scope_repo_not_owned")

    def test_working_tree_remote_owner_must_be_listed(self):
        elsewhere = self.make_repo("other", "git@github.com:stranger/thing.git")
        out = self.two_attempts(cwd=elsewhere)
        self.assertEqual(out["reason"], "scope_repo_not_owned")
        bare = os.path.join(self.tmp.name, "norepo")
        os.makedirs(bare)
        self.tearDown_state()
        self.assertEqual(self.two_attempts(cwd=bare)["reason"], "scope_repo_not_owned")

    def test_remote_urls_are_parsed_structurally_and_must_name_the_target_repository(self):
        tricks = ("https://evil.example/github.com/o/r.git", "https://github.com.evil.example/o/r.git", "https://o@github.com.evil.example/o/r",
                  "https://evil.example/x@github.com/o/r", "git@github.com.evil.example:o/r.git", "https://github.com/o/r/extra",
                  "ssh://evil@github.com/o/r", "file:///srv/github.com/o/r", "https://github.comx/o/r", "https://github.com/o")
        for url in tricks:
            with self.subTest(url):
                self.tearDown_state()
                other = self.make_repo("trick", url)
                self.assertEqual(fe.origin_repo(other), None)
                self.assertEqual(self.two_attempts(cwd=other)["reason"], "scope_repo_not_owned")
                subprocess.run(["rm", "-rf", other])
        for url in ("https://github.com/o/r.git", "https://user:tok@github.com/o/r", "ssh://git@github.com/o/r.git", "git@github.com:O/R.git",
                    "https://github.com/o/r/"):
            with self.subTest(url):
                other = self.make_repo("good", url)
                self.assertEqual(fe.origin_repo(other), ("o", "r"))
                subprocess.run(["rm", "-rf", other])

    def test_the_working_tree_must_be_a_clone_of_the_target_repository_not_just_an_owned_one(self):
        self.assertEqual(self.two_attempts(target="o/other#7")["reason"], "scope_repo_not_owned")

    def test_an_inherited_git_dir_cannot_redirect_the_remote_probe(self):
        elsewhere = self.make_repo("elsewhere", "https://github.com/stranger/x.git")
        os.environ["GIT_DIR"] = os.path.join(elsewhere, ".git")
        try:
            self.assertEqual(fe.origin_repo(self.repo), ("o", "r"))
        finally:
            del os.environ["GIT_DIR"]

    def test_managed_settings_file_makes_the_session_company_managed(self):
        Path(fe.MANAGED_SETTINGS).write_text("{}")
        self.refused_at_attempt("managed_session")

    def test_a_profile_outside_the_allow_list_is_company_managed(self):
        resolver = os.path.join(self.tmp.name, "resolver")
        Path(resolver).write_text("#!/bin/sh\necho 'status=ok name=corp root=/x overlay=/y'\n")
        os.chmod(resolver, 0o755)
        subprocess.run([resolver], capture_output=True, timeout=300)  # absorb the first-launch stall
        fe.PROFILE_BIN = resolver
        self.refused_at_attempt("managed_session")
        self.write_auth(scope={"allowed_profiles": ["corp"]})
        self.tearDown_state()
        self.assertEqual(self.two_attempts()["action"], "CONSULT")

    def resolver(self, body, rc=0):
        path = os.path.join(self.tmp.name, "resolver")
        Path(path).write_text(f"#!/bin/sh\n{body}\nexit {rc}\n")
        os.chmod(path, 0o755)
        subprocess.run([path], capture_output=True, timeout=300)  # absorb the first-launch stall
        fe.PROFILE_BIN = path

    def test_profile_resolver_output_that_is_not_an_exact_answer_is_company_managed(self):
        for name, body, rc in (("garbage", "echo hello", 0), ("empty", "true", 0), ("extra line", "echo 'status=none name= root= overlay='; echo more", 0),
                               ("none with a name", "echo 'status=none name=corp root= overlay='", 0),
                               ("unresolved", "echo 'status=unresolved name=x root=/r overlay=/o'", 4),
                               ("ok but nonzero", "echo 'status=ok name=mine root=/r overlay=/o'", 3),
                               ("none but nonzero", "echo 'status=none name= root= overlay='", 2),
                               ("extra tokens", "echo 'status=ok name=mine extra=1 root=/r overlay=/o'", 0),
                               ("prefix junk", "echo 'x status=none name= root= overlay='", 0)):
            with self.subTest(name):
                self.resolver(body, rc)
                self.write_auth(scope={"allowed_profiles": ["mine"]})
                self.assertTrue(fe.managed_session(self.repo, ["mine"]), name)
        self.resolver("echo 'status=none name= root= overlay='")
        self.assertFalse(fe.managed_session(self.repo, []))
        self.resolver("echo 'status=ok name=mine root=/r overlay=/o'")
        self.assertFalse(fe.managed_session(self.repo, ["mine"]))
        self.assertTrue(fe.managed_session(self.repo, ["other"]))

    def test_authorization_template_and_check_are_read_only_and_never_write_the_file(self):
        before = Path(self.auth_path).read_bytes()
        os.unlink(self.auth_path)
        tpl = fe.authorization_template(self.repo)
        self.assertFalse(os.path.exists(self.auth_path))
        self.assertEqual(tpl["host_sha256"], HOST)
        self.assertEqual(tpl["candidate"]["retention"]["model_improvement_setting"], "unconfirmed")
        self.assertEqual(tpl["candidate"]["scope"]["repo_owners"], ["o"])
        text = json.dumps(tpl["candidate"], indent=2, sort_keys=True) + "\n"
        self.assertEqual(tpl["file_sha256"], __import__("hashlib").sha256(text.encode()).hexdigest())
        self.assertEqual(fe.authorization_check(self.repo, "", T0)["authorization"], "no_authorization")
        Path(self.auth_path).write_bytes(before)
        os.chmod(self.auth_path, 0o600)
        out = fe.authorization_check(self.repo, "o/r#7", T0)
        self.assertEqual(out["authorization"], "ok")
        self.assertEqual((out["scope"]["host"], out["scope"]["cwd_repo_owner"], out["scope"]["target_repo"]), ("match", "owned", "owned"))
        self.assertEqual(out["isolation"]["permission_mode"], "dontAsk")
        self.assertTrue(out["isolation"]["cli_lists_flags"])
        self.assertIn("UNVERIFIED", out["isolation"]["honoured_in_print_mode"])
        self.assertEqual(out["month_budget"]["remaining_usd"], 40.0)
        self.assertNotIn(HOST, json.dumps(out))  # the check prints verdicts, not the host hash


class Consult(Env):
    def test_dry_run_prints_the_command_shape_and_calls_nothing(self):
        bid = self.ready()
        out = self.consult(bid, execute=False)
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.calls(), 0)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.READY)
        self.assertFalse(os.path.exists(self.ledger))  # a dry run reserves nothing

    def test_consult_is_read_only_subscription_shaped_pinned_to_fable_and_capped_below_the_reservation(self):
        argv = fe.consult_argv(fe.load(self.dir, self.ready()))
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-2")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.00")
        denied = argv[argv.index("--disallowedTools") + 1]
        for rule in ("Read(**/.env*)", "Read(~/.ssh/**)", "Read(~/.config/**)", "Read(~/.claude/**)"):
            self.assertIn(rule, denied)
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")

    def commit_files(self, files, link=None):
        for name, body in files.items():
            path = Path(self.repo, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        for name, dest in (link or {}).items():
            os.symlink(dest, os.path.join(self.repo, name))
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True, capture_output=True)

    def exported(self):
        dest = os.path.join(self.tmp.name, "export")
        os.makedirs(dest, exist_ok=True)
        ok, hits = fe.export_tracked(self.repo, dest)
        names = sorted(os.path.relpath(os.path.join(d, f), dest) for d, _, fs in os.walk(dest) for f in fs)
        return ok, names, hits

    def test_the_consult_runs_in_a_clean_export_of_committed_credential_free_blobs_and_removes_it(self):
        self.commit_files({"src/a.py": "print(1)", "docs/readme.md": "hi", ".env": "X=1", "deploy.pem": "k", "src/api_token.txt": "t",
                           ".claude/settings.json": "{}"})
        Path(self.repo, "untracked.txt").write_text("u")
        seen = os.path.join(self.tmp.name, "cwd.log")
        os.environ["FAKE_CWD_LOG"] = seen
        try:
            self.consult(self.ready())
        finally:
            del os.environ["FAKE_CWD_LOG"]
        row = json.loads(Path(seen).read_text().splitlines()[0])
        self.assertEqual(row["files"], ["docs/readme.md", "src/a.py"])
        self.assertNotEqual(os.path.realpath(row["cwd"]), os.path.realpath(self.repo))
        self.assertFalse(os.path.exists(row["cwd"]))  # removed after the call

    def test_export_uses_committed_blobs_not_the_working_tree_or_the_index(self):
        self.commit_files({"a.py": "committed = 1"})
        Path(self.repo, "a.py").write_text("edited = 'uncommitted'")
        Path(self.repo, "staged.py").write_text("staged = 1")
        subprocess.run(["git", "-C", self.repo, "add", "staged.py"], check=True, capture_output=True)
        dest = os.path.join(self.tmp.name, "export")
        os.makedirs(dest)
        fe.export_tracked(self.repo, dest)
        self.assertEqual(Path(dest, "a.py").read_text(), "committed = 1")
        self.assertFalse(Path(dest, "staged.py").exists())

    def test_symlinks_symlinked_directories_and_submodules_are_not_exported(self):
        outside = os.path.join(self.tmp.name, "outside.txt")
        Path(outside).write_text("outside secret")
        outdir = os.path.join(self.tmp.name, "outdir")
        os.makedirs(outdir)
        Path(outdir, "x.txt").write_text("x")
        self.commit_files({"ok.py": "1"}, link={"link.txt": outside, "linkdir": outdir})
        sub = "1" * 40
        subprocess.run(["git", "-C", self.repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},vendor/sub"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "sub"],
                       check=True, capture_output=True)
        ok, names, _ = self.exported()
        self.assertTrue(ok)
        self.assertEqual(names, ["ok.py"])

    def test_credential_looking_names_are_skipped_in_every_path_component_and_case(self):
        bad = [".env", "app/.env", "a/b/.env.local", "config.env", "prod.env.json", ".aws/credentials", "x/.ssh/id_rsa", "x/.config/tool.toml",
               ".gnupg/pubring.kbx", "Secrets/readme.md", "KEYS/Token.TXT", "deploy.PEM", "cert.p12", "vault.kdbx", ".codex/c.toml", "id_ed25519",
               "my-password-list.txt", "private_key.txt", ".Env", "A/.ENV.PROD"]
        self.commit_files({**{n: "data" for n in bad}, "keep/ok.py": "x = 1"})
        ok, names, hits = self.exported()
        self.assertEqual((ok, names, hits), (True, ["keep/ok.py"], []))

    def test_a_file_whose_content_holds_a_token_is_skipped_and_counted(self):
        self.commit_files({"notes.py": "x = 1\n# api_key = 'abc'\n", "fine.py": "x = 2\n"})
        ok, names, hits = self.exported()
        self.assertEqual((names, hits), (["fine.py"], ["notes.py"]))

    def test_a_prompt_that_names_a_file_with_a_secret_is_refused_before_any_reservation(self):
        self.commit_files({"locks/order.py": "token = 'abc'\n"})
        bid = self.ready(failure="AC3 deadlock in locks/order.py at step 4")
        out = self.consult(bid)
        self.assertEqual((out["action"], out["reason"]), ("CONTINUE", "isolation_secret_in_referenced_file"))
        self.assertEqual(self.calls(), 0)
        self.assertFalse(os.path.exists(self.ledger))
        self.assertFalse(fe.breaker(self.dir)["open"])

    def crafted(self, rows):
        """Run export_tracked against a hand-made `git ls-tree` listing; blob reads still go to the real object database."""
        self.commit_files({"ok.py": "x = 1"})
        sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD:ok.py"], capture_output=True, text=True).stdout.strip()
        listing = "".join(f"{mode} blob {sha} {size:>7}\t{name}\0" for mode, size, name in rows).encode()
        ok, names, hits = self.run_export(listing)
        return ok, names, hits

    def run_export(self, listing=None, script=None):
        """export_tracked with `git ls-tree` replaced by a child that prints `listing` (or runs `script`); other git calls are real."""
        real = subprocess.Popen
        self.children = []

        def popen(argv, *a, **kw):
            if "ls-tree" in argv:
                code = script or "import sys; sys.stdout.buffer.write(%r)" % (listing,)
                argv = [sys.executable, "-c", code]
            proc = real(argv, *a, **kw)
            self.children.append(proc)
            return proc
        dest = tempfile.mkdtemp(dir=self.tmp.name)
        with mock.patch.object(fe.subprocess, "Popen", side_effect=popen):
            ok, hits = fe.export_tracked(self.repo, dest)
        names = sorted(os.path.relpath(os.path.join(d, f), dest) for d, _, fs in os.walk(dest) for f in fs)
        return ok, names, hits

    def test_unsafe_tree_paths_fail_the_export_closed_and_write_nothing_outside(self):
        for name in ("../x.py", "a/../../x.py", "/abs/x.py", ".git/config", "a/.GIT/hooks/x", "a//b.py", "a/./b.py", "./b.py", "a/..", ""):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                ok, names, _ = self.crafted([("100644", 5, name)])
                self.assertFalse(ok)
                self.assertEqual(names, [])
                self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x.py")))

    def test_dotted_but_ordinary_components_are_allowed(self):
        ok, names, _ = self.crafted([("100644", 5, "pkg/.hidden/v1.2/a.py"), ("100644", 5, "...dots/b.py")])
        self.assertEqual((ok, names), (True, ["...dots/b.py", "pkg/.hidden/v1.2/a.py"]))

    def test_nul_padded_utf16_and_invalid_utf8_blobs_are_skipped_as_hits_not_scanned_clean(self):
        bodies = {"u16.txt": "api_key = 'abc'".encode("utf-16"), "nul.txt": b"t\0o\0k\0e\0n\0", "bad.txt": b"api\xff_key = 'a'", "clean.txt": b"fine = 1\n"}
        for name, body in bodies.items():
            Path(self.repo, name).write_bytes(body)
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        subprocess.run([*git, "commit", "-q", "-m", "x"], check=True, capture_output=True)
        ok, names, hits = self.exported()
        self.assertEqual((ok, names, sorted(hits)), (True, ["clean.txt"], ["bad.txt", "nul.txt", "u16.txt"]))

    def test_resource_bounds_are_enforced_from_the_listing_before_any_blob_is_read(self):
        reads = []
        real_read = fe._read_blob
        fe._read_blob = lambda *a: reads.append(a) or real_read(*a)
        saved = (fe.EXPORT_MAX_ENTRIES, fe.EXPORT_MAX_FILES, fe.EXPORT_MAX_TOTAL_BYTES, fe.EXPORT_MAX_FILE_BYTES, fe.EXPORT_DEADLINE_S)
        try:
            ok, names, _ = self.crafted([("100644", 10 * 1024 * 1024, "huge.bin"), ("100644", 5, "a.py")])  # huge is skipped, not read
            self.assertEqual((ok, names), (True, ["a.py"]))
            self.assertEqual(len(reads), 1)
            reads.clear()
            fe.EXPORT_MAX_ENTRIES = 3
            ok, names, _ = self.crafted([("100755", 5, f"d/f{i}.py") for i in range(10)])  # skipped names still count as entries
            self.assertFalse(ok)
            fe.EXPORT_MAX_ENTRIES = saved[0]
            fe.EXPORT_MAX_FILES = 2
            self.assertFalse(self.crafted([("100644", 5, f"f{i}.py") for i in range(5)])[0])
            fe.EXPORT_MAX_FILES = saved[1]
            fe.EXPORT_MAX_TOTAL_BYTES = 9
            reads.clear()
            self.assertFalse(self.crafted([("100644", 5, "a.py"), ("100644", 5, "b.py")])[0])
            self.assertEqual(len(reads), 1)  # the second entry is refused from its listed size, unread
            fe.EXPORT_MAX_TOTAL_BYTES = saved[2]
            fe.EXPORT_DEADLINE_S = -1.0
            self.assertFalse(self.crafted([("100644", 5, "a.py")])[0])
        finally:
            fe._read_blob = real_read
            (fe.EXPORT_MAX_ENTRIES, fe.EXPORT_MAX_FILES, fe.EXPORT_MAX_TOTAL_BYTES, fe.EXPORT_MAX_FILE_BYTES, fe.EXPORT_DEADLINE_S) = saved

    def test_an_endless_or_oversized_listing_is_cut_off_while_streaming_and_the_child_is_reaped(self):
        endless = "import sys\nrow = b'100644 blob ' + b'a' * 40 + b'      5\\tf.py\\0'\nwhile True:\n    sys.stdout.buffer.write(row * 1000); sys.stdout.buffer.flush()\n"
        saved = (fe.EXPORT_MAX_ENTRIES, fe.EXPORT_MAX_LISTING_BYTES)
        try:
            self.commit_files({"ok.py": "x = 1"})
            fe.EXPORT_MAX_ENTRIES = 10**9  # only the byte cap can stop it
            fe.EXPORT_MAX_LISTING_BYTES = 200000
            start = time.monotonic()
            dest = tempfile.mkdtemp(dir=self.tmp.name)
            self.assertEqual(self.run_with(endless, dest), False)
            self.assertLess(time.monotonic() - start, 30)
            self.assertTrue(all(c.poll() is not None for c in self.children))  # killed and reaped
            fe.EXPORT_MAX_ENTRIES, fe.EXPORT_MAX_LISTING_BYTES = 50, 10**9  # now only the entry cap
            self.assertEqual(self.run_with(endless, tempfile.mkdtemp(dir=self.tmp.name)), False)
            self.assertTrue(all(c.poll() is not None for c in self.children))
            fe.EXPORT_MAX_ENTRIES, fe.EXPORT_MAX_LISTING_BYTES = saved[0], 10**9
            fe.EXPORT_DEADLINE_S, old = 1.0, fe.EXPORT_DEADLINE_S  # a listing that stalls hits the deadline
            try:
                self.assertEqual(self.run_with("import time; time.sleep(60)", tempfile.mkdtemp(dir=self.tmp.name)), False)
            finally:
                fe.EXPORT_DEADLINE_S = old
            self.assertTrue(all(c.poll() is not None for c in self.children))
        finally:
            fe.EXPORT_MAX_ENTRIES, fe.EXPORT_MAX_LISTING_BYTES = saved

    def run_with(self, script, dest):
        self.children = []
        real = subprocess.Popen

        def popen(argv, *a, **kw):
            if "ls-tree" in argv:
                argv = [sys.executable, "-c", script]
            proc = real(argv, *a, **kw)
            self.children.append(proc)
            return proc
        with mock.patch.object(fe.subprocess, "Popen", side_effect=popen):
            return fe.export_tracked(self.repo, dest)[0]

    def test_a_blob_larger_than_its_listed_size_fails_the_export(self):
        self.assertFalse(self.crafted([("100644", 2, "ok2.py")])[0])  # the real blob is 5 bytes

    def test_a_repository_without_a_commit_exports_nothing(self):
        Path(self.repo, "a.py").write_text("1")
        self.assertEqual(self.exported(), (True, [], []))

    def test_consult_reserves_calls_once_and_settles_the_reported_cost(self):
        bid = self.ready()
        out = self.consult(bid)
        self.assertEqual(Path(out["advice_path"]).read_text(), FABLE_OK["result"])
        self.assertEqual(self.calls(), 1)
        entry = self.month()["entries"][0]
        self.assertEqual((entry["state"], entry["charged_usd"], entry["reserved_usd"], entry["blocker_id"]), ("SETTLED", 0.9, 4.0, bid))
        self.assertEqual(entry["basis"], "reported_total_cost_usd")
        journal = [json.loads(x)["event"] for x in Path(self.ledger, "journal.jsonl").read_text().splitlines()]
        self.assertEqual(journal, ["reserved", "settled"])
        self.assertEqual(stat.S_IMODE(os.stat(self.ledger).st_mode), 0o700)
        self.assertEqual(self.calls(), 1)

    def test_api_credential_in_environment_refuses_the_call_without_reading_it(self):
        bid = self.ready()
        os.environ["ANTHROPIC_API_KEY"] = "x"
        out = self.consult(bid)
        self.assertEqual(out["reason"], "api_env_present")
        self.assertEqual(self.calls(), 0)

    def test_old_claude_version_is_a_route_failure_and_reserves_nothing(self):
        bid = self.ready()
        os.environ["FAKE_VERSION"] = "2.1.306 (Claude Code)"
        with self.assertRaises(fe.EscalationError):
            self.consult(bid)
        self.assertTrue(fe.breaker(self.dir)["open"])
        self.assertEqual(self.calls(), 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_a_consult_that_is_not_ready_is_refused(self):
        with self.assertRaises(fe.EscalationError):
            self.consult(fe.fingerprint("o/r#7", "AC nothing"))


class Budget(Env):
    def test_missing_cost_charges_the_full_reservation(self):
        os.environ["FAKE_OUT"] = json.dumps({k: v for k, v in FABLE_OK.items() if k != "total_cost_usd"})
        bid = self.ready()
        self.consult(bid)  # a valid answer without a cost is still accepted; only the charge is conservative
        entry = self.month()["entries"][0]
        self.assertEqual((entry["charged_usd"], entry["basis"]), (4.0, "full_reservation_no_reported_cost"))

    def test_unparseable_output_timeout_and_exception_charge_the_full_reservation_and_trip_the_breaker(self):
        real = subprocess.run

        def boom(kind):
            def run(argv, *a, **kw):
                if "-p" in argv:
                    raise kind
                return real(argv, *a, **kw)
            return run
        for name, patch in (("garbage", None), ("timeout", subprocess.TimeoutExpired("claude", 540)), ("exception", RuntimeError("boom"))):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                if patch is None:
                    os.environ["FAKE_OUT"] = "not json"
                bid = self.ready()
                ctx = mock.patch.object(fe.subprocess, "run", side_effect=boom(patch)) if patch else contextlib.nullcontext()
                with ctx, self.assertRaisesRegex(fe.EscalationError, "breaker opened"):
                    self.consult(bid)
                entry = self.month()["entries"][0]
                self.assertEqual((entry["state"], entry["charged_usd"]), ("SETTLED", 4.0))
                self.assertEqual(fe.breaker(self.dir)["reason"], "route_failure")

    def test_a_charge_above_the_reservation_records_cap_overrun_and_opens_the_breaker(self):
        self.out(total_cost_usd=5.25)
        bid = self.ready()
        out = self.consult(bid)
        self.assertTrue(Path(out["advice_path"]).is_file())  # the advice is kept
        entry = self.month()["entries"][0]
        self.assertEqual(entry["charged_usd"], 5.25)
        self.assertIn("cap_overrun", entry["basis"])
        self.assertEqual(fe.breaker(self.dir)["reason"], "cap_overrun")
        self.assertEqual(fe.committed(self.month()), 5.25)

    def test_a_killed_parent_leaves_the_reservation_counted_and_reported_orphaned(self):
        bid = self.ready()
        real = subprocess.run

        def kill(argv, *a, **kw):
            if "-p" in argv:
                raise KeyboardInterrupt
            return real(argv, *a, **kw)
        with mock.patch.object(fe.subprocess, "run", side_effect=kill), self.assertRaises(KeyboardInterrupt):
            self.consult(bid)
        entry = self.month()["entries"][0]
        self.assertEqual((entry["state"], entry["reserved_usd"], entry["charged_usd"]), ("RESERVED", 4.0, None))
        self.assertEqual(fe.committed(self.month()), 4.0)
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.CONSULTING)
        auth = fe.load_authorization()[0]
        self.assertEqual(fe.month_budget(auth, T0 + timedelta(seconds=5))["orphaned"], [])
        later = fe.month_budget(auth, T0 + timedelta(seconds=fe.CONSULT_TIMEOUT_S + 1))
        self.assertEqual(later["orphaned"], [entry["rid"]])
        self.assertEqual(later["committed_usd"], 4.0)  # orphaned, and still counted
        self.assertEqual(fe.report(self.dir, later)["month_budget"]["orphaned"], [entry["rid"]])

    def test_one_more_consult_is_allowed_at_36_dollars_then_the_budget_is_exhausted_without_tripping_the_breaker(self):
        self.seed_month(36.0)
        self.out(total_cost_usd=4.0)
        first = self.ready(acceptance_check="AC1 first")
        self.consult(first)
        self.assertEqual(fe.committed(self.month()), 40.0)
        second = self.ready(acceptance_check="AC2 second")
        before = self.calls()
        out = self.consult(second)
        self.assertEqual((out["action"], out["reason"]), ("CONTINUE", "monthly_budget_exhausted"))
        self.assertIn("opus-architect", out["fallback"])
        self.assertEqual(self.calls(), before)
        self.assertFalse(fe.breaker(self.dir)["open"])
        self.assertEqual(fe.load(self.dir, second)["state"], fe.READY)  # quota state: retryable next month

    def test_month_key_is_asia_taipei_utc_plus_8(self):
        self.assertEqual(fe.month_key(datetime(2026, 11, 30, 15, 59, 59, tzinfo=timezone.utc)), "2026-11")
        self.assertEqual(fe.month_key(datetime(2026, 11, 30, 16, 0, 0, tzinfo=timezone.utc)), "2026-12")

    def test_a_consult_at_the_rollover_belongs_to_the_new_month_and_starts_a_fresh_budget(self):
        self.seed_month(40.0)
        rollover = datetime(2026, 11, 30, 16, 0, 0, tzinfo=timezone.utc)
        bid = self.ready()
        self.assertEqual(self.consult(bid, at=datetime(2026, 11, 30, 15, 59, 59, tzinfo=timezone.utc))["reason"], "monthly_budget_exhausted")
        out = self.consult(bid, at=rollover)
        self.assertIn("advice_path", out)
        self.assertTrue(Path(self.ledger, "2026-12.json").is_file())
        self.assertEqual(fe.committed(json.loads(Path(self.ledger, "2026-12.json").read_text())), 0.9)

    def test_twelve_concurrent_blockers_reserve_exactly_ten_slots_and_never_exceed_the_month(self):
        self.out(total_cost_usd=4.0)
        ids = [self.ready(acceptance_check=f"AC{i} concurrent") for i in range(12)]
        results = []
        threads = [threading.Thread(target=lambda b=b: results.append(self.consult(b))) for b in ids]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 12)
        refused = [r for r in results if r.get("action") == "CONTINUE"]
        self.assertEqual({r["reason"] for r in refused}, {"monthly_budget_exhausted"})
        self.assertEqual(len(refused), 2)
        self.assertEqual(len(self.month()["entries"]), 10)
        self.assertLessEqual(fe.committed(self.month()), 40.0)
        self.assertEqual(self.calls(), 10)
        self.assertFalse(fe.breaker(self.dir)["open"])

    def test_the_same_blocker_in_two_state_directories_is_reserved_once_per_month(self):
        other = os.path.join(self.tmp.name, "fable2")
        os.makedirs(os.path.join(other, "advice"), mode=0o700)
        a, b = self.ready(), self.ready(directory=other)
        self.assertEqual(a, b)
        self.consult(a)
        out = self.consult(b, directory=other)
        self.assertEqual(out["reason"], "blocker_already_reserved_this_month")
        self.assertEqual(self.calls(), 1)
        self.assertEqual(len(self.month()["entries"]), 1)
        self.assertFalse(fe.breaker(other)["open"])

    def test_an_unreadable_ledger_fails_closed(self):
        bid = self.ready()
        os.makedirs(self.ledger, mode=0o700)
        Path(self.ledger, fe.month_key(T0) + ".json").write_text("{corrupt")
        out = self.consult(bid)
        self.assertEqual(out["reason"], "ledger_unreadable")
        self.assertEqual(self.calls(), 0)
        self.assertFalse(fe.breaker(self.dir)["open"])
        os.unlink(Path(self.ledger, fe.month_key(T0) + ".json"))
        os.chmod(self.ledger, 0o755)
        self.assertEqual(self.consult(bid)["reason"], "ledger_unreadable")
        os.chmod(self.ledger, 0o700)

    def test_report_has_a_month_budget_section(self):
        self.consult(self.ready())
        budget = fe.month_budget(fe.load_authorization()[0], T0)
        self.assertEqual((budget["month"], budget["committed_usd"], budget["remaining_usd"]), ("2026-11", 0.9, 39.1))
        self.assertIn("month_budget", fe.report(self.dir, budget))
        self.assertIn("unavailable", fe.report(self.dir)["month_budget"])


class BreakerAndFallback(Env):
    def failing(self, out, rc="0"):
        os.environ["FAKE_OUT"], os.environ["FAKE_RC"] = json.dumps(out), rc

    def test_wrong_effective_model_opens_the_breaker_charges_as_reported_and_falls_back(self):
        self.failing(dict(FABLE_OK, modelUsage={"claude-opus-5-5": {}}, total_cost_usd=0.5))
        bid = self.ready()
        with self.assertRaisesRegex(fe.EscalationError, "breaker opened"):
            self.consult(bid)
        self.assertEqual(fe.breaker(self.dir)["reason"], "model_mismatch")
        self.assertEqual(fe.load(self.dir, bid)["state"], fe.FAILED)
        self.assertEqual(self.month()["entries"][0]["charged_usd"], 0.5)
        v = fe.evaluate(case(target="o/r#8", acceptance_check="AC9 other"), self.dir)
        self.assertFalse(v["eligible"])
        self.assertTrue(any("circuit breaker open" in r for r in v["reasons"]))
        later = fe.attempt(self.dir, self.args(target="o/r#8", acceptance_check="AC9 other", approach="x"), "s", T0)
        self.assertEqual(later["action"], "CONTINUE")
        self.assertIn("sonnet + opus-architect", later["fallback"])

    def test_fable_plus_a_fallback_model_is_a_mismatch(self):
        self.failing(dict(FABLE_OK, modelUsage={"claude-fable-5-2": {}, "claude-sonnet-5-5": {}}))
        with self.assertRaises(fe.EscalationError):
            self.consult(self.ready())
        self.assertTrue(fe.breaker(self.dir)["open"])

    def test_error_result_nonzero_exit_and_unparseable_output_all_trip_the_breaker(self):
        for out, rc in ((dict(FABLE_OK, is_error=True, result=None), "0"), (FABLE_OK, "1"), ("not json", "0")):
            self.tearDown()
            self.setUp()
            os.environ["FAKE_OUT"], os.environ["FAKE_RC"] = (out if isinstance(out, str) else json.dumps(out)), rc
            bid = self.ready()
            with self.assertRaises(fe.EscalationError):
                self.consult(bid)
            self.assertEqual(fe.breaker(self.dir)["reason"], "route_failure")
            self.assertEqual(self.calls(), 1)  # one consult spent, never retried

    def test_provider_failures_charge_what_was_reported_else_the_full_reservation(self):
        for out, charged in ((dict(FABLE_OK, is_error=True, total_cost_usd=1.25), 1.25), (dict(FABLE_OK, is_error=True, total_cost_usd="n/a"), 4.0),
                             (dict(FABLE_OK, is_error=True, total_cost_usd=True), 4.0)):
            self.tearDown()
            self.setUp()
            self.failing(out)
            with self.assertRaises(fe.EscalationError):
                self.consult(self.ready())
            self.assertEqual(self.month()["entries"][0]["charged_usd"], charged, out)

    def test_failed_consult_is_not_retried_for_the_same_blocker(self):
        self.failing(dict(FABLE_OK, is_error=True))
        bid = self.ready()
        with self.assertRaises(fe.EscalationError):
            self.consult(bid)
        self.failing(FABLE_OK)
        with self.assertRaises(fe.EscalationError):
            self.consult(bid)
        self.assertEqual(self.calls(), 1)

    def test_advice_that_does_not_advance_opens_the_breaker(self):
        bid = self.ready()
        self.consult(bid)
        fe.verdict(self.dir, bid, "not_advanced", {"rework_rounds": 3}, T0)
        self.assertEqual(fe.breaker(self.dir)["reason"], "no_advance")
        self.assertFalse(fe.evaluate(case(target="o/r#9", acceptance_check="AC1 new"), self.dir)["eligible"])

    def test_advancing_advice_keeps_the_breaker_closed_and_a_new_blocker_can_still_escalate(self):
        bid = self.ready()
        self.consult(bid)
        fe.verdict(self.dir, bid, "advanced", {"rework_rounds": 1, "elapsed_s": 900, "usd": 1.2}, T0)
        self.assertFalse(fe.breaker(self.dir)["open"])
        self.assertTrue(fe.evaluate(case(target="o/r#9", acceptance_check="AC1 new"), self.dir)["eligible"])

    def test_open_breaker_refuses_consult_of_a_ready_blocker_and_only_an_owner_reset_closes_it(self):
        bid = self.ready()
        fe.trip(self.dir, "route_failure", None, T0)
        out = self.consult(bid)
        self.assertTrue(out["reason"].startswith("breaker_open"))
        self.assertEqual(fe.attempt(self.dir, self.args(approach="again"), "s", T0)["action"], "CONTINUE")
        self.assertEqual(self.calls(), 0)
        with self.assertRaises(fe.EscalationError):
            fe.reset_breaker(self.dir, "", T0)
        fe.reset_breaker(self.dir, "route re-verified by canary on 2.1.325", T0)
        self.assertFalse(fe.breaker(self.dir)["open"])
        self.assertEqual(fe.attempt(self.dir, self.args(approach="again"), "s", T0)["action"], "CONSULT")

    def test_verdict_needs_a_consulted_blocker(self):
        with self.assertRaises(fe.EscalationError):
            fe.verdict(self.dir, self.ready(), "advanced", {}, T0)

    def test_report_makes_no_reduction_claim_without_a_baseline(self):
        bid = self.ready(spent={})
        self.consult(bid)
        fe.verdict(self.dir, bid, "advanced", {"rework_rounds": 1, "elapsed_s": 900, "usd": 1.2}, T0)
        out = fe.report(self.dir)
        self.assertIn("no control arm", out["note"])
        self.assertEqual(out["closed"][0]["verdict"], "advanced")


class Payload(Env):
    BAD = (("secret", "api_key = abc123"), ("secret", "token: xyz"), ("secret", "key sk-" + "ant-api03-abcdef"), ("secret", "xox" + "b-1234-abcd"),
           ("secret", "jwt ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"), ("secret", "-----BEGIN " + "PRIVATE KEY-----"),
           ("secret", "see https://user:hunter2@example.com/x"), ("secret", "blob " + "A1b2C3d4" * 6),
           ("transcript", "Human: please fix it"), ("transcript", "line one\nAssistant: ok"), ("transcript", '{"role": "assistant"}'),
           ("transcript", '{"type":"tool_use"}'), ("transcript", "<system-reminder>x</system-reminder>"),
           ("transcript", "read ~/.claude/projects/p/s.jsonl"), ("transcript", "see run.jsonl"),
           ("email", "mail me at someone@example.com"),
           ("secret", '{"api_key": "abc"}'), ("secret", "'secret': 'x'"), ("secret", "token_value=abc"), ("secret", "password-hash: x"),
           ("secret", "Authorization: Bearer abcdefgh12345678"), ("secret", "sent Bearer abcdefgh12345678"),
           ("secret", "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1l2"), ("secret", "key sk-" + "proj_" + "abcdefghijklmnopqrstu"))

    def test_text_that_breaks_a_rule_is_not_stored_at_attempt_and_is_reported_not_counted(self):
        for rule, text in self.BAD:
            with self.subTest(text[:30]):
                self.tearDown()
                self.setUp()
                out = fe.attempt(self.dir, self.args(approach="first idea", failure=text), "s", T0)
                self.assertEqual(out["note"], f"not counted: {rule}")
                for name in Path(self.dir, "attempts").glob("*.json"):
                    self.assertNotIn(text[:20], name.read_text())

    def test_a_bad_acceptance_check_or_question_is_not_tracked_at_all(self):
        out = fe.attempt(self.dir, self.args(acceptance_check="AC with token=abc", approach="x"), "s", T0)
        self.assertEqual((out["action"], out["note"]), ("CONTINUE", "not counted: secret"))
        out = fe.attempt(self.dir, self.args(approach="x", question="Mail bob@example.com about the lock order?"), "s", T0)
        self.assertEqual(out["note"], "not counted: email")
        stored = " ".join(p.read_text() for p in Path(self.dir, "attempts").glob("*.json"))
        self.assertNotIn("example.com", stored)
        self.assertNotIn("token=abc", stored)

    def test_a_noreply_address_and_ordinary_text_pass(self):
        self.assertIsNone(fe.payload_violation("Co-Authored-By: Claude <noreply@anthropic.com> fixed the lock order"))
        self.assertIsNone(fe.payload_violation("failed in scripts/fable-escalation.py at line 40 with exit 2"))
        self.assertEqual(fe.payload_violation("word " * 2000), "size")

    def test_consult_refuses_a_prompt_that_breaks_a_rule_before_any_reservation(self):
        for rule, text in self.BAD + (("size", "word " * 2000),):
            with self.subTest(text[:30]):
                self.tearDown()
                self.setUp()
                bid = self.ready()
                path = fe._record_path(self.dir, bid)
                record = json.loads(Path(path).read_text())
                record["attempts"][0]["failure"] = text
                Path(path).write_text(json.dumps(record))
                out = self.consult(bid)
                self.assertEqual((out["action"], out["reason"]), ("CONTINUE", f"ineligible_payload:{rule}"))
                self.assertEqual(self.calls(), 0)
                self.assertFalse(os.path.exists(self.ledger))
                self.assertFalse(fe.breaker(self.dir)["open"])

    def test_the_prompt_carries_only_the_blocker_and_the_read_only_rule(self):
        prompt = fe.consult_prompt(fe.load(self.dir, self.ready()))
        self.assertIn("Read only files in this repository; do not open .env*, credentials, ~/.ssh, ~/.config, ~/.claude", prompt)
        self.assertIsNone(fe.payload_violation(prompt))
        self.assertLess(len(prompt.encode()), fe.MAX_PROMPT_BYTES)


class ReviewFixes(Env):
    def test_non_object_json_result_trips_the_breaker_instead_of_crashing(self):
        for out in ("[]", "null", "7", json.dumps({"is_error": False, "modelUsage": {"claude-fable-5-2": {}}, "result": 5})):
            self.tearDown()
            self.setUp()
            os.environ["FAKE_OUT"] = out
            bid = self.ready()
            with self.assertRaisesRegex(fe.EscalationError, "breaker opened"):
                self.consult(bid)
            self.assertTrue(fe.breaker(self.dir)["open"], out)
            self.assertEqual(fe.load(self.dir, bid)["state"], fe.FAILED)

    def test_target_case_and_check_punctuation_do_not_make_a_new_blocker(self):
        self.ready()
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
            bid = self.ready(acceptance_check="AC " + name)
            os.environ[name] = "1"
            try:
                self.assertEqual(self.consult(bid)["reason"], "api_env_present")
            finally:
                del os.environ[name]
        self.assertEqual(self.calls(), 0)

    def test_attempt_text_goes_on_stdin_not_argv(self):
        record = fe.load(self.dir, self.ready())
        self.assertFalse(any("deadlock at step 4" in a for a in fe.consult_argv(record)))
        self.assertIn("deadlock at step 4", fe.consult_prompt(record))

    def test_owner_clearable_blockers_are_authority_problems_not_reasoning_blockers(self):
        for text in ("not satisfiable with current authorization: owner-held steps", "requires owner decision on the credential",
                     "only the owner can read the cause", "credentials are missing until the owner provisions them"):
            a = [dict(x, failure=text) for x in case()["attempts"]]
            self.assertFalse(fe.evaluate(case(attempts=a), self.dir)["eligible"], text)
        self.assertFalse(fe.evaluate(case(cause="owner_decision"), self.dir)["eligible"])

    def test_reordered_words_do_not_make_a_second_distinct_approach(self):
        same = [dict(x, approach=a) for x, a in zip(case()["attempts"], ("fix lock ordering", "Lock ordering, fix"))]
        self.assertFalse(fe.evaluate(case(attempts=same), self.dir)["eligible"])
        different = [dict(x, approach=a) for x, a in zip(case()["attempts"], ("fix lock ordering", "add a timeout and retry"))]
        self.assertTrue(fe.evaluate(case(attempts=different), self.dir)["eligible"])

    def test_ordinary_reasoning_failures_are_not_excluded(self):
        for text in ("test_deadlock timed out at step 4", "assert failed at line 403", "exit 503 from the lock test",
                     "needs the owner field populated", "needs an owner check", "requires the owner id",
                     "credentials are missing from the fixture"):
            a = [dict(x, failure=text) for x in case()["attempts"]]
            self.assertTrue(fe.evaluate(case(attempts=a), self.dir)["eligible"], text)

    def test_prompt_reaches_the_child_on_stdin(self):
        fake = Path(self.bin)
        fake.write_text(FAKE_CLAUDE.replace('open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")',
                                            'open(os.environ["FAKE_LOG"], "a").write(sys.stdin.read().replace("\\n", " ") + "\\n")'))
        self.consult(self.ready())
        logged = Path(self.log).read_text()
        self.assertIn("deadlock at step 4", logged)
        self.assertIn("Which lock order removes the cycle", logged)

    def test_version_probe_timeout_trips_the_breaker(self):
        bid = self.ready()
        real = fe.claude_version
        fe.claude_version = lambda: (_ for _ in ()).throw(fe.subprocess.TimeoutExpired("claude", 30))
        try:
            with self.assertRaises(fe.EscalationError):
                self.consult(bid)
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

    def test_the_consult_timeout_is_below_the_ten_minute_window(self):
        self.assertEqual(fe.CONSULT_TIMEOUT_S, 540)
        self.assertEqual((fe.PER_CONSULT_CEILING, fe.MONTHLY_CEILING), (4.0, 40.0))


class WorkflowBase(Env):
    """Helpers to drive the real CLI the way the development loop calls it (host probes pinned by RUNNER)."""
    SCRIPT = str(ROOT / "scripts/fable-escalation.py")

    def cli(self, *argv, rc=0):
        env = dict(os.environ, FABLE_ESCALATION_DIR=self.dir)
        proc = subprocess.run([sys.executable, "-c", RUNNER, self.SCRIPT, *argv], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def attempt(self, approach, failure="AC3 deadlock at step 4", model="claude-opus-5-5", target="o/r#7", check="AC3 lock ordering",
                question=QUESTION, **extra):
        argv = ["attempt", "--target", target, "--check", check, "--session", "dev-1", "--model", model, "--approach", approach,
                "--failure", failure, "--question", question, "--non-sensitive", "--cwd", self.repo]
        for k, v in extra.items():
            argv += [f"--{k.replace('_', '-')}", v] if v is not True else [f"--{k.replace('_', '-')}"]
        return self.cli(*argv)

    def ready_cli(self):
        self.attempt("reorder acquisition")
        return self.attempt("single global lock")["id"]


class WorkflowCases(WorkflowBase):
    """Controlled cases through the real CLI."""
    def test_two_distinct_failed_attempts_become_ready_and_every_later_call_says_the_same(self):
        self.assertEqual(self.attempt("reorder acquisition")["action"], "CONTINUE")
        second = self.attempt("single global lock")
        self.assertEqual(second["action"], "CONSULT")
        self.assertNotIn("packet", second)
        for approach in ("lock-free index", "retry with backoff"):
            self.assertEqual(self.attempt(approach)["id"], second["id"])
        self.assertEqual(len([r for r in Path(self.dir, "receipts.log").read_text().splitlines() if '"ready"' in r]), 1)
        self.assertEqual(self.calls(), 0)

    def test_consult_runs_once_through_the_cli_with_the_cap_and_tools(self):
        bid = self.ready_cli()
        out = self.cli("consult", "--id", bid, "--execute", "--cwd", self.repo)
        self.assertTrue(Path(out["advice_path"]).is_file())
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.attempt("fourth try")["action"], "CONTINUE")
        self.cli("consult", "--id", bid, "--execute", "--cwd", self.repo, rc=2)
        self.assertEqual(self.calls(), 1)
        argv = json.loads(Path(self.log).read_text().splitlines()[0])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-2")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.00")
        self.assertEqual(self.cli("report")["month_budget"]["committed_usd"], 0.9)

    def test_without_an_authorization_file_the_cli_continues_and_calls_nothing(self):
        os.unlink(self.auth_path)
        self.attempt("reorder acquisition")
        out = self.attempt("single global lock")
        self.assertEqual((out["action"], out["reason"]), ("CONTINUE", "no_authorization"))
        self.assertEqual(self.cli("authorization", "check")["authorization"], "no_authorization")
        self.assertEqual(self.calls(), 0)

    def test_authorization_subcommands_are_read_only_and_never_create_the_file_or_state(self):
        os.unlink(self.auth_path)
        fresh = os.path.join(self.tmp.name, "never-created")
        env = dict(os.environ, FABLE_ESCALATION_DIR=fresh)
        proc = subprocess.run([sys.executable, "-c", RUNNER, self.SCRIPT, "authorization", "template", "--cwd", self.repo],
                              capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tpl = json.loads(proc.stdout)
        self.assertEqual(tpl["host_sha256"], HOST)
        self.assertFalse(os.path.exists(self.auth_path))
        self.assertFalse(os.path.exists(fresh))

    def test_an_uncounted_attempt_says_so(self):
        out = self.attempt("wait for the runner", failure="CI_UNAVAILABLE_EXTERNAL billing limit")
        self.assertEqual(out["action"], "CONTINUE")
        self.assertIn("not counted", out["note"])
        self.assertNotIn("note", self.attempt("reorder acquisition"))

    def test_same_approach_repeated_or_a_different_failure_does_not_become_ready(self):
        self.attempt("reorder acquisition")
        self.assertEqual(self.attempt("reorder acquisition")["action"], "CONTINUE")
        self.assertEqual(self.attempt("single global lock", failure="AC3 times out in setup")["action"], "CONTINUE")

    def test_ordinary_ci_and_queue_delays_never_become_ready(self):
        for failure, cause in (("CI_UNAVAILABLE_EXTERNAL billing limit", None), ("job waiting in queue delay", "queue_delay"),
                               ("runner unavailable", None), ("permission denied on merge", "missing_permission"),
                               ("ECONNRESET from provider", "external_outage")):
            for approach in ("a", "b", "c"):
                extra = {"cause": cause} if cause else {}
                self.assertEqual(self.attempt(approach + failure, failure=failure, check="AC " + failure, **extra)["action"], "CONTINUE", failure)
        self.assertFalse(Path(self.dir, "receipts.log").exists())

    def test_provider_failures_print_the_fallback_and_are_charged_as_reported(self):
        for out, rc, charged in ((dict(FABLE_OK, total_cost_usd=0.9), "1", 0.9), (dict(FABLE_OK, is_error=True, total_cost_usd=1.5), "0", 1.5),
                                 (dict(FABLE_OK, modelUsage={"claude-opus-5-5": {}}, total_cost_usd=0.4), "0", 0.4)):
            self.tearDown()
            self.setUp()
            os.environ["FAKE_OUT"], os.environ["FAKE_RC"] = json.dumps(out), rc
            failure = self.cli("consult", "--id", self.ready_cli(), "--execute", "--cwd", self.repo, rc=2)
            self.assertIn("breaker opened", failure["error"])
            self.assertIn("sonnet + opus-architect", failure["fallback"])
            self.assertEqual(self.month()["entries"][0]["charged_usd"], charged)
            self.assertTrue(fe.breaker(self.dir)["open"])

    def test_failed_consult_stops_all_escalation_without_repeat_calls(self):
        bid = self.ready_cli()
        os.environ["FAKE_RC"] = "1"
        self.cli("consult", "--id", bid, "--execute", "--cwd", self.repo, rc=2)
        os.environ.pop("FAKE_RC")
        self.assertEqual(self.attempt("again")["action"], "CONTINUE")
        self.attempt("one", target="o/r#9", check="AC9 other", failure="AC9 broken")
        self.assertEqual(self.attempt("two", target="o/r#9", check="AC9 other", failure="AC9 broken")["action"], "CONTINUE")
        self.assertEqual(self.calls(), 1)

    def test_unrelated_blockers_continue_while_one_is_ready(self):
        self.ready_cli()
        start = time.monotonic()
        other = self.attempt("only one try", target="o/r#10", check="AC10 other", failure="AC10 flake")
        self.assertEqual(other["action"], "CONTINUE")
        self.assertLess(time.monotonic() - start, 60)  # it returns at once: nothing waits for the owner

    def test_an_attempt_tagged_with_a_cause_is_never_counted_toward_the_trigger(self):
        self.attempt("one", cause="queue_delay")
        self.assertEqual(self.attempt("two")["action"], "CONTINUE")
        self.assertEqual(self.attempt("three")["action"], "CONSULT")

    def test_a_settled_review_disagreement_cannot_trigger_later(self):
        base = ("--target", "o/r#8", "--check", "design review RV1", "--session", "dev-1", "--cwd", self.repo)
        self.cli("attempt", *base, "--review-unresolved", "--review-position", "queue per tenant", "--review-position", "shared queue")
        self.cli("attempt", *base, "--review-resolved")
        out = self.cli("attempt", *base, "--model", "claude-opus-5-5", "--approach", "x", "--failure", "RV1 flake",
                       "--question", "Which of the two queue designs keeps tenant isolation?", "--non-sensitive")
        self.assertEqual(out["action"], "CONTINUE")

    def test_non_ascii_acceptance_checks_are_distinct_blockers(self):
        self.assertNotEqual(fe.fingerprint("o/r#1", "验收一"), fe.fingerprint("o/r#1", "验收二"))


class RepoInvariants(unittest.TestCase):
    def test_the_loop_skills_call_the_attempt_command_and_never_ask_the_owner(self):
        flow = (ROOT / ".claude/skills/engineering-workflow/SKILL.md").read_text()
        loop = (ROOT / ".claude/skills/dev-impl-loop/SKILL.md").read_text()
        self.assertIn("coding-agent-environment-main-view/scripts/fable-escalation.py\" attempt", flow)  # absolute path: works from any repository
        self.assertNotIn("python3 scripts/fable-escalation.py", flow)
        for action in ("CONTINUE", "CONSULT"):
            self.assertIn(action, flow)
        for text in (flow, loop):
            for gone in ("ASK_OWNER", "CONSULT_APPROVED", "AskUserQuestion", "Approve / Decline"):
                self.assertNotIn(gone, text)
        self.assertIn("never ask the owner", flow)
        self.assertEqual(loop.count("Blocker escalation to Fable"), 2)

    def test_the_claude_only_fable_text_is_dropped_from_the_codex_copy(self):
        strip = lambda s: re.sub(r"<!-- claude-only:start -->.*?<!-- claude-only:end -->\n?", "", s, flags=re.S)  # the installer's Codex transform
        for skill in ("engineering-workflow", "dev-impl-loop"):
            text = (ROOT / f".claude/skills/{skill}/SKILL.md").read_text()
            self.assertEqual(text.count("<!-- claude-only:start -->"), text.count("<!-- claude-only:end -->"))
            self.assertIn("Fable", text)  # the Claude copy carries it ...
            for word in ("AskUserQuestion", "fable-escalation", "Fable"):
                self.assertNotIn(word, strip(text), f"{skill}: {word} reaches the Codex copy")  # ... the Codex copy does not

    def test_the_removed_approval_path_is_gone_from_the_script(self):
        text = (ROOT / "scripts/fable-escalation.py").read_text()
        for gone in ("ASK_OWNER", "CONSULT_APPROVED", "APPROVAL_TTL_S", "ask-pending", "AskUserQuestion", "def offer", "def answer", "def show"):
            self.assertNotIn(gone, text)
        for name in ("offer", "answer", "show"):
            proc = subprocess.run([sys.executable, str(ROOT / "scripts/fable-escalation.py"), name], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, name)  # argparse: invalid choice

    def test_no_scheduler_hook_or_workflow_calls_consult(self):
        for path in list((ROOT / ".github").rglob("*")) + list((ROOT / ".claude").rglob("*")):
            if path.is_file():
                self.assertNotIn("fable-escalation.py consult", path.read_text(errors="ignore"), str(path))

    def test_no_default_setting_or_agent_enables_fable(self):
        for path in (ROOT / ".claude").rglob("*"):
            if path.is_file() and path.suffix in (".md", ".json", ".toml", ".yml", ".yaml"):
                self.assertNotRegex(path.read_text(errors="ignore"), r"(?im)^\s*model:\s*\S*fable", str(path))


if __name__ == "__main__":
    unittest.main()
