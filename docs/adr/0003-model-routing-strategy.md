# ADR-0003: Sonnet main + selective Opus advisor/architect, not `opusplan` as default

## Status
Accepted (prior session; ported to this repo in SPE-71)

## Context
`opusplan` (or manually invoking Opus for everything) is slower and more
expensive than needed for the bulk of coding-agent work — implementation,
testing, CI repair, routine tool use. Interactive Plan Mode as a gate
before every non-trivial task also forces a stop-and-approve step that
isn't always a genuinely necessary human decision point.

## Decision
- Main model: Sonnet, medium effort, Auto permission mode — the default
  for routine work.
- `opus-architect` (Opus, high effort, read-only tools): spawned before
  implementation on genuinely non-trivial architecture/design/planning.
  Hands off a distilled plan; Sonnet continues execution without stopping
  for approval merely because planning finished.
- `advisorModel: opus`: available for important decisions, repeated
  failures, security/architecture uncertainty, final review of complex
  work — used selectively, not on every turn.
- Interactive Plan Mode is not the mechanism for obtaining Opus reasoning
  — `opus-architect` is.

## Consequences
- Faster, cheaper default path for the large majority of tasks.
- Requires judgment about when work is "non-trivial enough" for
  `opus-architect` — `.claude/CLAUDE.md`'s Model Routing section gives
  concrete examples (skip for typos/renames/simple fixes; use for
  meaningful architecture/migrations/security-sensitive design).
- SPE-74 (directory profiles, the epic's largest single ticket) is the
  concrete example this session used to validate the pattern: delegated
  to `opus-architect`, which validated Claude/Codex precedence against
  the installed binaries rather than assuming from training data, then
  handed off an implementation-ready plan Sonnet executed without a
  planning-approval stop.
