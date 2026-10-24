# Routing evaluation: Sonnet direct vs Sonnet plus a bounded Haiku worker (SPE-124)

Question: does a bounded Haiku worker raise accepted engineering outcomes per unit of model usage and wall time, without losing quality? The answer may be "no". The default stays Sonnet main until a recorded gate says otherwise.

Gates, arms and the decision rule are in `evals/routing/policy.json`, committed before any arm ran. Tool: `scripts/routing-eval.py`. Corpus: `evals/routing/corpus.json`.

## Method

1. `prepare` extracts the task's base revision (parent of the original merge) into a fresh one-commit repository. No later history exists in it.
2. `run` starts one headless `claude -p` per arm in that directory with a restricted tool list, a turn cap and a budget cap, using `--output-format stream-json` so agent calls are counted. Sonnet and the Haiku worker are pinned by explicit id. Arms load no repository or personal settings (`--setting-sources local`) and may not use tools on `~/Bryant-Developments`, `~/.claude` or `/private/tmp/claude-501` (merged solutions, transcripts, scratchpads). Arm B defines one Haiku worker for that run only. `~/.claude/agents` is never touched.
3. `grade` copies the original change's test files over the result (or, when the original change touched only tests, runs the arm's own tests), runs the oracle in a no-network sandbox that can write only to a scratch directory (one exception below), and checks that the change is non-empty and every changed file is inside the task's owned scope.
4. `review` asks a separate Sonnet session to judge the diff against the acceptance criteria (without the oracle). A reviewer flag that a correct result fails only because a test is coupled to implementation names marks the task for human adjudication; it never overrides a failed oracle.
5. `score` applies the decision rule and prints the scorecard.

## What this can and cannot show

- Six tasks, one trial each. That can reveal gross failure and whether the orchestrator declines work it should not delegate. It cannot show non-inferiority, and it never promotes.
- Usage is a vector per model (fresh input, cache write, cache read, output), plus wall time and turns. No weights are invented. The CLI's `total_cost_usd` is a list-price estimate, not a bill, and subscription utilization is not exposed per run.
- Headless runs load a slimmed context (`--strict-mcp-config --disable-slash-commands`) so both arms pay the same startup. Measured on 2.1.306: a one-word reply cost 190,961 prompt tokens with the default context and about 30,800 with those flags. Realistic interactive subagent startup is measured separately from transcripts (median about 70k for Sonnet, about 37k for Haiku 5.5).
- The `haiku` alias resolved to Haiku 4.5 on Claude Code 2.1.287 to 2.1.292 and to Haiku 5.5 on 2.1.306, so runs pin the explicit id and record the resolved id.

## Isolation limits (stated, not hidden)

Arms are not in a container or VM. Bash is limited to `python3 -I tests/*` and read-only git, the worker gets the same set, and each run gets a scrubbed environment (PATH, HOME, USER, LANG, TERM, TMPDIR only). HOME is kept because the CLI finds its subscription login there. The corpus is therefore public-repository work only, and no corporate or secret-bearing task may be added to it.

The test-runner allowance is deliberate and cannot be removed without changing what is measured: every task asks the arm to add or fix tests, and running them is how a real session verifies its own work. It also means an arm can execute test code it wrote itself. That is bounded by the scrubbed environment, the public-repository-only corpus, the absence of any network or interpreter tool beyond the test runner, and the turn and budget caps; it is not a sandbox. Git is limited to exact read-only forms (`git diff`, `git diff --stat`, `git status`, `git status --short`) so `git difftool` cannot match.

## What changed after review (before any run)

