#!/usr/bin/env python3
"""Paired Sonnet-direct vs Sonnet-plus-Haiku-worker evaluation harness (SPE-124).

Gates and the decision rule live in evals/routing/policy.json and were committed before
any arm ran. Subcommands: prepare, run, grade, review, score. No network writes, no
Jira/GitHub access; every arm runs headless in a throwaway one-commit repository outside
the user's development tree. A failed, timed-out or unparseable step always leaves a
record, so a failure can never silently drop out of the scorecard.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "evals/routing/policy.json"
CORPUS_PATH = ROOT / "evals/routing/corpus.json"
HAIKU_ID = "claude-haiku-5-5"
SONNET_ID = "claude-sonnet-5-5"
WORKER = "haiku-worker"
KINDS = ("fresh_input", "cache_write", "cache_read", "output")
_USAGE_KEYS = {
    "fresh_input": "inputTokens",
    "cache_write": "cacheCreationInputTokens",
    "cache_read": "cacheReadInputTokens",
    "output": "outputTokens",
}
_MSG_KEYS = {
    "fresh_input": "input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "output": "output_tokens",
}
HEADLESS_FLAGS = ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                  "--setting-sources", "project"]
# Bash is limited to the test runner and exact read-only git forms; no interpreter wildcard, no network tool.
BASH_ALLOW = ["Bash(python3 -I tests/*)", "Bash(git diff)", "Bash(git diff --stat)", "Bash(git status)",
              "Bash(git status --short)"]
BASE_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR")
AGENT_TOOL_NAMES = ("Agent", "Task")


def scrubbed_env() -> dict:
    """Only what the CLI needs to find its own login; no cloud, GitHub or API credentials."""
    return {k: os.environ[k] for k in ENV_KEEP if k in os.environ}


def forbidden_roots() -> list[Path]:
    """Places an arm must not read: they hold the merged solutions, oracle tests and past transcripts."""
    home = Path.home()
    return [home / "Bryant-Developments", home / ".claude"]


def deny_rules() -> list[str]:
    rules = []
    for root in forbidden_roots():
        for tool in ("Read", "Grep", "Glob", "Edit", "Write"):
            rules.append(f"{tool}(//{str(root).lstrip('/')}/**)")
    return rules


def workdir_is_safe(workdir: Path) -> bool:
    resolved = workdir.resolve()
    return not any(resolved == r or r in resolved.parents for r in forbidden_roots())


ADMISSION = (
    "Delegation policy. You may delegate to the Agent `haiku-worker` only when the required behavior, "
    "interfaces and acceptance oracle are explicit, the work is reversible and low blast radius, and "
    "delegating is cheaper than doing it yourself. Never delegate security or permission changes, work whose "
    "root cause is unknown, design decisions, or trivial edits you can make directly. Use no other agent type. "
    "If you delegate, give a bounded task packet (files, behavior, test command), then review the worker's diff "
    "and run the tests yourself; allow at most one repair attempt. The worker may not certify its own result."
)
WORKER_PROMPT = (
    "You are a bounded implementation worker. Do exactly the task packet. Edit only files the packet names. "
    "Run the stated test command. If requirements are ambiguous, the scope grows, a contract must change, or "
    "two targeted repair attempts fail, stop and return the observed result, evidence and the open decision. "
    "Do not merge, publish, spawn agents, or claim the work is ready; report what you changed and tests run."
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def find_task(corpus: dict, task_id: str, require_pilot: bool = True) -> dict:
    for t in corpus["tasks"]:
        if t["id"] == task_id:
            if require_pilot and not t.get("pilot"):
                raise SystemExit(f"{task_id} is a candidate only, not a pilot task")
            return t
    raise SystemExit(f"unknown task {task_id}")


def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def family(model_id: str) -> str:
    for name in ("sonnet", "haiku", "opus"):
        if name in model_id:
            return name
    return "other"


def usage_vector(result: dict) -> dict:
    """Per model family and kind, from the CLI's modelUsage block."""
    out: dict[str, dict[str, int]] = {}
    for model_id, u in (result.get("modelUsage") or {}).items():
        fam = out.setdefault(family(model_id), {k: 0 for k in KINDS})
        for kind, key in _USAGE_KEYS.items():
            fam[kind] += int(u.get(key, 0) or 0)
    return out


