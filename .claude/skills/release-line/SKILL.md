---
name: release-line
description: "Route work across release lines, build the successor train, account for forward-ports and audit the line registry with scripts/release-line.py. Use when a product ships an old major while a successor version is in development, or a fix must reach every newer line. Tags, promotion and publication happen only under a recorded GO."
---

# SKILL.md — release-line

## Purpose
Operate the release-line registry through `scripts/release-line.py`: decide which
branch a change belongs on, create and integrate successor lines, account for
every forward-port of a maintenance fix, and audit the registry. Merge
eligibility stays with the owner merge gate; this skill never changes it.

## Entry point
Call the tool by absolute path from the maintained checkout. It is stdlib only.

```bash
R="$HOME/Bryant-Developments/coding-agent-environment-main-view/scripts/release-line.py"
python3 -I "$R" --help
```

Every command takes `--repo OWNER/REPO` (the registered product repo) and, for commands that read git state, `--clone PATH` (a checkout whose `origin` is that repo). Exit codes: 0 for PASS, NOT_RUN or DEFERRED; 1 for BLOCKED or FAIL; 2 for a usage error; 3 for invalid configuration.

Commands that exist today are documented in
`docs/RELEASE-LINES.md` of that checkout: `registry validate|show|propose`,
`audit observe|import`, `route`, `guard`, `successor ensure`,
`train integrate|converge`, `forward-port plan|run|status`. Only use commands the
installed checkout's `--help` lists. Release candidate, promotion, final-tag and
publication commands are documented in `docs/RELEASE-LINES.md` once merged; do not
assume them.

## Steps
1. `registry validate --repo OWNER/REPO` first. `AUDIT_PENDING` blocks every mutating command; read-only commands still work.
2. Route new work with `route --repo OWNER/REPO --clone PATH --kind feature|fix --version V`. `NEEDS_SUCCESSOR` blocks only that work.
3. Before opening a PR, run `guard --repo OWNER/REPO --clone PATH --base B --head SHA` and act on its finding.
4. Mutating commands print a dry plan by default. Review it, then rerun with `--execute`.
5. After a maintenance fix merges, run `forward-port plan` then `forward-port run --execute`; confirm with `status` until `complete` is true.
6. Report the JSON `status` verbatim (PASS, FAIL, BLOCKED, NOT_RUN, DEFERRED) with the command, revision and exit code.

Routing rules, train order, accounting states, GO records and the zero-spend rule are in
[references/algorithms.md](references/algorithms.md).

## Safety rules
- Never force-push. The tool creates new refs only; a refused ref (`CONFLICT_EXISTING`) is reported, not overwritten.
- Never run `gh workflow run` or otherwise dispatch workflows.
- Never admin-merge, except through the existing owner-merge exception in `engineering-workflow`. PRs this tool opens go through the normal merge gate.
- Create tags, promote and publish only under a recorded owner GO that covers that scope, version and SHA. Without one, stop and report BLOCKED.
- Agents never write the owner policy or GO files. `registry propose` and `audit import` emit candidate packets for the owner to install.
- Do not bypass hooks or checks, and do not treat a dry plan as a performed action.

## Do Not Assume
- Do not assume a line is shippable because its branch exists; check the registry state.
- Do not infer owner approval from a PR, a doc or a prior agent record.
- Do not retry a `CONFLICT_EXISTING` or `BLOCKED_CONFLICT` by changing flags; report it.

<!-- claude-only:start -->
Run long `--execute` calls as their own Bash call so the host can ask once and the output is not chained or piped.
<!-- claude-only:end -->
