---
name: ticket-intake
description: "Establish an actionable outcome and acceptance criteria in the canonical ticket."
---

# SKILL.md — ticket-intake

## Purpose
Establish an actionable outcome and acceptance criteria in the canonical ticket.

## Type
Command-like. Use for new unaccepted work or a material requirement change.
Approved work has passed the requirement decision; do not reopen it automatically.

## Steps

1. Use the configured issue tracker to select the relevant new work. Read its
   description, comments, dependencies and existing decisions once.
2. Verify current behavior in code. Identify the intended outcome, constraints,
   observable acceptance, meaningful dependencies and material unknowns/risks.
   For NEW unvalidated scope use canonical `requirement-zero:requirement-zero`.
3. Search related issues/docs before creating another artifact. Reuse only a
   semantically matching issue. Resolve routine details from available evidence;
   ask concise questions only for consequential unresolved requirements.
4. Update the existing outcome/criteria in place only when a decision changes
   them. Preserve decision-critical original constraints; link a discussion that
   owns the rationale. Do not append repeated “Refined Requirements” or post a
   comment restating an already complete description.
5. Detect actual cross-repo dependencies from acceptance/code/contracts rather
   than keywords alone. Reference those dependencies in the ticket when useful.
6. Check dependencies, scope and blocking questions before moving to the
   tracker's supported accepted/ready state. Use `task-decomposition` for one
   repo or `cross-repo-coordinator` when coordinated integration is required.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
The ticket owns intended outcome, decision-relevant context, constraints,
acceptance, dependencies and material risks/unknowns. Post a comment only for a
new decision, unresolved question or required action not already represented.
Do not announce routine successful checks or repeat existing ticket context.
