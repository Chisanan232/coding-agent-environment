#!/usr/bin/env python3
"""session-notify `zeroclaw-spool` backend (SPE-176) on scratch state only: a fresh temp HOME, state, config and monitor directories.
Nothing here touches ~/.local/share/engineering-supervisor, ~/.claude or the network, and nothing is sent anywhere."""
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "docs/schemas/zeroclaw-incident-spool-v1.schema.json"


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sn = load("sn_spool", "scripts/session-notify.py")
am = load("am_spool", "scripts/admission-monitor.py")
cw = load("cw_spool", "scripts/claude-auth-watch.py")
SIG = "0123456789abcdef"
KEY_FIELDS = {"schema", "key", "project", "incident_ref", "kind", "transition", "severity", "progress", "action", "ts"}


def check_schema(doc, schema=None):
    """Independent, minimal JSON-Schema check of the published schema file (const, enum, pattern, required, additionalProperties)."""
    schema = schema or json.loads(SCHEMA_FILE.read_text())
    if not isinstance(doc, dict) or set(doc) != set(schema["required"]) or set(doc) - set(schema["properties"]):
        return False
    for key, rule in schema["properties"].items():
        value = doc[key]
        if "const" in rule and value != rule["const"] or "enum" in rule and value not in rule["enum"]:
            return False
        if "pattern" in rule and not (isinstance(value, str) and re.search(rule["pattern"], value)):
            return False
    return True


class Spool(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="zc-spool-")))   # realpath: macOS /var is a symlink and the spool refuses symlinks
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.sdir, self.spool = self.tmp / "state", self.tmp / "spool"
        self.spool.mkdir(mode=0o700)
        os.chmod(self.spool, 0o700)
        self.t = 2_000_000.0
        self.env = {"HOME": str(self.home), "SESSION_NOTIFY_STATE_DIR": str(self.sdir), "SESSION_NOTIFY_CONFIG": str(self.tmp / "config.json"),
                    "ADMISSION_MONITOR_DIR": str(self.tmp / "am"), "ADMISSION_WRAPPERS_DIR": str(self.tmp / "wrappers"),
                    "CLAUDE_AUTH_WATCH_DIR": str(self.tmp / "cw"), "PATH": "/usr/bin:/bin"}
        patcher = mock.patch.dict(os.environ, self.env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, **spool):
        c = json.loads(json.dumps(sn.DEFAULT_CONFIG))
        c["backends"]["zeroclaw-spool"].update({"enabled": True, "spool_dir": str(self.spool), **spool})
        return c

    def engine(self, cfg=None, transports=None):
        return sn.Engine(self.sdir, cfg or self.cfg(), transports=transports, clock=lambda: self.t, sleep=lambda s: None)

    def emit(self, engine=None, cause="unclaimed-handoff", recovery="none", action="claim-or-reenqueue-handoff", sig=SIG, **kw):
        engine = engine or self.engine()
        result = engine.emit_resource({"cause": cause, "recovery": recovery, "owner_action": action, "signature": sig}, **kw)
        return result, engine

    def files(self):
        return sorted(p for p in self.spool.iterdir())

    def docs(self):
        return [json.loads(p.read_text()) for p in self.files()]

    def record(self, sid="admission-monitor"):
        rec, problem = sn.load_record(self.sdir, sid)
        self.assertIsNone(problem)
        return rec

    def pending_codes(self, sid="admission-monitor"):
        return [(b["status"], b["code"], b["attempts"]) for e in self.record(sid)["pending"] for b in e["backends"].values() if b["status"] != "skipped"]


