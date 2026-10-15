#!/usr/bin/env python3
"""Transactionally install the managed-generated Glomeris resource policy."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "codex/resource-policy.json"
STATE_REL = Path(".local/share/coding-agent-environment/resource-policy")
POLICY_REL = Path("Library/Application Support/Glomeris/resources/policy.json")
AUTHORITY_REL = Path("Library/Application Support/Glomeris/autopilot.conf")
OWNER = "coding-agent-environment:resource-policy:v1"
SCHEMA = 1
REBOUND_SCHEMA = 2
HISTORICAL_REBIND = ("f98389017b8ea52035d25bb21598711fd5f882a0",
                    "839dbd518b9a1d56096bb6b8a0b427d2c660c88f")
SUPPORTED_REBIND = (HISTORICAL_REBIND[1], "fa7379adbc5dc1a94d847048ba549d3f4a30cfeb")
# Each edge retains the tested policy/restore contract; no arbitrary forward adoption.
SUPPORTED_REBINDS = {HISTORICAL_REBIND, SUPPORTED_REBIND,
                     (HISTORICAL_REBIND[0], SUPPORTED_REBIND[1])}
MAX_ACTIONS = 3
MAX_BYTES = 5368709120
MAX_DURATION = 60
CORE_DISABLED_POLICY = {
    "schema": 1,
    "classes": {"managed_generated": {"idle_threshold_secs": 86400, "unattended": False}},
    "max_actions": 3,
    "max_bytes": MAX_BYTES,
}
PHASES = {"PREPARED", "POLICY_APPLIED", "ENVELOPE_APPLIED", "COMPLETE",
          "ENV_RESTORED", "POLICY_RESTORED", "ROLLED_BACK"}


def load_helper(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Required shared installer helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = load_helper(ROOT / "scripts/storage-integration.py", "resource_policy_storage_integration")
resolver = load_helper(ROOT / ".claude/skills/engineering-runtime/scripts/storage.py", "resource_policy_storage_resolver")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_policy(value, require_unattended=False):
    if (not isinstance(value, dict) or set(value) != {"schema", "classes", "max_actions", "max_bytes"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or type(value["max_actions"]) is not int or value["max_actions"] != MAX_ACTIONS
            or type(value["max_bytes"]) is not int or value["max_bytes"] != MAX_BYTES
            or not isinstance(value["classes"], dict) or set(value["classes"]) != {"managed_generated"}):
        raise ValueError("Tracked resource policy does not match schema 1 managed-generated bounds")
    cls = value["classes"]["managed_generated"]
    if (not isinstance(cls, dict) or set(cls) != {"idle_threshold_secs", "unattended"}
            or type(cls["idle_threshold_secs"]) is not int or cls["idle_threshold_secs"] <= 0
            or type(cls["unattended"]) is not bool or (require_unattended and not cls["unattended"])):
        raise ValueError("Tracked managed-generated policy has invalid class gates")
    return value


def snapshot(path):
    storage.reject_symlink_ancestry(path)
    if not path.exists():
        return {"exists": False, "bytes_b64": "", "sha256": sha256(b"")}
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError(f"Native managed state is not a private user-owned file: {path}")
    raw = path.read_bytes()
    return {"exists": True, "bytes_b64": base64.b64encode(raw).decode("ascii"), "sha256": sha256(raw)}


def snapshot_bytes(value):
    if (not isinstance(value, dict) or set(value) != {"exists", "bytes_b64", "sha256"}
            or type(value["exists"]) is not bool or not isinstance(value["bytes_b64"], str)
            or not isinstance(value["sha256"], str)):
        raise ValueError("Invalid transaction snapshot")
    try:
        raw = base64.b64decode(value["bytes_b64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid transaction snapshot encoding") from exc
    if value["sha256"] != sha256(raw) or (not value["exists"] and raw):
        raise ValueError("Transaction snapshot digest mismatch")
    return raw


def same_snapshot(left, right):
    return left["exists"] == right["exists"] and left["sha256"] == right["sha256"]


class ResourcePolicyInstaller:
    def __init__(self, home, binary, runner=subprocess.run):
        self.home = Path(home).expanduser().absolute()
        self.binary = Path(binary)
        self.runner = runner
        self.state_dir = self.home / STATE_REL
        self.journal_path = self.state_dir / "journal.json"
        self.lock_path = self.state_dir / "installer.lock"
        self.policy_path = self.home / POLICY_REL
        self.authority_path = self.home / AUTHORITY_REL
        self.pin = storage.manifest()
        self.source_bytes = SOURCE.read_bytes()
        self.desired = validate_policy(json.loads(self.source_bytes), require_unattended=True)
        self.desired_digest = sha256(self.source_bytes)

    def native(self, *args):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        result = self.runner([str(self.binary), *args], capture_output=True, text=True, env=environment)
        if result.returncode:
            raise ValueError(f"Glomeris refused resource-policy operation ({result.returncode})")
        return result.stdout.strip()

    def observe_policy(self):
        try:
            value = json.loads(self.native("resource", "policy", "show"))
        except json.JSONDecodeError as exc:
            raise ValueError("Glomeris resource policy show did not return JSON") from exc
        validate_policy(value)
        return value

    def observe_authority(self):
        try:
            value = json.loads(self.native("autopilot", "show", "--json"))
        except json.JSONDecodeError as exc:
            raise ValueError("Glomeris autopilot show did not return JSON") from exc
        required = {"enabled", "allowed_kinds", "ask_preauthorizations", "max_actions", "max_bytes",
                    "max_duration_secs", "min_pressure", "respond_to_alerts", "starts_unprompted", "stored_at"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError("Glomeris Autopilot report lacks required authority fields")
        expected_path = self.authority_path
        stored = value["stored_at"]
        if not isinstance(stored, str) or not Path(stored).is_absolute() or Path(stored) != expected_path:
            raise ValueError("Glomeris autopilot stored_at does not match the selected native path")
        if (type(value["enabled"]) is not bool
                or not isinstance(value["allowed_kinds"], list)
                or any(not isinstance(kind, str) for kind in value["allowed_kinds"])
                or not isinstance(value["ask_preauthorizations"], list)
                or type(value["respond_to_alerts"]) is not bool
                or type(value["starts_unprompted"]) is not bool
                or value["min_pressure"] is not None and not isinstance(value["min_pressure"], str)):
            raise ValueError("Glomeris Autopilot authority report has invalid field types")
        return value

    def check_capabilities(self):
        policy_report = self.observe_policy()
        authority_report = self.observe_authority()
        if authority_report["enabled"] and (
                any(kind != "managed_generated" for kind in authority_report["allowed_kinds"])
                or authority_report["ask_preauthorizations"]):
            raise ValueError("Enabled Autopilot has unrelated kinds or preauthorizations; existing authority preserved")
        return policy_report, authority_report

    def capture(self):
        return {"policy": snapshot(self.policy_path), "envelope": snapshot(self.authority_path)}

    def validate_journal(self, value, check_pin=True):
        required = {"schema", "owner", "home", "pin_revision", "desired_policy_digest", "phase", "before",
                    "before_authority_report", "policy_post", "envelope_post", "rollback_policy_post",
                    "rollback_envelope_post"}
        rollback_fields = {"rollback_policy_before", "rollback_policy_restore_intent"}
        rebind_fields = {"pin_rebindings"}
        allowed = (required, required | rollback_fields, required | rebind_fields,
                   required | rollback_fields | rebind_fields)
        if not isinstance(value, dict) or set(value) not in allowed:
            raise ValueError("Invalid resource-policy journal fields")
        if type(value.get("schema")) is not int or value["schema"] not in (SCHEMA, REBOUND_SCHEMA):
            raise ValueError("Unsupported resource-policy journal schema")
        if (value["schema"] == SCHEMA and "pin_rebindings" in value
                or value["schema"] == REBOUND_SCHEMA and "pin_rebindings" not in value):
            raise ValueError("Resource-policy journal schema and rebind history disagree")
        if value["schema"] == REBOUND_SCHEMA:
            self.validate_pin_rebindings(value["pin_rebindings"], value["pin_revision"])
        if set(value) in (required, required | rebind_fields):
            value["rollback_policy_before"] = None
            value["rollback_policy_restore_intent"] = None
        if (not isinstance(value["pin_revision"], str)
                or len(value["pin_revision"]) != 40
                or any(char not in "0123456789abcdef" for char in value["pin_revision"])
                or not isinstance(value["desired_policy_digest"], str)
                or len(value["desired_policy_digest"]) != 64
                or any(char not in "0123456789abcdef" for char in value["desired_policy_digest"])
                or not isinstance(value["owner"], str) or not isinstance(value["home"], str)
                or not isinstance(value["phase"], str)
                or value["owner"] != OWNER or value["home"] != str(self.home)
                or check_pin and value["pin_revision"] != self.pin["revision"]
                or value["desired_policy_digest"] != self.desired_digest or value["phase"] not in PHASES):
            raise ValueError("Resource-policy journal conflicts with this home, source pin, or desired policy")
        if not isinstance(value["before"], dict) or set(value["before"]) != {"policy", "envelope"}:
            raise ValueError("Invalid original resource-policy snapshots")
        if any(value["before"][key] is None for key in ("policy", "envelope")):
            raise ValueError("Original resource-policy snapshots are missing")
        for entry in (value["before"]["policy"], value["before"]["envelope"], value["policy_post"],
                      value["envelope_post"], value["rollback_policy_post"], value["rollback_envelope_post"],
                      value["rollback_policy_before"]):
            if entry is not None:
                snapshot_bytes(entry)
        if (value["rollback_policy_restore_intent"] is not None
                and type(value["rollback_policy_restore_intent"]) is not bool):
            raise ValueError("Invalid rollback policy restore intent")
        if ((value["rollback_policy_before"] is None)
                != (value["rollback_policy_restore_intent"] is None)):
            raise ValueError("Incomplete rollback policy restore plan")
        prior = value["before_authority_report"]
        prior_required = {"enabled", "allowed_kinds", "ask_preauthorizations", "max_actions", "max_bytes",
                          "max_duration_secs", "min_pressure", "respond_to_alerts", "starts_unprompted",
                          "stored_at"}
        if (not isinstance(prior, dict) or not prior_required.issubset(prior)
                or any(type(prior[key]) is not bool for key in ("enabled", "respond_to_alerts", "starts_unprompted"))
                or any(not isinstance(prior[key], list) or any(not isinstance(item, str) for item in prior[key])
                       for key in ("allowed_kinds", "ask_preauthorizations"))
                or any(type(prior[key]) is not int for key in ("max_actions", "max_bytes", "max_duration_secs"))
                or prior["min_pressure"] is not None and not isinstance(prior["min_pressure"], str)
                or prior["stored_at"] != str(self.authority_path)):
            raise ValueError("Invalid original Autopilot authority report")
        return value

    @staticmethod
    def validate_pin_rebindings(entries, final_revision):
        if not isinstance(entries, list) or not entries:
            raise ValueError("Invalid resource-policy pin rebind history")
        previous = None
        for entry in entries:
            if (not isinstance(entry, dict)
                    or set(entry) != {"from_revision", "to_revision", "target_binary_sha256",
                                      "previous_journal_sha256"}
                    or (entry["from_revision"], entry["to_revision"]) not in SUPPORTED_REBINDS
                    or not isinstance(entry["target_binary_sha256"], str)
                    or len(entry["target_binary_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in entry["target_binary_sha256"])
                    or not isinstance(entry["previous_journal_sha256"], str)
                    or len(entry["previous_journal_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in entry["previous_journal_sha256"])):
                raise ValueError("Invalid resource-policy pin rebind lineage")
            if previous is not None and entry["from_revision"] != previous:
                raise ValueError("Broken resource-policy pin rebind lineage")
            previous = entry["to_revision"]
        if previous != final_revision:
            raise ValueError("Resource-policy pin rebind lineage does not end at the pinned revision")

    def read_journal(self, check_pin=True):
        value = storage.read_private_json(self.journal_path)
        return None if value is None else self.validate_journal(value, check_pin=check_pin)

    def save(self, journal):
        storage.atomic_json(self.journal_path, journal)

    def cas_file(self, label, value):
        raw = snapshot_bytes(value)
        fd, name = tempfile.mkstemp(prefix=f"{label}-", suffix=".snapshot", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            return Path(name)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def desired_authority(self, report, prior):
        return (report.get("enabled") is True
                and report.get("allowed_kinds") == ["managed_generated"]
                and report.get("ask_preauthorizations") == []
                and report.get("max_actions") == MAX_ACTIONS
                and report.get("max_bytes") == MAX_BYTES
                and report.get("max_duration_secs") == MAX_DURATION
                and report.get("respond_to_alerts") is True
                and report.get("starts_unprompted") is True
                and report.get("min_pressure") == prior.get("min_pressure"))

    @staticmethod
    def revoked_authority(report):
        return (report.get("enabled") is False and report.get("allowed_kinds") == []
                and report.get("ask_preauthorizations") == []
                and report.get("starts_unprompted") is False
                and report.get("respond_to_alerts") is False and report.get("min_pressure") is None)

    def policy_is_desired(self):
        return self.observe_policy() == self.desired

    def _verify_rebind_runtime(self):
        """Return the hash of the verified current runtime receipt's binary."""
        _old_revision, target_revision = SUPPORTED_REBIND
        if self.pin.get("revision") != target_revision:
            raise ValueError("No supported policy journal pin rebind exists for this source revision")
        receipt_path = self.home / storage.RUNTIME / "storage-core.json"
        current, _previous = storage.read_selection(receipt_path)
        if current is None or current["source_revision"] != target_revision:
            raise ValueError("Current Glomeris storage receipt does not match the target source pin")
        if storage.artifact_state(current, receipt_path.parent) != "installed":
            raise ValueError("Current Glomeris runtime receipt failed artifact verification")
        resolved, status = resolver.resolve(self.home, self.pin)
        if status != "verified_installation" or Path(resolved) != Path(current["path"]):
            raise ValueError("Current pinned Glomeris runtime did not resolve from its verified receipt")
        if Path(resolved) != self.binary:
            raise ValueError("Installer binary does not match the verified current runtime receipt")
        return current["binary_sha256"]

    def _validate_rebind_eligibility(self, journal):
        if (journal["schema"] not in (SCHEMA, REBOUND_SCHEMA)
                or (journal["pin_revision"], self.pin.get("revision")) not in SUPPORTED_REBINDS):
            raise ValueError("Resource-policy journal pin change is outside the supported migration")
        if journal["phase"] != "COMPLETE":
            raise ValueError("Only a completed resource-policy journal can be rebound")
        if (journal["policy_post"] is None or journal["envelope_post"] is None
                or journal["before"]["policy"] is None or journal["before"]["envelope"] is None
                or journal["rollback_policy_post"] is not None
                or journal["rollback_envelope_post"] is not None
                or journal.get("rollback_policy_before") is not None
                or journal.get("rollback_policy_restore_intent") is not None):
            raise ValueError("Completed resource-policy journal has rollback state or incomplete posts")
        binary_digest = self._verify_rebind_runtime()
        current_policy, current_envelope = snapshot(self.policy_path), snapshot(self.authority_path)
        if (not same_snapshot(current_policy, journal["policy_post"])
                or not same_snapshot(current_envelope, journal["envelope_post"])):
            raise ValueError("Live native state drifted from the completed install; journal preserved")
        policy, authority = self.check_capabilities()
        if policy != self.desired or not self.desired_authority(authority, journal["before_authority_report"]):
            raise ValueError("Live native policy or grant differs from the recorded managed install")
        if current_policy != snapshot(self.policy_path) or current_envelope != snapshot(self.authority_path):
            raise ValueError("Live native state changed during pin verification; journal preserved")
        return binary_digest, current_policy, current_envelope

    def _rebind_completed_journal(self, journal):
        binary_digest, current_policy, current_envelope = self._validate_rebind_eligibility(journal)
        old_revision, target_revision = journal["pin_revision"], self.pin["revision"]
        old_journal_bytes = self.journal_path.read_bytes()
        rebound = dict(journal)
        rebound["schema"] = REBOUND_SCHEMA
        rebound["pin_revision"] = target_revision
        rebound["pin_rebindings"] = list(journal.get("pin_rebindings", [])) + [{
            "from_revision": old_revision,
            "to_revision": target_revision,
            "target_binary_sha256": binary_digest,
            "previous_journal_sha256": sha256(old_journal_bytes),
        }]
        self.validate_journal(rebound)
        if (current_policy != snapshot(self.policy_path) or current_envelope != snapshot(self.authority_path)):
            raise ValueError("Live native state changed before journal rebind; journal preserved")
        self.save(rebound)
        if (not same_snapshot(snapshot(self.policy_path), rebound["policy_post"])
                or not same_snapshot(snapshot(self.authority_path), rebound["envelope_post"])):
            raise ValueError("Live native state changed after journal rebind; review required")
        return "pin rebound"

    def apply(self):
        storage.ensure_private_dir(self.state_dir)
        lock = storage.acquire_lock(self.lock_path)
        try:
            _, authority_report = self.check_capabilities()
            journal = self.read_journal(check_pin=False)
            if journal is not None and journal["pin_revision"] != self.pin["revision"]:
                return self._rebind_completed_journal(journal)
            if journal is None:
                before = self.capture()
                journal = {"schema": SCHEMA, "owner": OWNER, "home": str(self.home),
                           "pin_revision": self.pin["revision"], "desired_policy_digest": self.desired_digest,
                           "phase": "PREPARED", "before": before,
                           "before_authority_report": authority_report, "policy_post": None,
                           "envelope_post": None, "rollback_policy_post": None,
                           "rollback_envelope_post": None, "rollback_policy_before": None,
                           "rollback_policy_restore_intent": None}
                self.save(journal)
            phase = journal["phase"]
            if phase == "COMPLETE":
                if (same_snapshot(snapshot(self.policy_path), journal["policy_post"])
                        and same_snapshot(snapshot(self.authority_path), journal["envelope_post"])):
                    return "already installed"
                raise ValueError("Completed installation drifted; native changes preserved")
            if phase in {"ROLLED_BACK", "ENV_RESTORED", "POLICY_RESTORED"}:
                raise ValueError("Rollback is in progress or complete; refusing to replace its receipt")

            live_policy = snapshot(self.policy_path)
            if phase == "PREPARED":
                if same_snapshot(live_policy, journal["before"]["policy"]):
                    expected = self.cas_file("policy-original", journal["before"]["policy"])
                    try:
                        self.native("resource", "policy", "apply", str(SOURCE), "--expected-current", str(expected))
                    finally:
                        expected.unlink(missing_ok=True)
                    live_policy = snapshot(self.policy_path)
                elif not self.policy_is_desired():
                    raise ValueError("Resource policy changed after preparation; native state preserved")
                if not self.policy_is_desired():
                    raise ValueError("Glomeris did not persist the desired resource policy")
                journal["policy_post"] = live_policy
                journal["phase"] = "POLICY_APPLIED"
                self.save(journal)
                phase = "POLICY_APPLIED"

            if phase == "POLICY_APPLIED":
                if not self.policy_is_desired():
                    raise ValueError("Installed resource policy drifted before authority grant")
                live_authority = snapshot(self.authority_path)
                if same_snapshot(live_authority, journal["before"]["envelope"]):
                    expected = self.cas_file("authority-original", journal["before"]["envelope"])
                    try:
                        self.native("resource", "authority", "grant-managed", "--expected-current", str(expected),
                                    "--max-actions", str(MAX_ACTIONS), "--max-bytes", str(MAX_BYTES),
                                    "--max-duration", str(MAX_DURATION))
                    finally:
                        expected.unlink(missing_ok=True)
                else:
                    current_report = self.observe_authority()
                    if not self.desired_authority(current_report, journal["before_authority_report"]):
                        raise ValueError("Autopilot changed during interrupted grant; native authority preserved")
                report = self.observe_authority()
                if not self.desired_authority(report, journal["before_authority_report"]):
                    raise ValueError("Glomeris did not persist the exact managed-generated grant")
                journal["envelope_post"] = snapshot(self.authority_path)
                journal["phase"] = "ENVELOPE_APPLIED"
                self.save(journal)
                phase = "ENVELOPE_APPLIED"

            if phase == "ENVELOPE_APPLIED":
                if (not same_snapshot(snapshot(self.policy_path), journal["policy_post"])
                        or not same_snapshot(snapshot(self.authority_path), journal["envelope_post"])):
                    raise ValueError("Native policy or authority drifted before completion; preserved")
                journal["phase"] = "COMPLETE"
                self.save(journal)
            return "installed"
        finally:
            os.close(lock)

    def rollback(self):
        storage.ensure_private_dir(self.state_dir)
        lock = storage.acquire_lock(self.lock_path)
        try:
            journal = self.read_journal()
            if journal is None:
                raise ValueError("No resource-policy transaction exists")
            if journal["phase"] == "ROLLED_BACK":
                if (same_snapshot(snapshot(self.policy_path), journal["rollback_policy_post"])
                        and same_snapshot(snapshot(self.authority_path), journal["rollback_envelope_post"])):
                    return "already rolled back"
                raise ValueError("Rolled-back native state drifted; preserving current state")
            phase = journal["phase"]
            if phase == "POLICY_RESTORED":
                if (not same_snapshot(snapshot(self.policy_path), journal["rollback_policy_post"])
                        or not same_snapshot(snapshot(self.authority_path), journal["rollback_envelope_post"])):
                    raise ValueError("Restored native state drifted before rollback completion")
                journal["phase"] = "ROLLED_BACK"
                self.save(journal)
                return "rolled back"

            before_auth = journal["before"]["envelope"]
            before_policy = journal["before"]["policy"]
            current_auth = snapshot(self.authority_path)
            current_policy = snapshot(self.policy_path)
            if phase == "ENV_RESTORED":
                if not same_snapshot(current_auth, journal["rollback_envelope_post"]):
                    raise ValueError("Restored Autopilot authority drifted; preserving current state")
                auth_needs_restore = False
            elif same_snapshot(current_auth, before_auth):
                auth_needs_restore = False
            else:
                report = self.observe_authority()
                managed_result = self.desired_authority(report, journal["before_authority_report"])
                default_after_absence = not before_auth["exists"] and self.revoked_authority(report)
                expected_post = journal["envelope_post"]
                owned_result = (same_snapshot(current_auth, expected_post) if expected_post is not None
                                else managed_result)
                if default_after_absence:
                    auth_needs_restore = False
                elif managed_result and owned_result:
                    auth_needs_restore = True
                else:
                    raise ValueError("Autopilot no longer matches this transaction; authority preserved")

            if phase == "ENV_RESTORED":
                plan_before = journal["rollback_policy_before"]
                restore_intent = journal["rollback_policy_restore_intent"]
                if plan_before is None:
                    # Upgrade an interrupted pre-plan journal only from states
                    # independently tied to this transaction; never infer from absence.
                    if same_snapshot(current_policy, before_policy):
                        plan_before, restore_intent = current_policy, False
                    elif (journal["policy_post"] is not None
                          and same_snapshot(current_policy, journal["policy_post"])):
                        plan_before, restore_intent = current_policy, True
                    elif journal["policy_post"] is None and self.policy_is_desired():
                        plan_before, restore_intent = current_policy, True
                    else:
                        raise ValueError("Resource policy changed outside this transaction; current file preserved")
                    journal["rollback_policy_before"] = plan_before
                    journal["rollback_policy_restore_intent"] = restore_intent
                    self.save(journal)
                if same_snapshot(current_policy, plan_before):
                    policy_needs_restore = restore_intent
                else:
                    completed_original = (before_policy["exists"]
                                          and same_snapshot(current_policy, before_policy))
                    completed_absent_default = (
                        restore_intent and not before_policy["exists"] and current_policy["exists"]
                        and self.observe_policy() == CORE_DISABLED_POLICY)
                    if restore_intent and (completed_original or completed_absent_default):
                        policy_needs_restore = False
                    else:
                        raise ValueError("Resource policy changed outside this transaction; current file preserved")
            else:
                if same_snapshot(current_policy, before_policy):
                    policy_needs_restore = False
                elif journal["policy_post"] is not None and same_snapshot(current_policy, journal["policy_post"]):
                    policy_needs_restore = True
                elif journal["policy_post"] is None and self.policy_is_desired():
                    policy_needs_restore = True
                else:
                    raise ValueError("Resource policy changed outside this transaction; current file preserved")

            # Check both native files before the first mutation so drift in either
            # cannot cause the other original file to be restored partially.
            if auth_needs_restore:
                expected = self.cas_file("authority-post", current_auth)
                original = self.cas_file("authority-original", before_auth)
                try:
                    self.native("resource", "authority", "apply", str(original), "--expected-current", str(expected))
                finally:
                    expected.unlink(missing_ok=True)
                    original.unlink(missing_ok=True)
            journal["rollback_envelope_post"] = snapshot(self.authority_path)
            if phase != "ENV_RESTORED":
                journal["rollback_policy_before"] = current_policy
                journal["rollback_policy_restore_intent"] = policy_needs_restore
            journal["phase"] = "ENV_RESTORED"
            self.save(journal)

            if policy_needs_restore:
                expected = self.cas_file("policy-post", current_policy)
                original = self.cas_file("policy-original", before_policy)
                try:
                    self.native("resource", "policy", "restore", str(original), "--expected-current", str(expected))
                finally:
                    expected.unlink(missing_ok=True)
                    original.unlink(missing_ok=True)
            journal["rollback_policy_post"] = snapshot(self.policy_path)
            journal["phase"] = "POLICY_RESTORED"
            self.save(journal)
            journal["phase"] = "ROLLED_BACK"
            self.save(journal)
            return "rolled back"
        finally:
            os.close(lock)

    def inspect(self, dry_run=False):
        self.check_capabilities()
        journal = self.read_journal(check_pin=False)
        if journal and journal["pin_revision"] != self.pin["revision"]:
            self._validate_rebind_eligibility(journal)
            print("resource-policy: pin rebind required")
            return 1
        if journal is not None:
            self.validate_journal(journal)
        if journal and journal["phase"] == "COMPLETE":
            clean = (same_snapshot(snapshot(self.policy_path), journal["policy_post"])
                     and same_snapshot(snapshot(self.authority_path), journal["envelope_post"]))
            print(f"resource-policy: {'installed' if clean else 'drift'}")
            return 0 if clean else 1
        if journal:
            print(f"resource-policy: interrupted ({journal['phase']})")
            return 1
        print("resource-policy: absent; would install" if dry_run else "resource-policy: absent")
        return 0 if dry_run else 1


def runtime_binary(home):
    pin = storage.manifest()
    receipt_path = Path(home) / storage.RUNTIME / "storage-core.json"
    current, _previous = storage.read_selection(receipt_path)
    if current is None or current["source_revision"] != pin["revision"]:
        raise ValueError("Current Glomeris storage receipt does not match the source pin")
    if storage.artifact_state(current, receipt_path.parent) != "installed":
        raise ValueError("Current Glomeris runtime receipt failed artifact verification")
    binary, status = resolver.resolve(home, pin)
    if status != "verified_installation" or Path(binary) != Path(current["path"]):
        raise ValueError(f"Pinned Glomeris runtime is {status}; install/verify storage first")
    return Path(binary)


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    home = storage.home_path()
    installer = ResourcePolicyInstaller(home, runtime_binary(home), runner)
    if args.apply:
        print(installer.apply())
        return 0
    if args.rollback:
        print(installer.rollback())
        return 0
    return installer.inspect(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BlockingIOError:
        print("resource-policy: busy", file=sys.stderr)
        raise SystemExit(75)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"resource-policy: refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
