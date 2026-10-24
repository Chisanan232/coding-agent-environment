# Routing evaluation: Sonnet direct vs Sonnet plus a bounded Haiku worker (SPE-124)

Question: does a bounded Haiku worker raise accepted engineering outcomes per unit of model usage and wall time, without losing quality? The answer may be "no". The default stays Sonnet main until a recorded gate says otherwise.

Gates, arms and the decision rule are in `evals/routing/policy.json`, committed before any arm ran. Tool: `scripts/routing-eval.py`. Corpus: `evals/routing/corpus.json`.

## Method

1. `prepare` extracts the task's base revision (parent of the original merge) into a fresh one-commit repository. No later history exists in it.
2. `run` starts one headless `claude -p` per arm in that directory with a restricted tool list, a turn cap and a budget cap. Arm B defines one Haiku worker for that run only, pinned to an explicit model id. `~/.claude/agents` is never touched.
3. `grade` copies the original change's test files over the result, runs the oracle command, and checks that every changed file is inside the task's owned scope.
4. `review` asks a separate Sonnet session to judge the diff against the acceptance criteria (without the oracle). A reviewer judgement of "correct but different" outranks a test coupled to implementation names.
5. `score` applies the decision rule and prints the scorecard.

## What this can and cannot show

- Six tasks, one trial each. That can reveal gross failure and whether the orchestrator declines work it should not delegate. It cannot show non-inferiority, and it never promotes.
- Usage is a vector per model (fresh input, cache write, cache read, output), plus wall time and turns. No weights are invented. The CLI's `total_cost_usd` is a list-price estimate, not a bill, and subscription utilization is not exposed per run.
- Headless runs load a slimmed context (`--strict-mcp-config --disable-slash-commands`) so both arms pay the same startup. Measured on 2.1.306: a one-word reply cost 190,961 prompt tokens with the default context and about 30,800 with those flags. Realistic interactive subagent startup is measured separately from transcripts (median about 70k for Sonnet, about 37k for Haiku 5.5).
- The `haiku` alias resolved to Haiku 4.5 on Claude Code 2.1.287 to 2.1.292 and to Haiku 5.5 on 2.1.306, so runs pin the explicit id and record the resolved id.
