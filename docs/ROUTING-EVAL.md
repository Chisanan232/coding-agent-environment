# Routing evaluation: Sonnet direct vs Sonnet plus a bounded Haiku worker (SPE-124)

Question: does a bounded Haiku worker raise accepted engineering outcomes per unit of model usage and wall time, without losing quality? The answer may be "no". The default stays Sonnet main until a recorded gate says otherwise.

Gates, arms and the decision rule are in `evals/routing/policy.json`, committed before any arm ran. Tool: `scripts/routing-eval.py`. Corpus: `evals/routing/corpus.json`.

## Method

1. `prepare` extracts the task's base revision (parent of the original merge) into a fresh one-commit repository. No later history exists in it.
2. `run` starts one headless `claude -p` per arm in that directory with a restricted tool list, a turn cap and a budget cap, using `--output-format stream-json` so agent calls are counted. Sonnet and the Haiku worker are pinned by explicit id. Arms may not read `~/Bryant-Developments` or `~/.claude` (merged solutions and past transcripts). Arm B defines one Haiku worker for that run only. `~/.claude/agents` is never touched.
3. `grade` copies the original change's test files over the result (or, when the original change touched only tests, runs the arm's own tests), runs the oracle in a no-network sandbox that can write only to a scratch directory, and checks that the change is non-empty and every changed file is inside the task's owned scope.
4. `review` asks a separate Sonnet session to judge the diff against the acceptance criteria (without the oracle). A reviewer judgement of "correct but different" outranks a test coupled to implementation names.
5. `score` applies the decision rule and prints the scorecard.

## What this can and cannot show

- Six tasks, one trial each. That can reveal gross failure and whether the orchestrator declines work it should not delegate. It cannot show non-inferiority, and it never promotes.
- Usage is a vector per model (fresh input, cache write, cache read, output), plus wall time and turns. No weights are invented. The CLI's `total_cost_usd` is a list-price estimate, not a bill, and subscription utilization is not exposed per run.
- Headless runs load a slimmed context (`--strict-mcp-config --disable-slash-commands`) so both arms pay the same startup. Measured on 2.1.306: a one-word reply cost 190,961 prompt tokens with the default context and about 30,800 with those flags. Realistic interactive subagent startup is measured separately from transcripts (median about 70k for Sonnet, about 37k for Haiku 5.5).
- The `haiku` alias resolved to Haiku 4.5 on Claude Code 2.1.287 to 2.1.292 and to Haiku 5.5 on 2.1.306, so runs pin the explicit id and record the resolved id.

## Isolation limits (stated, not hidden)

Arms are not in a container or VM. Bash is limited to `python3 -I tests/*` and read-only git, the worker gets the same set, and each run gets a scrubbed environment (PATH, HOME, USER, LANG, TERM, TMPDIR only). HOME is kept because the CLI finds its subscription login there, so an arm can still read files under HOME with the Read tool. The corpus is therefore public-repository work only, and no corporate or secret-bearing task may be added to it.

The test-runner allowance is deliberate and cannot be removed without changing what is measured: every task asks the arm to add or fix tests, and running them is how a real session verifies its own work. It also means an arm can execute test code it wrote itself. That is bounded by the scrubbed environment, the public-repository-only corpus, the absence of any network or interpreter tool beyond the test runner, and the turn and budget caps; it is not a sandbox. Git is limited to exact read-only forms (`git diff`, `git diff --stat`, `git status`, `git status --short`) so `git difftool` cannot match.

## What changed after review (before any run)

Independent review of PR 129 failed the first version with five High findings, all fixed before any arm ran: a test-only task whose oracle was vacuous (now graded on the arm's own tests and a non-empty change is required); a pilot verdict that could promote on partial data (now every task must be scored); failed or timed-out steps that dropped out of the scorecard (every step now leaves an error record that counts as not accepted); the merged solution being readable from HOME (read, search and write denied under the dev tree and `~/.claude`); and unverified subagent accounting (agent calls are counted from the stream, and a delegating run whose Haiku usage cannot be seen is "unverified" and cannot support Haiku).
