---
name: evidence-first-briefing
description: Compose or review persistent engineering records for Jira/ClickUp, GitHub, Slack/Teams, releases and validation using semantic outcomes, evidence and explicit material uncertainty.
---

# Evidence-first briefing

Use before emitting persistent/shared engineering content. The calling workflow
owns what facts matter and its operational gates; this skill owns expression.
It adds no authorization to post, merge, close a ticket or notify anyone.

## Semantic delta

Lead with the outcome/current state and what it means for the reader: a changed
boundary, user/business behavior, domain rule, API/data/compatibility contract,
request/data/control flow, security risk, capability or limitation.

The diff/run already owns file edits and commands. Include mechanical detail
only when it is itself evidence needed for a decision, reproduction or action.
Do not lead with file lists, commits or an implementation diary.

## Evidence and uncertainty

Internally distinguish VERIFIED (direct evidence), INFERRED (supported
interpretation) and UNKNOWN / NOT VERIFIED (insufficient evidence).
State supported facts directly. Label material inference and its basis;
keep unknowns explicit, with the missing observation and owner action if known.
Do not prefix every sentence with an epistemic label.

Scope evidence to what it proves. Passing tests do not prove production safety;
a merged PR does not prove deployment; preparation does not prove publication.
Never claim completion, correctness or safety without the corresponding evidence.
Do not hide gaps behind “should be fine”, “probably”, “looks good”, “I believe”,
“seems safe” or “likely completed”. A useful inference must say what supports it
and what remains unproven. Omit praise, celebration, flattery, motivational filler,
emotional padding and unnecessary apologies, except relevant attributed quotations.

## Progressive disclosure

Aim for roughly 10–30 seconds to understand the decision-critical content.
This is a UX constraint, not a measured reading-speed claim or word limit.
Use the smallest useful message. Typical order is outcome → semantic impact →
evidence → material risk/unknown → required action → references. These are
content choices, not required fields or headings; omit empty/non-actionable sections.
A short status may be one sentence. Expand for failures, breaking changes,
material uncertainty or a decision that needs more context.

Use existing code, tickets, PRs, ADRs and runs as canonical references. Explain
only the new decision/delta; do not copy their context or repeat the same proof.
Keep one main claim per sentence/bullet where practical. Choose meaningful
headings only when they improve scanning; avoid nested prose and status tables
unless comparison is itself useful.

## Before emitting

Check internally: What is the outcome and semantic change? Why does the reader
care? What supports each material claim? What is still unknown? Must someone
act? Where does the canonical detail live? What can be deleted without losing
correctness or actionability? Then subtract repetition, chronology and filler.

## Surface examples

These illustrate content selection, not templates to fill. References below
are illustrative; replace them with actual evidence before posting.

- **Jira/ClickUp completion:** “Duplicate submissions now return the original
  receipt. Replay assertions passed [run]; merged [PR].” Omit ticket restatement.
- **Blocker:** “Migration is blocked: staging credentials are unavailable.
  Production compatibility is not verified. Platform owner must restore access
  before rollout [ticket].” Do not invent an owner if none is known.
- **GitHub PR:** “Charge retries reuse the original payment result across the
  API/provider boundary. Existing receipt shape is retained; replay and legacy
  client assertions passed [run]. Provider sandbox timeout recovery is not
  verified; required before rollout [ticket].” The diff owns the file list.
- **Issue/task proposal:** “Reject duplicate imports before queue admission to
  prevent repeated charges. Preserve the current receipt contract. Accept when
  replay keeps one charge and returns its original receipt. Depends on [API
  contract]. Provider timeout behavior remains unknown.” This is proposed scope,
  not a completion claim; apply the canonical Requirement Zero skill if unvalidated.
- **PR review/reply:** “Replay now checks the persisted key before charging;
  concurrent replay assertion passed [run/commit]. Timeout recovery remains
  unresolved in [thread].” Receipt-only acknowledgements usually add no value.
- **Slack/Teams:** “Staging retries now preserve one charge [PR/run]. Production
  rollout awaits sandbox recovery verification; release owner must run it.”
- **Release:** “v2.4 ships replay-safe payments [release]. Legacy receipt clients
  remain compatible in [run]. Timeout recovery is not verified; hold rollout
  until the release owner verifies it.” Distinguish published from prepared state.
- **Validation:** “31/31 required assertions passed [run]. Production recovery
  was not verified because sandbox access was unavailable; rollout remains
  blocked.” Expand failed/unknown cases, not every passing row.
