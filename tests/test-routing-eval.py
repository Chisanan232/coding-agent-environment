#!/usr/bin/env python3
"""Routing evaluation harness: isolation, accounting and the precommitted decision rule."""
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("routing_eval", ROOT / "scripts/routing-eval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

Z = {k: 0 for k in ev.KINDS}


def vec(**kw):
    return {**Z, **kw}


class Corpus(unittest.TestCase):
    def test_pilot_tasks_are_complete_and_cover_decline_cases(self):
        corpus = json.loads(ev.CORPUS_PATH.read_text())
        ids = [t["id"] for t in corpus["tasks"]]
        self.assertEqual(len(ids), len(set(ids)))
        pilot = [t for t in corpus["tasks"] if t["pilot"]]
        self.assertEqual(len(pilot), json.loads(ev.POLICY_PATH.read_text())["run_caps"]["pilot_tasks"])
        classes = {t["class"] for t in pilot}
        self.assertTrue({"bounded", "ambiguous", "high_risk", "control"} <= classes)
        for t in pilot:
            self.assertEqual(len(t["merge"]), 40)
            self.assertEqual(len(t["base"]), 40)
            self.assertTrue(t["task"].strip())

    def test_task_text_does_not_leak_solution_or_class(self):
        for t in json.loads(ev.CORPUS_PATH.read_text())["tasks"]:
            if t["pilot"]:
                self.assertNotIn(t["class"], t["task"].lower().split())


class Accounting(unittest.TestCase):
    def test_usage_vector_groups_by_family_and_kind(self):
        result = {"modelUsage": {
            "claude-sonnet-5-5": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                                  "cacheCreationInputTokens": 4},
            "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 6, "cacheReadInputTokens": 7,
                                 "cacheCreationInputTokens": 8}}}
        v = ev.usage_vector(result)
        self.assertEqual(v["sonnet"], {"fresh_input": 1, "output": 2, "cache_read": 3, "cache_write": 4})
        self.assertEqual(v["haiku"]["cache_write"], 8)
        self.assertEqual(ev.usage_vector({}), {})

    def test_scope_violations_ignore_bytecode_only(self):
        allowed = {"scripts/a.py", "tests/t.py"}
        self.assertEqual(ev.scope_violations(["scripts/a.py", "scripts/__pycache__/a.pyc"], allowed), [])
        self.assertEqual(ev.scope_violations(["scripts/a.py", "settings.json"], allowed), ["settings.json"])


class DecisionRule(unittest.TestCase):
    bounded = {"class": "bounded"}

    def arm(self, ok=True, sonnet=None, haiku=None):
        usage = {"sonnet": sonnet or vec()}
        if haiku is not None:
            usage["haiku"] = haiku
        return {"accepted": ok, "usage": usage}

    def test_accepted_requires_oracle_review_and_scope(self):
        self.assertTrue(ev.accepted({"oracle_passed": True, "out_of_scope": []}, {"critical_high": 0}))
        self.assertTrue(ev.accepted({"oracle_passed": None, "out_of_scope": []}, {"critical_high": 0}))
        self.assertFalse(ev.accepted({"oracle_passed": False, "out_of_scope": []}, {"critical_high": 0}))
        self.assertFalse(ev.accepted({"oracle_passed": True, "out_of_scope": ["x"]}, {"critical_high": 0}))
        self.assertFalse(ev.accepted({"oracle_passed": True, "out_of_scope": []}, {"critical_high": 1}))
        self.assertFalse(ev.accepted({"oracle_passed": True, "out_of_scope": []}, {}))

    def test_supports_only_when_every_sonnet_kind_is_not_higher(self):
        a = self.arm(sonnet=vec(fresh_input=10, cache_write=10, cache_read=10, output=10))
        better = self.arm(sonnet=vec(fresh_input=5, cache_write=10, cache_read=9, output=1), haiku=vec(output=3))
        mixed = self.arm(sonnet=vec(fresh_input=5, cache_write=11, cache_read=9, output=1), haiku=vec(output=3))
        self.assertEqual(ev.task_verdict(self.bounded, a, better), "SUPPORTS_HAIKU")
        self.assertEqual(ev.task_verdict(self.bounded, a, mixed), "INCONCLUSIVE")

    def test_rejects_when_haiku_arm_fails_and_direct_passes(self):
        a = self.arm()
        b = self.arm(ok=False, haiku=vec(output=1))
        self.assertEqual(ev.task_verdict(self.bounded, a, b), "REJECTS_HAIKU")

    def test_no_delegation_in_a_bounded_task_is_not_evidence(self):
        self.assertEqual(ev.task_verdict(self.bounded, self.arm(), self.arm()), "INCONCLUSIVE")

    def test_decline_classes_pass_only_with_zero_haiku_tokens(self):
        for cls in ("ambiguous", "high_risk", "control"):
            task = {"class": cls}
            self.assertEqual(ev.task_verdict(task, self.arm(), self.arm()), "DECLINE_OK")
            self.assertEqual(ev.task_verdict(task, self.arm(), self.arm(haiku=vec(output=1))), "DECLINE_FAILED")
            self.assertEqual(ev.task_verdict(task, self.arm(), self.arm(ok=False)), "DECLINE_FAILED")

    def test_pilot_verdict_never_promotes(self):
        self.assertEqual(ev.pilot_verdict(["DECLINE_FAILED", "SUPPORTS_HAIKU"]), "NO_HAIKU")
        self.assertEqual(ev.pilot_verdict(["REJECTS_HAIKU"]), "NO_HAIKU")
        self.assertEqual(ev.pilot_verdict(["SUPPORTS_HAIKU", "INCONCLUSIVE"]), "INCONCLUSIVE_KEEP_OPT_IN")
        self.assertEqual(ev.pilot_verdict(["SUPPORTS_HAIKU", "DECLINE_OK"]), "OPT_IN_CANDIDATE")
        self.assertEqual(ev.pilot_verdict([]), "INCONCLUSIVE_KEEP_OPT_IN")
        self.assertNotIn("PROMOTE", json.dumps(json.loads(ev.POLICY_PATH.read_text())["decision_rule"]["pilot_verdicts"]))