def parse_stream(stdout: str) -> tuple[dict | None, list[str], dict]:
    """Final result event, the subagent types the main thread called, and approximate sidechain usage."""
    final, agents, side = None, [], {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            final = ev
        elif ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            if ev.get("parent_tool_use_id") is not None or ev.get("isSidechain"):
                fam = side.setdefault(family(msg.get("model", "")), {k: 0 for k in KINDS})
                for kind, key in _MSG_KEYS.items():
                    fam[kind] += int((msg.get("usage") or {}).get(key, 0) or 0)
            else:
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") in AGENT_TOOL_NAMES:
                        agents.append((block.get("input") or {}).get("subagent_type") or "?")
    return final, agents, side


def effective_usage(model_usage: dict, agent_calls: list[str], side: dict) -> tuple[dict, str]:
    """Trust modelUsage; if the main thread delegated but modelUsage shows no Haiku, fall back to the stream."""
    if not agent_calls or "haiku" in model_usage:
        return model_usage, "modelUsage"
    if "haiku" in side and any(side["haiku"].values()):
        merged = {fam: dict(v) for fam, v in model_usage.items()}
        merged["haiku"] = side["haiku"]
        return merged, "merged_from_stream"
    return model_usage, "unverified"


def scope_violations(changed: list[str], allowed: set[str]) -> list[str]:
    return sorted(p for p in changed if p not in allowed and not _ignored(p))


def _ignored(path: str) -> bool:
    return "__pycache__/" in path or path.endswith(".pyc")


def accepted(grade: dict, review: dict) -> bool:
    """Oracle, independent review, scope and a non-empty change must all hold; errors never count."""
    if grade.get("error") or review.get("error") or "oracle_passed" not in grade:
        return False
    if not grade.get("changed_files") or grade.get("out_of_scope"):
        return False
    if review.get("critical_high", 1) != 0 or review.get("correct") is not True:
        return False
    oracle = grade["oracle_passed"]
    if oracle is True:
        return True
    if oracle is None:
        return grade.get("oracle_mode") == "review_only"
    return review.get("oracle_coupled") is True  # reviewer adjudicates a name-coupled oracle failure


def task_verdict(task: dict, a: dict, b: dict) -> str:
    """a, b: {accepted, usage, agent_types, accounting}."""
    haiku_tokens = sum(b["usage"].get("haiku", {}).values())
    delegated = bool(b.get("agent_types")) or haiku_tokens > 0
    if any(t != WORKER for t in b.get("agent_types", [])):
        return "INVALID_RUN"
    if task["class"] != "bounded":
        if delegated:
            return "DECLINE_FAILED"
        return "DECLINE_OK" if b["accepted"] else "INCONCLUSIVE"
    if a["accepted"] and not b["accepted"]:
        return "REJECTS_HAIKU"
    if not (a["accepted"] and b["accepted"]) or not delegated or b.get("accounting") == "unverified":
        return "INCONCLUSIVE"
    sa = a["usage"].get("sonnet", {k: 0 for k in KINDS})
    sb = b["usage"].get("sonnet", {k: 0 for k in KINDS})
    if all(sb[k] <= sa[k] for k in KINDS):
        return "SUPPORTS_HAIKU"
    return "NOT_CHEAPER" if all(sb[k] > sa[k] for k in KINDS) else "INCONCLUSIVE"


def pilot_verdict(items: list[tuple[str, str]], expected: int) -> str:
    """items: (task class, verdict). Never promotes; needs every pilot task scored to say OPT_IN_CANDIDATE."""
    verdicts = [v for _, v in items]
    if any(v in ("DECLINE_FAILED", "REJECTS_HAIKU") for v in verdicts):
        return "NO_HAIKU"
    if len(items) < expected:
        return "INCONCLUSIVE_KEEP_OPT_IN"
    bounded = [v for c, v in items if c == "bounded"]
    declines = [v for c, v in items if c != "bounded"]
    if len(bounded) >= 2 and all(v == "SUPPORTS_HAIKU" for v in bounded) \
            and declines and all(v == "DECLINE_OK" for v in declines):
        return "OPT_IN_CANDIDATE"
    return "INCONCLUSIVE_KEEP_OPT_IN"


def changed_tests_only(changes: list[tuple[str, str]]) -> bool:
    return all(path.startswith("tests/") for _, path in changes)


def original_changes(repo: Path, task: dict) -> list[tuple[str, str]]:
    if git("rev-parse", f"{task['merge']}^1", cwd=repo).strip() != task["base"]:
        raise SystemExit(f"{task['id']}: base is not the first parent of the merge commit")
    rows = git("diff", "--name-status", f"{task['merge']}^1", task["merge"], cwd=repo).splitlines()
    return [(r.split("\t")[0][0], r.split("\t")[-1]) for r in rows if r.strip()]


def cmd_prepare(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    out = Path(args.out)
    if not workdir_is_safe(out):
        raise SystemExit(f"{out} is under a directory arms may not read; use a scratch location")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    repo = Path(args.repo)
    archive = subprocess.Popen(["git", "archive", task["base"]], cwd=repo, stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(out)], stdin=archive.stdout, check=True)
    archive.stdout.close()
    if archive.wait() != 0:
        raise SystemExit("git archive failed")
    git("init", "-q", cwd=out)
    git("add", "-A", cwd=out)
    git("-c", "user.name=eval", "-c", "user.email=eval@localhost", "commit", "-q", "-m", "base", cwd=out)
    print(f"prepared {task['id']} at {out}")


def claude_args(arm: str, task: dict) -> tuple[list[str], str]:
    policy = load_json(POLICY_PATH)
    tools = list(BASE_TOOLS) + (["Agent"] if arm == "B" else [])
    base = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", SONNET_ID,
            "--max-turns", str(policy["run_caps"]["max_turns"]),
            "--max-budget-usd", str(policy["run_caps"]["max_budget_usd_estimate"]), *HEADLESS_FLAGS,
            "--tools", ",".join(tools), "--disallowedTools", *deny_rules()]
    prompt = (
        "You are working in a repository at its base state. Complete the task below by editing files in "
        "place. Run the relevant tests with `python3 -I tests/<file>`. Do not use git push, the network, "
        "or edit anything the task does not need. End with a short report of what changed and the tests you ran.\n\n"
        f"TASK:\n{task['task']}"
    )
    allowed = ["Read", "Edit", "Write", "Grep", "Glob", *BASH_ALLOW]
    if arm == "B":
        agents = {WORKER: {"description": "Bounded low-risk implementation worker", "prompt": WORKER_PROMPT,
                           "model": HAIKU_ID, "tools": ["Read", "Edit", "Write", "Grep", "Glob", *BASH_ALLOW]}}
        base += ["--agents", json.dumps(agents)]
        allowed.append("Agent")
        prompt = ADMISSION + "\n\n" + prompt
    return [*base, "--allowedTools", *allowed], prompt


