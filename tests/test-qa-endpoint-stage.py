#!/usr/bin/env python3
"""run_endpoint_phase and the endpoint self-test environment (SPE-145 PR-b4), with the lifecycle and Codex replaced by doubles. The real
thing is tests/test-qa-endpoint-stage-live.py. These pin the order (nothing that needs the daemon runs before it is proved), that the
endpoint is torn down on every path, and that only a needs_endpoint test is given the QA_ENDPOINT_* paths."""
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_stage_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)


class FakeLife:
    """Stands in for EndpointLifecycle: records the call order; failing=<method> makes that step raise EndpointFailed."""
    failing = None
    log = []

    def __init__(self, endpoint, record_dir, ops=None, manifest=None):
        self.record = {"port": endpoint.port, "controls": [], "problems": [], "teardown": None}
        FakeLife.log = []

    def _step(self, name):
        FakeLife.log.append(name)
        if FakeLife.failing == name:
            raise side.EndpointFailed("%s failed" % name)

    def create(self):
        self._step("create")

    def prove_profiles(self, *a):
        self._step("prove_profiles")

    def start(self):
        self._step("start")

    def supervise(self, final=False):
        FakeLife.log.append("supervise-final" if final else "supervise")
        return True

    def teardown(self):
        FakeLife.log.append("teardown")
        return {"clean": True}


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        FakeLife.failing = None
        patches = [mock.patch.object(side, "EndpointLifecycle", FakeLife), mock.patch.object(side, "CANARY_ROOT", self.root / "canary"),
                   mock.patch.object(side, "real_codex_home", lambda: self.root / ".codex"),
                   mock.patch.object(side, "run_selftests", self.fake_selftests), mock.patch.object(side, "endpoint_codex_probe", self.fake_probe)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.selftest_calls, self.probe_problems = 0, []
        self.endpoint = side.Endpoint(self.root / "qa", self.root / "x", "att", 25000, "fornax-daemon/1", self.root / "x/out/payload", self.root / "x/out/payload/bin/d")
        self.args = types.SimpleNamespace(codex_bin="codex")

    def fake_selftests(self, *a, **k):
        self.selftest_calls += 1
        return [{"id": "t"}]

    def fake_probe(self, *a):
        FakeLife.log.append("codex_controls")
        return [{"control": "c", "ok": not self.probe_problems}], self.probe_problems

    def run_phase(self):
        return side.run_endpoint_phase(self.args, self.root / "x/out", {}, self.endpoint, {}, self.root / "home")

    def test_the_order_is_prove_start_codex_controls_selftests_teardown(self):
        record = self.run_phase()
        self.assertEqual(FakeLife.log, ["create", "prove_profiles", "start", "codex_controls", "supervise", "supervise-final", "teardown"])
        self.assertTrue(record["established"])
        self.assertEqual(self.selftest_calls, 1)

    def test_a_failed_step_tears_down_and_runs_nothing_after_it(self):
        for step in ("create", "prove_profiles", "start"):
            FakeLife.failing = step
            self.selftest_calls = 0
            record = self.run_phase()
            self.assertFalse(record["established"], step)
            self.assertEqual(FakeLife.log[-1], "teardown", step)
            self.assertNotIn("codex_controls", FakeLife.log, step)
            self.assertEqual(self.selftest_calls, 0, step)
            self.assertIn("%s failed" % step, record["problems"][0])

    def test_a_failed_codex_control_blocks_the_selftests(self):
        self.probe_problems = ["neg_tcp_daemon_port expected DENIED_EPERM, observed ALLOWED"]
        record = self.run_phase()
        self.assertFalse(record["established"])
        self.assertEqual(self.selftest_calls, 0)
        self.assertEqual(FakeLife.log[-1], "teardown")

    def test_an_unclean_teardown_is_not_established(self):
        with mock.patch.object(FakeLife, "teardown", lambda self: {"clean": False}):
            self.assertFalse(self.run_phase()["established"])


class ControlWordingTests(unittest.TestCase):
    def test_the_proxy_control_passes_only_on_the_proxys_own_refusal(self):
        self.assertTrue(side.control_ok("DENIED_PROXY", "DENIED_PROXY"))
        for observed in ("NOPROXY", "ALLOWED", "ALLOWED:HTTP/1.1 502", "DENIED:1", "DENIED:61", "", "DENIED_403"):
            self.assertFalse(side.control_ok("DENIED_PROXY", observed), observed)

    def test_the_decoy_is_where_the_codex_profile_can_otherwise_write(self):
        decoy = [c for c in side.RELAY_CODEX_CONTROLS if c[0] == "neg_uds_decoy"][0]
        self.assertIn("chdir", decoy[1])          # a long attempt path never turns the refusal into ENAMETOOLONG


class SelftestEnvTests(unittest.TestCase):
    def test_only_a_needs_endpoint_test_gets_the_endpoint_paths(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "payload").mkdir()
        (root / "tmp").mkdir()
        endpoint = side.Endpoint(root / "qa", root / "x", "att", 25000, "fornax-daemon/1", root / "x/o/payload", root / "x/o/payload/bin/d")
        tests = [{"id": "plain", "argv": ["bin/t"], "stdin": None, "timeout_s": 5, "max_output_bytes": 100, "needs_endpoint": False},
                 {"id": "ep", "argv": ["bin/t"], "stdin": None, "timeout_s": 5, "max_output_bytes": 100, "needs_endpoint": True}]
        seen = []

        def popen(argv, **kw):
            seen.append(kw["env"])
            raise OSError("stop")
        with mock.patch.object(side.subprocess, "Popen", popen):
            side.run_selftests("codex", root, {"selftests": tests}, {"PATH": "/usr/bin"}, endpoint)
        self.assertNotIn("QA_ENDPOINT_HTTP_SOCK", seen[0])
        self.assertEqual(seen[1]["QA_ENDPOINT_HTTP_SOCK"], str(endpoint.relay_sock))
        self.assertEqual(seen[1]["QA_ENDPOINT_STATE"], str(endpoint.state))
        self.assertNotIn(str(endpoint.port), "".join(seen[1].values()))        # paths only, never the port


if __name__ == "__main__":
    unittest.main()