class Invocation(unittest.TestCase):
    task = {"task": "do the thing"}

    def test_arm_a_has_no_worker_and_arm_b_pins_an_explicit_haiku_id(self):
        argv_a, prompt_a = ev.claude_args("A", self.task)
        argv_b, prompt_b = ev.claude_args("B", self.task)
        self.assertNotIn("--agents", argv_a)
        self.assertNotIn("Agent", argv_a)
        agents = json.loads(argv_b[argv_b.index("--agents") + 1])
        self.assertEqual(agents["haiku-worker"]["model"], ev.HAIKU_ID)
        self.assertNotEqual(agents["haiku-worker"]["model"], "haiku")
        self.assertIn("Agent", argv_b)
        self.assertNotIn("Agent", agents["haiku-worker"]["tools"])
        self.assertIn("Delegation policy", prompt_b)
        self.assertNotIn("Delegation policy", prompt_a)

    def test_runs_are_capped_isolated_and_never_bypass_permissions(self):
        policy = json.loads(ev.POLICY_PATH.read_text())
        for arm in ("A", "B"):
            argv, _ = ev.claude_args(arm, self.task)
            joined = " ".join(argv)
            self.assertEqual(argv[argv.index("--max-turns") + 1], str(policy["run_caps"]["max_turns"]))
            self.assertIn("--max-budget-usd", argv)
            for flag in policy["isolation"]["claude_flags"]:
                self.assertIn(flag, argv)
            self.assertNotIn("dangerously", joined)
            self.assertNotIn("bypass", joined)
            self.assertFalse(any(t.startswith("Bash(git push") for t in argv))
            self.assertNotIn("Bash(python3 *)", argv)
            self.assertFalse(any(t == "Bash" for t in argv))

    def test_worker_gets_the_same_narrow_bash_and_env_is_scrubbed(self):
        argv, _ = ev.claude_args("B", self.task)
        tools = json.loads(argv[argv.index("--agents") + 1])["haiku-worker"]["tools"]
        self.assertNotIn("Bash", tools)
        self.assertIn("Bash(python3 -I tests/*)", tools)
        import os
        os.environ["GH_TOKEN_TEST_SENTINEL"] = "x"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "x"
        env = ev.scrubbed_env()
        self.assertNotIn("GH_TOKEN_TEST_SENTINEL", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertLessEqual(set(env), set(ev.ENV_KEEP))

    def test_policy_was_committed_with_the_gates_it_describes(self):
        policy = json.loads(ev.POLICY_PATH.read_text())
        self.assertEqual(policy["run_caps"]["trials_per_task_arm"], 1)
        self.assertIn("never from a pilot", policy["decision_rule"]["promotion"])
        self.assertIn("zero Haiku tokens", policy["decision_rule"]["decline_cases"])


class Prepare(unittest.TestCase):
    def test_prepared_workdir_has_one_commit_and_no_future_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                          cwd=src, check=True, capture_output=True, text=True).stdout
            g("init", "-q")
            (src / "a.txt").write_text("base")
            g("add", "-A"); g("commit", "-q", "-m", "base")
            base = g("rev-parse", "HEAD").strip()
            (src / "future.txt").write_text("solution")
            g("add", "-A"); g("commit", "-q", "-m", "future")
            corpus = Path(tmp) / "corpus.json"
            corpus.write_text(json.dumps({"tasks": [{"id": "t", "base": base}]}))
            orig = ev.CORPUS_PATH
            ev.CORPUS_PATH = corpus
            try:
                out = Path(tmp) / "work"
                with redirect_stdout(io.StringIO()):
                    ev.cmd_prepare(type("A", (), {"task": "t", "out": str(out), "repo": str(src)})())
            finally:
                ev.CORPUS_PATH = orig
            self.assertEqual((out / "a.txt").read_text(), "base")
            self.assertFalse((out / "future.txt").exists())
            count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=out, capture_output=True,
                                   text=True).stdout.strip()
            self.assertEqual(count, "1")
            self.assertEqual(subprocess.run(["git", "log", "--all", "--format=%s"], cwd=out,
                                            capture_output=True, text=True).stdout.split(), ["base"])


class Score(unittest.TestCase):
    def test_score_reads_a_run_directory_and_reports_a_pilot_verdict(self):
        corpus = json.loads(ev.CORPUS_PATH.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for t in (x for x in corpus["tasks"] if x["pilot"]):
                for arm in ("A", "B"):
                    (d / f"{t['id']}.{arm}.run.json").write_text(json.dumps(
                        {"is_error": False, "usage": {"sonnet": vec(output=5)}, "num_turns": 3,
                         "harness_wall_ms": 1000}))
                    (d / f"{t['id']}.{arm}.grade.json").write_text(json.dumps(
                        {"oracle_passed": True, "out_of_scope": []}))
                    (d / f"{t['id']}.{arm}.review.json").write_text(json.dumps({"critical_high": 0}))
            buf = io.StringIO()
            with redirect_stdout(buf):
                ev.cmd_score(type("A", (), {"dir": str(d)})())
        out = buf.getvalue()
        self.assertIn("PILOT VERDICT: INCONCLUSIVE_KEEP_OPT_IN", out)
        self.assertIn("promotion needs >= 30", out)


if __name__ == "__main__":
    unittest.main()
