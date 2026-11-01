#!/usr/bin/env python3
"""The producer's judgment of a daemon-endpoint stage record (SPE-145 PR-b4b): verify_endpoint_record recomputes every control result,
demands every control by name, checks the port proof, the teardown and the generated digests; and the profile digest the producer
computes is the one the QA side's own generator writes. No process starts."""
import copy
import hashlib
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_handoff_producer_test", SCRIPTS / "qa-handoff.py")
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
side = qa._SIDE
PORT = 25001


def good_observation(expect):
    return {"ALLOWED": "ALLOWED", "REFUSED": "REFUSED", "DENIED": "DENIED:1", "DENIED_EPERM": "DENIED:1", "DENIED_PROXY": "DENIED_PROXY"}[expect]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ident = types.SimpleNamespace(home=self.root / "qa", exchange=self.root / "x")
        self.outbox = self.ident.exchange / "outbox" / "att"
        self.endpoint = qa.endpoint_for(self.ident, "att", self.outbox, PORT, {"endpoint": {"daemon": "bin/d", "contract": "fornax-daemon/1"}})
        e = self.endpoint
        self.record = {
            "established": True, "problems": [],
            "controls": [{"control": n, "observed": good_observation(x)} for n, x in side.endpoint_control_expectations(e).items()],
            "codex_controls": [{"control": n, "observed": good_observation(x)} for n, _, x in side.ENDPOINT_CODEX_CONTROLS],
            "port_proof": {"listeners": ["127.0.0.1:%d" % PORT], "daemon_pid": 42},
            "teardown": {"clean": True, "descendants_left": [], "not_ours": False, "socket_removed_by_helper": ["http.sock"], "relay_stopped": True,
                         "daemon_stopped": True, "port_closed": True, "sockets_gone": True, "dir_removed": True},
            **side.endpoint_digests(e)}

    def verify(self, record=None):
        return qa.verify_endpoint_record(self.record if record is None else record, self.endpoint)


class VerifyTests(Base):
    def test_a_complete_record_passes(self):
        view, problems = self.verify()
        self.assertEqual(problems, [])
        self.assertEqual(view["derived_by"], "producer")
        self.assertTrue(all(c["ok"] for c in view["controls"] + view["codex_controls"]))

    def test_the_helper_unlinking_a_socket_is_reportable_not_fatal(self):
        self.record["teardown"]["socket_removed_by_helper"] = ["fornax.sock", "http.sock"]
        self.assertEqual(self.verify()[1], [])

    def test_a_missing_repeated_or_unknown_control_fails(self):
        for mutate in (lambda r: r["controls"].pop(), lambda r: r["codex_controls"].pop(0),
                       lambda r: r["controls"].append(dict(r["controls"][0])), lambda r: r["controls"].append({"control": "pos_extra", "observed": "ALLOWED"}),
                       lambda r: r["controls"].__setitem__(0, {"control": r["controls"][0]["control"], "observed": 7})):
            record = copy.deepcopy(self.record)
            mutate(record)
            self.assertTrue(self.verify(record)[1])

    def test_a_result_is_recomputed_not_taken_from_ok(self):
        record = copy.deepcopy(self.record)
        record["controls"][0].update(observed="DENIED:61", ok=True)
        record["codex_controls"][2].update(observed="ALLOWED", ok=True)
        problems = self.verify(record)[1]
        self.assertEqual(len(problems), 2, problems)

    def test_the_port_proof_must_show_loopback_and_one_listener(self):
        for listeners in (["*:%d" % PORT], ["127.0.0.1:%d" % PORT, "[::1]:%d" % PORT], [], ["127.0.0.1:%d" % (PORT + 1)]):
            record = copy.deepcopy(self.record)
            record["port_proof"]["listeners"] = listeners
            self.assertTrue(self.verify(record)[1], listeners)
        record = copy.deepcopy(self.record)
        record["port_proof"] = None
        self.assertTrue(self.verify(record)[1])

    def test_an_unclean_teardown_fails(self):
        for change in ({"clean": False}, {"descendants_left": [123]}, {"not_ours": True}, {"clean": "yes"}):
            record = copy.deepcopy(self.record)
            record["teardown"].update(change)
            self.assertTrue(self.verify(record)[1], change)
        record = copy.deepcopy(self.record)
        record["teardown"] = None
        self.assertTrue(self.verify(record)[1])

    def test_established_is_required_and_reported_problems_fail(self):
        for change in ({"established": False}, {"problems": ["x failed"]}, {"established": "true"}):
            self.assertTrue(self.verify(dict(self.record, **change))[1], change)

    def test_changed_generated_profiles_or_relay_fail(self):
        for key in ("daemon_profile_sha256", "relay_profile_sha256", "relay_source_sha256"):
            record = dict(self.record, **{key: "0" * 64})
            self.assertTrue(any(key in p for p in self.verify(record)[1]), key)
            record = {k: v for k, v in self.record.items() if k != key}
            self.assertTrue(any(key in p for p in self.verify(record)[1]), key)

    def test_hostile_shapes_fail_closed_instead_of_raising(self):
        for mutate in (lambda r: r["controls"].append({"control": ["x"], "observed": "ALLOWED"}),
                       lambda r: r["codex_controls"].append({"control": {"a": 1}, "observed": "ALLOWED"}),
                       lambda r: r.update(problems=5), lambda r: r.update(problems={"a": 1}), lambda r: r.update(problems="boom"),
                       lambda r: r["controls"].append(None), lambda r: r.update(controls="x")):
            record = copy.deepcopy(self.record)
            mutate(record)
            self.assertTrue(self.verify(record)[1])

    def test_the_port_proof_pid_must_be_a_real_pid(self):
        for pid in (0, 1, -4, True, False, 2.0, "7", None):
            record = copy.deepcopy(self.record)
            record["port_proof"]["daemon_pid"] = pid
            self.assertTrue(self.verify(record)[1], pid)

    def test_every_teardown_step_must_be_true_on_its_own(self):
        for step in ("relay_stopped", "daemon_stopped", "port_closed", "sockets_gone", "dir_removed"):
            for bad in (False, None, "yes"):
                record = copy.deepcopy(self.record)
                record["teardown"][step] = bad
                self.assertTrue(self.verify(record)[1], (step, bad))
            record = copy.deepcopy(self.record)
            del record["teardown"][step]
            self.assertTrue(self.verify(record)[1], step)

    def test_nothing_the_qa_side_sent_is_copied_into_the_producers_view(self):
        record = copy.deepcopy(self.record)
        record["port_proof"]["note"] = "x" * 5000
        record["teardown"]["note"] = {"deep": ["x" * 5000]}
        record["teardown"]["socket_removed_by_helper"] = ["y" * 5000, 7, None]
        view, problems = self.verify(record)
        self.assertEqual(problems, [])
        text = repr(view)
        self.assertLess(len(text), 20000)
        self.assertNotIn("x" * 200, text)
        self.assertNotIn("y" * 200, text)
        self.assertEqual(sorted(view["port_proof"]), ["daemon_pid", "listeners"])

    def test_a_non_dict_record_fails(self):
        for bad in (None, [], "x", 5):
            self.assertTrue(qa.verify_endpoint_record(bad, self.endpoint)[1])


