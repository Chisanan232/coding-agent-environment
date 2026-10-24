# Model routing and delegation

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

## Model Routing

- Main model: Sonnet, effort medium, permission mode Auto. This is the default
  for routine implementation, testing, fixing, tool usage, CI work, and
  orchestration — do not switch off it for ordinary work.
- Non-trivial architecture/design/planning (meaningful architecture, significant
  multi-module changes, migrations, security-sensitive design, important
  trade-offs, Jira/task decomposition, materially ambiguous requirements):
  delegate first to the `opus-architect` subagent (`.claude/agents/opus-architect.md`,
  Opus, high effort). It inspects, designs, and returns a distilled plan.
  Sonnet then continues execution automatically — do not stop for approval
  merely because planning finished.
- Skip `opus-architect` for trivial work: typos, renames, formatting, obvious
  small fixes, simple tests, dependency bumps, isolated straightforward edits.
- The `advisorModel` (Opus) is available for important decisions, repeated
  failures, security/architecture uncertainty, and final review of
  sufficiently complex work. Use it selectively, not ritualistically on every
  task.
- Do not enter interactive Plan Mode solely to obtain Opus planning — that's
  what `opus-architect` is for. Only stop and ask the user when there is a
  genuinely necessary human decision.

## Delegation Model

Delegation is capability/reasoning-based, not role-based. The main agent
(Sonnet, medium effort, Auto) executes routine work directly — planning,
implementation, testing, CI repair, merge decisions, release observation.
There is no fixed organizational-role roster (no `dev-agent`/`qa-agent`/
`dev-lead-agent`/`release-agent` split) and no required hand-off between
agent "roles" to move a piece of work forward.

Spawn an isolated sub-agent only when one of these is genuinely true:

1. **Stronger reasoning helps.** Non-trivial architecture/design/planning —
   delegate to `opus-architect` (Opus, high effort). See Model Routing above.
2. **Isolated context reduces noise.** A broad read-only search/audit that
   would otherwise bloat the main context — use a general-purpose or
   `Explore`-type agent, tools restricted to what the task needs.
3. **Independent verification benefits from a fresh perspective.** A
   separate read-only review/QA pass while the main lane keeps working.
4. **Genuine parallelism** — two provably independent pieces of work that
   don't touch overlapping files/state.

Subagent concurrency limits and scope ownership are owned by the `engineering-workflow`
Delegation section.

Do not invent new fixed role names as a substitute for the old roster.
When none of the above applies, do the work directly.

