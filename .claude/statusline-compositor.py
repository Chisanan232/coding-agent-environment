#!/usr/bin/env python3
"""Compose the canonical statusline with optional private local extensions.

The canonical base always owns the first output. Profile extensions are additive,
bounded and independently failure-safe; they cannot replace a missing base.
"""
from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_COMMAND_ENV = "CODING_AGENT_STATUSLINE_BASE"
CONFIG_ENV = "CODING_AGENT_STATUSLINE_CONFIG"
PROFILE_ACTIVE_ENV = "CODING_AGENT_PROFILE_ACTIVE"
PROFILE_MARKER_ENV = "CODING_AGENT_PROFILE_MARKER_PRESENT"
COLOR_ENV = "CODING_AGENT_STATUSLINE_COLOR"
DEPTH_ENV = "CODING_AGENT_STATUSLINE_COMPOSITOR_DEPTH"

DEFAULT_BASE = [sys.executable, os.path.expanduser("~/.claude/statusline.py")]
DEFAULT_TIMEOUT_MS = 250
MAX_TIMEOUT_MS = 2_000
DEFAULT_DEADLINE_MS = 600
MAX_DEADLINE_MS = 3_000
MAX_EXTENSIONS = 8
MAX_ARGV = 16
MAX_OUTPUT_BYTES = 64 * 1024
MAX_OUTPUT_LINES = 8
SAFE_ENV_NAMES = {"HOME", "PATH", "TMPDIR", "LANG", "TERM", "NO_COLOR"}
SAFE_ENV_PREFIXES = ("LC_", "XDG_")
EXTRA_ENV_ALLOWLIST = {"SSH_AUTH_SOCK", "HORONOM_STATUSLINE_HOME"}
SECRETISH = re.compile(r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API[_-]?KEY|AUTH)", re.I)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SGR = re.compile(r"\x1b\[[0-9;:]*m")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Extension:
    ident: str
    argv: tuple[str, ...]
    timeout_ms: int
    env_names: tuple[str, ...]
    renderer: str


@dataclass(frozen=True)
class Config:
    extensions: tuple[Extension, ...]
    deadline_ms: int


def bounded(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("invalid timeout")
    return min(value, maximum)


def color_capable() -> bool:
    explicit = os.environ.get(COLOR_ENV)
    if explicit in {"0", "false", "no"}:
        return False
    if explicit in {"1", "true", "yes"}:
        return True
    if os.environ.get("NO_COLOR") is not None:
        return False
    return os.environ.get("TERM", "").lower() not in {"", "dumb"}


def sanitize(text: str, color: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = OSC.sub("", text)
    text = ANSI.sub(lambda match: match.group(0) if color and SGR.fullmatch(match.group(0)) else "", text)
    # Remove remaining control bytes while retaining only verified SGR escapes.
    chunks = SGR.split(text)
    codes = SGR.findall(text)
    text = "".join(CONTROL.sub("", chunk) + (codes[index] if color and index < len(codes) else "")
                   for index, chunk in enumerate(chunks))
    lines = text.split("\n")
    return "\n".join(lines[:MAX_OUTPUT_LINES]).strip("\n")


def render_provider_contract(raw: str, color: bool) -> str | None:
    """Render an optional local provider contract without exposing raw output."""
    try:
        from importlib.util import spec_from_file_location, module_from_spec
        runtime = os.environ.get("CODING_AGENT_STATUSLINE_CONTRACT_RUNTIME")
        if not runtime:
            return None
        root = Path(runtime)
        contract_spec = spec_from_file_location("_local_status_contract", root / "statusline_contract.py")
        render_spec = spec_from_file_location("_local_status_render", root / "statusline_render.py")
        if not contract_spec or not render_spec or not contract_spec.loader or not render_spec.loader:
            return None
        contract = module_from_spec(contract_spec)
        sys.modules[contract_spec.name] = contract
        sys.modules["statusline_contract"] = contract
        contract_spec.loader.exec_module(contract)
        renderer = module_from_spec(render_spec)
        sys.modules[render_spec.name] = renderer
        render_spec.loader.exec_module(renderer)
        status = contract.provider_status_from_wire(json.loads(raw))
        capability = renderer.ColorCapability.ANSI256 if color else renderer.ColorCapability.NONE
        return renderer.render_provider(status, mode=renderer.PresentationMode.BALANCED, depth=renderer.InformationDepth.DETAIL, color=capability)
    except Exception:
        return None


def base_command() -> tuple[str, ...]:
    raw = os.environ.get(BASE_COMMAND_ENV)
    if not raw:
        return tuple(DEFAULT_BASE)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid base command") from exc
    if not isinstance(value, list) or not value or len(value) > MAX_ARGV:
        raise ValueError("invalid base command")
    if not all(isinstance(x, str) and x for x in value):
        raise ValueError("invalid base command")
    return tuple(value)


def read_config(path: str | None) -> Config:
    if not path:
        return Config((), DEFAULT_DEADLINE_MS)
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("config must be object")
    entries = payload.get("extensions", [])
    if not isinstance(entries, list) or len(entries) > MAX_EXTENSIONS:
        raise ValueError("invalid extensions")
    seen: set[str] = set()
    extensions = []
    for item in entries:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        ident = item.get("id")
        argv = item.get("argv")
        if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in seen:
            raise ValueError("invalid extension id")
        if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV:
            raise ValueError("invalid extension argv")
        if not all(isinstance(x, str) and x for x in argv):
            raise ValueError("invalid extension argv")
        requested = item.get("env", [])
        if not isinstance(requested, list) or not all(isinstance(x, str) for x in requested):
            raise ValueError("invalid extension env")
        for name in requested:
            if SECRETISH.search(name) or name not in EXTRA_ENV_ALLOWLIST:
                raise ValueError("unapproved extension env")
        renderer = item.get("renderer", "text")
        if renderer not in {"text", "provider-contract"}:
            raise ValueError("invalid extension renderer")
        approved = tuple(requested)
        seen.add(ident)
        extensions.append(Extension(ident, tuple(argv), bounded(item.get("timeout_ms"), DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS), approved, renderer))
    return Config(tuple(extensions), bounded(payload.get("deadline_ms"), DEFAULT_DEADLINE_MS, MAX_DEADLINE_MS))


def child_env(extension: Extension, color: bool) -> dict[str, str]:
    env = {}
    for name, value in os.environ.items():
        allowed = name in SAFE_ENV_NAMES or name.startswith(SAFE_ENV_PREFIXES) or name in extension.env_names
        if allowed and not SECRETISH.search(name):
            env[name] = value
    if color:
        env.pop("NO_COLOR", None)
    else:
        env["NO_COLOR"] = "1"
    env["TERM"] = os.environ.get("TERM", "xterm") if color else "dumb"
    env[DEPTH_ENV] = "1"
    return env


def run(argv: tuple[str, ...], payload: bytes, timeout_ms: int, env: dict[str, str] | None) -> tuple[bool, str]:
    proc = None
    try:
        # A seekable input avoids deadlock when the child writes before reading.
        with tempfile.TemporaryFile() as incoming:
            incoming.write(payload)
            incoming.seek(0)
            proc = subprocess.Popen(argv, stdin=incoming, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            os.set_blocking(proc.stdout.fileno(), False)
            deadline = time.monotonic() + timeout_ms / 1000
            out = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, ""
                if not select.select([proc.stdout], [], [], min(remaining, .05))[0]:
                    continue
                chunk = os.read(proc.stdout.fileno(), MAX_OUTPUT_BYTES + 1 - len(out))
                if not chunk:
                    proc.wait(timeout=max(.001, deadline - time.monotonic()))
                    break
                out.extend(chunk)
                if len(out) > MAX_OUTPUT_BYTES:
                    return False, ""
            if proc.returncode != 0:
                return False, ""
            return True, out.decode("utf-8", errors="strict")
    except Exception:
        return False, ""
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.stdout.close()
            try:
                proc.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                pass


def run_extensions(extensions: tuple[Extension, ...], color: bool, deadline_ms: int) -> dict[str, str]:
    deadline = time.monotonic() + deadline_ms / 1000
    active: dict[subprocess.Popen[bytes], dict[str, Any]] = {}
    results: dict[str, str] = {}

    def stop(proc: subprocess.Popen[bytes], state: dict[str, Any]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            state["stdout"].close()
        except OSError:
            pass
        try:
            proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass

    try:
        for ext in extensions:
            now = time.monotonic()
            if now >= deadline:
                break
            try:
                proc = subprocess.Popen(
                    ext.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=child_env(ext, color), start_new_session=True,
                )
                assert proc.stdout is not None
                os.set_blocking(proc.stdout.fileno(), False)
                state = {
                    "extension": ext,
                    "started": now,
                    "stdout": proc.stdout,
                    "output": bytearray(),
                    "eof": False,
                }
                active[proc] = state
            except Exception:
                continue

        while active:
            now = time.monotonic()
            for proc, state in tuple(active.items()):
                ext = state["extension"]
                expires = min(deadline, state["started"] + ext.timeout_ms / 1000)
                if now >= expires:
                    stop(proc, state)
                    del active[proc]
                    continue
                if proc.poll() is not None and state["eof"]:
                    if proc.returncode == 0:
                        try:
                            results[ext.ident] = bytes(state["output"]).decode("utf-8", errors="strict")
                        except UnicodeDecodeError:
                            pass
                    state["stdout"].close()
                    del active[proc]
            if not active:
                break
            now = time.monotonic()
            nearest = min(
                min(deadline, state["started"] + state["extension"].timeout_ms / 1000)
                for state in active.values()
            )
            readable, _, _ = select.select(
                [state["stdout"] for state in active.values()],
                [], [], max(0, min(nearest - now, 0.05)),
            )
            proc_by_fd = {state["stdout"].fileno(): proc for proc, state in active.items()}
            for stream in readable:
                proc = proc_by_fd.get(stream.fileno())
                state = active.get(proc)
                if state is None:
                    continue
                try:
                    chunk = os.read(stream.fileno(), MAX_OUTPUT_BYTES + 1 - len(state["output"]))
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    state["eof"] = True
                    continue
                state["output"].extend(chunk)
                if len(state["output"]) > MAX_OUTPUT_BYTES:
                    stop(proc, state)
                    del active[proc]
        return results
    finally:
        for proc, state in active.items():
            stop(proc, state)


def generic_fallback(payload: bytes) -> str:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    workspace = data.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    cwd = workspace.get("current_dir") or data.get("cwd")
    directory = Path(cwd).name if isinstance(cwd, str) and cwd else None
    model = data.get("model")
    model = model.get("display_name") if isinstance(model, dict) else None
    parts = ["Claude Code"]
    if isinstance(model, str) and model:
        parts.append(sanitize(model, False))
    if directory:
        parts.append(sanitize(directory, False))
    return " · ".join(part for part in parts if part)


def marker_present() -> bool:
    if os.environ.get(PROFILE_MARKER_ENV):
        return True
    current = Path.cwd().resolve()
    home = Path.home().resolve()
    while current != current.parent:
        if current != home and (current / ".coding-agent-profile").is_file():
            return True
        current = current.parent
    return False


def main() -> int:
    payload = sys.stdin.buffer.read()
    if os.environ.get(DEPTH_ENV):
        return 0
    try:
        base_ok, base = run(base_command(), payload, MAX_TIMEOUT_MS, None)
    except ValueError:
        base_ok, base = False, ""
    if not base_ok or not base.strip():
        base = generic_fallback(payload)
    config_path = os.environ.get(CONFIG_ENV)
    try:
        config = read_config(config_path)
    except Exception:
        config = Config((), DEFAULT_DEADLINE_MS)
    color = color_capable()
    if not color:
        base = ANSI.sub("", base)
    output = base
    if config.extensions:
        results = run_extensions(config.extensions, color, config.deadline_ms)
        additions = []
        for ext in config.extensions:
            value = results.get(ext.ident)
            if value is None:
                continue
            clean = render_provider_contract(value, color) if ext.renderer == "provider-contract" else sanitize(value, color)
            if clean:
                additions.append(clean)
        if additions:
            output = base.rstrip("\n") + "\n" + "\n".join(additions)
    if not os.environ.get(PROFILE_ACTIVE_ENV) and marker_present():
        output = output.rstrip("\n") + "\nProfile bypassed · use ca-claude"
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
