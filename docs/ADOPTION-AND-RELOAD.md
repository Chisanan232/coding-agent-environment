# Adoption coverage and session reload

How to see which projects and running sessions use the shared policy, and how to reload it without losing work or
overwriting settings. Nothing here changes a live file or a running session.

## Measure (read-only)

```
python3 scripts/adoption-coverage.py --map projects.json
```

`projects.json` maps product names to their local checkouts (`{"Fornax": ["/abs/path", ...]}`); keep it outside the
repository because paths are machine-local. For each checkout the report lists line numbers only, never text.

| Status | Meaning | Action |
|---|---|---|
| `ADOPTED` | A project file exists and none of the checks below fire | none |
| `OVERRIDE_REVIEW` | A project file permits squash merging, uses the three-part branch form, or uses the unparenthesised commit form | read the lines; keep a documented override, otherwise fix in a per-repo ticket and PR |
| `UNVERIFIED` | A project file was not read: it resolves outside the checkout (a link, or a linked parent directory), exceeds 1 MiB, or is unreadable | inspect it by hand; it is never reported as adopted |
| `NO_PROJECT_FILE` | No `.claude/CLAUDE.md`, `CLAUDE.md` or `AGENTS.md` | the shared policy applies unchanged; add a file only if the repo needs overrides |
| `NOT_FOUND` | The mapped path is missing | fix the map; do not guess a checkout |

Project values win over the global file (`~/.claude/CLAUDE.md`, layering rule), so a stale form in a project file defeats
the shared rule even after the global file is updated. The checks are line heuristics: a flag is a prompt to read the
line, and a clean result is not proof that the project file is compatible.

An override is legitimate when it is documented repository policy (for example a squash-only repository, which the merge
strategy decision allows with an approved repository policy). Record it; do not "fix" it from this repository. A change to a
product repository is its own ticket, branch and PR in that repository.

The same command lists live Claude sessions. It reads each session record (pid, working directory, start time), confirms
the pid is alive and is the same process by start time (a reused pid is ignored), and never reads a process command line or
a session name. A session is `RELOAD_NEEDED` when it started before the newest of the global file or its project file last
changed. Only checkouts listed in the map contribute a project file; a session in an unmapped repository is judged
against the global file alone.

## Reload without losing work

A running session keeps the instructions it loaded when it started. The installer, a rollout or a project edit never
changes it, and nothing here should: do not signal, restart or kill a session on its owner's behalf.

1. Let the session reach a checkpoint: commit or note uncommitted work, record the ticket, branch, head SHA and next step
   (`session-memory.sh`, `workflow-state.sh`).
2. Start a new session in the same worktree, or resume the old one, then run `/workflow-resume <ticket>` for ticketed work.
   The worktree, branch and workflow state are on disk and survive; only the loaded instructions change.
3. Confirm the new text is in effect with a fresh-session probe (the behavior matrix in
   `docs/ENGINEERING-CONSTITUTION.md` section 12). Whether a *resumed* session re-reads the instruction files is
   NOT_VERIFIED until a resume probe has run; treat a fresh session as the only guaranteed reload.
4. Sessions that stay `RELOAD_NEEDED` after a deployment keep running the earlier policy. Report them by working directory so
   their owner reloads at a natural stopping point.

Never copy a repository file over a live one to "reload". Deployment of the global files stays owner-run, hash-pinned and
backed up (`docs/ENGINEERING-POLICY.md` section 7, `docs/DEPLOY-SPE-108.md`).

## Coverage report format

Report coverage per project (status and flagged line numbers), the count of live sessions and how many need a reload,
projects whose checkout could not be located (not assessed, not adopted), and each override with its decision (documented,
fix ticket, or unknown). Never infer adoption from a file existing.

## Fresh-session probe (opt-in, spends model tokens)

`python3 scripts/adoption-probe.py plan` lists the seven scenario questions (continue after an intermediate result, light work
while heavy admission is refused, batching tests across small commits, answering a host ASK, readiness versus release approval,
not re-asking for authorized work, briefing a delegated agent). `adoption-probe.py run --max-budget-usd N` (N at most 1.0, split
evenly) starts one fresh `claude -p` haiku session per case in an empty directory carrying this repository's `.claude/CLAUDE.md`
and judges the reply with simple patterns. That is prose, so a PASS is weak evidence and a FAIL is a prompt to read the reply.
The resumed and delegated contexts are reported NOT_RUN: they need a prior session or a real subagent and are not simulated.
The behavior map (`docs/ENGINEERING-BASELINE.md`, "Required behavior map") and `tests/test-baseline-behavior-map.py` are the
contract-level check that the behavior is still written down in the shared source and in both host entry points.