Independent review of PR 129 failed the first version with five High findings, all fixed before any arm ran: a test-only task whose oracle was vacuous (now graded on the arm's own tests and a non-empty change is required); a pilot verdict that could promote on partial data (now every task must be scored); failed or timed-out steps that dropped out of the scorecard (every step now leaves an error record that counts as not accepted); the merged solution being readable from HOME (read, search and write denied under the dev tree and `~/.claude`); and unverified subagent accounting (agent calls are counted from the stream, and a delegating run whose Haiku usage cannot be seen is "unverified" and cannot support Haiku).

A second automated security review then found, and these were fixed before any run: `git` was executed inside a directory the arm can write (a crafted `.git/config` would run code in the harness, confirmed by a minimal reproduction), so grading and review now compute changes and diffs in pure Python against a fresh extract of the base and arms cannot touch `.git`; symlinks resolving outside the scratch copy are removed before an overlay can write through them; the oracle sandbox now also denies reads of the dev tree and `~/.claude` and confines writes to its scratch directory; the reviewer prompt marks the diff as untrusted data, and a reviewer can no longer turn a failed oracle into a pass. Residual: the reviewer is still one model reading arm-written text, so the review-only control task and the critical/high count rest on that single judgement.

## Run 0 (invalid) and what changed

A first pilot attempt ran 8 of 12 arms before an independent review showed it was loading the base repository's own allow rules (including `gh pr merge`) and `defaultMode auto`, and that one oracle could not run in the sandbox. It was stopped immediately. Repository events and the PR list showed no external side effect, but the arm streams were not retained, so that is observation, not proof. Run 0 is invalid and is not used for any verdict. It did show two things that shaped the corrected design, and both are recorded openly: in all four bounded and ambiguous tasks the Sonnet orchestrator under the admission policy never delegated (no agent call, zero Haiku tokens), so arm B behaved like arm A; and two bounded tasks failed their hidden oracles in both arms only because the oracles asserted exact wording or an internal function name the task text never specified. The corrected pilot adds the exact phrases or name to those two tasks, a preflight that every oracle accepts its known-good solution, a stream audit, and a diagnostic arm C that forces delegation so the cost of Haiku work can still be observed when routing declines it. Arm C never feeds the verdict.

## Isolation limits, complete list

- **Denied by rule:** tool use on `~/Bryant-Developments`, `~/.claude`, `/private/tmp/claude-501` and its `/tmp` alias, and on `~/.ssh`, `~/.aws`, `~/.config`, `~/.gnupg`, `~/Library` and common shell and git dotfiles; each arm's own `.git`. These are tool-level rules; the run phase has no sandbox, and arm-written Python can bypass them.
- **Detection, not a boundary:** the stream audit flags network and secret needles, `../` and `$HOME` in shell commands, and any tool path that resolves outside the workdir. It does not see obfuscated code in a written file, environment variables other than `$HOME`, or shell tricks beyond `../`, `~/`, `cd ..`, `cd /` and `$HOME`. Raw streams are kept next to each record. The first pilot's streams were not kept, and its arms could have read secret directories before they were denied: nothing suggests they did, and that cannot be proven.
- **One unsandboxed oracle:** only `t84-refresher-plist`, because its oracle runs `/bin/ps`, a setuid binary `sandbox-exec` cannot exec. It is graded unsandboxed only when the arm's run record exists and carries no network, secret or outside-workdir flag; otherwise grading is refused (fail closed). A test pins that exactly one task is exempt.
- **Conservative false positives:** Haiku tokens with no recorded agent call invalidate an arm B run (the CLI using Haiku internally would also do that), and source or test text containing words like `socket` or `requests` can flag a run. Both fail toward "not scored", never toward promotion.
- **Verdict scope:** SUPPORTS compares Sonnet tokens only; Haiku tokens and wall time are reported beside it and drive the cost discussion, not the rule. Operators run arms A, B and C of a task in separate workdirs; the driver script does.

## Pilot result (2026-10-24)

Six tasks, one trial per arm, Claude Code 2.1.306 on the claude.ai first-party route, pinned `claude-sonnet-5-5` and `claude-haiku-5-5`. Verdict under the precommitted rule: **INCONCLUSIVE_KEEP_OPT_IN**. Haiku does not currently justify promotion, and Sonnet main stays the default. Full detail is in Jira SPE-124 comment 28918.

- Arm B (admission policy, worker available) made **0 agent calls and used 0 Haiku tokens in all six tasks**, including the three bounded ones, so no bounded task could support Haiku. The ambiguous task, the high-risk task and the no-delegate control were all declined; the high-risk task was solved by neither arm.
- Diagnostic arm C (delegation forced) passed oracle and review on the three bounded tasks. Sonnet-side tokens fell (for example t84 cache write 18.5k to 8.8k, output 7.2k to 1.4k) but Haiku added more (t84 Haiku cache read 564k, output 8.2k) and wall time rose on every task (t91 16 to 28 s, t87 15 to 34 s, t84 48 to 66 s). Net plan usage is UNKNOWN.
- Single-trial variance exceeded any effect: arm A failed the oracle on t87 where B, which did the same thing, passed.
- Limits: the first attempt was invalid and unused; this pilot's raw streams were not retained and its audit was later narrowed; the `/tmp` alias and secret directories were not denied during it; N=6 cannot show non-inferiority.
