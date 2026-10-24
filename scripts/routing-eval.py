#!/usr/bin/env python3
"""Paired Sonnet-direct vs Sonnet-plus-Haiku-worker evaluation harness (SPE-124).

Gates and the decision rule live in evals/routing/policy.json and were committed before
any arm ran. Subcommands: prepare, run, grade, review, score. No network writes, no
Jira/GitHub access; every arm runs headless in a throwaway one-commit repository.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "evals/routing/policy.json"
CORPUS_PATH = ROOT / "evals/routing/corpus.json"
HAIKU_ID = "claude-haiku-5-5"
KINDS = ("fresh_input", "cache_write", "cache_read", "output")
_USAGE_KEYS = {
    "fresh_input": "inputTokens",
    "cache_write": "cacheCreationInputTokens",
    "cache_read": "cacheReadInputTokens",
    "output": "outputTokens",
}
HEADLESS_FLAGS = ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
# Bash is limited to the test runner shape and read-only git; no interpreter wildcard, no network tools.
BASH_ALLOW = ["Bash(python3 -I tests/*)", "Bash(git diff)", "Bash(git diff --stat)", "Bash(git status)",
              "Bash(git status --short)"]
WORK_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", *BASH_ALLOW]
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR")


def scrubbed_env() -> dict:
    """Only what the CLI needs to find its own login; no cloud, GitHub or API credentials."""
    return {k: os.environ[k] for k in ENV_KEEP if k in os.environ}

ADMISSION = (
    "Delegation policy. You may delegate to the Agent `haiku-worker` only when the required behavior, "
    "interfaces and acceptance oracle are explicit, the work is reversible and low blast radius, and "
    "delegating is cheaper than doing it yourself. Never delegate security or permission changes, work whose "
    "root cause is unknown, design decisions, or trivial edits you can make directly. If you delegate, give a "
    "bounded task packet (files, behavior, test command), then review the worker's diff and run the tests "
    "yourself; allow at most one repair attempt. The worker may not certify its own result."
)
WORKER_PROMPT = (
    "You are a bounded implementation worker. Do exactly the task packet. Edit only files the packet names. "
    "Run the stated test command. If requirements are ambiguous, the scope grows, a contract must change, or "
    "two targeted repair attempts fail, stop and return the observed result, evidence and the open decision. "
    "Do not merge, publish, spawn agents, or claim the work is ready; report what you changed and tests run."
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def find_task(corpus: dict, task_id: str) -> dict:
    for t in corpus["tasks"]:
        if t["id"] == task_id:
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


def scope_violations(changed: list[str], allowed: set[str]) -> list[str]:
    ignored = ("__pycache__/", ".pyc")
    return sorted(p for p in changed if p not in allowed and not any(i in p for i in ignored))


def accepted(grade: dict, review: dict) -> bool:
    oracle_ok = grade.get("oracle_passed") in (True, None)
    return bool(oracle_ok and not grade.get("out_of_scope") and review.get("critical_high", 1) == 0)


def task_verdict(task: dict, a: dict, b: dict) -> str:
    """a, b: {accepted: bool, usage: {family: {kind: n}}}."""
    haiku_tokens = sum(b["usage"].get("haiku", {}).values())
    if task["class"] != "bounded":
        return "DECLINE_OK" if haiku_tokens == 0 and b["accepted"] else "DECLINE_FAILED"
    if a["accepted"] and not b["accepted"]:
        return "REJECTS_HAIKU"
    if not (a["accepted"] and b["accepted"]):
        return "INCONCLUSIVE"
    sa = a["usage"].get("sonnet", {k: 0 for k in KINDS})
    sb = b["usage"].get("sonnet", {k: 0 for k in KINDS})
    if haiku_tokens == 0:
        return "INCONCLUSIVE"  # arm B never delegated; nothing to compare
    return "SUPPORTS_HAIKU" if all(sb[k] <= sa[k] for k in KINDS) else "INCONCLUSIVE"


def pilot_verdict(verdicts: list[str]) -> str:
    bounded = [v for v in verdicts if v in ("SUPPORTS_HAIKU", "REJECTS_HAIKU", "INCONCLUSIVE")]
    if any(v == "DECLINE_FAILED" for v in verdicts) or any(v == "REJECTS_HAIKU" for v in bounded):
        return "NO_HAIKU"
    if bounded and all(v == "SUPPORTS_HAIKU" for v in bounded):
        return "OPT_IN_CANDIDATE"
    return "INCONCLUSIVE_KEEP_OPT_IN"


def cmd_prepare(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    out = Path(args.out)
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
    base = ["claude", "-p", "--output-format", "json", "--max-turns", str(policy["run_caps"]["max_turns"]),
            "--max-budget-usd", str(policy["run_caps"]["max_budget_usd_estimate"]), *HEADLESS_FLAGS,
            "--model", "sonnet"]
    prompt = (
        "You are working in a repository at its base state. Complete the task below by editing files in "
        "place. Run the relevant tests with `python3 -I tests/<file>`. Do not use git push, the network, "
        "or edit anything the task does not need. End with a short report of what changed and the tests you ran.\n\n"
        f"TASK:\n{task['task']}"
    )
    tools = list(WORK_TOOLS)
    if arm == "B":
        agents = {"haiku-worker": {"description": "Bounded low-risk implementation worker",
                                   "prompt": WORKER_PROMPT, "model": HAIKU_ID,
                                   "tools": ["Read", "Edit", "Write", "Grep", "Glob", *BASH_ALLOW]}}
        base += ["--agents", json.dumps(agents)]
        tools.append("Agent")
        prompt = ADMISSION + "\n\n" + prompt
    return [*base, "--allowedTools", *tools], prompt


def cmd_run(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    argv, prompt = claude_args(args.arm, task)
    t0 = time.time()
    p = subprocess.run(argv, input=prompt, cwd=args.workdir, capture_output=True, text=True,
                       timeout=args.timeout, env=scrubbed_env())
    wall_ms = int((time.time() - t0) * 1000)
    try:
        result = json.loads(p.stdout)
    except json.JSONDecodeError:
        result = {"is_error": True, "result": p.stdout[-500:] or p.stderr[-500:]}
    record = {"task": task["id"], "arm": args.arm, "returncode": p.returncode, "harness_wall_ms": wall_ms,
              "is_error": result.get("is_error"), "num_turns": result.get("num_turns"),
              "usage": usage_vector(result), "models": sorted((result.get("modelUsage") or {}).keys()),
              "list_price_estimate_usd_not_a_bill": result.get("total_cost_usd"),
              "report": (result.get("result") or "")[:1500]}
    Path(args.out).write_text(json.dumps(record, indent=2))
    print(json.dumps({k: record[k] for k in ("task", "arm", "is_error", "num_turns", "models")}))


def changed_in_workdir(workdir: Path) -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all", cwd=workdir)
    return [line[3:] for line in out.splitlines()]


def cmd_grade(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    repo, work = Path(args.repo), Path(args.workdir)
    original = git("diff", "--name-only", f"{task['merge']}^1", task["merge"], cwd=repo).split()
    violations = scope_violations(changed_in_workdir(work), set(original))
    oracle = None
    detail = ""
    if task["oracle_cmd"]:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp) / "repo"
            shutil.copytree(work, sandbox, ignore=shutil.ignore_patterns("__pycache__"))
            for rel in original:
                if rel.startswith("tests/"):
                    dest = sandbox / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(git("show", f"{task['merge']}:{rel}", cwd=repo))
            env = {"PATH": os.environ["PATH"], "HOME": str(Path(tmp) / "home"), "LANG": "C.UTF-8"}
            (Path(tmp) / "home").mkdir()
            p = subprocess.run(shlex.split(task["oracle_cmd"]), cwd=sandbox, env=env,
                               capture_output=True, text=True, timeout=args.timeout)
            oracle, detail = p.returncode == 0, (p.stdout + p.stderr)[-800:]
    Path(args.out).write_text(json.dumps({"task": task["id"], "oracle_passed": oracle,
                                          "out_of_scope": violations, "oracle_tail": detail}, indent=2))
    print(json.dumps({"task": task["id"], "oracle_passed": oracle, "out_of_scope": violations}))


def cmd_review(args: argparse.Namespace) -> None:
    task = find_task(load_json(CORPUS_PATH), args.task)
    work = Path(args.workdir)
    git("add", "-A", cwd=work)
    diff = git("diff", "--cached", "HEAD", cwd=work)[:60000]
    prompt = (
        "You are an independent code reviewer. Judge the diff against the task. Count only genuine Critical or "
        "High defects (wrong behavior, missed acceptance criteria, security or contract regressions, edits "
        "outside the task). A result that is correct but implemented differently is not a defect. Reply with "
        'one JSON object only: {"critical_high": <int>, "correct": <bool>, "findings": [<short strings>]}.\n\n'
        f"TASK:\n{task['task']}\n\nDIFF:\n{diff or '(empty diff)'}"
    )
    p = subprocess.run(["claude", "-p", "--output-format", "json", "--model", "sonnet", "--max-turns", "1",
                        "--tools", "", *HEADLESS_FLAGS], input=prompt, capture_output=True, text=True,
                       timeout=args.timeout, env=scrubbed_env())
    res = json.loads(p.stdout)
    text = res.get("result", "")
    review = json.loads(text[text.index("{"): text.rindex("}") + 1])
    review["review_usage"] = usage_vector(res)
    Path(args.out).write_text(json.dumps(review, indent=2))
    print(json.dumps({k: review[k] for k in ("critical_high", "correct")}))


def cmd_score(args: argparse.Namespace) -> None:
    corpus, d = load_json(CORPUS_PATH), Path(args.dir)
    rows, verdicts = [], []
    for task in (t for t in corpus["tasks"] if t["pilot"]):
        arms = {}
        for arm in ("A", "B"):
            try:
                run = load_json(d / f"{task['id']}.{arm}.run.json")
                grade = load_json(d / f"{task['id']}.{arm}.grade.json")
                review = load_json(d / f"{task['id']}.{arm}.review.json")
            except FileNotFoundError:
                break
            arms[arm] = {"accepted": accepted(grade, review) and not run["is_error"],
                         "usage": run["usage"], "turns": run["num_turns"], "wall_ms": run["harness_wall_ms"]}
        if len(arms) < 2:
            rows.append(f"| {task['id']} | {task['class']} | not run | | |")
            continue
        verdict = task_verdict(task, arms["A"], arms["B"])
        verdicts.append(verdict)
        sa, sb = arms["A"]["usage"].get("sonnet", {}), arms["B"]["usage"].get("sonnet", {})
        hb = sum(arms["B"]["usage"].get("haiku", {}).values())
        rows.append(f"| {task['id']} | {task['class']} | A={arms['A']['accepted']} B={arms['B']['accepted']} | "
                    f"sonnet A {dict(sa)} / B {dict(sb)}; haiku B tokens {hb} | {verdict} |")
    pilot = pilot_verdict(verdicts)
    print("| task | class | accepted | usage (tokens per kind) | verdict |\n|---|---|---|---|---|")
    print("\n".join(rows))
    print(f"\nPILOT VERDICT: {pilot} (n={len(verdicts)} paired tasks; promotion needs >= 30)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare"); p.add_argument("task"); p.add_argument("--out", required=True)
    p.add_argument("--repo", default=str(ROOT)); p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("run"); p.add_argument("task"); p.add_argument("--arm", choices=("A", "B"), required=True)
    p.add_argument("--workdir", required=True); p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=1800); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("grade"); p.add_argument("task"); p.add_argument("--workdir", required=True)
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
