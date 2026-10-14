#!/usr/bin/env python3
"""Hermetic real-Glomeris wire checks; all native state lives in TemporaryDirectory."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("GLOMERIS_NATIVE_WIRE_BINARY", str(ROOT / "target/debug/glomeris"))).resolve()
POLICY_REL = Path("Library/Application Support/Glomeris/resources/policy.json")
AUTHORITY_REL = Path("Library/Application Support/Glomeris/autopilot.conf")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER = load_module("native_wire_resource_policy_installer", ROOT / "scripts/resource-policy-install.py")


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
        if not re.search(r"\brev v[0-9].*", cls.binary_version):
            raise AssertionError(f"debug binary did not report its source revision: {cls.binary_version!r}")

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
        # This test-only bootstrap accepts only the exact executable and revision
        # captured above; the adapter's ordinary binary override env var is poison.
        bootstrap = f'''#!/usr/bin/env python3
import hashlib, pathlib, subprocess, sys
binary = pathlib.Path({str(BINARY)!r})
expected_hash = {self.binary_sha256!r}
expected_version = {self.binary_version!r}
actual_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
probe = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
actual_version = (probe.stdout + probe.stderr).strip()
if actual_hash != expected_hash or actual_version != expected_version:
    raise SystemExit("test fixture refused binary digest/revision mismatch")
print("export ENGINEERING_STORAGE_BIN=" + __import__("shlex").quote(str(binary)))
print("export ENGINEERING_STORAGE_STATUS=verified_installation")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
