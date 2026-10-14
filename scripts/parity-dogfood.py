#!/usr/bin/env python3
"""Capture fresh native-host SPE-92 scenario runs; captures are not verdicts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/parity-dogfood.json"
DEFAULT_OUTPUT = ".local/state/coding-agent-environment/parity-dogfood"
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
PRIVATE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
MAX_CLAUDE_HISTORY_BYTES = 64 * 1024 * 1024


class DogfoodError(Exception):
    pass


def fixture_process_env() -> dict[str, str]:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(GIT_ENV)
    return env


def load_baseline(path: Path = BASELINE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DogfoodError(f"Cannot read scenario baseline: {exc}") from exc
    if not isinstance(data, dict):
        raise DogfoodError("Scenario baseline must be a JSON object")
    scenarios = data.get("scenarios")
    if data.get("schema") != 1 or not isinstance(scenarios, list) or len(scenarios) != 18:
        raise DogfoodError("Scenario baseline must be schema 1 with exactly 18 scenarios")
    if any(not isinstance(case, dict) for case in scenarios):
        raise DogfoodError("Scenario baseline entries must be JSON objects")
    ids = [case.get("id") for case in scenarios]
    if ids != list(range(1, 19)):
        raise DogfoodError("Scenario baseline IDs must be the ordered canonical 1–18 set")
    allowed_modes = {"plan_only", "local_fixture", "native_discovery"}
    for case in scenarios:
        if (not isinstance(case.get("name"), str) or not case["name"].strip()
                or not isinstance(case.get("prompt"), str) or not case["prompt"].strip()
                or case.get("mode") not in allowed_modes):
            raise DogfoodError("Every scenario needs a name, prompt, and supported mode")
        if case["mode"] == "local_fixture" and not isinstance(case.get("fixture"), str):
            raise DogfoodError("Local fixture scenario has no fixture name")
        if case["id"] == 17:
            stages = case.get("stages")
            if (not isinstance(stages, list) or len(stages) != 2
                    or any(not isinstance(stage, dict) or not isinstance(stage.get("prompt"), str)
                           or not stage["prompt"].strip() for stage in stages)):
                raise DogfoodError("Scenario 17 must define two nonempty fresh-process stages")
    return data


def safe_output_root(home: Path, requested: str | None) -> Path:
    home = home.resolve()
    root = (Path(requested).expanduser() if requested else home / DEFAULT_OUTPUT)
    if not root.is_absolute():
        root = Path.cwd() / root
    # Keep persistent evidence in the operator's home and reject symlinked path
    # components so a typo or redirected state path cannot spill elsewhere.
    candidate = Path(os.path.normpath(str(root.absolute())))
    try:
        candidate.relative_to(home)
    except ValueError as exc:
        raise DogfoodError("Output directory must be beneath the current user's home") from exc
    relative = candidate.relative_to(home)
    cursor = home
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DogfoodError(f"Refusing symlinked output path: {cursor}")
    return candidate


def assert_private_dir(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(parents=True, mode=PRIVATE_DIR_MODE, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise DogfoodError(f"Not a regular output directory: {path}")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DogfoodError(f"Output directory must be owned by this user and private (0700): {path}")
    path.chmod(PRIVATE_DIR_MODE)


def private_write(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, PRIVATE_MODE)
    try:
        os.fchmod(fd, PRIVATE_MODE)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def save_record(path: Path, record: dict, *, replace: bool = False) -> None:
    data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
    if not replace:
        private_write(path, data)
        return
    tmp = path.with_name(path.name + ".new")
    private_write(tmp, data)
    os.replace(tmp, path)
    os.chmod(path, PRIVATE_MODE)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Parity Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, env=fixture_process_env(), check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def write_seed_file(root: Path, relative: str, body: str) -> None:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise DogfoodError(f"Unsafe fixture path in baseline: {relative}")
    target = root.joinpath(*rel.parts)
    target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if target.exists() or target.is_symlink():
        raise DogfoodError(f"Fixture seed would overwrite a path: {target}")
    private_write(target, body.encode())


def init_repository(path: Path, *, branch: str = "main") -> None:
    path.mkdir(parents=True, mode=PRIVATE_DIR_MODE)
    git(path.parent, "init", "--quiet", "--initial-branch", branch, str(path))
    git(path, "config", "--local", "user.name", "Parity Fixture")
    git(path, "config", "--local", "user.email", "fixture@example.invalid")
    git(path, "config", "--local", "core.hooksPath", "/dev/null")


def commit_all(path: Path, message: str) -> None:
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "-m", message)


def materialize_fixture(run_case: Path, case: dict, baseline: dict) -> Path:
    fixture_name = case["fixture"]
    seed = baseline.get("local_fixtures", {}).get(fixture_name)
    if not isinstance(seed, dict) or not isinstance(seed.get("files"), dict):
        raise DogfoodError(f"No local fixture seed for scenario {case['id']}")
    repo = run_case / "repository"
    init_repository(repo)
    files = seed["files"]
    for relative, body in files.items():
        write_seed_file(repo, relative, body)
    git_kind = seed.get("git")
    if git_kind == "single_initial_commit":
        commit_all(repo, "🧪 fixture: seed worktree task")
        git(repo, "worktree", "add", "--quiet", "-b", "control/untouched",
            str(run_case / "control-worktree"), "HEAD")
    elif git_kind == "initial_commit_except_untracked_control":
        git(repo, "add", "CONTRIBUTING.md", "TASK.md", "src/label.py", "tests/test_label.py")
        git(repo, "commit", "--quiet", "-m", "🧪 fixture: seed commit task")
    elif git_kind == "none":
        commit_all(repo, "🧪 fixture: seed language repair")
    elif git_kind == "merged_target_and_dirty_control_worktrees":
        write_seed_file(repo, "README.md", "Disposable closeout fixture.\n")
        git(repo, "add", "README.md", "main.txt")
        git(repo, "commit", "--quiet", "-m", "🧪 fixture: seed main")
        git(repo, "switch", "--quiet", "-c", "fixture/merged-task")
        write_seed_file(repo, "merged.txt", seed["files"]["merged.txt"])
        git(repo, "add", "merged.txt")
        git(repo, "commit", "--quiet", "-m", "🧪 fixture: merged change")
        git(repo, "switch", "--quiet", "main")
        git(repo, "merge", "--quiet", "--no-ff", "fixture/merged-task", "-m", "fixture merge")
        git(repo, "worktree", "add", "--quiet", str(run_case / "merged-worktree"),
            "fixture/merged-task")
        git(repo, "branch", "--quiet", "fixture/dirty-control")
        git(repo, "worktree", "add", "--quiet", str(run_case / "control-worktree"),
            "fixture/dirty-control")
        control = run_case / "control-worktree"
        write_seed_file(control, "dirty-control.txt", "preserve this uncommitted content\n")
        write_seed_file(control, "untracked-control.txt", "preserve this untracked content\n")
    else:
        raise DogfoodError(f"Unsupported fixture setup in baseline: {git_kind!r}")
    return repo


def classify_failure(stdout: str, stderr: str, exit_code: int) -> str:
    combined = (stdout + "\n" + stderr).lower()
    if re.search(r"weekly.{0,20}(quota|limit)|quota.{0,20}(limit|exceeded)|rate.?limit|usage limit", combined):
        return "FAILED_QUOTA"
    if re.search(r"authentication|not logged in|unauthorized|\b401\b|invalid api key", combined):
        return "FAILED_AUTH"
    if re.search(r"permission denied|approval required|not approved|access denied", combined):
        return "FAILED_PERMISSION"
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            return "FAILED_JOB"
    if re.search(r"job.{0,30}(failed|error)|worker.{0,30}(failed|error)", combined):
        return "FAILED_JOB"
    return "FAILED_EXIT" if exit_code else "FAILED_OUTPUT"


def parse_host_output(host: str, stdout: str) -> tuple[bool, str, dict]:
    if not stdout.strip():
        return False, "missing stdout", {}
    if host == "claude":
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return False, f"invalid Claude JSON: {exc.msg}", {}
        if not isinstance(payload, dict):
            return False, "Claude JSON top level is not an object", {}
        native_session_id = payload.get("session_id")
        if native_session_id is not None and (
                not isinstance(native_session_id, str) or not native_session_id.strip()):
            return False, "Claude session_id field is malformed", {}
        metadata = {}
        if native_session_id is not None:
            metadata["native_session_id"] = native_session_id
        if payload.get("is_error") is True or str(payload.get("subtype", "")).startswith("error"):
            return False, "Claude returned a structured error result", metadata
        if not isinstance(payload.get("result"), str) or not payload["result"].strip():
            return False, "Claude result field is missing or empty", metadata
        usage = payload.get("modelUsage")
        models = set(usage) if isinstance(usage, dict) else set()
        if isinstance(payload.get("model"), str) and payload["model"].strip():
            models.add(payload["model"])
        metadata.update(response_text=payload["result"],
                        observed_models=sorted(model for model in models if isinstance(model, str)))
        return True, "valid Claude result envelope", metadata
    events = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return False, f"invalid Codex JSONL at line {line_number}: {exc.msg}", {}
        if not isinstance(event, dict):
            return False, f"Codex JSONL line {line_number} is not an object", {}
        events.append(event)
    if not events:
        return False, "Codex JSONL contains no events", {}
    errors = [event for event in events if event.get("type") in {"error", "turn.failed"}]
    if errors:
        return False, "Codex JSONL contains a structured error event", {}
    started = next((event for event in events if event.get("type") == "thread.started"), None)
    thread_id = started.get("thread_id") if isinstance(started, dict) else None
    if not isinstance(thread_id, str) or not thread_id.strip():
        return False, "Codex JSONL has no native thread.started ID", {}
    messages = []
    for event in events:
        item = event.get("item") if event.get("type") == "item.completed" else None
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        if isinstance(item.get("text"), str):
            messages.append(item["text"])
        elif isinstance(item.get("content"), list):
            messages.extend(part["text"] for part in item["content"]
                            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
                            and isinstance(part.get("text"), str))
    response = "\n".join(message.strip() for message in messages if message.strip())
    if not response:
        return False, "Codex JSONL has no nonempty completed agent_message", {}
    if not any(event.get("type") == "turn.completed" for event in events):
        return False, "Codex JSONL has no completed-turn event", {}
    model = started.get("model") or started.get("model_slug")
    metadata = {"response_text": response, "native_thread_id": thread_id}
    if isinstance(model, str) and model.strip():
        metadata["observed_models"] = [model]
    return True, "valid Codex thread, agent response, and completed-turn events", metadata


def collect_claude_tool_evidence(home: Path, cwd: Path, requested_session_id: str,
                                 native_session_id: str | None) -> dict:
    """Read only the exact, verified Claude session history; return no transcript content."""
    if not native_session_id or native_session_id != requested_session_id:
        return {"status": "unverified_session_id"}
    try:
        if str(uuid.UUID(native_session_id)) != native_session_id:
            return {"status": "unverified_session_id"}
    except (ValueError, AttributeError):
        return {"status": "unverified_session_id"}

    home = home.resolve()
    project_key = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    history_dir = home / ".claude" / "projects" / project_key
    history_path = history_dir / f"{native_session_id}.jsonl"
    cursor = home
    try:
        for part in history_path.relative_to(home).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return {"status": "history_unsafe_path"}
        info = history_path.stat()
    except FileNotFoundError:
        return {"status": "history_missing"}
    except OSError:
        return {"status": "history_unreadable"}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return {"status": "history_unreadable"}
    if info.st_size > MAX_CLAUDE_HISTORY_BYTES:
        return {"status": "history_too_large"}
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(history_path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                return {"status": "history_unreadable"}
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_CLAUDE_HISTORY_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return {"status": "history_unreadable"}
    if len(raw) > MAX_CLAUDE_HISTORY_BYTES:
        return {"status": "history_too_large"}

    digest = hashlib.sha256(raw).hexdigest()
    tool_names: list[str] = []
    skill_identifiers: list[str] = []
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            message = entry.get("message") if isinstance(entry, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", name):
                    continue
                if name not in tool_names:
                    tool_names.append(name)
                if name == "Skill":
                    args = block.get("input")
                    skill = args.get("skill") if isinstance(args, dict) else None
                    if isinstance(skill, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", skill):
                        if skill not in skill_identifiers:
                            skill_identifiers.append(skill)
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return {"status": "history_invalid", "sha256": digest}
    return {"status": "available", "sha256": digest,
            "tool_names": tool_names, "skill_identifiers": skill_identifiers}


def wrapper_path(host: str, home: Path) -> Path:
    wrapper = home / ".local/lib/coding-agent-environment/bin" / f"ca-{host}"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise DogfoodError(f"Installed private native wrapper is unavailable: {wrapper}")
    return wrapper


def observe_cli_version(wrapper: Path) -> str | None:
    try:
        result = subprocess.run([str(wrapper), "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=False, shell=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.decode("utf-8", errors="replace").strip()
    return version or None


def host_argv(host: str, wrapper: Path, session_id: str, prompt: str,
              read_only: bool = False) -> list[str]:
    if host == "claude":
        args = [str(wrapper), "-p", "--output-format", "json", "--session-id", session_id]
        if read_only:
            args.extend(("--permission-mode", "plan", "--tools=Read,Glob,Grep,Skill"))
        return [*args, prompt]
    args = [str(wrapper), "exec", "--json"]
    if read_only:
        args.extend(("--sandbox", "read-only"))
    return [*args, prompt]


def build_prompt(case: dict, prompt: str, handoff: str | None = None,
                 host: str | None = None) -> str:
    read_boundary = ""
    if case["mode"] in {"plan_only", "native_discovery"}:
        read_boundary = (
            "This case is read-only. Use no write-capable local tools or remote-mutating "
            "integrations. Do not request or bypass approval. If this cannot be enforced, "
            "stop and report that limitation. "
        )
    if host == "codex" and case["id"] == 18:
        read_boundary += (
            "Read-only exec commands are allowed within the already selected read-only "
            "sandbox for inspecting installed skills, the active profile, and locally "
            "available documentation lookup capabilities. Do not write files, install or "
            "enable tools, change settings, or perform remote mutations. "
        )
        prompt = prompt.replace(
            "If Context7 MCP tools are exposed, use resolve-library-id for Python packaging "
            "and then query-docs for current packaging guidance; report the tool use and result. ",
            "Use an already exposed public-documentation lookup tool only if its actual "
            "interface is discoverable; report the observed tool use and result. ",
        )
    text = (
        "Inspect and read applicable installed skill instructions available in this fresh "
        "host session before responding. Distinguish skills actually read from those merely "
        "present, and say plainly if none is available. " + read_boundary + "\n\n" + prompt
    )
    if handoff is not None:
        text += "\n\nDurable handoff artifact from the preceding fresh process follows verbatim:\n\n" + handoff
    return text + (
        "\n\nThis is a fresh-session parity evidence capture. Do not claim work, tool use, "
        "validation, or external state changes that you did not actually perform. Do not "
        "treat this prompt as permission to bypass native approvals or policy."
    )


def execute_process(host: str, wrapper: Path, cwd: Path, stage_dir: Path, case: dict,
                    prompt: str, invocation_id: str, step: int = 1) -> tuple[dict, bool, dict]:
    stage_dir.mkdir(parents=True, mode=PRIVATE_DIR_MODE, exist_ok=True)
    if stage_dir.is_symlink() or not stage_dir.is_dir():
        raise DogfoodError(f"Invalid case evidence directory: {stage_dir}")
    private_write(stage_dir / "request.txt", prompt.encode())
    stdout_path, stderr_path = stage_dir / "stdout.bin", stage_dir / "stderr.bin"
    record_path = stage_dir / "result.json"
    record = {
        "scenario_id": case["id"], "scenario": case["name"], "step": step,
        "mode": case["mode"], "host": host, "invocation_id": invocation_id,
        "status": "STARTING", "working_directory": str(cwd),
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if host == "claude":
        record["requested_session_id"] = invocation_id
    save_record(record_path, record)
    with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
        os.chmod(stdout_path, PRIVATE_MODE)
        os.chmod(stderr_path, PRIVATE_MODE)
        argv = host_argv(host, wrapper, invocation_id, prompt,
                         read_only=case["mode"] in {"plan_only", "native_discovery"})
        record["status"] = "RUNNING"
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=stdout_file, stderr=stderr_file, shell=False,
                                    env=fixture_process_env(), start_new_session=True)
        except OSError as exc:
            record.update(status="FAILED_LAUNCH", detail=str(exc))
            record["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_record(record_path, record, replace=True)
            return record, True, {}
        record["pid"] = proc.pid
        record["process_group_id"] = proc.pid
        save_record(record_path, record, replace=True)
        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:
            try:
                exit_code = proc.poll()
                child_state = "alive" if exit_code is None else "exited"
            except Exception as exc:
                exit_code = None
                child_state = "unknown"
                record["interrupt_observation_error"] = type(exc).__name__
            status = {
                "alive": "RUNNING_HANDLE_PRESERVED",
                "exited": "INTERRUPTED_CHILD_EXITED",
                "unknown": "CHILD_STATE_UNKNOWN",
            }[child_state]
            record.update(status=status, child_state_after_interrupt=child_state,
                          exit_code=exit_code,
                          detail="Interrupted wait; child was not signalled or killed")
            record["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_record(record_path, record, replace=True)
            return record, True, {}
    stdout_bytes, stderr_bytes = stdout_path.read_bytes(), stderr_path.read_bytes()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    record["stdout_sha256"] = hashlib.sha256(stdout_bytes).hexdigest()
    record["stderr_sha256"] = hashlib.sha256(stderr_bytes).hexdigest()
    record["exit_code"] = exit_code
    record["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    metadata = {}
    if exit_code != 0:
        record["status"] = classify_failure(stdout, stderr, exit_code)
        record["detail"] = "Native wrapper exited unsuccessfully; inspect private stdout/stderr evidence"
    else:
        valid, detail, metadata = parse_host_output(host, stdout)
        if host == "claude" and metadata.get("native_session_id"):
            matches = metadata["native_session_id"] == invocation_id
            record["session_id_matches_request"] = matches
            if not matches:
                valid = False
                detail = "Claude returned a session_id different from the requested session ID"
        record["status"] = "CAPTURED" if valid else classify_failure(stdout, stderr, exit_code)
        record["detail"] = detail
    if metadata.get("native_session_id"):
        record["native_session_id"] = metadata["native_session_id"]
    if metadata.get("native_thread_id"):
        record["native_thread_id"] = metadata["native_thread_id"]
    if metadata.get("observed_models"):
        record["resolved_models_observed"] = metadata["observed_models"]
    if host == "claude":
        record["native_tool_evidence"] = collect_claude_tool_evidence(
            Path.home(), cwd, invocation_id, metadata.get("native_session_id"))
    save_record(record_path, record, replace=True)
    stop_run = record["status"] in {
        "FAILED_QUOTA", "FAILED_AUTH", "FAILED_PERMISSION", "FAILED_JOB", "FAILED_EXIT",
        "FAILED_LAUNCH", "RUNNING_HANDLE_PRESERVED", "INTERRUPTED_CHILD_EXITED",
        "CHILD_STATE_UNKNOWN",
    }
    return record, stop_run, metadata


def execute_case(host: str, wrapper: Path, run_dir: Path, case: dict,
                 baseline: dict) -> tuple[dict, bool]:
    case_dir = run_dir / "cases" / f"{case['id']:02d}"
    case_dir.mkdir(parents=True, mode=PRIVATE_DIR_MODE)
    fixture_repo = None
    if case["mode"] == "local_fixture":
        fixture_repo = materialize_fixture(case_dir, case, baseline)
        cwd = fixture_repo
    else:
        work = case_dir / "workspace"
        init_repository(work)
        write_seed_file(work, "README.md", "Empty disposable parity-run workspace.\n")
        git(work, "add", "README.md")
        git(work, "commit", "--quiet", "-m", "🧪 fixture: initialize isolated case")
        cwd = work
    if case["id"] != 17:
        prompt = build_prompt(case, case["prompt"], host=host)
        record, stop_run, _ = execute_process(
            host, wrapper, cwd, case_dir, case, prompt, str(uuid.uuid4()))
        if fixture_repo:
            record["fixture_repository"] = str(fixture_repo)
            save_record(case_dir / "result.json", record, replace=True)
        return record, stop_run

    top_path = case_dir / "result.json"
    top_record = {"scenario_id": 17, "scenario": case["name"], "mode": case["mode"],
                  "host": host, "status": "RUNNING", "steps": []}
    save_record(top_path, top_record)
    handoff = None
    for step, stage in enumerate(case["stages"], 1):
        prompt = build_prompt(case, stage["prompt"], handoff if step == 2 else None,
                              host=host)
        step_record, stop_run, metadata = execute_process(
            host, wrapper, cwd, case_dir / "steps" / f"{step:02d}", case,
            prompt, str(uuid.uuid4()), step=step)
        native_id_key = "native_session_id" if host == "claude" else "native_thread_id"
        top_record["steps"].append({"step": step, "status": step_record["status"],
                                    "invocation_id": step_record["invocation_id"],
                                    "native_session_id": step_record.get("native_session_id"),
                                    "native_thread_id": step_record.get("native_thread_id")})
        if stop_run or step_record["status"] != "CAPTURED":
            top_record.update(status=step_record["status"], detail="Handoff stopped because a stage did not produce a valid host response")
            save_record(top_path, top_record, replace=True)
            return top_record, True
        native_id = step_record.get(native_id_key)
        if not isinstance(native_id, str) or not native_id.strip():
            failure = "FAILED_NATIVE_SESSION_ID_MISSING" if host == "claude" else "FAILED_NATIVE_THREAD_ID_MISSING"
            top_record.update(status=failure,
                              detail=f"Case 17 process {step} returned no observed native {host} ID")
            save_record(top_path, top_record, replace=True)
            return top_record, True
        if step == 2 and native_id == top_record["steps"][0].get(native_id_key):
            top_record.update(status="FAILED_NATIVE_ID_REUSED",
                              detail=f"Case 17 fresh {host} processes returned the same native ID")
            save_record(top_path, top_record, replace=True)
            return top_record, True
        if step == 1:
            handoff = metadata.get("response_text")
            if not isinstance(handoff, str) or not handoff.strip():
                top_record.update(status="FAILED_HANDOFF_MISSING", detail="First process produced no assistant response to persist")
                save_record(top_path, top_record, replace=True)
                return top_record, True
            handoff_bytes = (handoff.rstrip() + "\n").encode()
            private_write(case_dir / "handoff.md", handoff_bytes)
            top_record["handoff_path"] = str(case_dir / "handoff.md")
            top_record["handoff_sha256"] = hashlib.sha256(handoff_bytes).hexdigest()
    top_record.update(status="CAPTURED", acceptance_verdict="NOT_EVALUATED")
    save_record(top_path, top_record, replace=True)
    return top_record, False


def select_scenarios(data: dict, case_id: int | None, all_cases: bool) -> list[dict]:
    if (case_id is None) == (not all_cases):
        raise DogfoodError("Choose exactly one of --case ID or --all")
    if case_id is None:
        return data["scenarios"]
    for scenario in data["scenarios"]:
        if scenario["id"] == case_id:
            return [scenario]
    raise DogfoodError(f"Unknown scenario ID: {case_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", type=int)
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--run", action="store_true", help="explicitly launch fresh native host processes")
    parser.add_argument("--output-dir", help="private persistent output root beneath the current user's home")
    parser.add_argument("--max-cases", type=int, help="optional finite scenario-count cap for --all")
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline()
        scenarios = select_scenarios(baseline, args.case, args.all)
        if args.max_cases is not None:
            if not args.all or args.max_cases < 1:
                raise DogfoodError("--max-cases requires --all and a positive integer")
            omitted_by_budget = scenarios[args.max_cases:]
            scenarios = scenarios[:args.max_cases]
        else:
            omitted_by_budget = []
        home = Path.home().resolve()
        output_root = safe_output_root(home, args.output_dir)
        if not args.run:
            print(f"DRY PLAN: host={args.host}; scenarios={len(scenarios)}; execution=dry_plan")
            if omitted_by_budget:
                print(f"Finite scenario cap leaves {len(omitted_by_budget)} cases NOT_RUN_BY_BUDGET")
            for case in scenarios:
                print(f"{case['id']:02d} {case['mode']}: {case['name']}")
            print("No output directory, session, or host process created. Add --run to capture.")
            return 0
        wrapper = wrapper_path(args.host, home)
        cli_version = observe_cli_version(wrapper)
        if output_root.exists():
            assert_private_dir(output_root, create=False)
        else:
            assert_private_dir(output_root, create=True)
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        run_dir = output_root / run_id
        run_dir.mkdir(mode=PRIVATE_DIR_MODE)
        os.chmod(run_dir, PRIVATE_DIR_MODE)
        run_record = {
            "schema": 1, "status": "RUNNING", "host": args.host,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "baseline": str(BASELINE), "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
            "wrapper": str(wrapper), "selected_scenarios": [item["id"] for item in scenarios],
            "host_cli_version_observed": cli_version,
            "not_run_by_budget": [item["id"] for item in omitted_by_budget],
            "max_cases": args.max_cases,
            "model_or_effort_override": False,
        }
        save_record(run_dir / "run.json", run_record)
        print(f"Private evidence directory: {run_dir}")
        outcomes = []
        stopped = False
        for case in scenarios:
            if stopped:
                record = {"scenario_id": case["id"], "scenario": case["name"], "host": args.host,
                          "status": "NOT_RUN_AFTER_HOST_FAILURE"}
                case_dir = run_dir / "cases" / f"{case['id']:02d}"
                case_dir.mkdir(parents=True, mode=PRIVATE_DIR_MODE)
                save_record(case_dir / "result.json", record)
            else:
                record, stopped = execute_case(args.host, wrapper, run_dir, case, baseline)
                print(f"{case['id']:02d}: {record['status']}")
            outcomes.append(record)
        outcomes.extend({"scenario_id": case["id"], "scenario": case["name"],
                         "host": args.host, "status": "NOT_RUN_BY_BUDGET"}
                        for case in omitted_by_budget)
        run_record.update({
            "status": "CAPTURED" if any(x.get("status") == "CAPTURED" for x in outcomes) else "NO_CAPTURE",
            "host": args.host, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scenarios": [{"id": x.get("scenario_id"), "status": x.get("status")} for x in outcomes],
            "acceptance_verdict": "NOT_EVALUATED",
        })
        save_record(run_dir / "run.json", run_record, replace=True)
        print("Acceptance verdict: NOT_EVALUATED (captures require independent review)")
        return 0 if all(x.get("status") in {
            "CAPTURED", "NOT_RUN_AFTER_HOST_FAILURE", "NOT_RUN_BY_BUDGET",
        } for x in outcomes) else 1
    except (DogfoodError, OSError, subprocess.CalledProcessError) as exc:
        print(f"parity-dogfood: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