class ProfileDigestTests(Base):
    def test_the_producers_digest_is_what_the_qa_side_writes(self):
        """Run the QA side's own prepare_codex_home as the QA account would (HOME and login at the QA home) and compare."""
        (self.ident.home / ".codex").mkdir(parents=True)
        (self.outbox / "payload").mkdir(parents=True)
        with mock.patch.object(side, "HOME", self.ident.home), mock.patch.object(side, "real_codex_home", lambda: self.ident.home / ".codex"):
            home = side.prepare_codex_home(self.outbox, [self.outbox / "payload"], home=self.root / "codexhome", endpoint=self.endpoint)
        written = hashlib.sha256((home / "config.toml").read_bytes()).hexdigest()
        self.assertEqual(qa.expected_endpoint_profile_sha(self.ident, self.outbox, self.endpoint), written)

    def test_a_different_port_or_attempt_changes_the_digest(self):
        base = qa.expected_endpoint_profile_sha(self.ident, self.outbox, self.endpoint)
        other = qa.endpoint_for(self.ident, "att2", self.outbox, PORT, {"endpoint": {"daemon": "bin/d", "contract": "fornax-daemon/1"}})
        self.assertNotEqual(base, qa.expected_endpoint_profile_sha(self.ident, self.outbox, other))


class NetworkModeTests(Base):
    def payload_dir(self, endpoint):
        import json
        import os
        directory = self.root / "payload-src"
        (directory / "bin").mkdir(parents=True)
        files = {"bin/d": b"#!/bin/sh\nexit 0\n", "harness/t.txt": b"t\n"}
        entries = []
        for path, data in sorted(files.items()):
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if path == "bin/d" else 0o644)
            entries.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "kind": "exec" if path == "bin/d" else "data"})
        manifest = {"schema": "cae-qa-payload/2", "files": entries, "selftests": [], "scope": "offline library-level checks",
                    "producer_run": ["harness/t.txt"], "endpoint": endpoint}
        (directory / "MANIFEST.json").write_text(json.dumps(manifest))
        os.chmod(directory / "MANIFEST.json", 0o644)
        return directory

    def test_an_endpoint_payload_is_accepted_only_for_a_daemon_endpoint_prepare(self):
        named = self.payload_dir({"daemon": "bin/d", "contract": "fornax-daemon/1"})
        with self.assertRaises(SystemExit) as caught:
            qa.read_payload_source(named)
        self.assertIn("not started with one", str(caught.exception))
        source = qa.read_payload_source(named, "daemon-endpoint")
        qa.os.close(source["fd"])

    def test_a_daemon_endpoint_prepare_needs_a_payload_that_names_a_daemon(self):
        import shutil
        shutil.rmtree(self.root / "payload-src", ignore_errors=True)
        none = self.payload_dir(None)
        with self.assertRaises(SystemExit) as caught:
            qa.read_payload_source(none, "daemon-endpoint")
        self.assertIn("names none", str(caught.exception))

    def test_pick_port_stays_in_the_endpoint_range(self):
        for _ in range(5):
            port = qa.pick_port()
            self.assertTrue(side.PORT_RANGE[0] <= port <= side.PORT_RANGE[1])


if __name__ == "__main__":
    unittest.main()
