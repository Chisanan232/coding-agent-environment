"""Black-box tests for opt-in native Glomeris leases around agent launches."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "bin/coding-agent-resource"


class NativeResourceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.events = self.root / "events.jsonl"
        self.state = self.root / "native-state.json"
        self.child_record = self.root / "child.json"
        self.child_started = self.root / "child-started"
        self.confirm_observed = self.root / "confirm-observed"
        self.core = self.root / "glomeris"
        self.core.write_text('''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
state_path = os.environ["NATIVE_STATE"]
if os.path.exists(state_path):
 state = json.load(open(state_path))
else:
 state = {item["id"]: {"id": item["id"], "generation": item["generation"],
                        "revision": 0, "claims": [], "transitions": []}
          for item in json.loads(os.environ["NATIVE_RESOURCES"])}
if args == ["resource", "native", "inspect", "--json"]:
 print(json.dumps(list(state.values())))
 sys.exit(0)
if args != ["resource", "native", "request", "--json"]: sys.exit(90)
request = json.load(sys.stdin)
action = request.get("op")
if action == "update": action = request.get("update", {}).get("kind")
safe_request = dict(request)
if "token" in safe_request: safe_request["token"] = "[redacted]"
if action == "confirm_child" and os.environ.get("WAIT_FOR_CHILD_START") == "1":
 deadline = time.monotonic() + 5
 while not os.path.exists(os.environ["CHILD_STARTED"]) and time.monotonic() < deadline:
  time.sleep(.01)
 if not os.path.exists(os.environ["CHILD_STARTED"]): sys.exit(86)
with open(os.environ["NATIVE_EVENTS"], "a") as out:
 out.write(json.dumps({"request": safe_request, "child_exists": os.path.exists(os.environ["CHILD_STARTED"])}) + "\\n")
resource = state[request["id"]]
if resource["generation"] != request["generation"] or resource["revision"] != request["revision"]:
 sys.exit(91)
if request["request_id"] in [entry for entry in os.environ.get("SEEN_IDS", "").split(",") if entry]: sys.exit(92)
fail = os.environ.get("REFUSE_ACTION") == action and os.environ.get("REFUSE_ID", request["id"]) == request["id"]
if fail:
 print(json.dumps({"status":"refused"}))
 sys.exit(0)
if action == "acquire":
 capability = {"claim":"claim-" + request["id"], "epoch":1, "token":"secret-token-" + request["id"]}
 resource["claims"].append({"capability":capability, "state":"acquired"})
 reply_extra = {"capability": capability}
else:
 resource["transitions"].append(request["update"]["kind"])
 if request["update"]["kind"] == "wait": resource["claims"][0]["state"] = "waiting"
 reply_extra = {}
resource["revision"] += 1
json.dump(state, open(state_path, "w"))
if action == "confirm_child":
 open(os.environ["CONFIRM_OBSERVED"], "w").write("confirmed")
if os.environ.get("DROP_ACK_ACTION") == action:
 sys.exit(0)
reply = {"status":"acknowledged", "id":resource["id"], "generation":resource["generation"],
         "revision":resource["revision"], **reply_extra}
print(json.dumps(reply))
''')
        self.core.chmod(0o755)
        for host in ("claude", "codex"):
            scripts = self.home / f".{host}/skills/engineering-runtime/scripts"
            scripts.mkdir(parents=True)
            (scripts / "bootstrap.py").write_text(
                "print('export ENGINEERING_STORAGE_BIN=' + repr(" + repr(str(self.core)) + "))\n"
                "print('export ENGINEERING_STORAGE_STATUS=verified_installation')\n")
        self.resources = [{"id": "1" * 32, "generation": "a" * 32}]
        self.env = dict(os.environ, HOME=str(self.home), NATIVE_STATE=str(self.state),
                        NATIVE_EVENTS=str(self.events), CHILD_RECORD=str(self.child_record),
                        CHILD_STARTED=str(self.child_started), CONFIRM_OBSERVED=str(self.confirm_observed),
                        NATIVE_RESOURCES=json.dumps(self.resources),
                        CODING_AGENT_NATIVE_RESOURCES=json.dumps(self.resources),
                        CODING_AGENT_RESOURCE_SESSION_ID="session-existing",
                        OPENAI_MODEL="preserve-model", ENGINEERING_STORAGE_BIN="/must-not-be-used")

    def run_adapter(self, host, *command, env=None, timeout=10):
        return subprocess.run([sys.executable, str(ADAPTER), "--host", host, "--", *command],
                              env=env or self.env, text=True, capture_output=True, timeout=timeout)

    def events_read(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def state_read(self):
        return json.loads(self.state.read_text())

    def child(self, code="import json,os,sys; json.dump({'args':sys.argv[1:], 'env':dict(os.environ)}, open(os.environ['CHILD_RECORD'],'w'))"):
        path = self.root / "child.py"
        path.write_text(code)
        return [sys.executable, str(path), "arg with spaces"]

    def child_until_confirmation(self):
        code = """import json, os, sys, time
