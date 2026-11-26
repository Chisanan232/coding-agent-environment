#!/usr/bin/env python3
"""Classify ONE workflow step as light or heavy, from the command it would run (ADR 0017, owner policy 2026-11-26).

Only a genuinely resource-heavy step enters the heavy queue. A light step runs at once, whatever the lanes hold. This tool is the
shared, deterministic answer to "which one is this step?", so that sessions stop hand-typing a class per call:

    classify-step.py [--trust-file PATH] -- COMMAND [ARG...]      prints one JSON object, exit 0 (classified) or 3 (unknown)

weight  light   -> run the step now, directly, outside any lane; never wait behind a queued step for it
        heavy   -> `heavy-run --class <class> ...` (or `--class auto`); the step waits only for a lane
        unknown -> not recognised; read the class table in docs/RESOURCE-ADMISSION.md and declare a class yourself

The failure direction that matters is UNDER-charging (heavy work called light), so light is an allow-list of exact, known-safe shapes:
every other command, flag or wrapper form is `unknown` or heavy, never light. It never guesses and never executes anything. A wrapper
that hides its work (`owner-merge-local-ci.py record`) is classified by the checks the owner's pinned trust file lists for that repo,
and the heaviest check wins. The tool reads nothing else and writes nothing.

Limits: it sees argv only. A tool's own config (a repo's `diff.external`, a `.cargo/config.toml` alias) can name a program the argv does not, so
a hostile checkout is out of scope; and a symlink inside a trusted tool directory is not followed.
"""
import argparse
import json
import os
import re
import sys

LIGHT, BUILD_CRATE, BUILD_WORKSPACE, CONTAINER = "light", "build-crate", "build-workspace", "container-suite"
ORDER = {LIGHT: 0, BUILD_CRATE: 1, BUILD_WORKSPACE: 2, CONTAINER: 3}
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
LOCAL_DIR = os.path.expanduser("~/.config/coding-agent-environment/local")
DEFAULT_TRUST = os.path.join(LOCAL_DIR, "local-first-ci.json")
WRAPPER_SCRIPT = "owner-merge-local-ci.py"
PINNED_SCRIPTS = {"local-pip-audit.py": (LIGHT, "owner-pinned short dependency audit"),
                  "local-agent-python-check.py": (BUILD_CRATE, "owner-pinned Python journey test suite")}

PASS_THROUGH = {"env", "time", "command", "exec", "nohup"}                 # bare launchers; a flag or value after them is unknown
UV_BOOL_FLAGS = {"--locked", "--frozen", "--no-sync", "--isolated", "--no-project", "--all-extras", "--no-build", "--no-dev", "--no-editable",
                 "--offline", "--quiet", "-q", "--verbose", "-v", "--exact", "--no-cache", "--active", "--all-packages", "--no-env-file",
                 "--reinstall", "--refresh", "--upgrade", "-U", "--native-tls", "--no-config", "--all-groups", "--no-default-groups", "--no-binary"}
UV_VALUE_FLAGS = {"--with", "--with-editable", "--with-requirements", "--extra", "--group", "--only-group", "--no-group", "--python", "-p",
                  "--project", "--directory", "--env-file", "--no-binary-package", "--no-build-package", "--index", "--index-url",
                  "--default-index", "--resolution", "--prerelease", "--package", "--reinstall-package", "--upgrade-package",
                  "--refresh-package", "--no-install-package", "--config-file", "--cache-dir", "-w", "--index-strategy", "--link-mode",
                  "--keyring-provider", "--python-preference", "--color", "--find-links", "-f", "--constraints", "-c", "--overrides",
                  "--exclude-newer", "--exclude-newer-package", "--config-setting", "-C", "--python-platform", "--python-version"}
