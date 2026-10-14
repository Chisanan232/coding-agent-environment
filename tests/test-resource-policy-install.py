import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/resource-policy-install.py"
SPEC = importlib.util.spec_from_file_location("resource_policy_install", SCRIPT)
installer_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer_module)


class Crash(BaseException):
    pass


class Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakeNative:
    def __init__(self, home):
        self.home = home
        self.policy_path = home / installer_module.POLICY_REL
        self.authority_path = home / installer_module.AUTHORITY_REL
        self.calls = []
        self.crash_after = None
        self.crash_before = None
        self.crashed = False
        self.stored_at_override = None
        self.default_policy = copy.deepcopy(installer_module.CORE_DISABLED_POLICY)
        self.authority = self.report()
        self.authority["min_pressure"] = "CRITICAL"
        self.prior_authority = copy.deepcopy(self.authority)

    def report(self):
        return {"enabled": False, "allowed_kinds": [], "ask_preauthorizations": [],
                "max_actions": 3, "max_bytes": installer_module.MAX_BYTES,
                "max_duration_secs": 60, "min_pressure": None,
                "respond_to_alerts": False, "starts_unprompted": False,
                "stored_at": str(self.authority_path)}

    def bytes_at(self, path):
        return path.read_bytes() if path.exists() else b""

    def check(self, path, expected):
        expected_bytes = Path(expected).read_bytes()
        if self.bytes_at(path) != expected_bytes:
            raise AssertionError("fake native CAS rejected stale expected-current snapshot")

    def maybe_crash(self, operation):
        if self.crash_after == operation and not self.crashed:
            self.crashed = True
            raise Crash("simulated process interruption after native CAS")

    def __call__(self, command, capture_output=False, text=False, env=None):
        if env is not None and env.get("HOME") != str(self.home):
            raise AssertionError("native core must use the selected home")
        args = command[1:]
        self.calls.append(tuple(args))
        if args[:3] == ["resource", "policy", "apply"] and self.crash_before == "policy_apply":
            self.crash_before = None
            raise Crash("simulated interruption before native policy CAS")
        if args == ["resource", "policy", "show"]:
            value = self.default_policy
            if self.policy_path.exists():
                value = json.loads(self.policy_path.read_bytes())
            return Result(stdout=json.dumps(value))
        if args == ["autopilot", "show", "--json"]:
            report = dict(self.authority)
            report["stored_at"] = self.stored_at_override or str(self.authority_path)
            return Result(stdout=json.dumps(report))
        if args[:3] == ["resource", "policy", "apply"]:
            source, expected = Path(args[3]), args[5]
            self.check(self.policy_path, expected)
            self.policy_path.parent.mkdir(parents=True, exist_ok=True)
            desired = json.loads(source.read_bytes())
            self.policy_path.write_text(json.dumps(desired, sort_keys=True) + "\n")
            self.policy_path.chmod(0o600)
            self.default_policy = desired
            self.maybe_crash("policy_apply")
            return Result()
        if args[:3] == ["resource", "policy", "restore"]:
            source, expected = Path(args[3]), args[5]
            self.check(self.policy_path, expected)
            raw = source.read_bytes()
            if raw:
                self.policy_path.write_bytes(raw)
                self.policy_path.chmod(0o600)
                self.default_policy = json.loads(raw)
            else:
                self.default_policy = copy.deepcopy(installer_module.CORE_DISABLED_POLICY)
                self.policy_path.parent.mkdir(parents=True, exist_ok=True)
                self.policy_path.write_text(json.dumps(self.default_policy, separators=(",", ":")))
                self.policy_path.chmod(0o600)
            self.maybe_crash("policy_restore")
            return Result()
        if args[:3] == ["resource", "authority", "grant-managed"]:
            expected = args[4]
            self.check(self.authority_path, expected)
            floor = self.authority.get("min_pressure")
            self.authority = {"enabled": True, "allowed_kinds": ["managed_generated"],
                              "ask_preauthorizations": [], "max_actions": installer_module.MAX_ACTIONS,
                              "max_bytes": installer_module.MAX_BYTES,
                              "max_duration_secs": installer_module.MAX_DURATION, "min_pressure": floor,
                              "respond_to_alerts": True, "starts_unprompted": True,
                              "stored_at": str(self.authority_path)}
            self.authority_path.parent.mkdir(parents=True, exist_ok=True)
            self.authority_path.write_text("managed-generated-grant\n")
            self.authority_path.chmod(0o600)
            self.maybe_crash("authority_grant")
            return Result(stdout=json.dumps(self.authority))
        if args[:3] == ["resource", "authority", "apply"]:
            source, expected = Path(args[3]), args[5]
            self.check(self.authority_path, expected)
            raw = source.read_bytes()
            self.authority_path.parent.mkdir(parents=True, exist_ok=True)
            if raw:
                self.authority_path.write_bytes(raw)
                self.authority_path.chmod(0o600)
                self.authority = copy.deepcopy(self.prior_authority)
            else:
                # Native core materializes its canonical revoked default; it never unlinks.
                self.authority_path.write_text("canonical-revoked-envelope\n")
                self.authority_path.chmod(0o600)
                self.authority = self.report()
            self.authority["stored_at"] = str(self.authority_path)
            self.maybe_crash("authority_restore")
            return Result(stdout=json.dumps(self.authority))
        return Result(2, "")


class ResourcePolicyInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir(mode=0o700)
        self.native = FakeNative(self.home)
        self.install = lambda: installer_module.ResourcePolicyInstaller(
            self.home, Path("/verified/fake-glomeris"), self.native)

    def tearDown(self):
        self.temp.cleanup()

    def completed_old_pin_install(self):
        old_pin = dict(installer_module.storage.manifest())
        old_pin["revision"] = installer_module.SUPPORTED_REBIND[0]
        with patch.object(installer_module.storage, "manifest", return_value=old_pin):
            old_installer = installer_module.ResourcePolicyInstaller(
                self.home, Path("/verified/fake-glomeris"), self.native)
            self.assertEqual(old_installer.apply(), "installed")
        return old_installer

    def rebind_installer(self):
        installer = installer_module.ResourcePolicyInstaller(
            self.home, Path("/verified/fake-glomeris"), self.native)
        installer._verify_rebind_runtime = lambda: "a" * 64
        return installer

    def test_read_only_modes_do_not_create_installer_state(self):
        inst = self.install()
        self.assertEqual(inst.inspect(dry_run=True), 0)
        self.assertEqual(inst.inspect(), 1)
        self.assertFalse(inst.state_dir.exists())

    def test_apply_idempotence_and_rollback_absence_materializes_default(self):
        inst = self.install()
        self.assertEqual(inst.apply(), "installed")
        journal_before = inst.journal_path.read_bytes()
        writes_before = sum(call[1:3] in [("policy", "apply"), ("authority", "grant-managed")]
                            for call in self.native.calls if call[0] == "resource")
        self.assertEqual(inst.apply(), "already installed")
        self.assertEqual(inst.journal_path.read_bytes(), journal_before)
        writes_after = sum(call[1:3] in [("policy", "apply"), ("authority", "grant-managed")]
                           for call in self.native.calls if call[0] == "resource")
        self.assertEqual(writes_after, writes_before)
        self.assertEqual(inst.rollback(), "rolled back")
        self.assertTrue(inst.authority_path.is_file())
        self.assertEqual(inst.authority_path.read_text(), "canonical-revoked-envelope\n")
        self.assertTrue(inst.policy_path.is_file())
        self.assertEqual(json.loads(inst.policy_path.read_text()), installer_module.CORE_DISABLED_POLICY)
        self.assertEqual(inst.rollback(), "already rolled back")

    def test_existing_native_bytes_and_pressure_floor_are_preserved(self):
        inst = self.install()
        old_policy = b'{"schema":1,"classes":{"managed_generated":{"idle_threshold_secs":172800,"unattended":false}},"max_actions":3,"max_bytes":5368709120}\n'
        inst.policy_path.parent.mkdir(parents=True, mode=0o700)
        inst.policy_path.write_bytes(old_policy)
        inst.policy_path.chmod(0o600)
        old_envelope = b"version = 1\nenabled = false\n"
        inst.authority_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        inst.authority_path.write_bytes(old_envelope)
        inst.authority_path.chmod(0o600)
        self.native.default_policy = json.loads(old_policy)
        self.native.authority["min_pressure"] = "EMERGENCY"
        inst.apply()
        self.assertEqual(self.native.authority["min_pressure"], "EMERGENCY")
        inst.rollback()
        self.assertEqual(inst.policy_path.read_bytes(), old_policy)
        self.assertEqual(inst.authority_path.read_bytes(), old_envelope)

    def test_policy_apply_interruption_resumes_without_second_policy_write(self):
        native = self.native
        native.crash_after = "policy_apply"
        with self.assertRaises(Crash):
            self.install().apply()
        native.crash_after = None
        self.assertEqual(self.install().apply(), "installed")
        self.assertEqual(sum(c[:3] == ("resource", "policy", "apply") for c in native.calls), 1)

    def test_authority_apply_interruption_is_recognized_and_resumed(self):
        native = self.native
        native.crash_after = "authority_grant"
        with self.assertRaises(Crash):
            self.install().apply()
        native.crash_after = None
        self.assertEqual(self.install().apply(), "installed")
        self.assertEqual(sum(c[:3] == ("resource", "authority", "grant-managed") for c in native.calls), 1)

    def test_rollback_interruption_after_authority_cas_resumes(self):
        inst = self.install()
        inst.apply()
        self.native.crash_after = "authority_restore"
        with self.assertRaises(Crash):
            inst.rollback()
        self.native.crash_after = None
        self.assertEqual(self.install().rollback(), "rolled back")
        self.assertTrue(inst.authority_path.exists())

    def test_rollback_interruption_after_policy_cas_resumes(self):
        inst = self.install()
        inst.apply()
        self.native.crash_after = "policy_restore"
        with self.assertRaises(Crash):
            inst.rollback()
        self.native.crash_after = None
        self.assertEqual(self.install().rollback(), "rolled back")

    def test_rollback_resumes_after_native_default_restore_before_journal_capture(self):
        inst = self.install()
        inst.apply()
        self.native.crash_after = "policy_restore"
        with self.assertRaises(Crash):
            inst.rollback()
        journal = json.loads(inst.journal_path.read_text())
        self.assertEqual(journal["phase"], "ENV_RESTORED")
        self.assertTrue(inst.policy_path.is_file())
        self.assertEqual(json.loads(inst.policy_path.read_text()), installer_module.CORE_DISABLED_POLICY)
        self.native.crash_after = None
        self.assertEqual(self.install().rollback(), "rolled back")
        self.assertEqual(json.loads(inst.policy_path.read_text()), installer_module.CORE_DISABLED_POLICY)

    def test_env_restored_plan_resumes_after_prepared_policy_apply_without_policy_post(self):
        self.native.crash_after = "policy_apply"
        with self.assertRaises(Crash):
            self.install().apply()
        self.native.crash_after = None
        inst = self.install()
        original_save = inst.save

        def crash_after_env_plan(journal):
            original_save(journal)
            if journal["phase"] == "ENV_RESTORED":
                raise Crash("interrupted after rollback plan persisted")

        inst.save = crash_after_env_plan
        with self.assertRaises(Crash):
            inst.rollback()
        journal = json.loads(inst.journal_path.read_text())
        self.assertEqual(journal["phase"], "ENV_RESTORED")
        self.assertIsNone(journal["policy_post"])
        self.assertTrue(journal["rollback_policy_restore_intent"])
        self.assertEqual(journal["rollback_policy_before"], installer_module.snapshot(inst.policy_path))

        self.assertEqual(self.install().rollback(), "rolled back")
        self.assertEqual(json.loads(inst.policy_path.read_text()), installer_module.CORE_DISABLED_POLICY)

    def test_env_restored_plan_resumes_from_prepared_before_any_native_write(self):
        self.native.crash_before = "policy_apply"
        with self.assertRaises(Crash):
            self.install().apply()
        inst = self.install()
        original_save = inst.save

        def crash_after_env_plan(journal):
            original_save(journal)
            if journal["phase"] == "ENV_RESTORED":
                raise Crash("interrupted after rollback plan persisted")

        inst.save = crash_after_env_plan
        with self.assertRaises(Crash):
            inst.rollback()
        journal = json.loads(inst.journal_path.read_text())
        self.assertEqual(journal["phase"], "ENV_RESTORED")
        self.assertIsNone(journal["policy_post"])
        self.assertFalse(journal["rollback_policy_restore_intent"])
        self.assertFalse(journal["rollback_policy_before"]["exists"])
        self.assertEqual(journal["rollback_policy_before"]["bytes_b64"], "")

        self.assertEqual(self.install().rollback(), "rolled back")
        self.assertFalse(inst.policy_path.exists())

    def test_rollback_does_not_adopt_noncanonical_policy_drift_in_resume_phase(self):
        inst = self.install()
        inst.apply()
        self.native.crash_after = "policy_restore"
        with self.assertRaises(Crash):
            inst.rollback()
        inst.policy_path.unlink()
        with self.assertRaisesRegex(ValueError, "changed outside this transaction"):
            inst.rollback()

        drift = copy.deepcopy(installer_module.CORE_DISABLED_POLICY)
        drift["classes"]["managed_generated"]["idle_threshold_secs"] = 3600
        inst.policy_path.write_text(json.dumps(drift))
        inst.policy_path.chmod(0o600)
        self.native.default_policy = drift
        bytes_before = inst.policy_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "changed outside this transaction"):
            inst.rollback()
        self.assertEqual(inst.policy_path.read_bytes(), bytes_before)

    def test_concurrent_drift_refuses_before_mutating_either_native_file(self):
        inst = self.install()
        inst.apply()
        inst.policy_path.write_bytes(b"concurrent user policy\n")
        inst.policy_path.chmod(0o600)
        auth_before = inst.authority_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "policy changed outside"):
            inst.rollback()
        self.assertEqual(inst.authority_path.read_bytes(), auth_before)

    def test_unrelated_enabled_authority_refuses_before_native_writes(self):
        inst = self.install()
        self.native.authority.update(enabled=True, allowed_kinds=["cargo_target_dir"], ask_preauthorizations=[])
        with self.assertRaisesRegex(ValueError, "unrelated kinds"):
            inst.apply()
        self.assertFalse(inst.policy_path.exists())
        self.assertFalse(inst.authority_path.exists())

    def test_native_authority_path_must_match_stored_at(self):
        inst = self.install()
        self.native.stored_at_override = str(self.home / "other.conf")
        with self.assertRaisesRegex(ValueError, "stored_at"):
            inst.apply()
        self.assertFalse(inst.policy_path.exists())
        self.assertFalse(inst.authority_path.exists())

    def test_installer_lock_contention_fails_closed_with_busy_status(self):
        inst = self.install()
        installer_module.storage.ensure_private_dir(inst.state_dir)
        fd = os.open(inst.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                inst.apply()
        finally:
            os.close(fd)

    def test_profile_install_and_sync_check_delegate_to_resource_policy_mode(self):
        repo = SCRIPT.parents[1]
        shim_dir = Path(self.temp.name) / "shims"
        shim_dir.mkdir(mode=0o700)
        log_path = Path(self.temp.name) / "delegated-argv"
        shim = shim_dir / "python3"
        shim.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CALL_LOG\"\n")
        shim.chmod(0o700)
        env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}", CALL_LOG=str(log_path))
        subprocess.run(["bash", str(repo / "scripts/profile-install.sh"), "--resource-policy", "--check"],
                       env=env, check=True, capture_output=True, text=True)
        self.assertEqual(log_path.read_text().splitlines(),
                         [str(repo / "scripts/resource-policy-install.py"), "--check"])

        bash_shim = shim_dir / "bash"
        bash_shim.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CALL_LOG\"\n")
        bash_shim.chmod(0o700)
        subprocess.run(["/bin/bash", str(repo / "scripts/sync-check.sh"), "--resource-policy"],
                       env=env, check=True, capture_output=True, text=True)
        self.assertEqual(log_path.read_text().splitlines(),
                         [str(repo / "scripts/profile-install.sh"), "--resource-policy", "--check"])

    def test_profile_install_links_resource_adapter_and_preserves_unowned_wrapper(self):
        repo = SCRIPT.parents[1]
        home = Path(self.temp.name) / "profile-home"
        bin_dir = Path(self.temp.name) / "profile-bin"
        home.mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(home), INSTALL_BIN_DIR=str(bin_dir),
                   CODING_AGENT_PROFILE_DIR=str(home / "profiles"), CODEX_HOME=str(home / ".codex"))
        subprocess.run(["bash", str(repo / "scripts/profile-install.sh")],
                       env=env, check=True, capture_output=True, text=True)
        adapter = bin_dir / "coding-agent-resource"
        self.assertTrue(adapter.is_symlink())
        self.assertEqual(os.readlink(adapter), str(repo / "bin/coding-agent-resource"))

        occupied = Path(self.temp.name) / "occupied-bin"
        occupied.mkdir(mode=0o700)
        wrapper = occupied / "coding-agent-resource"
        wrapper.write_text("user-owned wrapper\n")
        env["INSTALL_BIN_DIR"] = str(occupied)
        subprocess.run(["bash", str(repo / "scripts/profile-install.sh")],
                       env=env, check=True, capture_output=True, text=True)
        self.assertFalse(wrapper.is_symlink())
        self.assertEqual(wrapper.read_text(), "user-owned wrapper\n")

    def test_journal_pin_or_policy_conflict_refuses(self):
        inst = self.install()
        inst.apply()
        journal = json.loads(inst.journal_path.read_text())
        journal["pin_revision"] = "0" * 40
        installer_module.storage.atomic_json(inst.journal_path, journal)
        with self.assertRaises(ValueError):
            self.install().apply()

    def test_journal_desired_policy_digest_conflict_refuses(self):
        inst = self.install()
        inst.apply()
        changed = json.loads(inst.source_bytes)
        changed["classes"]["managed_generated"]["idle_threshold_secs"] += 1
        source = Path(self.temp.name) / "different-policy.json"
        source.write_text(json.dumps(changed))
        with patch.object(installer_module, "SOURCE", source):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                self.install().apply()

    def test_completed_install_rebinds_once_and_preserves_receipt_posts(self):
        old = self.completed_old_pin_install()
        old_bytes = old.journal_path.read_bytes()
        old_journal = json.loads(old_bytes)
        native_writes = sum(call[:3] in (("resource", "policy", "apply"),
                                         ("resource", "authority", "grant-managed"))
                            for call in self.native.calls)
        installer = self.rebind_installer()
        self.assertEqual(installer.apply(), "pin rebound")
        rebound = json.loads(installer.journal_path.read_bytes())
        self.assertEqual(rebound["schema"], installer_module.REBOUND_SCHEMA)
        self.assertEqual(rebound["pin_revision"], installer_module.SUPPORTED_REBIND[1])
        for key in ("before", "before_authority_report", "policy_post", "envelope_post",
                    "rollback_policy_post", "rollback_envelope_post"):
            self.assertEqual(rebound[key], old_journal[key])
        self.assertEqual(rebound["pin_rebindings"], [{
            "from_revision": installer_module.SUPPORTED_REBIND[0],
            "to_revision": installer_module.SUPPORTED_REBIND[1],
            "target_binary_sha256": "a" * 64,
            "previous_journal_sha256": installer_module.sha256(old_bytes),
        }])
        rebound_bytes = installer.journal_path.read_bytes()
        self.assertEqual(installer.apply(), "already installed")
        self.assertEqual(installer.journal_path.read_bytes(), rebound_bytes)
        self.assertEqual(sum(call[:3] in (("resource", "policy", "apply"),
                                          ("resource", "authority", "grant-managed"))
                             for call in self.native.calls), native_writes)

    def test_read_only_inspect_reports_rebind_required(self):
        old = self.completed_old_pin_install()
        before = old.journal_path.read_bytes()
        self.assertEqual(self.rebind_installer().inspect(), 1)
        self.assertEqual(old.journal_path.read_bytes(), before)

    def test_read_only_inspect_refuses_ineligible_rebind_without_mutation(self):
        old = self.completed_old_pin_install()
        original = json.loads(old.journal_path.read_text())
        for variation in ("interrupted", "unsupported", "policy_drift", "envelope_drift", "runtime"):
            with self.subTest(variation=variation):
                journal = copy.deepcopy(original)
                installer = self.rebind_installer()
                if variation == "interrupted":
                    journal["phase"] = "PREPARED"
                elif variation == "unsupported":
                    journal["pin_revision"] = "0" * 40
                installer_module.storage.atomic_json(old.journal_path, journal)
                paths = (old.journal_path, old.policy_path, old.authority_path)
                pristine = {path: path.read_bytes() for path in paths}
                if variation == "policy_drift":
                    old.policy_path.write_bytes(pristine[old.policy_path] + b" ")
                elif variation == "envelope_drift":
                    old.authority_path.write_bytes(pristine[old.authority_path] + b" ")
                elif variation == "runtime":
                    installer._verify_rebind_runtime = lambda: (_ for _ in ()).throw(
                        ValueError("Current receipt unavailable; previous must not authorize"))
                observed = {path: path.read_bytes() for path in paths}
                with self.assertRaises(ValueError):
                    installer.inspect()
                self.assertEqual({path: path.read_bytes() for path in paths}, observed)
                for path, raw in pristine.items():
                    path.write_bytes(raw)

    def test_rebind_crash_before_or_after_atomic_journal_replace_is_idempotent(self):
        for crash_after_save in (False, True):
            with self.subTest(crash_after_save=crash_after_save):
                case_temp = tempfile.TemporaryDirectory()
                home = (Path(case_temp.name) / "home").resolve()
                home.mkdir(mode=0o700)
                native = FakeNative(home)
                old_pin = dict(installer_module.storage.manifest())
                old_pin["revision"] = installer_module.SUPPORTED_REBIND[0]
                with patch.object(installer_module.storage, "manifest", return_value=old_pin):
                    old = installer_module.ResourcePolicyInstaller(
                        home, Path("/verified/fake-glomeris"), native)
                    self.assertEqual(old.apply(), "installed")
                installer = installer_module.ResourcePolicyInstaller(
                    home, Path("/verified/fake-glomeris"), native)
                installer._verify_rebind_runtime = lambda: "a" * 64
                original_save = installer.save

                def interrupted_save(journal):
                    if crash_after_save:
                        original_save(journal)
                    raise Crash("simulated interruption around atomic journal replace")

                installer.save = interrupted_save
                with self.assertRaises(Crash):
                    installer.apply()
                current = json.loads(old.journal_path.read_text())
                if crash_after_save:
                    self.assertEqual(current["schema"], installer_module.REBOUND_SCHEMA)
                    self.assertEqual(installer.apply(), "already installed")
                else:
                    self.assertEqual(current["pin_revision"], installer_module.SUPPORTED_REBIND[0])
                    installer.save = original_save
                    self.assertEqual(installer.apply(), "pin rebound")
                case_temp.cleanup()

    def test_rollback_interrupted_old_pin_cannot_be_rebound(self):
        old = self.completed_old_pin_install()
        self.native.crash_after = "authority_restore"
        with self.assertRaises(Crash):
            old.rollback()
        journal_before = old.journal_path.read_bytes()
        self.assertIn(json.loads(journal_before)["phase"], {"COMPLETE", "ENV_RESTORED"})
        with self.assertRaisesRegex(ValueError, "preserved"):
            self.rebind_installer().apply()
        self.assertEqual(old.journal_path.read_bytes(), journal_before)

    def test_cross_pin_rebind_refuses_every_noncomplete_phase(self):
        old = self.completed_old_pin_install()
        original = old.journal_path.read_bytes()
        for phase in sorted(installer_module.PHASES - {"COMPLETE"}):
            with self.subTest(phase=phase):
                journal = json.loads(original)
                journal["phase"] = phase
                installer_module.storage.atomic_json(old.journal_path, journal)
                before = old.journal_path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Only a completed"):
                    self.rebind_installer().apply()
                self.assertEqual(old.journal_path.read_bytes(), before)
                old.journal_path.write_bytes(original)

    def test_rebind_refuses_live_policy_or_envelope_drift(self):
        old = self.completed_old_pin_install()
        policy_bytes = old.policy_path.read_bytes()
        envelope_bytes = old.authority_path.read_bytes()
        for drift in ("policy", "envelope"):
            with self.subTest(drift=drift):
                old.policy_path.write_bytes(policy_bytes)
                old.authority_path.write_bytes(envelope_bytes)
                self.native.default_policy = copy.deepcopy(installer_module.validate_policy(
                    json.loads(policy_bytes)))
                path = old.policy_path if drift == "policy" else old.authority_path
                if drift == "policy":
                    changed_policy = copy.deepcopy(installer_module.CORE_DISABLED_POLICY)
                    changed_policy["classes"]["managed_generated"]["idle_threshold_secs"] = 42
                    path.write_text(json.dumps(changed_policy))
                    path.chmod(0o600)
                    self.native.default_policy = changed_policy
                else:
                    path.write_bytes(b"different native envelope\n")
                before_journal = old.journal_path.read_bytes()
                with self.assertRaises(ValueError):
                    self.rebind_installer().apply()
                self.assertEqual(old.journal_path.read_bytes(), before_journal)

    def test_rebind_refuses_pressure_floor_drift_and_unverified_current_receipt(self):
        old = self.completed_old_pin_install()
        self.native.authority["min_pressure"] = "EMERGENCY"
        with self.assertRaisesRegex(ValueError, "recorded managed install"):
            self.rebind_installer().apply()
        self.native.authority["min_pressure"] = "CRITICAL"
        installer = self.rebind_installer()
        installer._verify_rebind_runtime = lambda: (_ for _ in ()).throw(
            ValueError("current receipt verification failed"))
        with self.assertRaisesRegex(ValueError, "receipt verification failed"):
            installer.apply()
        self.assertEqual(json.loads(old.journal_path.read_text())["pin_revision"],
                         installer_module.SUPPORTED_REBIND[0])

    def test_rebind_refuses_changed_source_policy_and_corrupt_lineage(self):
        old = self.completed_old_pin_install()
        changed = json.loads(installer_module.SOURCE.read_text())
        changed["classes"]["managed_generated"]["idle_threshold_secs"] += 1
        changed_source = Path(self.temp.name) / "changed-policy.json"
        changed_source.write_text(json.dumps(changed))
        with patch.object(installer_module, "SOURCE", changed_source):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                installer_module.ResourcePolicyInstaller(
                    self.home, Path("/verified/fake-glomeris"), self.native).apply()
        installer = self.rebind_installer()
        self.assertEqual(installer.apply(), "pin rebound")
        journal = json.loads(installer.journal_path.read_text())
        journal["pin_rebindings"][0]["target_binary_sha256"] = "not-a-digest"
        installer_module.storage.atomic_json(installer.journal_path, journal)
        with self.assertRaisesRegex(ValueError, "lineage"):
            self.install().apply()

    def test_schema_two_journal_accepts_same_pin_rollback_phase_without_optional_fields(self):
        self.completed_old_pin_install()
        installer = self.rebind_installer()
        self.assertEqual(installer.apply(), "pin rebound")
        journal = json.loads(installer.journal_path.read_text())
        journal["phase"] = "ENV_RESTORED"
        journal.pop("rollback_policy_before", None)
        journal.pop("rollback_policy_restore_intent", None)
        validated = installer.validate_journal(journal)
        self.assertEqual(validated["phase"], "ENV_RESTORED")
        self.assertIsNone(validated["rollback_policy_before"])
        self.assertIsNone(validated["rollback_policy_restore_intent"])


if __name__ == "__main__":
    unittest.main()