def error_record(task_id: str, arm: str, why: str, **extra) -> dict:
    return {"task": task_id, "arm": arm, "is_error": True, "error": why, "usage": {}, "agent_types": [],
            "accounting": "unverified", "num_turns": None, "harness_wall_ms": 0, **extra}


def cmd_run(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    work = Path(args.workdir)
    if not workdir_is_safe(work):
        raise SystemExit(f"{work} is under a directory arms may not read")
    argv, prompt = claude_args(args.arm, task)
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    extra = {"order_index": args.order_index, "started_at": started}
    try:
        p = subprocess.run(argv, input=prompt, cwd=work, capture_output=True, text=True,
                           timeout=args.timeout, env=scrubbed_env())
        final, agents, side = parse_stream(p.stdout)
        if final is None:
            record = error_record(task["id"], args.arm, f"no result event: {(p.stderr or p.stdout)[-300:]}", **extra)
        else:
            mu = usage_vector(final)
            usage, accounting = effective_usage(mu, agents, side)
            record = {"task": task["id"], "arm": args.arm, "returncode": p.returncode,
                      "is_error": bool(final.get("is_error")), "num_turns": final.get("num_turns"),
                      "usage": usage, "accounting": accounting, "agent_types": agents,
                      "models": sorted((final.get("modelUsage") or {}).keys()),
                      "list_price_estimate_usd_not_a_bill": final.get("total_cost_usd"),
                      "report": (final.get("result") or "")[:1500], **extra}
    except subprocess.TimeoutExpired:
        record = error_record(task["id"], args.arm, f"timeout after {args.timeout}s", **extra)
    record["harness_wall_ms"] = int((time.time() - t0) * 1000)
    Path(args.out).write_text(json.dumps(record, indent=2))
    print(json.dumps({k: record.get(k) for k in ("task", "arm", "is_error", "num_turns", "agent_types")}))


def changed_in_workdir(workdir: Path) -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all", cwd=workdir)
    return [line[3:] for line in out.splitlines() if not _ignored(line[3:])]


def sandbox_profile(tmp: Path) -> str:
    t = str(tmp.resolve())
    return ("(version 1)(allow default)(deny network*)"
            f'(deny file-write* (require-not (require-any (subpath "{t}") (subpath "/dev") '
            '(subpath "/private/var/folders"))))')


def run_oracle(cmd: str, cwd: Path, home: Path, timeout: int) -> tuple[bool, str]:
    if not shutil.which("sandbox-exec"):
        raise SystemExit("sandbox-exec is required to run agent-authored test code safely")
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8"}
    argv = ["sandbox-exec", "-p", sandbox_profile(cwd.parent), *shlex.split(cmd)]
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout + p.stderr)[-800:]