CARGO_LIGHT = {"fmt", "metadata", "tree", "locate-project", "audit", "deny", "search", "version", "--version", "-V", "help"}
CARGO_HEAVY = {"build", "b", "test", "t", "nextest", "clippy", "check", "c", "bench", "doc", "run", "r", "llvm-cov", "tarpaulin", "miri", "install"}
DOCKER_LIGHT = {"ps", "inspect", "version", "info", "logs", "images", "ls", "stats", "port"}   # only as the FIRST argument, no global flag before
DOCKER_BINARIES = {"docker", "podman", "nerdctl", "docker-compose", "podman-compose"}
NATIVE_GATE = {"pytest", "py.test", "mypy", "pyright", "tox", "nox", "pip-audit", "pip_audit", "coverage", "tsc", "vitest", "jest", "mvn", "gradle", "gradlew"}
NODE_HEAVY = {"test", "build", "ci", "install", "i", "e2e", "typecheck", "compile"}
GO_HEAVY = {"build", "test", "vet", "install"}
MAKE_HEAVY = {"test", "build", "all", "check", "ci", "docker"}
GIT_LIGHT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "describe", "blame", "cat-file", "rev-list", "for-each-ref"}
# git accepts unambiguous abbreviations of long options (`--upload-p=PROG` runs PROG), so a deny-list cannot work: only these exact
# read-only flags (and the `--name=value` forms below) keep a git step light; any other `-`/`--` token makes it unknown.
GIT_SAFE_FLAGS = {"--stat", "--numstat", "--shortstat", "--name-only", "--name-status", "--check", "--cached", "--staged", "--oneline", "--graph",
                  "--decorate", "--no-color", "--abbrev-commit", "--patch", "-p", "--no-patch", "-s", "-n", "-w", "--quiet", "--exit-code", "--all",
                  "--first-parent", "--merges", "--no-merges", "--reverse", "--follow", "--no-pager", "--short", "--branch", "-b", "--porcelain",
                  "--ignored", "-uno", "-unormal", "-uall", "--show-toplevel", "--git-dir", "--abbrev-ref", "--verify", "--is-inside-work-tree",
                  "--no-ext-diff", "--no-textconv", "--", "--tags", "--long", "--always", "--dirty"}
GIT_SAFE_VALUE_PREFIXES = ("--format=", "--pretty=", "--max-count=", "--since=", "--until=", "--author=", "--grep=", "--diff-filter=", "--abbrev=",
                           "--porcelain=", "--git-dir=", "-U", "--unified=", "--relative=")
GIT_GLOBAL_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace"}
TRUSTED_BIN_DIRS = {"/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin", os.path.expanduser("~/.cargo/bin"), os.path.expanduser("~/.local/bin")}
RECORDER_FLAGS = {"--repo", "--pr", "--base", "--worktree", "--expect-head", "--evidence-host"}   # `--config` (another trust file) is not allowed
GH_LIGHT = {"pr": {"view", "list", "checks", "diff", "status"}, "issue": {"view", "list", "status"}, "run": {"view", "list"},
            "repo": {"view"}, "release": {"view", "list"}, "workflow": {"view", "list"}, "auth": {"status"}, "label": {"list"},
            "gist": {"view"}, "search": None, "status": None, "api": None}          # None = any arguments; never checkout/create/clone/extension/alias
# Read/inspect/format/lint tools that do no build and start no program, minus the flags that make them execute one.
LIGHT_BINARIES = {"jq": (), "yq": (), "grep": (), "rg": ("--pre", "--pre-glob", "--hostname-bin"), "ls": (), "cat": (), "head": (), "tail": (), "uniq": (), "wc": (), "diff": (),
                  "echo": (), "printf": (), "test": (), "true": (), "false": (), "sleep": (), "mkdir": (), "cp": (), "mv": (), "touch": (),
                  "stat": (), "date": (), "basename": (), "dirname": (), "tr": (), "cut": (), "ruff": (), "black": (), "isort": (),
                  "shellcheck": (), "yamllint": (), "codespell": (), "gitleaks": (), "actionlint": ("-shellcheck", "-pyflakes"),
                  "markdownlint": ("--rules", "-r", "--custom-rules", "--config", "-c"), "shasum": (), "sha256sum": (), "readlink": (), "realpath": (),
                  "wait": (), "tee": ()}
MODULE_LIGHT = {"json.tool", "ruff", "black", "isort"}
HELP_FLAGS = ("--help", "-h", "--version", "-V")


def _is_assignment(token):
    return "=" in token and not token.startswith("-") and token.split("=", 1)[0].replace("_", "").isalnum()


def _result(klass, reason, cargo=None):
    out = {"weight": "light" if klass == LIGHT else "heavy", "class": klass, "reason": reason}
    if cargo is False:
        out["cargo"] = False      # a pinned pure-Python tool run: heavy-run declares --no-cargo-target (no Cargo-target exclusion)
    return out


