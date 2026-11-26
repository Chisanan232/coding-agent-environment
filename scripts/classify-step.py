#!/usr/bin/env python3
"""Classify ONE workflow step as light or heavy, from the command it would run (ADR 0017, owner policy 2026-11-26).

Only a genuinely resource-heavy step enters the heavy queue. A light step runs at once, whatever the lanes hold. This tool is the
shared, deterministic answer to "which one is this step?", so that sessions stop hand-typing a class per call:

    classify-step.py [--trust-file PATH] -- COMMAND [ARG...]      prints one JSON object, exit 0 (classified) or 3 (unknown)

weight  light   -> run the step now, directly, outside any lane; never wait behind a queued step for it
        heavy   -> `heavy-run --class <class> ...` (or `--class auto`); the step waits only for a lane
        unknown -> the command is not recognised; read the class table in docs/RESOURCE-ADMISSION.md and declare a class yourself

It never guesses: an unrecognised command is `unknown`, not light. A wrapper that hides its work (`owner-merge-local-ci.py record`) is
classified by the checks the owner's trust file pins for that repo, and the heaviest check wins. The tool reads nothing else, runs
nothing and writes nothing.
"""
import argparse
import json
import os
import sys

LIGHT, BUILD_CRATE, BUILD_WORKSPACE, CONTAINER = "light", "build-crate", "build-workspace", "container-suite"
ORDER = {LIGHT: 0, BUILD_CRATE: 1, BUILD_WORKSPACE: 2, CONTAINER: 3}
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
LOCAL_DIR = os.path.expanduser("~/.config/coding-agent-environment/local")
DEFAULT_TRUST = os.path.expanduser("~/.config/coding-agent-environment/local/local-first-ci.json")

# git runs hooks (pre-commit, pre-push, post-checkout) and `rebase --exec` / `bisect run` run commands, so only these never do.
GIT_LIGHT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-remote", "describe", "blame", "cat-file", "rev-list", "for-each-ref", "fetch", "add"}
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
PASS_THROUGH = {"rtk", "env", "time", "nice", "command", "exec", "nohup"}
UV_FLAGS_WITH_VALUE = {"--no-binary-package", "--no-build-package", "--no-binary", "--index", "--index-url", "--default-index", "--resolution", "--prerelease", "--package", "--reinstall-package", "--upgrade-package", "--with", "--extra", "--group", "--python", "-p", "--project", "--directory", "--env-file", "--with-requirements"}
CARGO_LIGHT = {"fmt", "metadata", "tree", "locate-project", "audit", "deny", "search", "version", "--version", "-V", "help", "vendor"}
CARGO_HEAVY = {"build", "b", "test", "t", "nextest", "clippy", "check", "c", "bench", "doc", "run", "r", "llvm-cov", "tarpaulin", "miri", "install"}
CARGO_PACKAGE_SELECTORS = {"-p", "--package"}   # --lib/--bin/--test/--manifest-path still compile every member at a virtual-workspace root
DOCKER_LIGHT = {"ps", "inspect", "version", "info", "logs", "images", "ls", "stats", "context", "port"}
DOCKER_BINARIES = {"docker", "podman", "nerdctl", "docker-compose", "podman-compose"}
NATIVE_GATE = {"pytest", "py.test", "mypy", "pyright", "tox", "nox", "pip-audit", "pip_audit", "coverage", "tsc", "vitest", "jest", "mvn", "gradle", "gradlew"}
NODE_HEAVY = {"test", "build", "ci", "install", "i", "e2e", "lint:ci", "typecheck", "compile"}
GO_HEAVY = {"build", "test", "vet", "install"}
MAKE_HEAVY = {"test", "build", "all", "check", "ci", "docker"}
LIGHT_BINARIES = {"gh", "jq", "yq", "grep", "rg", "ls", "cat", "head", "tail", "sort", "uniq", "wc", "diff", "echo",
                  "printf", "test", "true", "false", "sleep", "mkdir", "cp", "mv", "touch", "stat", "date", "basename", "dirname", "tr", "cut",
                  "ruff", "black", "isort", "shellcheck", "yamllint", "markdownlint", "codespell", "gitleaks", "actionlint", "curl", "shasum",
                  "sha256sum", "readlink", "realpath", "wait", "tee"}
WRAPPER_SCRIPT = "owner-merge-local-ci.py"
PINNED_SCRIPTS = {"local-pip-audit.py": (LIGHT, "owner-pinned short dependency audit"),
                  "local-agent-python-check.py": (BUILD_CRATE, "owner-pinned Python journey test suite")}


def _is_assignment(token):
    return "=" in token and not token.startswith("-") and token.split("=", 1)[0].replace("_", "").isalnum()


def _under(path, directory):
    """True when `path` resolves to a file directly inside `directory` (a same-named script elsewhere is not the pinned one)."""
    return os.path.dirname(os.path.realpath(path)) == os.path.realpath(directory)


