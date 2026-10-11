#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import importlib.util
import select
import time
from unittest.mock import patch
import unittest

SPEC = importlib.util.spec_from_file_location("compositor", Path(__file__).resolve().parents[1] / ".claude/statusline-compositor.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ROOT = Path(__file__).resolve().parents[1]
COMPOSITOR = ROOT / ".claude/statusline-compositor.py"


class StatuslineComposition(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.script("base.py", "import sys\np=sys.stdin.buffer.read()\nprint('BASE:' + p.decode())\n")

    def tearDown(self):
        self.temp.cleanup()

    def script(self, name, source):
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + source)
        path.chmod(0o700)
        return path

    def config(self, payload):
        path = self.root / "statusline.json"
        path.write_text(json.dumps(payload))
        return path

    def invoke(self, config=None, payload=b"payload", env=None, cwd=None):
        e = os.environ.copy()
        e.update({
            "CODING_AGENT_STATUSLINE_BASE": json.dumps([sys.executable, str(self.base)]),
            "HOME": str(self.root),
            "PATH": os.environ.get("PATH", ""),
            "TERM": "xterm-256color",
        })
        for name in tuple(e):
            if name.startswith("CODING_AGENT_") or name == "NO_COLOR":
                e.pop(name, None)
        e["CODING_AGENT_STATUSLINE_BASE"] = json.dumps([sys.executable, str(self.base)])
        if env:
            e.update(env)
        if config:
            e["CODING_AGENT_STATUSLINE_CONFIG"] = str(config)
        return subprocess.run(
            [sys.executable, str(COMPOSITOR)], input=payload, capture_output=True,
            env=e, cwd=cwd or self.root, timeout=5,
        )

    def test_base_only_is_byte_identical(self):
        result = self.invoke(payload=b"X")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"BASE:X\n")

    def test_extensions_are_additive_and_deterministic(self):
        slow = self.script("z-slow.py", "import time\ntime.sleep(.05)\nprint('EXT-A')\n")
        fast = self.script("a-fast.py", "print('EXT-B')\n")
        cfg = self.config({"extensions": [
            {"id": "extension-a", "argv": [sys.executable, str(slow)], "timeout_ms": 1500},
            {"id": "extension-b", "argv": [sys.executable, str(fast)], "timeout_ms": 1500},
        ], "deadline_ms": 2500})
        first = self.invoke(cfg).stdout
        second = self.invoke(cfg).stdout
        self.assertEqual(first, b"BASE:payload\nEXT-A\nEXT-B")
        self.assertEqual(second, first)

    def test_failure_timeout_and_invalid_extension_preserve_base(self):
        fail = self.script("fail.py", "import sys\nprint('private', file=sys.stderr)\nsys.exit(1)\n")
        slow = self.script("slow.py", "import time\ntime.sleep(2)\nprint('late')\n")
        cfg = self.config({"extensions": [
            {"id": "failure", "argv": [sys.executable, str(fail)]},
            {"id": "timeout", "argv": [sys.executable, str(slow)], "timeout_ms": 20},
        ]})
        result = self.invoke(cfg)
        self.assertEqual(result.stdout, b"BASE:payload\n")
        self.assertEqual(result.stderr, b"")

    def test_base_failure_uses_truthful_generic_fallback_and_keeps_extensions_additive(self):
        ext = self.script("extension.py", "import sys\nprint('EXTENSION', flush=True)\n")
        proc = subprocess.Popen([sys.executable, str(ext)], stdout=subprocess.PIPE, start_new_session=True)
        try:
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
            proc.wait(timeout=5)
            extension = MODULE.Extension("extension", (sys.executable, str(ext)), 250, (), "text")
            with patch.object(MODULE.subprocess, "Popen", return_value=proc):
                values = MODULE.run_extensions((extension,), False, 600)
            self.assertEqual(values, {"extension": "EXTENSION\n"})
            import io
            payload = json.dumps({"model": {"display_name": "Sonnet"}, "workspace": {"current_dir": "/tmp/project"}}).encode()
            output = io.StringIO()
            with patch.object(MODULE, "run", return_value=(False, "")), \
                 patch.object(MODULE, "read_config", return_value=MODULE.Config((extension,), 600)), \
                 patch.object(MODULE, "run_extensions", return_value=values), \
                 patch.object(MODULE, "marker_present", return_value=False), \
                 patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload))), \
                 patch.object(sys, "stdout", output):
                code = MODULE.main()
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "Claude Code · Sonnet · project\nEXTENSION")
            self.assertNotIn("tokens", output.getvalue())
            self.assertNotIn("cost", output.getvalue())
        finally:
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_base_timeout_uses_generic_fallback(self):
        slow = self.script("slow-base.py", "import time\ntime.sleep(10)\n")
        payload = json.dumps({"model": {"display_name": "Sonnet"}, "cwd": "/tmp/cold-start"}).encode()
        result = self.invoke(
            payload=payload,
            env={"CODING_AGENT_STATUSLINE_BASE": json.dumps([sys.executable, str(slow)])},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Claude Code · Sonnet · cold-start".encode())

    def test_hung_extension_and_descendant_are_killed_within_deadline(self):
        hung = self.script("hung.py", "import os, signal, subprocess, sys\nchild = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])\nprint(child.pid, flush=True)\nsignal.pause()\n")
        proc = subprocess.Popen([sys.executable, str(hung)], stdout=subprocess.PIPE,
                                start_new_session=True)
        try:
            # Establish readiness before timing; startup is not runner latency.
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
            descendant = int(proc.stdout.readline())
            os.set_blocking(proc.stdout.fileno(), False)
            ext = MODULE.Extension("hung", (sys.executable, str(hung)), 80, (), "text")
            started = time.monotonic()
            with patch.object(MODULE.subprocess, "Popen", return_value=proc):
                result = MODULE.run_extensions((ext,), False, 120)
            self.assertEqual(result, {})
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIsNotNone(proc.poll())
            # A killed child may briefly remain a zombie; neither state can run.
            state = subprocess.run(["ps", "-p", str(descendant), "-o", "stat="],
                                   capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)
        finally:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)
            proc.stdout.close()


    def test_sanitized_environment_does_not_expose_secret(self):
        ext = self.script("env.py", "import os\nprint(os.environ.get('SYNTH_SECRET', 'absent'))\n")
        cfg = self.config({"extensions": [{"id": "env", "argv": [sys.executable, str(ext)], "env": ["SYNTH_SECRET"]}]})
        result = self.invoke(cfg, env={"SYNTH_SECRET": "do-not-leak"})
        self.assertEqual(result.stdout, b"BASE:payload\n")

    def test_color_capability_and_machine_output_sanitization(self):
        ext = self.script("color.py", "print('\\033[31mCOLOR\\033[0m\\x00\\x01')\n")
        cfg = self.config({"extensions": [{"id": "color", "argv": [sys.executable, str(ext)], "timeout_ms": 1000}], "deadline_ms": 1200})
        color = self.invoke(cfg)
        self.assertIn(b"\x1b[31mCOLOR\x1b[0m", color.stdout)
        no_color = self.invoke(cfg, env={"NO_COLOR": "1", "TERM": "dumb", "CODING_AGENT_STATUSLINE_COLOR": "0"})
        self.assertEqual(no_color.stdout, b"BASE:payload\nCOLOR")
        self.assertNotIn(b"\x1b", no_color.stdout)
        self.assertNotIn(b"\x00", no_color.stdout)
        self.assertNotIn(b"\x01", no_color.stdout)

    def test_colored_base_honors_no_color_without_truncating_lines(self):
        self.base = self.script("base.py", "print('\\033[32m' + '\\n'.join(str(i) for i in range(12)) + '\\033[0m')\n")
        expected = "\n".join(str(i) for i in range(12)).encode() + b"\n"
        ext = self.script("extension.py", "print('EXTENSION')\n")
        cfg = self.config({"extensions": [{"id": "extension", "argv": [sys.executable, str(ext)], "timeout_ms": 1000}], "deadline_ms": 1500})
        for config in (None, cfg):
            with self.subTest(config=config):
                suffix = b"EXTENSION" if config else b""
                for env in ({"NO_COLOR": "1"}, {"CODING_AGENT_STATUSLINE_COLOR": "0"}, {"TERM": "dumb"}):
                    result = self.invoke(config, env=env)
                    self.assertEqual(result.stdout, expected + suffix)
                    self.assertNotIn(b"\x1b", result.stdout)
                colored = self.invoke(config)
                self.assertIn(b"\x1b[32m", colored.stdout)
                self.assertIn(b"11\x1b[0m", colored.stdout)

    def test_color_child_does_not_receive_no_color_sentinel(self):
        ext = self.script("env.py", "import os\nprint('PLAIN' if os.environ.get('NO_COLOR') else 'COLOR')\n")
        cfg = self.config({"extensions": [{"id": "color", "argv": [sys.executable, str(ext)], "timeout_ms": 1000}], "deadline_ms": 1200})
        self.assertEqual(self.invoke(cfg).stdout, b"BASE:payload\nCOLOR")
        self.assertEqual(self.invoke(cfg, env={"NO_COLOR": "1"}).stdout, b"BASE:payload\nPLAIN")

    def test_marker_bypass_warning_is_generic(self):
        marked = self.root / "marked"
        marked.mkdir()
        (marked / ".coding-agent-profile").write_text("alpha\n")
        result = self.invoke(cwd=marked)
        self.assertIn(b"Profile bypassed \xc2\xb7 use ca-claude", result.stdout)
        self.assertNotIn(b"alpha", result.stdout)

    def test_escape_controls_are_removed_but_color_is_preserved(self):
        text = '\x1b[2J\x1b]52;c;secret\x07\x1b[31mOK\x1b[0m'
        self.assertEqual(MODULE.sanitize(text, True), '\x1b[31mOK\x1b[0m')
        self.assertEqual(MODULE.sanitize(text, False), 'OK')

    def test_successful_empty_base_uses_fallback(self):
        self.base = self.script('empty.py', 'pass\n')
        result = self.invoke(payload=b'{}')
        self.assertEqual(result.stdout, b'Claude Code')

    def test_base_output_overflow_is_rejected(self):
        noisy = self.script('noisy.py', "import os\nwhile True: os.write(1, b'x' * 65536)\n")
        ok, output = MODULE.run((sys.executable, str(noisy)), b'{}', 2000, None)
        self.assertFalse(ok)
        self.assertEqual(output, '')

    def test_malformed_config_preserves_base(self):
        cfg = self.root / "bad.json"
        cfg.write_text('{bad')
        self.assertEqual(self.invoke(cfg).stdout, b"BASE:payload\n")


if __name__ == "__main__":
    unittest.main()
