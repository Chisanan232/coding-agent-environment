import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/storage-integration.py"
SPEC = importlib.util.spec_from_file_location("storage_integration", SCRIPT)
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)
REPO = SCRIPT.parents[1]
RUNTIME_SKILL = REPO / ".claude/skills/engineering-runtime"
RESOLVER_SPEC = importlib.util.spec_from_file_location(
    "engineering_storage_resolver", RUNTIME_SKILL / "scripts/storage.py"
)
resolver_core = importlib.util.module_from_spec(RESOLVER_SPEC)
RESOLVER_SPEC.loader.exec_module(resolver_core)


class FakeRunner:
    origin = "https://github.com/Chisanan232/glomeris"
    head = storage.manifest()["revision"]
    dirty = ""
    payload = b"fake-glomeris-runtime"
    fail_build = False
    cargo_calls = 0
    description = head[:7]
    version_output = None
    status_output = '{"total_bytes":100,"free_bytes":20,"used_percent":80.0,"pressure_state":"PRESSURED"}'

    def __call__(self, command, cwd=None, capture_output=False, text=False):
        if command[0] == "git":
            if command[1:] == ["remote", "get-url", "origin"]:
                output = self.origin
            elif command[1:] == ["rev-parse", "HEAD"]:
                output = self.head
            elif command[1:] == ["status", "--porcelain", "--untracked-files=all"]:
                output = self.dirty
            elif command[1:] == ["describe", "--tags", "--always"]:
                output = self.description
            elif command[1:2] == ["clone"]:
                Path(command[-1]).mkdir()
                output = ""
            elif command[1:2] == ["checkout"]:
                output = ""
            else:
                return Result(2, "", "unexpected fixture git command")
        elif command[0] == "cargo":
            self.cargo_calls += 1
            if self.fail_build:
                return Result(1, "", "fixture build failed; TOKEN=fixture-secret")
            target = Path(command[command.index("--target-dir") + 1])
            binary = target / "release/glomeris"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(self.payload)
            output = ""
        elif command[1:] == ["--version"]:
            version = storage.manifest()["version"]
            output = self.version_output or f"glomeris {version} (dev build, rev {self.description})"
        elif command[1:] == ["status", "--json"]:
            output = self.status_output
        else:
            return Result(2, "", "unexpected fixture command")
        return Result(0, output, "")


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class StorageIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.runner = FakeRunner()

    def tearDown(self):
        self.temp.cleanup()

    def receipt_path(self):
        return self.home / storage.RUNTIME / "storage-core.json"

    def write_private_json(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def create_installed_fixture(self, home):
        pin = storage.manifest()
        payload = b"fixture-installed-glomeris"
        checksum = hashlib.sha256(payload).hexdigest()
        base = home / storage.RUNTIME
        binary = base / "glomeris" / checksum / "bin/glomeris"
        binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (base, base / "glomeris", binary.parents[1], binary.parent):
            directory.chmod(0o700)
        binary.write_bytes(payload)
        binary.chmod(0o755)
        receipt = {
            "source_revision": pin["revision"],
            "binary_sha256": checksum,
            "path": str(binary),
        }
        self.write_private_json(base / "glomeris" / checksum / "artifact.json", receipt)
        self.write_private_json(base / "storage-core.json", receipt)
        return pin, receipt, binary

    def materialize_runtime(self, project, host_name):
        source_skill = RUNTIME_SKILL
        host_root = project / host_name
        skill = host_root / "skills/engineering-runtime"
        shutil.copytree(source_skill, skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shared_manifest = json.loads((REPO / "codex/shared-skills.json").read_text())
        resources = shared_manifest["resources"]["engineering-runtime"]
        for skill_relative, repo_source in resources.items():
            source = REPO / repo_source
            target = skill / skill_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return skill

    def run_bootstrap(self, project, skill, home):
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CLAUDE_API_KEY": "fixture-secret-must-not-be-exported",
        }
        result = subprocess.run(
            [sys.executable, str(skill / "scripts/bootstrap.py")],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fixture-secret-must-not-be-exported", result.stdout)
        self.assertNotIn("fixture-secret-must-not-be-exported", result.stderr)
        exports = {}
        for line in result.stdout.splitlines():
            words = shlex.split(line)
            self.assertEqual(len(words), 2)
            self.assertEqual(words[0], "export")
            key, value = words[1].split("=", 1)
            exports[key] = value
        return exports

    def invoke(self, *args):
        old = os.environ.get("CODING_AGENT_SYNC_HOME")
        os.environ["CODING_AGENT_SYNC_HOME"] = str(self.home)
        try:
            return storage.main(list(args), self.runner)
        finally:
            if old is None:
                os.environ.pop("CODING_AGENT_SYNC_HOME", None)
            else:
                os.environ["CODING_AGENT_SYNC_HOME"] = old

    def test_read_only_modes_report_without_building(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.invoke("--dry-run"), 0)
        self.assertEqual(self.invoke("--check"), 1)
        self.assertFalse(self.receipt_path().exists())
        self.assertEqual(self.runner.cargo_calls, 0)

    def test_apply_is_repeatable_and_receipt_records_full_provenance(self):
        self.assertEqual(storage.install(self.home, self.checkout, self.runner), "installed")
        before = self.receipt_path().read_bytes()
        receipt = storage.read_selection(self.receipt_path())[0]
        self.assertEqual(receipt["source_revision"], self.runner.head)
        self.assertEqual(receipt["binary_sha256"], hashlib.sha256(self.runner.payload).hexdigest())
        self.assertEqual(Path(receipt["path"]).read_bytes(), self.runner.payload)
        self.assertEqual(self.runner.cargo_calls, 1)
        self.assertEqual(storage.install(self.home, self.checkout, self.runner), "installed")
        self.assertEqual(self.receipt_path().read_bytes(), before)
        self.assertEqual(storage.receipt_state(self.receipt_path()), "installed")
        self.assertEqual(self.runner.cargo_calls, 1)

    def test_refuses_dirty_or_mismatched_checkout(self):
        for field, value in (("dirty", "?? extra"), ("head", "0" * 40), ("origin", "https://example.com/fork")):
            original = getattr(self.runner, field)
            setattr(self.runner, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                storage.install(self.home, self.checkout, self.runner)
            setattr(self.runner, field, original)
        self.assertFalse(self.receipt_path().exists())

    def test_refuses_unowned_destination_and_receipt_drift(self):
        checksum = hashlib.sha256(self.runner.payload).hexdigest()
        destination = self.home / storage.RUNTIME / "glomeris" / checksum / "bin/glomeris"
        storage.ensure_private_dir(self.home / storage.RUNTIME)
        storage.ensure_private_dir(destination.parents[2])
        storage.ensure_private_dir(destination.parents[1])
        storage.ensure_private_dir(destination.parent)
        destination.write_bytes(self.runner.payload)
        with self.assertRaisesRegex(ValueError, "unowned"):
            storage.install(self.home, self.checkout, self.runner)
        shutil.rmtree(destination.parents[1])
        storage.install(self.home, self.checkout, self.runner)
        receipt_before = self.receipt_path().read_bytes()
        Path(storage.read_selection(self.receipt_path())[0]["path"]).write_bytes(b"drift")
        with self.assertRaisesRegex(ValueError, "drift"):
            storage.install(self.home, self.checkout, self.runner)
        self.assertEqual(self.receipt_path().read_bytes(), receipt_before)

    def test_symlinked_storage_ancestry_and_active_lock_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        local = self.home / ".local"
        local.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            storage.install(self.home, self.checkout, self.runner)
        local.unlink()
        storage.ensure_private_dir(self.home / storage.RUNTIME)
        lock = self.home / storage.RUNTIME / "storage-core.lock"
        held = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(BlockingIOError, "Another storage integration"):
                storage.install(self.home, self.checkout, self.runner)
        finally:
            os.close(held)

    def test_rollback_restores_only_verified_previous_receipt(self):
        storage.install(self.home, self.checkout, self.runner)
        first = self.receipt_path().read_bytes()
        second_revision = "b" * 40
        second_spec = dict(storage.manifest(), revision=second_revision)
        self.runner.head = second_revision
        self.runner.description = second_revision[:7]
        self.runner.payload = b"second-fake-runtime"
        with patch.object(storage, "manifest", return_value=second_spec):
            storage.install(self.home, self.checkout, self.runner)
        self.assertNotEqual(self.receipt_path().read_bytes(), first)
        self.assertEqual(storage.rollback(self.home), "rolled back")
        self.assertEqual(storage.read_selection(self.receipt_path())[0], json.loads(first)["current"])
        self.assertEqual(storage.rollback(self.home), "rolled back")

    def test_failed_build_leaves_previous_receipt_unchanged(self):
        storage.install(self.home, self.checkout, self.runner)
        before = self.receipt_path().read_bytes()
        second_revision = "b" * 40
        second_spec = dict(storage.manifest(), revision=second_revision)
        self.runner.head = second_revision
        self.runner.description = second_revision[:7]
        for field, value, error in (("fail_build", True, "cargo failed"),
                                    ("version_output", "glomeris 9.9.9", "version does not match"),
                                    ("status_output", "not json", "did not return JSON"),
                                    ("status_output", "{}", "command contract 1"),
                                    ("status_output", "null", "command contract 1")):
            original = getattr(self.runner, field)
            setattr(self.runner, field, value)
            with patch.object(storage, "manifest", return_value=second_spec):
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                    storage.install(self.home, self.checkout, self.runner)
            setattr(self.runner, field, original)
            self.assertEqual(self.receipt_path().read_bytes(), before)

    def test_crash_after_package_publication_recovers_from_artifact_receipt(self):
        atomic_json = storage.atomic_json

        def fail_primary_receipt(path, value):
            if path == self.receipt_path():
                raise OSError("simulated publication interruption")
            return atomic_json(path, value)

        with patch.object(storage, "atomic_json", side_effect=fail_primary_receipt):
            with self.assertRaisesRegex(OSError, "publication interruption"):
                storage.install(self.home, self.checkout, self.runner)
        self.assertFalse(self.receipt_path().exists())
        packages = list((self.home / storage.RUNTIME / "glomeris").glob("*/artifact.json"))
        self.assertEqual(len(packages), 1)
        storage.install(self.home, self.checkout, self.runner)
        self.assertEqual(storage.receipt_state(self.receipt_path()), "installed")

    def test_upgrade_rollback_and_reapply_reuse_immutable_packages(self):
        first_spec = storage.manifest()
        storage.install(self.home, self.checkout, self.runner)
        first = self.receipt_path().read_bytes()
        second_revision = "b" * 40
        second_spec = dict(first_spec, revision=second_revision)
        self.runner.head = second_revision
        self.runner.description = second_revision[:7]
        self.runner.payload = b"second-pinned-runtime"
        with patch.object(storage, "manifest", return_value=second_spec):
            storage.install(self.home, self.checkout, self.runner)
            second = self.receipt_path().read_bytes()
            self.assertNotEqual(second, first)
            self.assertEqual(storage.rollback(self.home), "rolled back")
            self.assertEqual(storage.read_selection(self.receipt_path())[0], json.loads(first)["current"])
            self.assertEqual(self.invoke_check_with_manifest(second_spec), 1)
            storage.install(self.home, self.checkout, self.runner)
            self.assertEqual(self.receipt_path().read_bytes(), second)
        self.assertEqual(storage.read_selection(self.receipt_path())[1], json.loads(first)["current"])
        packages = list((self.home / storage.RUNTIME / "glomeris").glob("*/artifact.json"))
        self.assertEqual(len(packages), 2)
        expected_receipts = (json.loads(first)["current"], json.loads(second)["current"])
        for package in packages:
            artifact_receipt = storage.read_receipt(package)
            self.assertIn(artifact_receipt, expected_receipts)
            artifact_binary = Path(artifact_receipt["path"])
            self.assertTrue(artifact_binary.is_file())
            self.assertEqual(storage.digest(artifact_binary), artifact_receipt["binary_sha256"])

    def invoke_check_with_manifest(self, spec):
        with patch.object(storage, "manifest", return_value=spec):
            return self.invoke("--check")

    def test_failed_third_upgrade_preserves_prior_rollback_target(self):
        first_spec = storage.manifest()
        storage.install(self.home, self.checkout, self.runner)
        first = storage.read_selection(self.receipt_path())[0]
        for revision, payload in (("b" * 40, b"second"), ("c" * 40, b"third")):
            self.runner.head = revision
            self.runner.description = revision[:7]
            self.runner.payload = payload
            spec = dict(first_spec, revision=revision)
            with patch.object(storage, "manifest", return_value=spec):
                if revision.startswith("b"):
                    storage.install(self.home, self.checkout, self.runner)
                    before = self.receipt_path().read_bytes()
                else:
                    atomic = storage.atomic_json
                    def fail_selection(path, value):
                        if path == self.receipt_path():
                            raise OSError("fixture interrupted selection")
                        return atomic(path, value)
                    with patch.object(storage, "atomic_json", side_effect=fail_selection), self.assertRaises(OSError):
                        storage.install(self.home, self.checkout, self.runner)
        self.assertEqual(self.receipt_path().read_bytes(), before)
        self.assertEqual(storage.read_selection(self.receipt_path())[1], first)
        storage.rollback(self.home)
        self.assertEqual(storage.read_selection(self.receipt_path())[0], first)
        after = self.receipt_path().read_bytes()
        storage.rollback(self.home)
        self.assertEqual(self.receipt_path().read_bytes(), after)

    def test_reapply_refuses_unsafe_retained_package_without_selection_change(self):
        storage.install(self.home, self.checkout, self.runner)
        spec = dict(storage.manifest(), revision="b" * 40)
        self.runner.head = spec["revision"]
        self.runner.description = "bbbbbbb"
        self.runner.payload = b"retained-runtime"
        with patch.object(storage, "manifest", return_value=spec):
            storage.install(self.home, self.checkout, self.runner)
            retained = storage.read_selection(self.receipt_path())[0]
            storage.rollback(self.home)
            before = self.receipt_path().read_bytes()
            binary = Path(retained["path"])
            for target, unsafe, safe in ((binary, 0o777, 0o755), (binary.parents[1], 0o777, 0o700)):
                target.chmod(unsafe)
                with self.assertRaises(ValueError):
                    storage.install(self.home, self.checkout, self.runner)
                self.assertEqual(self.receipt_path().read_bytes(), before)
                target.chmod(safe)

    def test_rollback_recovers_damaged_current_but_refuses_damaged_target(self):
        storage.install(self.home, self.checkout, self.runner)
        first = storage.read_selection(self.receipt_path())[0]
        spec = dict(storage.manifest(), revision="b" * 40)
        self.runner.head = spec["revision"]
        self.runner.description = "bbbbbbb"
        self.runner.payload = b"second-runtime"
        with patch.object(storage, "manifest", return_value=spec):
            storage.install(self.home, self.checkout, self.runner)
        current = storage.read_selection(self.receipt_path())[0]
        Path(current["path"]).write_bytes(b"damaged")
        self.assertEqual(storage.receipt_state(self.receipt_path()), "drift")
        storage.rollback(self.home)
        self.assertEqual(storage.read_selection(self.receipt_path())[0], first)
        Path(first["path"]).write_bytes(b"damaged-target")
        before = self.receipt_path().read_bytes()
        with self.assertRaisesRegex(ValueError, "drift"):
            storage.rollback(self.home)
        self.assertEqual(self.receipt_path().read_bytes(), before)

    def test_both_host_bootstraps_refuse_unsafe_permissions(self):
        for category in ("receipt", "artifact", "base", "package", "binary"):
            home = self.root / ("permissions-" + category)
            home.mkdir(mode=0o700)
            pin, _, binary = self.create_installed_fixture(home)
            base = home / storage.RUNTIME
            target = {"receipt": base / "storage-core.json", "artifact": binary.parents[1] / "artifact.json",
                      "base": base, "package": binary.parents[1], "binary": binary}[category]
            target.chmod(0o777 if target.is_dir() else 0o666)
            self.assertEqual(resolver_core.resolve(home, pin), ("", "refused"))
            with self.assertRaises(ValueError):
                storage.receipt_state(base / "storage-core.json")
            for host in (".claude", ".codex"):
                project = self.root / (category + host)
                project.mkdir()
                skill = self.materialize_runtime(project, host)
                exports = self.run_bootstrap(project, skill, home)
                self.assertEqual(exports["ENGINEERING_STORAGE_BIN"], "")
                self.assertEqual(exports["ENGINEERING_STORAGE_STATUS"], "refused")

    def test_private_child_preserves_existing_shared_parent(self):
        parent = self.home / ".local/share/coding-agent-environment"
        parent.mkdir(parents=True, mode=0o755)
        parent.chmod(0o755)
        visual = parent / "visual-skills"
        visual.mkdir()
        marker = visual / "unrelated.txt"
        marker.write_text("existing visual configuration")
        storage.install(self.home, self.checkout, self.runner)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(marker.read_text(), "existing visual configuration")
        self.assertEqual((parent / "storage").stat().st_mode & 0o777, 0o700)
        self.assertEqual(storage.receipt_state(self.receipt_path()), "installed")
        self.assertEqual(resolver_core.resolve(self.home, storage.manifest())[1], "verified_installation")
        before = self.receipt_path().read_bytes()
        parent.chmod(0o777)
        with self.assertRaises(ValueError):
            storage.install(self.home, self.checkout, self.runner)
        self.assertEqual(self.receipt_path().read_bytes(), before)
        self.assertEqual(resolver_core.resolve(self.home, storage.manifest()), ("", "refused"))

    def test_changed_pin_makes_check_fail(self):
        storage.install(self.home, self.checkout, self.runner)
        changed = dict(storage.manifest(), revision="b" * 40)
        self.assertEqual(self.invoke_check_with_manifest(changed), 1)

    def test_symlinked_artifact_and_lock_are_refused(self):
        storage.install(self.home, self.checkout, self.runner)
        receipt = storage.read_selection(self.receipt_path())[0]
        artifact = Path(receipt["path"]).parents[1] / "artifact.json"
        outside = self.root / "outside-artifact.json"
        outside.write_text(artifact.read_text())
        artifact.unlink()
        artifact.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            storage.receipt_state(self.receipt_path())
        artifact.unlink()
        artifact.write_text(outside.read_text())
        artifact.chmod(0o600)

        current_receipt = self.receipt_path()
        current_bytes = current_receipt.read_bytes()
        current_receipt.unlink()
        current_receipt.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            storage.receipt_state(current_receipt)
        current_receipt.unlink()
        current_receipt.write_bytes(current_bytes)
        current_receipt.chmod(0o600)

        lock = self.home / storage.RUNTIME / "storage-core.lock"
        lock.unlink()
        lock.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            storage.install(self.home, self.checkout, self.runner)

    def test_failed_command_diagnostics_do_not_leak_output_credentials(self):
        def leaking_runner(*args, **kwargs):
            return Result(1, "TOKEN=stdout-secret", "TOKEN=stderr-secret")

        with self.assertRaises(ValueError) as caught:
            storage.run_checked(["cargo", "build"], leaking_runner)
        self.assertNotIn("stdout-secret", str(caught.exception))
        self.assertNotIn("stderr-secret", str(caught.exception))

    def test_both_host_bootstraps_export_the_shared_verified_storage_binary(self):
        pin, receipt, binary = self.create_installed_fixture(self.home)
        for host_name in (".claude", ".codex"):
            with self.subTest(host=host_name):
                project = self.root / ("project-" + host_name[1:])
                project.mkdir()
                skill = self.materialize_runtime(project, host_name)
                exports = self.run_bootstrap(project, skill, self.home)
                self.assertEqual(exports["ENGINEERING_STORAGE_BIN"], str(binary))
                self.assertEqual(exports["ENGINEERING_STORAGE_STATUS"], "verified_installation")
                resolver_spec = importlib.util.spec_from_file_location("storage_copy", skill / "scripts/storage.py")
                resolver = importlib.util.module_from_spec(resolver_spec)
                resolver_spec.loader.exec_module(resolver)
                self.assertEqual(resolver.resolve(self.home, pin), (str(binary), "verified_installation"))
                # A partial multi-file update must preserve existing workflow exports.
                (skill / "scripts/storage.py").unlink()
                fallback = self.run_bootstrap(project, skill, self.home)
                self.assertEqual(fallback["ENGINEERING_STORAGE_BIN"], "")
                self.assertEqual(fallback["ENGINEERING_STORAGE_STATUS"], "unavailable")
                self.assertEqual(fallback["ENGINEERING_RUNTIME"], str(skill / "scripts"))
        self.assertEqual(receipt["source_revision"], pin["revision"])

    def test_resolver_returns_empty_path_for_corrupt_unowned_and_symlinked_state(self):
        cases = ("corrupt_digest", "unowned_path", "symlinked_binary", "symlinked_receipt")
        for case in cases:
            with self.subTest(case=case):
                home = self.root / ("resolver-" + case)
                home.mkdir(mode=0o700)
                pin, receipt, binary = self.create_installed_fixture(home)
                receipt_path = home / storage.RUNTIME / "storage-core.json"
                external = self.root / (case + "-outside")
                external.write_bytes(binary.read_bytes())
                if case == "corrupt_digest":
                    binary.write_bytes(b"changed bytes")
                elif case == "unowned_path":
                    receipt["path"] = str(external)
                    self.write_private_json(receipt_path, receipt)
                elif case == "symlinked_binary":
                    binary.unlink()
                    binary.symlink_to(external)
                else:
                    receipt_path.unlink()
                    receipt_path.symlink_to(external)
                self.assertEqual(resolver_core.resolve(home, pin), ("", "refused"))

    def test_resolver_distinguishes_missing_and_stale_pins(self):
        missing_home = self.root / "missing-home"
        missing_home.mkdir(mode=0o700)
        pin = storage.manifest()
        self.assertEqual(resolver_core.resolve(missing_home, pin), ("", "missing"))

        stale_home = self.root / "stale-home"
        stale_home.mkdir(mode=0o700)
        installed_pin, _, _ = self.create_installed_fixture(stale_home)
        stale = dict(installed_pin, revision="b" * 40)
        self.assertEqual(resolver_core.resolve(stale_home, stale), ("", "outdated"))


class RollingPinnedResolver(unittest.TestCase):
    def setUp(self):
        self.fixture=StorageIntegration();self.fixture.setUp()
        self.home=self.fixture.home
        self.pin,self.old,self.old_binary=self.fixture.create_installed_fixture(self.home)
        self.base=self.home/storage.RUNTIME
    def tearDown(self):self.fixture.tearDown()
    def entry(self,revision,payload=b"new pinned runtime"):
        checksum=hashlib.sha256(payload).hexdigest()
        binary=self.base/"glomeris"/checksum/"bin/glomeris"
        binary.parent.mkdir(parents=True,mode=0o700)
        for directory in (binary.parent,binary.parents[1]):directory.chmod(0o700)
        binary.write_bytes(payload);binary.chmod(0o755)
        entry={"source_revision":revision,"binary_sha256":checksum,"path":str(binary)}
        self.fixture.write_private_json(binary.parents[1]/"artifact.json",entry)
        return entry,binary
    def selection(self,current,previous):
        self.fixture.write_private_json(self.fixture.receipt_path(),{"schema":1,"current":current,"previous":previous})
    def test_mixed_host_pins_keep_both_exact_revisions_verified(self):
        current,binary=self.entry("d"*40);self.selection(current,self.old)
        self.assertEqual(resolver_core.resolve(self.home,self.pin),(str(self.old_binary),"verified_installation"))
        self.assertEqual(resolver_core.resolve(self.home,dict(self.pin,revision="d"*40)),(str(binary),"verified_installation"))
        self.assertEqual(resolver_core.resolve(self.home,dict(self.pin,revision="e"*40)),("","outdated"))
    def test_matching_corrupt_current_never_falls_back(self):
        current,binary=self.entry(self.pin["revision"]);self.selection(current,self.old)
        binary.write_bytes(b"tampered")
        self.assertEqual(resolver_core.resolve(self.home,self.pin),("","refused"))
    def test_legacy_receipt_and_rollback_order(self):
        self.assertEqual(resolver_core.resolve(self.home,self.pin)[1],"verified_installation")
        current,_=self.entry("d"*40);self.selection(current,self.old)
        self.assertEqual(resolver_core.resolve(self.home,self.pin)[1],"verified_installation")
        self.selection(self.old,self.old)
        self.assertEqual(resolver_core.resolve(self.home,dict(self.pin,revision="d"*40)),("","outdated"))
        self.assertEqual(resolver_core.resolve(self.home,self.pin)[1],"verified_installation")
    def test_malformed_other_slot_and_redirected_matching_artifact_refuse(self):
        current,_=self.entry("d"*40);self.selection(current,{"source_revision":self.pin["revision"]})
        self.assertEqual(resolver_core.resolve(self.home,self.pin),("","refused"))
        self.selection(current,self.old)
        self.old_binary.unlink();self.old_binary.symlink_to(Path(current["path"]))
        self.assertEqual(resolver_core.resolve(self.home,self.pin),("","refused"))


if __name__ == "__main__":
    unittest.main()