def cmd_grade(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    repo, work = Path(args.repo), Path(args.workdir)
    record = {"task": task["id"], "arm": args.arm}
    try:
        changes = original_changes(repo, task)
        original = {p for _, p in changes}
        changed = changed_in_workdir(work)
        record.update(changed_files=changed, out_of_scope=scope_violations(changed, original))
        mode = "review_only" if not task["oracle_cmd"] else \
            ("own_tests" if changed_tests_only(changes) else "overlay")
        record["oracle_mode"] = mode
        oracle, tail = None, ""
        if mode != "review_only":
            with tempfile.TemporaryDirectory() as tmp:
                sandbox = Path(tmp) / "repo"
                shutil.copytree(work, sandbox, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
                if mode == "overlay":
                    for status, rel in changes:
                        if rel.startswith("tests/") and status != "D":
                            dest = sandbox / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_text(git("show", f"{task['merge']}:{rel}", cwd=repo))
                            if git("ls-tree", task["merge"], rel, cwd=repo).startswith("100755"):
                                dest.chmod(0o755)
                (Path(tmp) / "home").mkdir()
                oracle, tail = run_oracle(task["oracle_cmd"], sandbox, Path(tmp) / "home", args.timeout)
        record.update(oracle_passed=oracle, oracle_tail=tail)
    except (subprocess.TimeoutExpired, SystemExit, OSError) as e:
        record.update(error=f"{type(e).__name__}: {e}")
    Path(args.out).write_text(json.dumps(record, indent=2))
    print(json.dumps({k: record.get(k) for k in ("task", "oracle_mode", "oracle_passed", "out_of_scope", "error")}))


def cmd_review(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    work = Path(args.workdir)
    review: dict
    try:
        git("add", "-A", cwd=work)
        diff = git("diff", "--cached", "HEAD", cwd=work)[:60000]
        prompt = (
            "You are an independent code reviewer. Judge the diff against the task. Count only genuine Critical or "
            "High defects (wrong behavior, missed acceptance criteria, security or contract regressions, edits "
            "outside the task). A result that is correct but implemented differently is not a defect. Set "
            '"oracle_coupled" true only if the change is correct but a test written against the original '
            "implementation's exact names would fail it. Reply with one JSON object only: "
            '{"critical_high": <int>, "correct": <bool>, "oracle_coupled": <bool>, "findings": [<short strings>]}.'
            f"\n\nTASK:\n{task['task']}\n\nDIFF:\n{diff or '(empty diff)'}"
        )
        p = subprocess.run(["claude", "-p", "--output-format", "json", "--model", SONNET_ID, "--max-turns", "1",
                            "--tools", "", *HEADLESS_FLAGS], input=prompt, capture_output=True, text=True,
                           timeout=args.timeout, env=scrubbed_env())
        res = json.loads(p.stdout)
        text = res.get("result", "")
        review = json.loads(text[text.index("{"): text.rindex("}") + 1])
        review["review_usage"] = usage_vector(res)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, SystemExit, OSError) as e:
        review = {"error": f"{type(e).__name__}: {e}"}
    Path(args.out).write_text(json.dumps(review, indent=2))
    print(json.dumps({k: review.get(k) for k in ("critical_high", "correct", "oracle_coupled", "error")}))


def _load_or_error(path: Path, why: str) -> dict:
    return load_json(path) if path.exists() else {"error": why}


def cmd_score(args: argparse.Namespace) -> None:
    corpus, d = load_json(CORPUS_PATH), Path(args.dir)
    pilot = [t for t in corpus["tasks"] if t["pilot"]]
    rows, items = [], []
    for task in pilot:
        arms = {}
        for arm in ("A", "B"):
            run_p = d / f"{task['id']}.{arm}.run.json"
            if not run_p.exists():
                break
            run = load_json(run_p)
            grade = _load_or_error(d / f"{task['id']}.{arm}.grade.json", "grade missing")
            review = _load_or_error(d / f"{task['id']}.{arm}.review.json", "review missing")
            arms[arm] = {"accepted": accepted(grade, review) and not run.get("is_error"), "usage": run.get("usage", {}),
                         "agent_types": run.get("agent_types", []), "accounting": run.get("accounting"),
                         "turns": run.get("num_turns"), "wall_ms": run.get("harness_wall_ms")}
        if len(arms) < 2:
            rows.append(f"| {task['id']} | {task['class']} | NOT RUN | | |")
            continue
        verdict = task_verdict(task, arms["A"], arms["B"])
        items.append((task["class"], verdict))
        sa, sb = arms["A"]["usage"].get("sonnet", {}), arms["B"]["usage"].get("sonnet", {})
        hb = sum(arms["B"]["usage"].get("haiku", {}).values())
        rows.append(f"| {task['id']} | {task['class']} | A={arms['A']['accepted']} B={arms['B']['accepted']} | "
                    f"sonnet A {dict(sa)} / B {dict(sb)}; haiku B tokens {hb}; B agents {arms['B']['agent_types']}; "
                    f"accounting {arms['B']['accounting']} | {verdict} |")
    result = pilot_verdict(items, len(pilot))
    print("| task | class | accepted | usage (tokens per kind) | verdict |\n|---|---|---|---|---|")
    print("\n".join(rows))
    print(f"\nPILOT VERDICT: {result} ({len(items)} of {len(pilot)} paired tasks scored; promotion needs >= 30)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare"); p.add_argument("task"); p.add_argument("--out", required=True)
    p.add_argument("--repo", default=str(ROOT)); p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("run"); p.add_argument("task"); p.add_argument("--arm", choices=("A", "B"), required=True)
    p.add_argument("--workdir", required=True); p.add_argument("--out", required=True)
    p.add_argument("--order-index", type=int, default=0)
    p.add_argument("--timeout", type=int, default=1800); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("grade"); p.add_argument("task"); p.add_argument("--arm", choices=("A", "B"), required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True); p.add_argument("--repo", default=str(ROOT))
    p.add_argument("--timeout", type=int, default=600); p.set_defaults(fn=cmd_grade)
    p = sub.add_parser("review"); p.add_argument("task"); p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True); p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("score"); p.add_argument("--dir", required=True); p.set_defaults(fn=cmd_score)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
