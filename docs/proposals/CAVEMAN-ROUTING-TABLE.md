# Proposal: record the model behind the caveman plugin's Haiku aliases (SPE-171)

Status: PROPOSED, not applied. Needs one owner decision (below). It adds no `deny` or `ask` rule, keeps `caveman@caveman` enabled with its terse-output mode, and respects SPE-106 (static `permissions.deny` stays `[]`).

## Problem
The plugin ships `cavecrew-investigator` and `cavecrew-reviewer` with the bare alias `model: haiku`. `docs/MODEL-ROUTING.md` admits Haiku only as the id-pinned `haiku-worker`, so these two agents are an unadmitted Haiku route, and an alias can move: on Claude Code 2.1.287 to 2.1.292 it resolved to Haiku 4.5.

## Measured (2026-11-19, Claude Code 2.1.325, fresh headless session in an unrelated repository)
One run delegated one read-only task to each agent. `modelUsage` recorded only `claude-sonnet-5-5` (the orchestrating session) and `claude-haiku-5-5` (list-price estimate $0.0148 for both agents together). So on 2.1.325 the alias resolves to the same id the admitted worker pins. This is one observation per version, not a guarantee about other versions.

## Proposed routing-table text (replaces the "outside the admission table" wording once the owner chooses option A)
| Agent | Declared model | Resolved model (measured) | Version | Date | Status |
|---|---|---|---|---|---|
| `haiku-worker` | `claude-haiku-5-5` (id) | `claude-haiku-5-5` | 2.1.306, 2.1.325 | 2026-11-14, 2026-11-19 | admitted, opt-in, id-pinned |
| `caveman:cavecrew-investigator` | `haiku` (alias) | `claude-haiku-5-5` | 2.1.325 | 2026-11-19 | observed route, admitted only while the measurement matches |
| `caveman:cavecrew-reviewer` | `haiku` (alias) | `claude-haiku-5-5` | 2.1.325 | 2026-11-19 | observed route, admitted only while the measurement matches |
| `caveman:cavecrew-builder` | none (inherits) | the session model | n/a | n/a | not a Haiku route |

Rule: an alias agent counts as admitted only while its measured resolved model equals the admitted id. Re-measure after every Claude Code upgrade (this is a detect-and-record control; it does not stop a moved alias from routing in the meantime). A mismatch removes the row from "admitted" and sends the choice back to the owner (SPE-106 amendment, an `ask` rule, or disabling the plugin). Re-measure with one run from any unrelated repository:

`claude -p "Use the Agent tool with subagent_type caveman:cavecrew-investigator to locate the word alpha in notes.txt, then caveman:cavecrew-reviewer to review notes.txt in one line." --output-format json` and read the keys of `modelUsage`.

## What each option costs
- **A (this proposal): record, keep both agents.** Nothing changes on the host; the table becomes accurate. Residual risk: a silently moved alias is noticed only at the next re-measure.
- **B: ask rule for the two agents.** Compatible with SPE-106 (it is how former denies became asks), but each use prompts and an unanswered prompt stalls that turn. Not applied without your decision.
- **C: SPE-106 amendment allowing one deny.** Tested earlier the same day (agents refused, no Haiku usage), narrowest effect, loses both agents. Not applied without your decision.
- No setting constrains an alias without one of those: `CLAUDE_CODE_SUBAGENT_MODEL` overrides every subagent and a plugin agent file cannot be overridden by a user-level file of the same name.

## Decision needed
Choose A, B or C. Until then the host behaves exactly as before (agents available) and the routing doc states that.
