#!/usr/bin/env python3
"""SPE-188: scripts/scheduler-observe.py prints one line and reports only material change.

Every test uses private temporary directories for the queue receipts, the monitor status, the supervisor state and the observer's own
fingerprint. The owner's real queue, monitor and supervisor state are never read or written, and no network or `gh` call is made
(the pull-request provider is injected)."""
import contextlib
import io
import json
import os
from pathlib import Path
from importlib.util import module_from_spec, spec_from_file_location
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = spec_from_file_location("scheduler_observe", ROOT / "scripts/scheduler-observe.py")
so = module_from_spec(_spec)
_spec.loader.exec_module(so)


class Observe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.queue, self.sup, self.state_dir = root / "queue", root / "sup", root / "obs"
        self.queue.mkdir()
        self.sup.mkdir()
        self.monitor = root / "monitor.json"
        self.receipts = []
        self.prs = [229, 227]
        self.pr_error = False
        self.write_all(telegram="running", incidents=[], handoff=[], mode_actions=[])

    # -- fixtures ------------------------------------------------------------------------------------
    def event(self, event, ident, **extra):
        self.receipts.append(dict(event=event, id=ident, ts="2026-11-24T00:00:00Z", **extra))
        (self.queue / "receipts.jsonl").write_text("\n".join(json.dumps(r) for r in self.receipts) + "\n")

    def write_all(self, telegram="running", incidents=(), handoff=(), mode_actions=(), provider=False, poll=1):
        (self.queue / "receipts.jsonl").write_text("\n".join(json.dumps(r) for r in self.receipts) + ("\n" if self.receipts else ""))
        self.monitor.write_text(json.dumps({"actions": list(mode_actions), "handoff_incidents": list(handoff)}))
        (self.sup / "telegram-health.json").write_text(json.dumps({"status": telegram, "completed_poll_batches": poll}))
        (self.sup / "incident-status.json").write_text(json.dumps(
            {"open_incidents": [{"incident_ref": r, "message_id": "9"} for r in incidents]}))
        marker = self.sup / "provider-maintenance.json"
        if provider:
            marker.write_text("{}")
        elif marker.exists():
            marker.unlink()

    def run_once(self):
        argv = ["--state", str(self.state_dir / "last.json"), "--queue-dir", str(self.queue), "--monitor-status", str(self.monitor),
                "--supervisor-dir", str(self.sup)]

        def provider(repo):
            if self.pr_error:
                raise RuntimeError("gh unavailable")
            return list(self.prs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = so.main(argv, environ={}, pr_provider=provider)
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    # -- behaviour -----------------------------------------------------------------------------------
    def test_the_first_run_records_a_baseline_quietly_and_the_second_is_no_change(self):
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_volatile_counter_never_changes_the_fingerprint(self):
        self.run_once()
        self.write_all(poll=999999)
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_new_incident_is_reported_once_then_quiet(self):
        self.run_once()
        self.write_all(incidents=["inc:aaaa"])
        self.assertEqual(self.run_once(), "CHANGE: incident opened inc:aaaa")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_recovery_is_reported_once(self):
        self.write_all(incidents=["inc:aaaa"])
        self.run_once()
        self.write_all(incidents=[])
        self.assertEqual(self.run_once(), "CHANGE: incident resolved inc:aaaa")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_the_supervisor_leaving_running_and_coming_back_is_reported_both_times(self):
        self.run_once()
        self.write_all(telegram="degraded")
        self.assertEqual(self.run_once(), "CHANGE: supervisor telegram running->degraded")
        self.assertEqual(self.run_once(), "NO_CHANGE")
        self.write_all(telegram="running")
        self.assertEqual(self.run_once(), "CHANGE: supervisor telegram degraded->running")

    def test_routine_queue_churn_is_quiet_and_then_settles(self):
        self.run_once()
        self.event("granted", "q1")
        self.event("claimed", "q1")
        self.assertEqual(self.run_once(), "QUIET")
        self.event("released", "q1")
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_live_backfill_claim_is_reported_but_an_unclaimed_or_revoked_one_is_quiet(self):
        self.run_once()
        self.event("backfill", "q2", reason="head-floor-only")
        self.event("backfill-revoked", "q2", kind="head-unprotected")
        self.assertEqual(self.run_once(), "QUIET")
        self.event("backfill", "q3", reason="qualified")
        self.event("claimed", "q3")
        line = self.run_once()
        self.assertTrue(line.startswith("CHANGE: live backfill claimed q3"), line)
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_the_first_cap_refusal_is_reported_and_later_ones_are_quiet(self):
        self.run_once()
        self.event("backfill-decision", "q4", reason="cumulative-loss-cap", outcome="refused")
        self.assertEqual(self.run_once(), "CHANGE: first cumulative-loss-cap refusal")
        self.event("backfill-decision", "q5", reason="cumulative-loss-cap", outcome="refused")
        self.assertIn(self.run_once(), ("NO_CHANGE", "QUIET"))

    def test_a_monitor_mode_change_and_a_new_open_pr_are_reported(self):
        self.run_once()
        self.write_all(mode_actions=[{"mode": "act", "executed": True}])
        self.assertEqual(self.run_once(), "CHANGE: monitor mode observe->act")
        self.prs = [229, 227, 321]
        self.assertEqual(self.run_once(), "CHANGE: new open PR #321")
        self.prs = [229, 227]
        self.assertEqual(self.run_once(), "QUIET")

    def test_monitor_handoff_incident_changes_alone_are_quiet(self):
        self.run_once()
        self.write_all(handoff=[{"id": "q9", "resolved_at": None}])
        self.assertEqual(self.run_once(), "QUIET")
        self.write_all(handoff=[{"id": "q9", "resolved_at": 5}])
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_an_unreadable_source_keeps_its_value_and_is_reported_only_when_it_repeats(self):
        self.run_once()
        (self.sup / "telegram-health.json").write_text("{not json")
        self.assertEqual(self.run_once(), "QUIET")                                   # first failure: unconfirmed
        self.assertEqual(self.run_once(), "CHANGE: source supervisor unreadable (JSONDecodeError) twice running")
        self.assertEqual(self.run_once(), "NO_CHANGE")                               # the same error again: already reported
        self.write_all(telegram="running")
        self.assertEqual(self.run_once(), "CHANGE: source supervisor readable again")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_failed_read_does_not_hide_a_change_that_happened_meanwhile(self):
        self.run_once()
        (self.sup / "telegram-health.json").write_text("{not json")
        self.assertEqual(self.run_once(), "QUIET")
        self.write_all(telegram="degraded")
        self.assertEqual(self.run_once(), "CHANGE: supervisor telegram running->degraded")

    def test_a_single_transient_failure_costs_nothing_afterwards(self):
        self.run_once()
        self.pr_error = True
        self.assertEqual(self.run_once(), "QUIET")
        self.pr_error = False
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_corrupt_state_file_is_rebuilt_without_a_report(self):
        self.run_once()
        (self.state_dir / "last.json").write_text("garbage")
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")
        data = json.loads((self.state_dir / "last.json").read_text())
        self.assertEqual(data["schema"], 1)

    def test_the_state_file_is_private_and_the_only_thing_written(self):
        before = {p: p.stat().st_mtime_ns for p in Path(self.tmp.name).rglob("*") if p.is_file()}
        self.run_once()
        state = self.state_dir / "last.json"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        changed = {p for p in Path(self.tmp.name).rglob("*") if p.is_file() and before.get(p) != p.stat().st_mtime_ns}
        self.assertEqual(changed, {state})
        self.assertEqual([p.name for p in self.state_dir.iterdir()], ["last.json"])

    def test_output_never_carries_a_path_or_secretlike_text(self):
        self.write_all(incidents=["inc:aaaa"])
        self.run_once()
        self.write_all(incidents=["inc:aaaa", "inc:bbbb"], telegram="running")
        line = self.run_once()
        self.assertNotIn("/", line)
        self.assertNotIn(self.tmp.name, line)
        self.assertNotIn("message_id", line)
        self.assertEqual(so.clean("a/b\\c \x1b[31m$(x)`y`"), "abc 31m(x)y")

    def test_a_missing_queue_directory_never_crashes_the_caller(self):
        self.run_once()
        for p in self.queue.iterdir():
            p.unlink()
        self.queue.rmdir()
        self.assertIn(self.run_once(), ("QUIET", "NO_CHANGE"))


if __name__ == "__main__":
    unittest.main()