def strip_wrappers(argv):
    """Drop env assignments and transparent launchers (rtk, env, timeout N, nice, uv run [flags], python -m) down to the real command."""
    tokens = [t for t in argv if isinstance(t, str)]
    changed = True
    while tokens and changed:
        changed = False
        base = os.path.basename(tokens[0])
        if _is_assignment(tokens[0]):
            tokens, changed = tokens[1:], True
        elif base in PASS_THROUGH:
            tokens, changed = tokens[1:], True
        elif base == "timeout":
            rest = tokens[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            tokens, changed = rest[1:] if rest else [], True
        elif base == "uv" and len(tokens) > 1 and tokens[1] == "run":
            rest, i = tokens[2:], 0
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in UV_FLAGS_WITH_VALUE else 1
            tokens, changed = rest[i:], True
        elif base.startswith("python") and len(tokens) > 2 and tokens[1] == "-m":
            tokens, changed = tokens[2:], True
    return tokens


def _result(klass, reason):
    return {"weight": "light" if klass == LIGHT else "heavy", "class": klass, "reason": reason}


def _unknown(reason):
    return {"weight": "unknown", "class": None, "reason": reason}


def _cargo(args):
    sub = next((a for a in args if not a.startswith("+") and not (a.startswith("-") and a not in ("--version", "-V"))), "")
    if sub in CARGO_LIGHT:
        return _result(LIGHT, f"cargo {sub} does not compile the workspace")
    if sub in CARGO_HEAVY:
        if "--workspace" in args or "--all" in args:
            return _result(BUILD_WORKSPACE, f"cargo {sub} --workspace compiles every member")
        if any(a in CARGO_PACKAGE_SELECTORS or a.startswith("--package=") for a in args):
            return _result(BUILD_CRATE, f"cargo {sub} is scoped to one package or target")
        return _result(BUILD_WORKSPACE, f"cargo {sub} without a package selector builds the whole workspace")
    return _unknown(f"cargo {sub or '(none)'} is not in the table")


def _trusted_checks(argv, trust_file):
    flags = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
    repo = flags.get("--repo")
    try:
        with open(trust_file) as handle:
            checks = json.load(handle)["repos"][repo]["checks"]
        return [c["argv"] for c in checks if isinstance(c, dict) and isinstance(c.get("argv"), list)]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def classify(argv, trust_file=DEFAULT_TRUST, _depth=0):
    tokens = strip_wrappers(argv)
    if not tokens:
        return _unknown("empty command")
    base, args = os.path.basename(tokens[0]), tokens[1:]
    script = next((a for a in args if not a.startswith("-")), "") if base.startswith("python") else ""
    wrapper = os.path.basename(script) == WRAPPER_SCRIPT and _under(script, SCRIPT_DIR)
    pinned = os.path.basename(script) in PINNED_SCRIPTS and _under(script, LOCAL_DIR)
    if wrapper:
        if _depth:
            return _unknown("nested trust-file wrapper")
        checks = _trusted_checks(args, trust_file)
        if not checks:
            return _unknown("owner-merge-local-ci.py: no pinned checks for this repo in the trust file; declare the class yourself")
        parts = [classify(c, trust_file, _depth + 1) for c in checks]
        if any(p["weight"] == "unknown" for p in parts):
            return _unknown("a pinned check is not recognised: " + "; ".join(p["reason"] for p in parts if p["weight"] == "unknown")[:200])
        top = max(parts, key=lambda p: ORDER[p["class"]])
        return _result(top["class"], f"heaviest of {len(parts)} pinned checks: {top['reason']}")
    if len(args) == 1 and args[0] in ("--help", "-h", "--version", "-V") and base not in DOCKER_BINARIES | {"mvn", "gradle", "gradlew"}:
        return _result(LIGHT, f"{base} with only a help/version flag prints and exits")
    if pinned:
        return _result(*PINNED_SCRIPTS[os.path.basename(script)])
    if base in DOCKER_BINARIES:
        sub = next((a for a in args if not a.startswith("-")), "")
        return _result(LIGHT, f"{base} {sub} only inspects") if sub in DOCKER_LIGHT else _result(CONTAINER, f"{base} runs containers (docker-exclusive)")
    if base == "cargo" or base == "cargo-nextest":
        return _cargo(args if base == "cargo" else ["nextest"] + args)
    if base in NATIVE_GATE:
        return _result(BUILD_CRATE, f"{base} is a native test/type gate (no docker, no cargo target)")
    if base in ("npm", "pnpm", "yarn", "bun"):
        sub = next((a for a in args if not a.startswith("-") and a != "run" and a != "exec"), "")
        return _result(BUILD_CRATE, f"{base} {sub} is a native build/test") if sub in NODE_HEAVY else _unknown(f"{base} {sub or '(none)'} is not in the table")
    if base == "go":
        sub = args[0] if args else ""
        return _result(BUILD_CRATE, f"go {sub} is a native build/test") if sub in GO_HEAVY else _unknown(f"go {sub or '(none)'} is not in the table")
    if base == "uv" and args[:1] == ["build"]:
        return _result(BUILD_CRATE, "uv build compiles a wheel")
    if base == "make":
        sub = next((a for a in args if not a.startswith("-")), "")
        return _result(BUILD_CRATE, f"make {sub} is a native build/test") if sub in MAKE_HEAVY else _unknown(f"make {sub or '(default)'} is not in the table")
    if base == "git":
        rest, i = args, 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in GIT_GLOBAL_WITH_VALUE else 1
        sub = rest[i] if i < len(rest) else ""
        return _result(LIGHT, f"git {sub} is read-only and runs no hook") if sub in GIT_LIGHT else _unknown(f"git {sub or '(none)'} may run hooks or commands")
    if base in LIGHT_BINARIES:
        return _result(LIGHT, f"{base} is a short read/inspect/format/lint command")
    return _unknown(f"{base} is not recognised (scripts, bash -c and pre-commit hide their work)")


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