from pathlib import Path
Path(os.environ['CHILD_STARTED']).write_text('started')
deadline = time.monotonic() + 8
while not Path(os.environ['CONFIRM_OBSERVED']).exists() and time.monotonic() < deadline:
    time.sleep(.01)
if not Path(os.environ['CONFIRM_OBSERVED']).exists():
    raise SystemExit(86)
json.dump({'args': sys.argv[1:], 'env': dict(os.environ)}, open(os.environ['CHILD_RECORD'], 'w'))
"""
        return self.child(code)

    def test_acquire_and_prepare_precede_child_and_normal_exit_waits_without_release(self):
        result = self.run_adapter("claude", *self.child_until_confirmation(),
                                  env=dict(self.env, WAIT_FOR_CHILD_START="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(self.child_record.read_text())
        self.assertEqual(observed["args"], ["arg with spaces"])
        self.assertNotIn("CODING_AGENT_NATIVE_RESOURCES", observed["env"])
        self.assertFalse(any("secret-token" in value or "claim-" in value
                             for value in observed["env"].values()))
        self.assertEqual(observed["env"]["CODING_AGENT_RESOURCE_SESSION_ID"], "session-existing")
        self.assertEqual(observed["env"]["OPENAI_MODEL"], "preserve-model")
        events = self.events_read()
        requests = [event["request"] for event in events]
        operations = [request["op"] if request["op"] == "acquire" else request["update"]["kind"]
                      for request in requests]
        self.assertEqual(operations, ["acquire", "prepare_child", "confirm_child", "wait"])
        self.assertFalse(events[1]["child_exists"])
        self.assertTrue(events[2]["child_exists"])
        self.assertEqual(len({request["request_id"] for request in requests}), len(requests))
        claim = self.state_read()[self.resources[0]["id"]]["claims"][0]
        self.assertEqual(claim["state"], "waiting")
        self.assertNotIn("release", operations)

    def test_child_exit_status_is_preserved_and_all_host_scopes_are_sent(self):
        self.resources = [{"id": "1" * 32, "generation": "a" * 32},
                          {"id": "2" * 32, "generation": "b" * 32}]
        env = dict(self.env, NATIVE_RESOURCES=json.dumps(self.resources),
                   CODING_AGENT_NATIVE_RESOURCES=json.dumps(self.resources))
        result = self.run_adapter("codex", *self.child("raise SystemExit(7)"), env=env)
        self.assertEqual(result.returncode, 7, result.stderr)
        acquired = [event["request"] for event in self.events_read()
                    if event["request"]["op"] == "acquire"]
        self.assertEqual([request["id"] for request in acquired], [item["id"] for item in self.resources])
        self.assertEqual({request["host"] for request in acquired}, {"codex"})

    def test_managed_native_conflict_refuses_before_core_or_child(self):
        env = dict(self.env, CODING_AGENT_MANAGED_RESOURCE_ID="legacy-id")
        result = self.run_adapter("claude", *self.child(), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stderr)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.child_record.exists())

    def test_invalid_native_list_or_missing_session_refuses_before_launch(self):
        child = self.child()
        for raw in ("[]", "{}", "[{}]", json.dumps([{"id": "x", "generation": "a" * 32}]),
                    json.dumps([self.resources[0], self.resources[0]])):
            with self.subTest(raw=raw):
                result = self.run_adapter("claude", *child,
                                          env=dict(self.env, CODING_AGENT_NATIVE_RESOURCES=raw))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.child_record.exists())
        env = dict(self.env)
        env.pop("CODING_AGENT_RESOURCE_SESSION_ID")
        env.pop("CODEX_THREAD_ID", None)
        result = self.run_adapter("claude", *child, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.child_record.exists())

    def test_partial_acquisition_failure_preserves_prior_claim_and_never_starts_child(self):
        self.resources = [{"id": "1" * 32, "generation": "a" * 32},
                          {"id": "2" * 32, "generation": "b" * 32}]
        env = dict(self.env, NATIVE_RESOURCES=json.dumps(self.resources),
                   CODING_AGENT_NATIVE_RESOURCES=json.dumps(self.resources),
                   REFUSE_ACTION="acquire", REFUSE_ID=self.resources[1]["id"])
        result = self.run_adapter("claude", *self.child(), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.child_record.exists())
        state = self.state_read()
        self.assertEqual(len(state[self.resources[0]["id"]]["claims"]), 1)
        self.assertEqual(state[self.resources[1]["id"]]["claims"], [])

    def test_partial_prepare_failure_cancels_known_unlaunched_intent_but_keeps_claims(self):
        self.resources = [{"id": "1" * 32, "generation": "a" * 32},
                          {"id": "2" * 32, "generation": "b" * 32}]
        env = dict(self.env, NATIVE_RESOURCES=json.dumps(self.resources),
                   CODING_AGENT_NATIVE_RESOURCES=json.dumps(self.resources),
                   REFUSE_ACTION="prepare_child", REFUSE_ID=self.resources[1]["id"])
        result = self.run_adapter("claude", *self.child(), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.child_record.exists())
        state = self.state_read()
        self.assertEqual(len(state[self.resources[0]["id"]]["claims"]), 1)
        self.assertEqual(state[self.resources[0]["id"]]["transitions"],
                         ["prepare_child", "cancel_unlaunched"])

    def test_lost_acquire_ack_is_not_retried_and_capabilities_are_not_exposed(self):
        env = dict(self.env, DROP_ACK_ACTION="acquire")
        result = self.run_adapter("claude", *self.child(), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.child_record.exists())
        self.assertEqual(len(self.events_read()), 1)
        self.assertEqual(len(self.state_read()[self.resources[0]["id"]]["claims"]), 1)
        for output in (result.stdout, result.stderr):
            self.assertNotIn("secret-token", output)
            self.assertNotIn("claim-", output)

    def test_lost_prepare_ack_never_launches_or_retries_uncertain_intent(self):
        env = dict(self.env, DROP_ACK_ACTION="prepare_child")
        result = self.run_adapter("claude", *self.child(), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.child_record.exists())
        requests = [event["request"] for event in self.events_read()]
        self.assertEqual([request["op"] for request in requests], ["acquire", "update"])
        self.assertEqual(requests[1]["update"]["kind"], "prepare_child")
        self.assertEqual(len({request["request_id"] for request in requests}), len(requests))
        self.assertEqual(self.state_read()[self.resources[0]["id"]]["transitions"], ["prepare_child"])

    def test_confirm_failure_does_not_stop_useful_child_or_release_claim(self):
        child_code = "import os; open(os.environ['CHILD_RECORD'],'w').write('finished'); raise SystemExit(9)"
        env = dict(self.env, REFUSE_ACTION="confirm_child")
        result = self.run_adapter("claude", *self.child(child_code), env=env)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(self.child_record.read_text(), "finished")
        self.assertIn("unconfirmed", result.stderr)
        kinds = [event["request"].get("update", {}).get("kind") for event in self.events_read()]
        self.assertNotIn("release", kinds)
        self.assertEqual(kinds[-1], "wait")

    def test_renewal_uses_current_revision_without_releasing(self):
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['RENEW_INTERVAL']=.04; raise SystemExit(ns['main'](sys.argv[2:]))")
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "codex", "--",
                                 *self.child("import time; time.sleep(.16)")], env=self.env,
                                text=True, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = [event["request"] for event in self.events_read()]
        kinds = [request.get("update", {}).get("kind") for request in requests]
        self.assertIn("renew", kinds)
        self.assertEqual(kinds[-1], "wait")
        self.assertNotIn("release", kinds)
        self.assertTrue(all(request["revision"] >= 0 and request["request_id"] for request in requests))

    def test_claude_and_codex_wrappers_route_native_opt_in_preserving_arguments(self):
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        native_calls = self.root / "native-calls.jsonl"
        for host in ("claude", "codex"):
            native = fakebin / host
            native.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["NATIVE_CALLS"], "a") as stream:
 stream.write(json.dumps({"args":sys.argv[1:],"session":os.environ.get("CODING_AGENT_RESOURCE_SESSION_ID"),
                          "native_opt_in":os.environ.get("CODING_AGENT_NATIVE_RESOURCES")})+"\\n")
''')
            native.chmod(0o755)
        env = dict(self.env, PATH=f"{fakebin}:{os.environ['PATH']}", NATIVE_CALLS=str(native_calls),
                   CODEX_HOME=str(self.home / ".codex"))
        args = ["--model", "keep-this-model", "value with spaces"]
        legacy = dict(env)
        legacy.pop("CODING_AGENT_NATIVE_RESOURCES")
        for host in ("claude", "codex"):
            result = subprocess.run([str(ROOT / f"bin/ca-{host}"), *args], env=legacy,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.events.exists())
        for host in ("claude", "codex"):
            result = subprocess.run([str(ROOT / f"bin/ca-{host}"), *args], env=env,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in native_calls.read_text().splitlines()]
        self.assertEqual([call["args"] for call in calls], [args, args, args, args])
        self.assertEqual([call["session"] for call in calls], ["session-existing"] * 4)
        self.assertTrue(all(call["native_opt_in"] is None for call in calls))
        acquired = [event["request"] for event in self.events_read()
                    if event["request"]["op"] == "acquire"]
        self.assertEqual([request["host"] for request in acquired], ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