class SchemaExactness(Spool):
    def test_one_closed_schema_file_per_transition(self):
        result, engine = self.emit()
        self.assertEqual(result, "queued")
        self.assertEqual(engine.last_delivery, "sent")
        (path,) = self.files()
        doc = json.loads(path.read_text())
        self.assertEqual(set(doc), KEY_FIELDS)
        self.assertTrue(check_schema(doc))
        self.assertEqual(path.name, doc["key"] + ".json")
        self.assertEqual(doc["key"], hashlib.sha256(f"inc:{SIG}|opened".encode()).hexdigest()[:32])
        self.assertEqual((doc["schema"], doc["project"], doc["incident_ref"], doc["kind"], doc["transition"], doc["severity"]),
                         (1, "coding-agent-environment", f"inc:{SIG}", "unclaimed-handoff", "opened", "action"))
        self.assertEqual(doc["progress"], "hand-off grant unclaimed; recovery: none tried")
        self.assertEqual(doc["action"], "claim or re-enqueue hand-off")
        self.assertRegex(doc["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_every_incident_kind_maps_to_its_transition_and_severity(self):
        cases = [("unclaimed-handoff", "none", "claim-or-reenqueue-handoff", "unclaimed-handoff", "opened", "action"),
                 ("unclaimed-handoff", "lapsed", "claim-or-reenqueue-handoff", "unclaimed-handoff", "escalated", "action"),
                 ("queue-wait-excessive", "wait-expiry-near", "check-queued-wait", "queue-wait-excessive", "escalated", "action"),
                 ("workload-over-deadline", "none", "check-over-deadline-workload", "workload-over-deadline", "opened", "action"),
                 ("auth-required", "no-auto-recovery", "sign-in-claude", "auth-interrupted", "opened", "action"),
                 ("remote-control-disconnect", "remote-control-only", "reconnect-remote-control", "auth-interrupted", "opened", "action"),
                 ("signin-recovered", "sessions-resumed", "verify-resumed-work", "recovered", "recovered", "info")]
        for n, (cause, recovery, action, kind, transition, severity) in enumerate(cases):
            with self.subTest(cause=cause, recovery=recovery):
                self.assertEqual(self.emit(cause=cause, recovery=recovery, action=action, sig=f"{n:016x}")[0], "queued")
        docs = {(d["kind"], d["transition"], d["severity"]) for d in self.docs()}
        self.assertEqual(docs, {(k, t, s) for _, _, _, k, t, s in cases})
        self.assertTrue(all(check_schema(d) for d in self.docs()))

    def test_test_notice_is_a_notify_test_event(self):
        engine = self.engine()
        self.assertEqual(engine.test_notice(), "queued")
        (doc,) = self.docs()
        self.assertTrue(check_schema(doc))
        self.assertEqual((doc["kind"], doc["transition"], doc["severity"], doc["progress"], doc["action"]),
                         ("notify-test", "test", "info", "delivery test notice", "no action needed"))
        self.assertEqual(engine.test_notice(), "deduped")
        self.assertEqual(len(self.files()), 1)

    def test_configured_project_name_is_used_and_checked(self):
        self.emit(engine=self.engine(self.cfg(project="eltanin")))
        self.assertEqual(self.docs()[0]["project"], "eltanin")
        path = self.tmp / "bad.json"
        path.write_text(json.dumps({"backends": {"zeroclaw-spool": {"enabled": True, "project": "Bad Name/../x"}}}))
        cfg, problem = sn.load_config(path)
        self.assertEqual(cfg["backends"]["zeroclaw-spool"]["project"], "coding-agent-environment")
        self.assertIn("backends.zeroclaw-spool.project", problem)

    def test_published_schema_matches_the_script_constants(self):
        schema = json.loads(SCHEMA_FILE.read_text())["properties"]
        self.assertEqual(tuple(schema["kind"]["enum"]), sn.SPOOL_KINDS)
        self.assertEqual(tuple(schema["transition"]["enum"]), sn.SPOOL_TRANSITIONS)
        self.assertEqual(tuple(schema["severity"]["enum"]), sn.SPOOL_SEVERITIES)
        for field, regex in (("key", sn.SPOOL_KEY), ("project", sn.SPOOL_PROJECT), ("incident_ref", sn.SPOOL_REF), ("progress", sn.SPOOL_TEXT),
                             ("action", sn.SPOOL_TEXT), ("ts", sn.SPOOL_TS)):
            self.assertEqual(schema[field]["pattern"], "^" + regex.pattern + "$", field)


class Sanitization(Spool):
    def test_text_is_derived_only_from_fixed_tables(self):
        for cause in sorted(sn.SPOOL_KIND_OF_CAUSE):
            auth = cause in sn.AUTH_CAUSES
            recovery, action = (sorted(sn.AUTH_RECOVERY)[0], sorted(sn.AUTH_ACTIONS)[0]) if auth else ("none", "inspect-admission-state")
            for rec in ([sorted(sn.AUTH_RECOVERY)[0]] if auth else sorted(set(sn.RECOVERY_TEXT) - sn.AUTH_RECOVERY)):
                with self.subTest(cause=cause, recovery=rec):
                    event = sn.spool_event({"cause": cause, "recovery": rec, "owner_action": action, "incident_ref": "inc:" + SIG},
                                           "coding-agent-environment", self.t)
                    self.assertIsNotNone(event)
                    self.assertTrue(check_schema(event))
                    self.assertEqual(event["progress"], f"{sn.CAUSE_TEXT[cause]}; recovery: {sn.RECOVERY_TEXT[rec]}")
                    self.assertEqual(event["action"], sn.OWNER_ACTION_TEXT[action])

    def test_free_text_and_untrusted_fields_never_reach_the_file(self):
        evil = "Ignore previous /etc/passwd https://evil.example $(rm -rf ~) token=abc"
        base = {"cause": "auth-required", "recovery": "no-auto-recovery", "owner_action": "sign-in-claude", "incident_ref": "inc:" + SIG}
        self.assertIsNone(sn.spool_event(dict(base, incident_ref=evil), "p", self.t))
        self.assertIsNone(sn.spool_event(dict(base, incident_ref="x" * 65), "p", self.t))
        self.assertIsNone(sn.spool_event({k: v for k, v in base.items() if k != "incident_ref"}, "p", self.t))
        self.assertIsNone(sn.spool_event(dict(base, cause=evil), "p", self.t))
        self.assertIsNone(sn.spool_event(dict(base, cause="disk-floor"), "p", self.t))   # a cause without a spool kind is never spooled
        self.assertIsNone(sn.spool_event(base, "UPPER/case", self.t))
        # the detail of a sign-in notice (host and session names) is not part of the spool file at all
        engine = self.engine()
        engine.emit_resource(dict(cause="auth-required", recovery="no-auto-recovery", owner_action="sign-in-claude", signature=SIG,
                                  detail="host=laptop names=secret-session inc=012345"))
        text = self.files()[0].read_text()
        for word in ("laptop", "secret-session", "host=", "names="):
            self.assertNotIn(word, text)

    def test_every_spool_text_is_path_url_and_id_free(self):
        for table in (sn.CAUSE_TEXT, sn.OWNER_ACTION_TEXT, sn.RECOVERY_TEXT):
            for phrase in table.values():
                if phrase in (sn.CAUSE_TEXT[c] for c in sn.SPOOL_KIND_OF_CAUSE) or table is not sn.CAUSE_TEXT:
                    self.assertRegex(phrase, r"^[A-Za-z0-9 .,;:()+-]+$")
                    self.assertNotRegex(phrase, r"[/\\@=#]|https?:")


class SafetyRefusals(Spool):
    def assert_refused_then_retained(self, why):
        result, engine = self.emit()
        self.assertEqual(result, "queued", why)
        self.assertEqual(engine.last_delivery, "pending", why)
        self.assertEqual(self.files(), [], why)
        self.assertEqual(self.pending_codes(), [("failed", "spool-unavailable", 0)], why)

    def test_mode_other_than_0700_is_refused(self):
        for mode in (0o755, 0o750, 0o770, 0o500):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.spool, mode)
                shutil.rmtree(self.sdir, True)
                self.assert_refused_then_retained(oct(mode))
                self.assertEqual(list(self.spool.iterdir()), [])
        os.chmod(self.spool, 0o700)

    def test_symlinked_directory_and_symlinked_component_are_refused(self):
        link = self.tmp / "link"
        link.symlink_to(self.spool)
        self.assertEqual(self.emit(engine=self.engine(self.cfg(spool_dir=str(link))))[1].last_delivery, "pending")
        self.assertEqual(self.files(), [])
        shutil.rmtree(self.sdir, True)
        shutil.rmtree(self.sdir, True)
        self.assertEqual(self.emit(engine=self.engine(self.cfg(spool_dir=str(link) + "/")))[1].last_delivery, "pending")
        self.assertEqual(self.emit(engine=self.engine(self.cfg(spool_dir=str(self.tmp / "spool" / ".." / "spool"))))[1].last_delivery, "pending")   # not normalised
        parent_link = self.tmp / "plink"
        parent_link.symlink_to(self.tmp)
        shutil.rmtree(self.sdir, True)
        self.assertEqual(self.emit(engine=self.engine(self.cfg(spool_dir=str(parent_link / "spool"))))[1].last_delivery, "pending")
        self.assertEqual(self.files(), [])

    def test_missing_directory_is_never_created(self):
        gone = self.tmp / "absent" / "spool"
        result, engine = self.emit(engine=self.engine(self.cfg(spool_dir=str(gone))))
        self.assertEqual((result, engine.last_delivery), ("queued", "pending"))
        self.assertFalse(gone.exists() or gone.parent.exists())

    def test_regular_file_in_place_of_directory_is_refused(self):
        notdir = self.tmp / "notdir"
        notdir.write_text("x")
        self.assertEqual(self.emit(engine=self.engine(self.cfg(spool_dir=str(notdir))))[1].last_delivery, "pending")
        self.assertEqual(notdir.read_text(), "x")

    def test_relative_spool_dir_is_rejected_at_config_load(self):
        path = self.tmp / "rel.json"
        path.write_text(json.dumps({"backends": {"zeroclaw-spool": {"enabled": True, "spool_dir": "relative/spool"}}}))
        cfg, problem = sn.load_config(path)
        self.assertFalse(cfg["backends"]["zeroclaw-spool"]["enabled"])
        self.assertEqual(cfg["backends"]["zeroclaw-spool"]["spool_dir"], "")
        self.assertIn("backends.zeroclaw-spool.spool_dir", problem)

    def test_file_is_0600_and_no_temporary_file_survives(self):
        self.emit()
        (path,) = self.files()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertFalse([p for p in self.spool.iterdir() if p.name.startswith(".tmp-")])
        self.assertRegex(path.name, r"^[0-9a-f]{32}\.json$")

    def test_failed_write_leaves_no_partial_or_temporary_file(self):
        real_rename = os.rename

        def boom(*a, **k):
            raise OSError(28, "no space")
        with mock.patch.object(os, "rename", boom):
            result, engine = self.emit()
        self.assertEqual(engine.last_delivery, "pending")
        self.assertEqual(list(self.spool.iterdir()), [])
        self.assertIs(os.rename, real_rename)
        self.assertEqual(self.pending_codes(), [("failed", "spool-unavailable", 0)])

    def test_state_dir_ledger_is_not_taken_for_a_session_record(self):
        self.emit()
        self.assertTrue((self.sdir / sn.SPOOL_LEDGER).is_file())
        out = self.engine().sweep(now=self.t + 1)
        self.assertEqual(out["untrusted"], 0)


class Dedup(Spool):
    def test_same_transition_twice_is_one_file(self):
        engine = self.engine()
        self.assertEqual(self.emit(engine=engine)[0], "queued")
        self.assertEqual(self.emit(engine=engine)[0], "deduped")
        self.assertEqual(self.emit(engine=self.engine())[0], "deduped")   # a second process
        self.assertEqual(len(self.files()), 1)

    def test_a_present_file_is_never_overwritten(self):
        self.emit()
        (path,) = self.files()
        marker = path.read_text() + " "
        path.write_text(marker)
        os.chmod(path, 0o600)
        shutil.rmtree(self.sdir)          # the local state is gone, the spool file is still there
        self.assertEqual(self.emit()[0], "queued")
        self.assertEqual(path.read_text(), marker)
        self.assertEqual(len(self.files()), 1)

    def test_recurrence_inside_the_dedup_window_after_the_consumer_took_the_file_is_not_respooled(self):
        self.emit()
        for p in self.files():
            p.unlink()                                    # the supervisor consumed it
        engine = self.engine()
        engine.emit_resource({}, clear=True)              # session-notify's own dedup forgets the notice (a clear bumps the cycle)
        self.assertEqual(self.emit(engine=engine)[0], "queued")
        self.assertEqual(self.files(), [])                # the ledger still knows this transition was written
        self.assertEqual(engine.last_delivery, "sent")

    def test_distinct_transitions_and_incidents_are_distinct_files(self):
        self.emit(recovery="none", sig="1" * 16)
        self.emit(recovery="lapsed", sig="2" * 16)        # a later phase of the monitor's incident carries its own signature
        self.emit(sig="f" * 16)                           # another incident
        self.assertEqual(len(self.files()), 3)
        self.assertEqual(sorted(d["transition"] for d in self.docs()), ["escalated", "opened", "opened"])


class DelayedDelivery(Spool):
    def test_missing_directory_keeps_the_event_then_delivers_once_after_it_appears(self):
        absent = self.tmp / "later"
        engine = self.engine(self.cfg(spool_dir=str(absent)))
        for _ in range(12):                               # far more than max_attempts: an unavailable spool never exhausts the retries
            self.emit(engine=engine)
            self.t += 31
        self.assertEqual(self.pending_codes(), [("failed", "spool-unavailable", 0)])
        self.assertEqual(self.record()["dropped"], 0)
        absent.mkdir(mode=0o700)
        os.chmod(absent, 0o700)
        self.assertEqual(self.engine(self.cfg(spool_dir=str(absent))).sweep(now=self.t)["retried"], 1)
        self.assertEqual([p.name for p in absent.iterdir()], [sn.hashlib.sha256(f"inc:{SIG}|opened".encode()).hexdigest()[:32] + ".json"])
        self.assertEqual(self.record()["pending"], [])
        self.t += 3600
        self.engine(self.cfg(spool_dir=str(absent))).sweep(now=self.t)
        self.assertEqual(self.emit(engine=self.engine(self.cfg(spool_dir=str(absent))))[0], "deduped")
        self.assertEqual(len(list(absent.iterdir())), 1)       # delivered once, no duplicate

    def test_a_later_tick_of_the_emitter_delivers_it_too(self):
        os.chmod(self.spool, 0o755)
        engine = self.engine()
        self.assertEqual(self.emit(engine=engine)[1].last_delivery, "pending")
        os.chmod(self.spool, 0o700)
        self.t += 31
        result, engine = self.emit(engine=self.engine())
        self.assertEqual((result, engine.last_delivery), ("deduped", "sent"))
        self.assertEqual(len(self.files()), 1)

    def test_status_shows_the_undelivered_event(self):
        os.chmod(self.spool, 0o755)
        self.emit()
        out = subprocess.run([sys.executable, str(ROOT / "scripts/session-notify.py"), "status", "--json"], capture_output=True, text=True,
                             env=dict(os.environ), stdin=subprocess.DEVNULL, timeout=30).stdout
        row = next(r for r in json.loads(out) if r["session"] == "admission-mo")
        self.assertEqual(row["pending_deliveries"], 1)


class Routing(Spool):
    def both(self, tele_kinds=None, spool_kinds=None):
        c = self.cfg()
        c["backends"]["telegram"].update(enabled=True, approved_destination="api.telegram.org", chat_id_env="X", token_env="Y")
        if tele_kinds is not None:
            c["backends"]["telegram"]["kinds"] = tele_kinds
        if spool_kinds is not None:
            c["backends"]["zeroclaw-spool"]["kinds"] = spool_kinds
        return c

    def write_cfg(self, cfg):
        path = pathlib.Path(self.env["SESSION_NOTIFY_CONFIG"])
        path.write_text(json.dumps(cfg))
        os.chmod(path, 0o600)
        return path

    def test_both_enabled_for_the_same_kind_is_rejected_at_config_load(self):
        cfg, problem = sn.load_config(self.write_cfg(self.both()))
        self.assertTrue(problem.startswith("conflict:"))
        self.assertIn("telegram+zeroclaw-spool both enabled for kinds", problem)
        self.assertIn("auth-interrupted", problem)
        self.assertFalse(cfg["backends"]["telegram"]["enabled"] or cfg["backends"]["zeroclaw-spool"]["enabled"])

    def test_cli_refuses_with_a_clear_error_and_emits_nothing(self):
        self.write_cfg(self.both())
        done = subprocess.run([sys.executable, str(ROOT / "scripts/session-notify.py"), "emit-resource", "--cause", "unclaimed-handoff", "--recovery", "none",
                               "--owner-action", "claim-or-reenqueue-handoff", "--signature", SIG], capture_output=True, text=True, env=dict(os.environ),
                              stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(done.returncode, 2)
        self.assertIn("config rejected: telegram+zeroclaw-spool both enabled", done.stderr)
        self.assertEqual(self.files(), [])
        tn = subprocess.run([sys.executable, str(ROOT / "scripts/session-notify.py"), "test-notice"], capture_output=True, text=True, env=dict(os.environ),
                            stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(tn.returncode, 2)

    def test_disjoint_kinds_are_accepted_and_each_notice_goes_to_one_destination(self):
        sent = []
        transports = {"telegram": lambda payload, b: sent.append(payload["body"]), sn.SPOOL_BACKEND: sn.send_zeroclaw_spool}
        cfg, problem = sn.load_config(self.write_cfg(self.both(tele_kinds=["session"])))
        self.assertIsNone(problem)
        engine = self.engine(cfg, transports=transports)
        self.emit(engine=engine)                                                         # spool kind -> spool only
        self.emit(engine=engine, cause="disk-floor", recovery="none", action="reclaim-disk", sig="a" * 16)   # no spool kind -> telegram only
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(sent, ["disk below floor | recovery: none tried | action: reclaim disk"])

    def test_spool_never_carries_a_notice_without_a_spool_kind(self):
        result, engine = self.emit(cause="disk-floor", recovery="none", action="reclaim-disk")
        self.assertIn(result, ("queued", "deduped", "no-backend"))
        self.assertEqual(self.files(), [])
        self.assertEqual(self.pending_codes(), [])

    def test_a_kind_left_out_of_kinds_is_not_spooled(self):
        engine = self.engine(self.cfg(kinds=["auth-interrupted"]))
        self.emit(engine=engine)
        self.assertEqual(self.files(), [])
        self.emit(engine=engine, cause="auth-required", recovery="no-auto-recovery", action="sign-in-claude", sig="b" * 16)
        self.assertEqual(len(self.files()), 1)


class SignedOut(Spool):
    """The reserved sessions (admission-monitor, claude-auth-watch, notify-test) run with no Claude Code sign-in, no credential and no ~/.claude."""

    def clean_env(self):
        env = {k: v for k, v in self.env.items()}
        env["PATH"] = "/usr/bin:/bin"
        self.assertFalse(any(k.startswith(("ANTHROPIC", "CLAUDE_CODE")) for k in env))
        return env

    def write_config(self):
        pathlib.Path(self.env["SESSION_NOTIFY_CONFIG"]).write_text(json.dumps(self.cfg()))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/session-notify.py"), *args], capture_output=True, text=True, env=self.clean_env(),
                              stdin=subprocess.DEVNULL, timeout=30)

    def test_emit_resource_and_test_notice_work_with_no_claude_state(self):
        self.write_config()
        self.assertFalse((self.home / ".claude").exists())
        done = self.run_cli("emit-resource", "--cause", "auth-required", "--recovery", "no-auto-recovery", "--owner-action", "sign-in-claude",
                            "--signature", SIG, "--detail", "host=h names=a inc=012345", "--strict")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "emit-resource: queued delivery=sent"), done.stderr)
        done = self.run_cli("test-notice", "--strict")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "test-notice: queued delivery=sent"), done.stderr)
        done = self.run_cli("emit-resource", "--cause", "signin-recovered", "--recovery", "sessions-resumed", "--owner-action", "verify-resumed-work",
                            "--signature", "c" * 16)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual({d["kind"] for d in self.docs()}, {"auth-interrupted", "notify-test", "recovered"})
        self.assertFalse((self.home / ".claude").exists())

    def test_default_spool_directory_is_under_home(self):
        default = self.home / ".local/share/engineering-supervisor/state/incident-spool"
        default.mkdir(parents=True, mode=0o700)
        os.chmod(default, 0o700)
        cfg = self.cfg()
        cfg["backends"]["zeroclaw-spool"]["spool_dir"] = ""
        self.assertEqual(self.emit(engine=self.engine(cfg))[1].last_delivery, "sent")
        self.assertEqual(len(list(default.iterdir())), 1)
        self.assertEqual(self.files(), [])

    def test_admission_monitor_incident_path_spools(self):
        self.write_config()
        rec = am.new_incident("job-1", "escalated", self.t, self.t + 600, self.t, self.t + 300, "grant", "none")
        alert = am.incident_alert("job-1@2026-11-20T00:00:00Z", rec, self.t)
        outcome = am.session_notify_incident(alert, runner=lambda argv, **kw: subprocess.run(argv, env=self.clean_env(), **kw))
        self.assertEqual(outcome, {"result": "queued", "delivery": "sent"})
        (doc,) = self.docs()
        self.assertEqual((doc["kind"], doc["incident_ref"]), ("unclaimed-handoff", "inc:" + alert["signature"]))
        for clock, kind in (("wait", "queue-wait-excessive"), ("run", "workload-over-deadline")):
            rec = am.new_incident("job-2", "escalated", self.t, self.t + 600, self.t, self.t + 300, clock, "none")
            alert = am.incident_alert("job-2@" + clock, rec, self.t)
            self.assertEqual(am.session_notify_incident(alert, runner=lambda argv, **kw: subprocess.run(argv, env=self.clean_env(), **kw))["delivery"], "sent")
        self.assertEqual({d["kind"] for d in self.docs()}, {"unclaimed-handoff", "queue-wait-excessive", "workload-over-deadline"})

    def test_claude_auth_watch_notifier_spools_alert_and_recovery(self):
        self.write_config()
        runner = lambda argv, **kw: subprocess.run(argv, env=self.clean_env(), **kw)   # noqa: E731
        notifier = cw.Notifier(runner=runner)
        sig = "d" * 16
        self.assertEqual(notifier.emit("auth-required", "no-auto-recovery", "sign-in-claude", sig, cw.detail_text("h", ["a"], 0, 0, sig)),
                         {"result": "queued", "delivery": "sent"})
        self.assertEqual(notifier.emit("signin-recovered", "sessions-resumed", "verify-resumed-work", sig, cw.detail_text("h", ["a"], 0, 0, sig)),
                         {"result": "queued", "delivery": "sent"})
        kinds = sorted((d["kind"], d["transition"]) for d in self.docs())
        self.assertEqual(kinds, [("auth-interrupted", "opened"), ("recovered", "recovered")])
        self.assertEqual({d["incident_ref"] for d in self.docs()}, {"inc:" + sig})


if __name__ == "__main__":
    unittest.main()
