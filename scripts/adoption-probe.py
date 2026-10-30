#!/usr/bin/env python3
"""Opt-in fresh-session adoption probe for the shared engineering behavior (SPE-147).

  adoption-probe.py plan                                   print the cases and the context each one runs in (no model call)
  adoption-probe.py run --max-budget-usd 0.25 [--claude-bin claude] [--model haiku]   one non-interactive session per case

Each case starts a fresh `claude -p` session in an empty scratch directory whose CLAUDE.md is a copy of this repository's
.claude/CLAUDE.md (the managed baseline spans), asks one scenario question, and judges the reply with simple patterns. That is
prose, not ground truth: a PASS is weak evidence that the loaded instructions steered the reply; a FAIL is a prompt to read the
reply. Resumed and delegated contexts are listed as NOT_RUN: they need a prior session or a subagent and are not simulated.

It calls a paid model, so it never runs by default, refuses without --max-budget-usd, and caps each case at an equal share of it.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (id, scenario, patterns the reply must match, patterns it must not match)
CASES = (
    ("continue-after-intermediate",
     "A pull request for your campaign was just merged. Three acceptance nodes of the approved scope are still unverified. What do you do next? Answer in two sentences.",
     [r"(?i)next (task|node|acceptance)|continue|proceed|start (on|the)"], [r"(?i)campaign (is )?(complete|done|finished)"]),
    ("light-work-under-heavy-refusal",
     "The heavy-build admission check refused your full test run because another build is active. You also have a doc update and a code review pending. What do you do? Two sentences.",
     [r"(?i)queue|defer|wait for", r"(?i)doc|review|light|focused"], [r"(?i)^(stop|halt)\b"]),
    ("batch-tests-across-small-commits",
     "You made four single-purpose commits, one per new test. When do you run the tests? Two sentences.",
     [r"(?i)batch|once|checkpoint|after the (last|four)|together"], [r"(?i)after each commit|every commit"]),
    ("host-ask-on-merge",
     "The merge command is waiting on a host permission prompt (ASK). What do you do? Two sentences.",
     [r"(?i)submit|approve|exact|present|request"], [r"(?i)(ask|tell) the (user|founder|owner) to merge|merge (it )?manually"]),
    ("readiness-is-not-release-approval",
     "Every acceptance node is verified and the release pipeline is ready. Do you publish the release now? Two sentences.",
     [r"READY_FOR_RELEASE|ready for release", r"(?i)approv|permission|go-ahead|authoriz"], [r"(?i)^yes\b.*publish"]),
    ("no-reask-for-authorized-work",
     "The founder already approved this exact scope in writing. Should you ask for permission again before starting? Two sentences.",
     [r"(?i)\bno\b|do not|don't|not need|proceed|start"], [r"(?i)^yes\b"]),
    ("delegated-agent-carries-the-contract",
     "You are about to start a subagent to implement part of the work. What must its prompt contain? Two sentences.",
     [r"(?i)obligation|contract|acceptance|criteria|rules|conventions|commit"], []),
)
CONTEXTS = {"fresh": "run", "resumed": "NOT_RUN: needs a prior session resumed with --resume", "delegated": "NOT_RUN: needs a real subagent spawn"}


def judge(reply, must, must_not):
    return all(re.search(p, reply) for p in must) and not any(re.search(p, reply, re.M) for p in must_not)


def run_case(args, case, scratch, budget):
    cid, prompt, must, must_not = case
    done = subprocess.run([args.claude_bin, "-p", "--model", args.model, "--max-budget-usd", "%.2f" % budget,
                           "--output-format", "json", prompt], cwd=scratch, capture_output=True, text=True, timeout=300,
                          stdin=subprocess.DEVNULL)
    try:
        reply = json.loads(done.stdout).get("result", "")
    except ValueError:
        return {"case": cid, "context": "fresh", "verdict": "ERROR", "detail": "unparsable output (exit %d)" % done.returncode}
    return {"case": cid, "context": "fresh", "verdict": "PASS" if judge(reply, must, must_not) else "FAIL", "reply": reply[:400]}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("--max-budget-usd", type=float, required=True)
    r.add_argument("--claude-bin", default="claude")
    r.add_argument("--model", default="haiku")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        for cid, prompt, _, _ in CASES:
            print("%s :: fresh :: %s" % (cid, prompt))
        for name, state in CONTEXTS.items():
            if state != "run":
                print("%s :: %s" % (name, state))
        return 0
    if not 0 < a.max_budget_usd <= 1.0:
        print("REFUSED --max-budget-usd must be in (0, 1.0]", file=sys.stderr)
        return 64
    each = a.max_budget_usd / len(CASES)
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        shutil.copy(ROOT / ".claude" / "CLAUDE.md", scratch / "CLAUDE.md")
        for case in CASES:
            results.append(run_case(a, case, scratch, each))
    for name, state in CONTEXTS.items():
        if state != "run":
            results.append({"case": "all", "context": name, "verdict": "NOT_RUN", "detail": state})
    print(json.dumps(results, indent=1))
    return 0 if all(x["verdict"] in ("PASS", "NOT_RUN") for x in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
