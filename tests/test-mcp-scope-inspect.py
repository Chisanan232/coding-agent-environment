#!/usr/bin/env python3
"""scripts/mcp-scope-inspect.py on synthetic homes only (SPE-117). Never reads the real ~/.claude.json."""
import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("msi", ROOT / "scripts/mcp-scope-inspect.py")
msi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msi)

TOKEN = "ghp_SYNTHETICTOKEN0123456789abcdefghij"      # fake, shaped like a token
HDR = "Bearer SYNTHETIC-BEARER-VALUE-XYZ"
URLTOK = "SYNTHETIC-URL-QUERY-TOKEN"
ENVSECRET = "SYNTHETIC-ENV-SECRET-VALUE"


def home_with(user=None, projects=None):
    d = pathlib.Path(tempfile.mkdtemp(prefix="msi-test-"))
    (d / ".claude.json").write_text(json.dumps({"mcpServers": user or {}, "projects": projects or {}}))
    return d


class Detection(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))

    def run_inspect(self, user=None, projects=None, roots=(), managed="/nonexistent/managed-mcp.json"):
        self.home = home_with(user, projects)
        return msi.inspect(str(self.home), list(roots), managed)

    def test_github_recognised_by_name_url_host_command_and_image_and_nothing_else(self):
        out = self.run_inspect(user={
            "github": {"type": "http", "url": "http://localhost:8082/", "headers": {"Authorization": HDR}},
            "gh-remote": {"type": "http", "url": f"https://api.githubcopilot.com/mcp/?t={URLTOK}"},
            "docker-gh": {"command": "docker", "args": ["run", "-i", "--rm", "-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}", "ghcr.io/github/github-mcp-server"]},
            "viaCmd": {"command": "/usr/local/bin/github-mcp-server", "args": ["stdio"]},
            "my_github_tools": {"command": "node", "args": ["x.js"]},
            "jira": {"command": "uvx", "args": ["mcp-atlassian"], "env": {"GITHUB_TOKEN": ENVSECRET, "JIRA_API_TOKEN": ENVSECRET}},
            "githubby": {"command": "node", "args": ["x.js"]},
            "clone-helper": {"command": "git", "args": ["clone", "https://github.com/octocat/hello-world"]},
            "figma": {"type": "http", "url": "https://mcp.figma.com/mcp"},
        })
        by = {r["server"]: r for r in out["servers"]}
        self.assertEqual({k for k, r in by.items() if r["github"]}, {"github", "gh-remote", "docker-gh", "viaCmd", "my_github_tools"})
        self.assertEqual(by["github"]["matched_by"], "name")
        self.assertEqual(by["docker-gh"]["matched_by"], "image")
        self.assertEqual(by["viaCmd"]["matched_by"], "command")
        self.assertEqual(by["gh-remote"]["matched_by"], "url_host")
        self.assertFalse(by["jira"]["github"])           # an env var NAME mentioning GITHUB does not make a server GitHub
        self.assertFalse(by["githubby"]["github"])
        self.assertFalse(by["clone-helper"]["github"])   # a github.com URL in args is not an MCP server image

    def test_no_secret_value_ever_reaches_the_output(self):
        self.run_inspect(user={
            "github": {"type": "http", "url": f"http://localhost:8082/?k={URLTOK}", "headers": {"Authorization": HDR}},
            "docker-gh": {"command": "docker", "args": ["run", "-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}", "ghcr.io/github/github-mcp-server"], "env": {"X": ENVSECRET}},
        })
        for argv in (["--home", str(self.home), "--project-root", str(self.home), "--managed-file", "/nonexistent"],
                     ["--home", str(self.home), "--json", "--managed-file", "/nonexistent"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                msi.main(argv)
            text = buf.getvalue()
            for secret in (TOKEN, HDR, URLTOK, ENVSECRET, "Authorization", "GITHUB_PERSONAL_ACCESS_TOKEN", "localhost:8082", "ghcr.io"):
                self.assertNotIn(secret, text, secret)
            self.assertIn("github", text)

    def test_inspection_is_read_only(self):
        self.home = home_with(user={"github": {"type": "http", "url": "http://x/"}})
        before = hashlib.sha256((self.home / ".claude.json").read_bytes()).hexdigest()
        with redirect_stdout(io.StringIO()):
            msi.main(["--home", str(self.home), "--managed-file", "/nonexistent"])
        self.assertEqual(hashlib.sha256((self.home / ".claude.json").read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".claude.json"])


class Retirement(unittest.TestCase):
    def test_exact_commands_touch_only_the_github_entry(self):
        home = home_with(user={"github": {"type": "http", "url": "http://x/"}, "neon": {"type": "http", "url": "https://mcp.neon.tech"}},
                         projects={"/work/proj one": {"mcpServers": {"github": {"command": "docker", "args": ["ghcr.io/github/github-mcp-server"]}, "jira": {"command": "uvx"}}}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        proj = pathlib.Path(tempfile.mkdtemp(prefix="msi-proj-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(proj, True))
        (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "docker"}, "fetch": {"command": "uvx"}}}))
        managed = proj / "managed-mcp.json"
        managed.write_text(json.dumps({"mcpServers": {"github": {"type": "http", "url": "https://x/"}}}))
        out = msi.inspect(str(home), [str(proj)], str(managed))
        cmds = {(r["scope"], r["server"]): r.get("retire") for r in out["servers"]}
        self.assertEqual(cmds[("user", "github")], "claude mcp remove 'github' -s user")
        self.assertEqual(cmds[("local", "github")], "cd '/work/proj one' && claude mcp remove 'github' -s local")
        self.assertIn("by hand", cmds[("project", "github")])
        self.assertIn("organization", cmds[("managed", "github")])
        for key in (("user", "neon"), ("local", "jira"), ("project", "fetch")):
            self.assertIsNone(cmds[key])
        self.assertEqual(sum(1 for v in cmds.values() if v), 4)

    def test_awkward_server_name_is_quoted_not_interpreted(self):
        home = home_with(user={"github'; rm -rf ~; echo '": {"type": "http", "url": "http://x/"}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        cmd = msi.inspect(str(home), [], "/nonexistent")["servers"][0]["retire"]
        self.assertTrue(cmd.startswith("claude mcp remove '"))
        self.assertIn("'\\''", cmd)   # embedded quote escaped, so the whole name stays one argument


class Robustness(unittest.TestCase):
    def test_missing_and_malformed_files_are_reported_not_fatal(self):
        home = pathlib.Path(tempfile.mkdtemp(prefix="msi-empty-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        out = msi.inspect(str(home), [str(home)], "/nonexistent")
        self.assertEqual(out["servers"], [])
        self.assertTrue(any("missing or unreadable" in n for n in out["notes"]))
        (home / ".claude.json").write_text("{not json")
        (home / ".mcp.json").write_text("[1,2")
        out = msi.inspect(str(home), [str(home)], "/nonexistent")
        self.assertEqual(out["servers"], [])
        self.assertTrue(any(".mcp.json" in n for n in out["notes"]))

    def test_wrong_shapes_do_not_crash(self):
        home = home_with(user={"a": "string", "b": None, "c": {"args": "notalist", "url": 5}}, projects={"/p": None, "/q": {"mcpServers": []}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        out = msi.inspect(str(home), [], "/nonexistent")
        self.assertEqual({r["server"] for r in out["servers"]}, {"a", "b", "c"})
        self.assertFalse(any(r["github"] for r in out["servers"]))


class ReviewHardening(unittest.TestCase):
    """Concrete failures from the independent review of PR #99."""

    def test_raw_type_text_is_never_echoed(self):
        home = home_with(user={"github": {"type": "SYNTHETIC-SECRET-IN-TYPE", "url": "http://x/"}, "ok": {"type": "SSE", "url": "http://y/"}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        out = msi.inspect(str(home), [], "/nonexistent")
        by = {r["server"]: r for r in out["servers"]}
        self.assertEqual((by["github"]["transport"], by["ok"]["transport"]), ("other", "sse"))
        self.assertNotIn("SYNTHETIC-SECRET-IN-TYPE", json.dumps(out))

    def test_control_characters_never_reach_the_terminal_and_get_no_copy_paste_command(self):
        evil = "github\x1b[2J\nfake-line"
        home = home_with(user={evil: {"type": "http", "url": "http://x/"}}, projects={"/p\x07ath": {"mcpServers": {"github": {"command": "docker", "args": ["ghcr.io/github/github-mcp-server"]}}}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        buf = io.StringIO()
        with redirect_stdout(buf):
            msi.main(["--home", str(home), "--managed-file", "/nonexistent"])
        text = buf.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("fake-line\n", text.replace("?fake-line", ""))
        self.assertIn("no command is generated", text)
        self.assertNotIn("claude mcp remove", text)

    def test_very_long_names_are_truncated_for_display_and_get_no_command(self):
        home = home_with(user={"github-" + "a" * 500: {"type": "http", "url": "http://x/"}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        row = msi.inspect(str(home), [], "/nonexistent")["servers"][0]
        self.assertLessEqual(len(row["server"]), 60)
        self.assertIn("by hand", row["retire"])

    def test_non_dict_project_entries_do_not_abort_the_inspection(self):
        home = home_with(user={"a": {"command": "x"}}, projects={"/l": ["x"], "/n": 5, "/s": "str", "/ok": {"mcpServers": {"github": {"type": "http", "url": "http://x/"}}}})
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        out = msi.inspect(str(home), [], "/nonexistent")
        self.assertEqual(sorted((r["scope"], r["server"]) for r in out["servers"]), [("local", "github"), ("user", "a")])


if __name__ == "__main__":
    unittest.main()
