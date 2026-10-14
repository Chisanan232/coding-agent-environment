#!/usr/bin/env python3
"""Hermetic real-Glomeris wire checks; all native state lives in TemporaryDirectory."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POLICY_REL = Path("Library/Application Support/Glomeris/resources/policy.json")
AUTHORITY_REL = Path("Library/Application Support/Glomeris/autopilot.conf")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER = load_module("native_wire_resource_policy_installer", ROOT / "scripts/resource-policy-install.py")
if "GLOMERIS_NATIVE_WIRE_BINARY" in os.environ:
    BINARY = Path(os.environ["GLOMERIS_NATIVE_WIRE_BINARY"]).resolve()
else:
    installed, installation_status = INSTALLER.resolver.resolve(Path.home(), INSTALLER.storage.manifest())
    # Optional local execution uses the same verified receipt as either host;
    # CI's explicit required binary still fails if unavailable or mismatched.
    BINARY = Path(installed) if installation_status == "verified_installation" else ROOT / "native-core/target/debug/glomeris"

REVISION_PATTERN = re.compile(
    r"\brev\s*(?:(?:v\d+\.\d+\.\d+-\d+-g)?(?P<sha>[0-9a-f]{7,40})(?:-dirty)?)(?=[)\s]|$)"
)


def reported_revision(version):
    match = REVISION_PATTERN.search(version)
    if match is None:
        raise ValueError("Glomeris version must report a git-describe or hexadecimal source revision")
    return match.group("sha")


class VersionRevisionFormatTests(unittest.TestCase):
    def test_git_describe_and_shallow_sha_formats_require_a_revision(self):
        self.assertEqual(reported_revision("glomeris 0.2.0 (dev build, rev v0.2.0-1168-g911563e)"),
                         "911563e")
        self.assertEqual(reported_revision("glomeris 0.2.0 (dev build, rev 911563e)"), "911563e")
        for value in ("glomeris 0.2.0 (dev build)", "glomeris 0.2.0 (rev v0.2.0)",
                      "glomeris 0.2.0 (rev 91156xz)"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reported_revision(value)


class NativeRestoreCrash(BaseException):
    pass


class NativeWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
            if "GLOMERIS_NATIVE_WIRE_BINARY" in os.environ:
                raise AssertionError(f"required native wire binary unavailable: {BINARY}")
            raise unittest.SkipTest(f"optional local native wire binary unavailable: {BINARY}")
        cls.binary_sha256 = hashlib.sha256(BINARY.read_bytes()).hexdigest()
        probe = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True, timeout=10)
        cls.binary_version = (probe.stdout + probe.stderr).strip()
        cls.pin = INSTALLER.storage.manifest()
        try:
            source_revision = reported_revision(cls.binary_version)
        except ValueError as exc:
            raise AssertionError(f"debug binary did not report its source revision: {cls.binary_version!r}") from exc
        if not cls.pin["revision"].startswith(source_revision):
            raise AssertionError(f"binary revision {source_revision} does not match configured source pin")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="glomeris-native-wire-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = dict(os.environ, HOME=str(self.home), LC_ALL="C", TZ="UTC")
        self.env["RUSTUP_HOME"] = str(Path.home() / ".rustup")
        for key in list(self.env):
            if key.startswith("CARGO_") or key in {"RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
                                                   "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}:
                self.env.pop(key)

    def core(self, *args, env=None, check=True):
        result = subprocess.run([str(BINARY), *map(str, args)], env=env or self.env,
                                capture_output=True, text=True, timeout=30)
        if check and result.returncode:
            self.fail(f"Glomeris {args!r} failed ({result.returncode}): {result.stderr}\n{result.stdout}")
        return result

    def install_actual_adapter_bootstrap(self):
        scripts = self.home / ".claude/skills/engineering-runtime/scripts"
        scripts.mkdir(parents=True, mode=0o700)
        container = self.home / ".local/share/coding-agent-environment"
        managed = container / "storage"
        binary_dir = managed / "glomeris" / self.binary_sha256 / "bin"
        binary_dir.mkdir(parents=True, mode=0o700)
        for directory in (self.home / ".local", self.home / ".local/share", container,
                          managed, managed / "glomeris", binary_dir.parent, binary_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        fixture_binary = binary_dir / "glomeris"
        shutil.copyfile(BINARY, fixture_binary)
        fixture_binary.chmod(0o755)
        receipt = {"source_revision": self.pin["revision"], "binary_sha256": self.binary_sha256,
                   "path": str(fixture_binary)}
        for receipt_path in (managed / "storage-core.json", binary_dir.parent / "artifact.json"):
            receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
            receipt_path.chmod(0o600)
        self.assertEqual(INSTALLER.resolver.resolve(self.home, self.pin),
                         (str(fixture_binary), "verified_installation"))
        self.assertEqual(json.loads((managed / "storage-core.json").read_text())["source_revision"],
                         self.pin["revision"])
        # The actual resolver validates this fixture receipt, executable digest,
        # private layout, and exact source pin before the adapter may launch.
        bootstrap = f'''#!/usr/bin/env python3
import importlib.util, pathlib, shlex
spec = importlib.util.spec_from_file_location("storage_resolver", {str(ROOT / '.claude/skills/engineering-runtime/scripts/storage.py')!r})
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
binary, status = resolver.resolve(pathlib.Path.home(), {self.pin!r})
if status != "verified_installation":
    raise SystemExit("test fixture refused unverified executable receipt")
print("export ENGINEERING_STORAGE_BIN=" + shlex.quote(binary))
print("export ENGINEERING_STORAGE_STATUS=" + status)
'''
        (scripts / "bootstrap.py").write_text(bootstrap)

    def git_source(self):
        source = self.root / "workspace"
        source.mkdir()
        (source / "src").mkdir()
        (source / "Cargo.toml").write_text('[package]\nname = "native-wire-fixture"\nversion = "0.1.0"\nedition = "2021"\n')
        (source / "src/lib.rs").write_text("pub fn generated_value() -> u32 { 7 }\n")
        subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Native Wire Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "native-wire@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "Cargo.toml", "src/lib.rs"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture source"], check=True)
        return source

    def create_produced_generation(self):
        source = self.git_source()
        created = json.loads(self.core("resource", "create", "--source-root", source,
                                       "--recipe", "native wire isolated fixture").stdout)
        produced = self.core("resource", "produce", created["id"], created["generation"],
                             "--input", "src/lib.rs")
        self.assertIn('"status":"produced"', produced.stdout.replace(" ", ""))
        output = Path(created["path"]) / "artifact.o"
        self.assertTrue(output.is_file())
        return created, output

    def test_adapter_real_hold_register_release_preserves_exposed_outputs(self):
        created, artifact = self.create_produced_generation()
        self.install_actual_adapter_bootstrap()
        adapter_env = dict(self.env,
                           CODING_AGENT_MANAGED_RESOURCE_ID=created["id"],
                           CODING_AGENT_MANAGED_RESOURCE_GENERATION=created["generation"],
                           CODING_AGENT_RESOURCE_SESSION_ID="native-wire-fixture-session",
                           ENGINEERING_STORAGE_BIN="/must-not-be-used")
        child = [sys.executable, "-c", "import time; time.sleep(0.65)"]
        run = subprocess.run([sys.executable, str(ROOT / "bin/coding-agent-resource"),
                              "--host", "claude", "--", *child], env=adapter_env,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(artifact.is_file(), "native release must not delete exposed outputs")

        record_path = self.home / "Library/Application Support/Glomeris/resources/records" / f"{created['id']}.json"
        record = json.loads(record_path.read_text())
        self.assertEqual(record["generation"], created["generation"])
        self.assertTrue(record["claims"][-1]["released"])
        self.assertFalse(record["producer"]["closed_consumers"])
        self.assertIsNone(record["sealed"], "native exposure permanently prevents automatic sealing")

    def test_core_export_retains_independent_native_consumer_copy(self):
        created, artifact = self.create_produced_generation()
        destination = self.root / "consumer-artifact.o"
        source_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        result = self.core("resource", "export", created["id"], created["generation"],
                           "--artifact", "artifact.o", "--destination", destination)
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(destination.stat().st_ino, artifact.stat().st_ino)
        consumer = subprocess.run(
            [sys.executable, "-c", "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
             str(destination)], env=self.env, capture_output=True, text=True, check=True)
        self.assertEqual(consumer.stdout.strip(), source_hash)
        record_path = self.home / "Library/Application Support/Glomeris/resources/records" / f"{created['id']}.json"
        record = json.loads(record_path.read_text())
        self.assertTrue(record["producer"]["closed_consumers"])
        self.assertTrue(record["read_claims"][-1]["ended"])
        self.assertTrue(record["read_claims"][-1]["exported"])
        self.assertNotEqual(self.core("resource", "export", created["id"], created["generation"],
                                   "--artifact", "artifact.o", "--destination", destination,
                                   check=False).returncode, 0)
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), source_hash)

    def test_real_native_completed_pin_rebind_preserves_authority_and_rollback(self):
        self.install_actual_adapter_bootstrap()
        installer = INSTALLER.ResourcePolicyInstaller(self.home, BINARY)
        self.assertEqual(installer.apply(), "installed")
        journal = json.loads(installer.journal_path.read_text())
        journal["pin_revision"] = "f98389017b8ea52035d25bb21598711fd5f882a0"
        INSTALLER.storage.atomic_json(installer.journal_path, journal)
        before = {p: p.read_bytes() for p in (installer.policy_path, installer.authority_path)}
        original_before = journal["before"]
        fixture_binary, status = INSTALLER.resolver.resolve(self.home, self.pin)
        self.assertEqual(status, "verified_installation")
        installer.binary = Path(fixture_binary)
        self.assertEqual(installer.apply(), "pin rebound")
        rebound = json.loads(installer.journal_path.read_text())
        self.assertEqual(rebound["before"], original_before)
        self.assertEqual(rebound["schema"], 2)
        self.assertEqual(rebound["pin_revision"], self.pin["revision"])
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(installer.apply(), "already installed")
        self.assertEqual(installer.inspect(), 0)
        self.assertEqual(installer.rollback(), "rolled back")
        self.assertEqual(json.loads(self.core("resource", "policy", "show").stdout),
                         INSTALLER.CORE_DISABLED_POLICY)
        self.assertFalse(json.loads(self.core("autopilot", "show", "--json").stdout)["enabled"])

    def test_native_rebind_refuses_previous_receipt_fallback(self):
        self.install_actual_adapter_bootstrap()
        installer = INSTALLER.ResourcePolicyInstaller(self.home, BINARY)
        installer.apply()
        journal = json.loads(installer.journal_path.read_text())
        journal["pin_revision"] = "f98389017b8ea52035d25bb21598711fd5f882a0"
        INSTALLER.storage.atomic_json(installer.journal_path, journal)
        selection_path = self.home / INSTALLER.storage.RUNTIME / "storage-core.json"
        valid = json.loads(selection_path.read_text())
        invalid = dict(valid, source_revision="0" * 40)
        INSTALLER.storage.atomic_json(selection_path, {"schema": 1, "current": invalid, "previous": valid})
        resolved, status = INSTALLER.resolver.resolve(self.home, self.pin)
        self.assertEqual(status, "verified_installation")
        self.assertEqual(resolved, valid["path"], "general resolver deliberately permits verified previous")
        installer.binary = Path(valid["path"])
        paths = (installer.journal_path, installer.policy_path, installer.authority_path)
        before = {path: path.read_bytes() for path in paths}
        for operation in (installer.inspect, installer.apply):
            with self.assertRaisesRegex(ValueError, "Current.*receipt"):
                operation()
            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_resource_policy_installer_cas_apply_check_and_rollback_absent_policy(self):
        policy_path = self.home / POLICY_REL
        authority_path = self.home / AUTHORITY_REL
        self.assertFalse(policy_path.exists())
        self.assertFalse(authority_path.exists())
        installer = INSTALLER.ResourcePolicyInstaller(self.home, BINARY)

        self.assertEqual(installer.apply(), "installed")
        installed_policy = json.loads(self.core("resource", "policy", "show").stdout)
        self.assertEqual(installed_policy, installer.desired)
        report = json.loads(self.core("autopilot", "show", "--json").stdout)
        self.assertEqual(report["stored_at"], str(authority_path))
        self.assertTrue(report["enabled"])
        self.assertEqual(report["allowed_kinds"], ["managed_generated"])
        self.assertTrue(report["respond_to_alerts"])
        self.assertTrue(report["starts_unprompted"])
        self.assertEqual(installer.inspect(), 0)

        self.assertEqual(installer.rollback(), "rolled back")
        # Core restore of an absent snapshot writes its canonical defaults instead
        # of unlinking either native-owned file.
        self.assertTrue(policy_path.is_file())
        restored_policy = json.loads(self.core("resource", "policy", "show").stdout)
        self.assertNotEqual(restored_policy, installer.desired)
        restored_authority = json.loads(self.core("autopilot", "show", "--json").stdout)
        self.assertFalse(restored_authority["enabled"])
        self.assertEqual(restored_authority["allowed_kinds"], [])
        self.assertFalse(restored_authority["respond_to_alerts"])
        self.assertFalse(restored_authority["starts_unprompted"])
        self.assertEqual(installer.rollback(), "already rolled back")

    def test_real_native_absent_policy_restore_interruption_resumes(self):
        policy_path = self.home / POLICY_REL
        authority_path = self.home / AUTHORITY_REL
        self.assertFalse(policy_path.exists())
        self.assertFalse(authority_path.exists())
        installer = INSTALLER.ResourcePolicyInstaller(self.home, BINARY)
        self.assertEqual(installer.apply(), "installed")

        def crash_after_real_restore(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            if command[1:4] == ["resource", "policy", "restore"]:
                self.assertEqual(result.returncode, 0, result.stderr)
                raise NativeRestoreCrash("interrupted after native policy CAS, before journal capture")
            return result

        installer.runner = crash_after_real_restore
        with self.assertRaises(NativeRestoreCrash):
            installer.rollback()
        journal = json.loads(installer.journal_path.read_text())
        self.assertEqual(journal["phase"], "ENV_RESTORED")
        self.assertTrue(journal["rollback_policy_restore_intent"])
        self.assertEqual(journal["rollback_policy_before"]["exists"], True)
        self.assertTrue(policy_path.is_file())
        self.assertEqual(json.loads(self.core("resource", "policy", "show").stdout),
                         INSTALLER.CORE_DISABLED_POLICY)

        installer.runner = subprocess.run
        self.assertEqual(installer.rollback(), "rolled back")
        self.assertEqual(json.loads(self.core("resource", "policy", "show").stdout),
                         INSTALLER.CORE_DISABLED_POLICY)
        self.assertEqual(installer.rollback(), "already rolled back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
