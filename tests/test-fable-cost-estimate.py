#!/usr/bin/env python3
"""Fable cost estimate (SPE-176, SPE-171): a list-price estimate before any invocation. Never calls a real model."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fable-escalation.py"
spec = spec_from_file_location("fable_escalation", SCRIPT)
fe = module_from_spec(spec)
spec.loader.exec_module(fe)

T0 = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)
KEYS = {"estimated_usd_low", "estimated_usd_high", "cap_usd", "basis", "model", "invoked"}
# A stand-in for the claude CLI that logs every call and fails, as in the measured experiment: any invocation would be visible.
STUB = '#!/bin/sh\necho "$@" >> "$STUB_LOG"\nexit 1\n'


def case(**over):
    base = {"target": "o/r#7", "acceptance_check": "AC3 lock ordering test passes", "blocker": "deadlock under AC3",
            "attempts": [{"model": "claude-opus-5-5", "approach": "reorder acquisition", "failure": "AC3 deadlock at step 4"},
                         {"model": "claude-opus-5-5", "approach": "single global lock", "failure": "AC3 deadlock at step 4"}],
            "question": "Which lock order removes the cycle between the cache and the index?", "cause": "none",
            "inputs_non_sensitive": True}
    base.update(over)
    return base


class Estimate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "fable")
        os.makedirs(os.path.join(self.dir, "advice"), mode=0o700)
        self.stub = os.path.join(self.tmp.name, "claude")
        Path(self.stub).write_text(STUB)
        os.chmod(self.stub, 0o755)
        self.log = os.path.join(self.tmp.name, "stub.log")
        self.saved = {k: os.environ.get(k) for k in ("FABLE_CLAUDE_BIN", "STUB_LOG", "FABLE_ESCALATION_DIR", "HOME")}
        os.environ.update(FABLE_CLAUDE_BIN=self.stub, STUB_LOG=self.log, FABLE_ESCALATION_DIR=self.dir, HOME=self.tmp.name)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def assert_never_invoked(self):
        self.assertFalse(os.path.exists(self.log), "the claude stub was called")

    def test_closed_schema_and_values(self):
        cost = fe.estimate_case(case())
        self.assertEqual(set(cost), KEYS)
        self.assertIs(cost["invoked"], False)
        self.assertEqual(cost["model"], "claude-fable-5-2")
        argv = fe.consult_argv({})
        self.assertEqual(cost["cap_usd"], fe.PER_CONSULT_CEILING)  # the reservation; the client cap sits $1 below it
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), cost["cap_usd"] - 1)
        self.assertEqual(cost["estimated_usd_high"], cost["cap_usd"])
        self.assertTrue(0 < cost["estimated_usd_low"] <= cost["estimated_usd_high"])
        self.assertTrue(all(isinstance(b, str) and b for b in cost["basis"]))
        for value in (cost["estimated_usd_low"], cost["estimated_usd_high"]):
            self.assertEqual(value, round(value, 2))

    def test_low_is_at_least_the_measured_cold_prefix_and_grows_with_the_packet(self):
        small = fe.estimate_case(case())
        many = [{"model": "claude-opus-5-5", "approach": f"{i} " + "a" * 190, "failure": "f" * 200} for i in range(8)]
        big = fe.estimate_case(case(attempts=many, blocker="b" * 300))
        self.assertGreaterEqual(small["estimated_usd_low"], 0.739)
        self.assertGreater(big["estimated_usd_low"], small["estimated_usd_low"])

    def test_deterministic(self):
        self.assertEqual(fe.estimate_case(case()), fe.estimate_case(case()))
        self.assertEqual(json.dumps(fe.estimate_case(case()), sort_keys=True), json.dumps(fe.estimate_case(case()), sort_keys=True))

    def test_basis_cites_the_documented_reference_points(self):
        basis = " ".join(fe.estimate_case(case())["basis"])
        for fragment in ("$1.29", "$0.74", "46190", "3 bytes per token", "reservation ceiling $4.00", "--max-budget-usd 3.00", "nothing was invoked"):
            self.assertIn(fragment, basis)

    def test_reference_figures_match_their_records(self):
        ref = json.loads((ROOT / "docs/fable/consult-cost-reference.json").read_text())
        for run in ref["cold_prefix_runs"]:
            text = (ROOT / run["source"]).read_text()
            self.assertIn(f"${run['usd']}", text)
            self.assertIn(f"{run['cache_creation_tokens']:,}", text)
        for consult in ref["consults"]:
            self.assertIn(f"${consult['usd']}", (ROOT / consult["source"]).read_text())

    def test_unreadable_reference_falls_back_to_the_cap_without_raising(self):
        cost = fe.estimate(fe._consult_fields(case()), reference_path=os.path.join(self.tmp.name, "missing.json"))
        self.assertEqual(set(cost), KEYS)
        self.assertEqual(cost["estimated_usd_low"], cost["cap_usd"])
        self.assertIn("reference file unreadable: low set to the cap", cost["basis"])

    def test_estimate_never_invokes_and_never_touches_state(self):
        with mock.patch.object(fe.subprocess, "run", side_effect=AssertionError("subprocess called")), \
                mock.patch.object(fe, "state_dir", side_effect=AssertionError("state touched")):
            fe.estimate_case(case())
        self.assert_never_invoked()
        self.assertEqual(sorted(os.listdir(self.dir)), ["advice"])

    def test_cli_estimate_creates_no_state_and_calls_nothing(self):
        fresh = os.path.join(self.tmp.name, "never-created")
        path = os.path.join(self.tmp.name, "case.json")
        Path(path).write_text(json.dumps(case()))
        proc = subprocess.run([sys.executable, str(SCRIPT), "estimate", "--case", path], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, FABLE_ESCALATION_DIR=fresh))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout), fe.estimate_case(case()))
        self.assertFalse(os.path.exists(fresh))
        self.assert_never_invoked()

    def test_cli_estimate_rejects_an_incomplete_case(self):
        path = os.path.join(self.tmp.name, "case.json")
        Path(path).write_text(json.dumps({"target": "o/r#7"}))
        proc = subprocess.run([sys.executable, str(SCRIPT), "estimate", "--case", path], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error", json.loads(proc.stdout))

    def test_the_consult_path_carries_no_cost_prompt_and_the_estimate_matches_the_reservation(self):
        text = (ROOT / "scripts/fable-escalation.py").read_text()
        for gone in ("Approve one read-only", "Nothing is invoked until you approve", "packet_for", "why_opus_insufficient"):
            self.assertNotIn(gone, text)
        cost = fe.estimate_case(case())
        self.assertEqual(cost["estimated_usd_high"], 4.0)
        self.assert_never_invoked()

    def test_estimate_basis_states_the_client_cap_and_headroom(self):
        basis = " ".join(fe.estimate_case(case())["basis"])
        self.assertIn("checks only between turns", basis)
        self.assertIn("$1 headroom", basis)

    def test_a_dry_run_consult_without_authorization_refuses_and_never_invokes(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        os.environ["HOME"] = home
        base = {"target": "o/r#8", "acceptance_check": "AC1 passes", "model": "claude-opus-5-5", "failure": "AC1 timeout at 30 s",
                "question": "Which call blocks the event loop during AC1?", "non_sensitive": True, "cwd": self.tmp.name}
        fe.attempt(self.dir, dict(base, approach="cache the client"), "sess-1", T0)
        out = fe.attempt(self.dir, dict(base, approach="move to a thread pool"), "sess-1", T0)
        self.assertEqual((out["action"], out["reason"]), ("CONTINUE", "no_authorization"))
        self.assertIn("opus-architect", out["fallback"])
        self.assert_never_invoked()


if __name__ == "__main__":
    unittest.main()