def _unknown(reason):
    return {"weight": "unknown", "class": None, "reason": reason}


def _under(path, directory):
    """True when `path` resolves to a file directly inside `directory` (a same-named script elsewhere is not the pinned one)."""
    return os.path.dirname(os.path.realpath(path)) == os.path.realpath(directory)


def peel(argv):
    """Strip transparent launchers down to the real command. Returns (tokens, notes); notes say which wrappers were crossed.
    An env assignment, a flag after a bare launcher, an unknown uv flag or a `rtk`/`timeout` form this does not model makes the
    result `["<?>"]`: the caller reports unknown rather than misreading a value as the command."""
    tokens, notes = [t for t in argv if isinstance(t, str)], set()
    while tokens:
        base = os.path.basename(tokens[0])
        if "/" in tokens[0] and not _is_assignment(tokens[0]) and os.path.dirname(tokens[0]) not in TRUSTED_BIN_DIRS:
            return tokens, notes                  # a launcher/tool at a repo-local path is not the system one: _classify_core says unknown
        if _is_assignment(tokens[0]):
            notes.add("env")
            tokens = tokens[1:]
        elif base == "rtk" and len(tokens) > 1 and tokens[1] in ("test", "err"):
            tokens = tokens[2:]                       # `rtk test CMD` / `rtk err CMD` run CMD; this `test` is not coreutils test
        elif base == "rtk":
            tokens = tokens[1:]
            if tokens and tokens[0] in ("proxy", "gain", "discover", "summary", "init", "config"):
                return ["<?>"], notes
        elif base in PASS_THROUGH:
            tokens = tokens[1:]
            if tokens and tokens[0].startswith("-"):
                return ["<?>"], notes
        elif base == "timeout":
            rest = tokens[1:]
            if not rest or rest[0].startswith("-") or not rest[0].rstrip("smhd").replace(".", "").isdigit():
                return ["<?>"], notes
            tokens = rest[1:]
        elif base == "uv" and len(tokens) > 1 and tokens[1] == "run":
            rest, i = tokens[2:], 0
            while i < len(rest) and rest[i].startswith("-"):
                flag = rest[i].split("=", 1)[0]
                if flag in UV_VALUE_FLAGS:
                    i += 1 if "=" in rest[i] else 2
                elif flag in UV_BOOL_FLAGS:
                    i += 1
                else:
                    return ["<?>"], notes
            notes.add("uv")
            tokens = rest[i:]
        elif base.startswith("python") and len(tokens) > 2 and tokens[1] == "-m" and ("/" not in tokens[0] or os.path.dirname(tokens[0]) in TRUSTED_BIN_DIRS):
            notes.add("module")
            return ["<module>", tokens[2]] + tokens[3:], notes
        else:
            break
    return tokens, notes


def _packages(args):
    """Package selectors before `--` (after it the flags belong to the test binary): list of values."""
    own, values, i = (args[:args.index("--")] if "--" in args else args), [], 0
    while i < len(own):
        if own[i] in ("-p", "--package") and i + 1 < len(own):
            values.append(own[i + 1])
            i += 2
            continue
        if own[i].startswith("--package="):
            values.append(own[i].split("=", 1)[1])
        i += 1
    return values


def _cargo(args):
    lead = [a for a in args if not a.startswith("+")][:1]      # the subcommand must come first (after an optional +toolchain)
    sub = lead[0] if lead else ""
    if sub.startswith("-") and sub not in ("--version", "-V"):
        return _unknown("a cargo flag before the subcommand can swallow it (`cargo --config version build`)")
    if sub in CARGO_LIGHT:
        return _result(LIGHT, f"cargo {sub} does not compile the workspace")
    if sub not in CARGO_HEAVY:
        return _unknown(f"cargo {sub or '(none)'} is not in the table")
    if "--workspace" in args or "--all" in args:
        return _result(BUILD_WORKSPACE, f"cargo {sub} --workspace compiles every member")
    packages = _packages(args)
    if len(packages) == 1 and not any(ch in packages[0] for ch in "*?["):
        return _result(BUILD_CRATE, f"cargo {sub} is scoped to one package")
    if packages:
        return _result(BUILD_WORKSPACE, f"cargo {sub} names several or glob packages; charged as the workspace")
    return _result(BUILD_WORKSPACE, f"cargo {sub} without a package selector builds the whole workspace")


def _trusted_checks(repo, trust_file):
    try:
        with open(trust_file) as handle:
            checks = json.load(handle)["repos"][repo]["checks"]
        return [c["argv"] for c in checks if isinstance(c, dict) and isinstance(c.get("argv"), list)]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _git(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
        elif args[i] == "--no-pager":
            i += 1
        else:
            return _unknown("git global option not in the read-only list")
    sub = args[i] if i < len(args) else ""
    if sub not in GIT_LIGHT:
        return _unknown(f"git {sub or '(none)'} may run hooks or commands")
    for tok in args[i + 1:]:
        if tok.startswith("-") and tok not in GIT_SAFE_FLAGS and not tok.startswith(GIT_SAFE_VALUE_PREFIXES) and not tok[1:].isdigit():
            return _unknown("git option not in the read-only list (git accepts abbreviated long options that execute programs)")
    return _result(LIGHT, f"git {sub} is read-only and runs no hook")


def _denied(tok, denylist):
    """A program-executing option, with one or two dashes (Go flags take both), `=value`, or an attached short value (`-rx.js`)."""
    if not tok.startswith("-"):
        return False
    name = tok.split("=", 1)[0].lstrip("-")
    for entry in denylist:
        bare = entry.lstrip("-")
        if name == bare or (len(entry) == 2 and tok.startswith(entry) and not tok.startswith("--")):
            return True
    return False


def _recorder_repo(args):
    """The repo of an exactly-formed `record --repo X --pr N --base B --worktree W [--expect-head H] [--evidence-host H]`, else None.
    The script's argparse takes abbreviations, `=` forms and the last duplicate, so anything but this exact shape is unknown."""
    if args[:1] != ["record"]:
        return None
    seen, rest = {}, args[1:]
    if len(rest) % 2:
        return None
    for i in range(0, len(rest), 2):
        flag, value = rest[i], rest[i + 1]
        if flag not in RECORDER_FLAGS or flag in seen or value.startswith("-"):
            return None
        seen[flag] = value
    return seen.get("--repo") if {"--repo", "--pr", "--base", "--worktree"} <= set(seen) else None


def _classify_core(tokens, notes, trust_file, depth):
    base, args = os.path.basename(tokens[0]), tokens[1:]
    if "/" in tokens[0] and tokens[0] != "<module>" and os.path.dirname(tokens[0]) not in TRUSTED_BIN_DIRS:
        return _unknown(f"{tokens[0]} is a path outside the system tool directories; a repo-local script can share a tool's name")
    if base == "<module>":
        name = args[0]
        if name in NATIVE_GATE:
            return _result(BUILD_CRATE, f"python -m {name} is a native test/type gate", cargo=False)
        return _result(LIGHT, f"python -m {name} is a formatter") if name in MODULE_LIGHT else _unknown(f"python -m {name} is not in the table")
    script = args[0] if base.startswith("python") and args and not args[0].startswith("-") else ""
    if script and os.path.basename(script) == WRAPPER_SCRIPT and _under(script, SCRIPT_DIR):
        if depth:
            return _unknown("nested trust-file wrapper")
        repo = _recorder_repo(args[1:])
        checks = _trusted_checks(repo, trust_file) if repo else None
        if not checks:
            return _unknown("owner-merge-local-ci.py: not the exact `record --repo ... --pr ... --base ... --worktree ...` form, or no pinned checks for that repo; declare the class yourself")
        parts = [classify(c, trust_file, depth + 1) for c in checks]
        if any(p["weight"] == "unknown" for p in parts):
            return _unknown("a pinned check is not recognised: " + "; ".join(p["reason"] for p in parts if p["weight"] == "unknown")[:200])
        top = max(parts, key=lambda p: ORDER[p["class"]])
        return _result(top["class"], f"heaviest of {len(parts)} pinned checks: {top['reason']}")
    if script and os.path.basename(script) in PINNED_SCRIPTS and _under(script, LOCAL_DIR) and len(args) == 1:
        return _result(*PINNED_SCRIPTS[os.path.basename(script)])
    recognised = base in LIGHT_BINARIES or base in ("cargo", "uv", "claude", "pytest", "mypy", "npm", "pnpm", "yarn", "go", "git", "gh")
    if len(args) == 1 and args[0] in HELP_FLAGS and (recognised or depth) and base not in DOCKER_BINARIES | {"mvn", "gradle", "gradlew"}:
        return _result(LIGHT, f"{base} with only a help/version flag prints and exits")
    if base in DOCKER_BINARIES:
        if args and args[0] in DOCKER_LIGHT:
            return _result(LIGHT, f"{base} {args[0]} only inspects")
        return _result(CONTAINER, f"{base} runs containers (docker-exclusive)")
    if base in ("cargo", "cargo-nextest"):
        return _cargo(args if base == "cargo" else ["nextest"] + args)
    if base in NATIVE_GATE:
        return _result(BUILD_CRATE, f"{base} is a native test/type gate (no docker, no cargo target)", cargo=False)
    if base in ("npm", "pnpm", "yarn", "bun"):
        sub = next((a for a in args if not a.startswith("-") and a not in ("run", "exec")), "")
        return _result(BUILD_CRATE, f"{base} {sub} is a native build/test", cargo=False) if sub in NODE_HEAVY else _unknown(f"{base} {sub or '(none)'} is not in the table")
    if base == "go":
        sub = args[0] if args else ""
        return _result(BUILD_CRATE, f"go {sub} is a native build/test", cargo=False) if sub in GO_HEAVY else _unknown(f"go {sub or '(none)'} is not in the table")
    if base == "uv" and args[:1] == ["build"]:
        return _result(BUILD_CRATE, "uv build compiles a wheel", cargo=False)
    if base == "make":
        sub = next((a for a in args if not a.startswith("-")), "")
        return _result(BUILD_CRATE, f"make {sub} is a native build/test") if sub in MAKE_HEAVY else _unknown(f"make {sub or '(default)'} is not in the table")
    if base == "git":
        return _git(args)
    if base == "gh":
        words = [a for a in args[:2] if not a.startswith("-")]     # a flag before the subcommand can swallow it (`gh pr -R view checkout`)
        if len(words) < len(args[:2]):
            return _unknown("gh flag before the subcommand")
        allowed = GH_LIGHT.get(words[0] if words else "", False)
        ok = allowed is None or (allowed and len(words) > 1 and words[1] in allowed)
        return _result(LIGHT, f"gh {' '.join(words[:2])} is a GitHub read call") if ok else _unknown(f"gh {' '.join(words[:2]) or '(none)'} is not a known read form")
    if base in LIGHT_BINARIES:
        if any(_denied(a, LIGHT_BINARIES[base]) for a in args):
            return _unknown(f"{base} option that executes a program")
        return _result(LIGHT, f"{base} is a short read/inspect/format/lint command")
    if base == "uvx":
        return _uvx(args, trust_file, depth)
    if base in SHELLS and len(args) == 2 and args[0] == "-c":
        return _shell_chain(args[1], trust_file, depth)
    return _unknown(f"{base} is not recognised (scripts and pre-commit hide their work; `bash -c` is read only in the cd/export/command/log-redirect shape)")


SHELL_ENV_OK = {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "RUSTUP_TOOLCHAIN", "RUST_BACKTRACE", "CI", "CARGO_TARGET_DIR"}
SHELLS = {"bash", "sh", "zsh"}


def _redirect_ok(path):
    """A log-file redirect target: absolute, literal, inside the account's home (the caller's own files), no `..`."""
    return (os.path.isabs(path) and "$" not in path and ".." not in path.split("/")
            and os.path.realpath(path).startswith(os.path.expanduser("~") + os.sep))


def _shell_chain(script, trust_file, depth):
    """`bash -c SCRIPT` for the one shape agents write around a gate: `cd DIR && export NAME=literal; CMD ARGS > LOG 2>&1; echo "exit=$?"
    >> LOG`. Segments split on `&&` and `;`; each is a `cd`, an allow-listed `export`, the exit-code echo, or one command classified on
    its own. Anything else (pipes, substitutions, subshells, background `&`, here-docs, globs, a variable in any word) is unknown, and
    the result is the heaviest segment. Argv only: the script file is not read, and a hostile checkout stays out of scope as documented."""
    import shlex
    if depth or len(script) > 4096 or any(c in script for c in "`\n\r|(){}<*[~\\"):
        return _unknown("bash -c script uses a construct this classifier does not model")
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return _unknown("bash -c script does not parse")
    segments, current = [], []
    for word in words:
        if word in ("&&", ";"):
            segments.append(current)
            current = []
        elif set(word) <= set("&;<>|") and word not in (">", ">>", ">&"):
            return _unknown(f"bash -c shell operator {word!r} is not modelled")
        else:
            current.append(word)
    segments.append(current)
    parts = []
    for seg in segments:
        i = 0
        while i < len(seg):                      # strip log redirects: `> F`, `>> F`, `2 >& 1`
            if seg[i] in (">", ">>") and i + 1 < len(seg) and _redirect_ok(seg[i + 1]):
                del seg[i:i + 2]
            elif seg[i:i + 3] == ["2", ">&", "1"]:
                del seg[i:i + 3]
            else:
                i += 1
        if any(w in (">", ">>", ">&") for w in seg):
            return _unknown("bash -c redirect to a path outside the account's home or in an unmodelled form")
        if not seg:
            return _unknown("empty bash -c segment")
        if seg[0] == "cd" and len(seg) == 2 and "$" not in seg[1] and os.path.isabs(seg[1]):
            continue
        if seg[0] == "export" and len(seg) == 2 and "=" in seg[1]:
            name, value = seg[1].split("=", 1)
            if name in SHELL_ENV_OK and "$" not in value:
                continue
            return _unknown(f"export {name} is not in the allowed list")
        if seg == ["echo", "exit=$?"]:
            continue
        if any("$" in w for w in seg):
            return _unknown("a shell variable in a bash -c word")
        parts.append(classify(seg, trust_file, depth + 1))
    if not parts:
        return _unknown("bash -c script runs nothing recognisable")
    bad = [p for p in parts if p["weight"] == "unknown"]
    if bad:
        return _unknown("bash -c segment: " + bad[0]["reason"])
    heavy = [p for p in parts if p["weight"] == "heavy"] or parts
    top = dict(max(heavy, key=lambda p: ORDER.get(p["class"], 0) if p["class"] else -1))
    if any(p["weight"] == "heavy" and p.get("cargo") is not False for p in parts):
        top.pop("cargo", None)       # one segment may use cargo: keep the exclusion
    return top


def _uvx(args, trust_file, depth):
    """`uvx [--python V] [--from NAME==VER] [--with NAME==VER]... TOOL ARGS`: a pinned tool run. The tool must be the `--from` package (or
    no `--from`), so a pinned wheel cannot run a different executable. Classified as the tool itself (pytest and mypy are native gates)."""
    i, source = 0, None
    while i < len(args) and args[i].startswith("-"):
        flag = args[i]
        if flag in ("--python", "-p") and i + 1 < len(args):
            i += 2
        elif flag == "--from" and i + 1 < len(args) and re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.+!-]*", args[i + 1]):
            source, i = args[i + 1].split("==")[0], i + 2
        elif flag == "--with" and i + 1 < len(args) and re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.+!-]*", args[i + 1]):
            i += 2
        else:
            return _unknown(f"uvx flag {flag} is not a pinned form")
    if i >= len(args):
        return _unknown("uvx without a tool")
    if source is not None and args[i] != source:
        return _unknown("uvx tool differs from its --from package")
    return classify(args[i:], trust_file, depth + 1)


def classify(argv, trust_file=DEFAULT_TRUST, depth=0):
    tokens, notes = peel(argv)
    if not tokens:
        return _unknown("empty command")
    if tokens == ["<?>"]:
        return _unknown("a wrapper form this classifier does not model (flag after a launcher, unknown uv flag, rtk/timeout form)")
    out = _classify_core(tokens, notes, trust_file, depth)
    if out["weight"] == "light" and "env" in notes:
        return _unknown("an environment assignment precedes the command: it can name a program to run")
    if out["weight"] == "light" and "uv" in notes:
        return _result(BUILD_CRATE, "uv run may sync or build the project environment first")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="classify-step", description=__doc__.split("\n")[0])
    parser.add_argument("--trust-file", default=DEFAULT_TRUST)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    out = classify(command, args.trust_file)
    out["argv0"] = os.path.basename(command[0]) if command else None
    print(json.dumps(out, sort_keys=True))
    return 3 if out["weight"] == "unknown" else 0


if __name__ == "__main__":
    sys.exit(main())
