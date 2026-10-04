---
name: task-decomposition
description: Plan approved work by semantic outcome and dependencies; create child issues only when independent tracking adds concrete value.
---

# Task decomposition

Read the ticket, current implementation, related work and constraints before
planning. Approved requirements are settled. Use canonical
`requirement-zero:requirement-zero` only for newly proposed unvalidated scope;
use `requirement-zero:codebase-zero` for existing artifact audit decisions.

## Plan within the current work item

Identify acceptance criteria, affected boundaries, dependencies and material
security/compatibility/migration risks. Keep implementation steps and validation
inside the parent or an existing implementation plan. Reference acceptance
criteria rather than copying them. Resolve routine choices from evidence;
ask only when a consequential requirement or boundary remains unresolved.

Order independently meaningful outcomes by dependency. Each step names its
observable result and validation boundary. Do not add per-step agent labels,
status tables or artificial review/merge/testing child issues.

## Separate tracking gate

Search existing issues first. Create a child only if a concrete benefit exists:
independent lifecycle or owner, meaningful dependency, actual parallel execution,
independently shippable/reviewable outcome, or materially different risk/validation.
Name that benefit. Parallelizable syntax or multiple commits alone is insufficient.
Reuse an issue only if its semantic responsibility matches.

If no step passes the gate, create zero children. If one does, record only its
own outcome/acceptance, constraints, dependencies and risks; reference the parent
for shared context. Use the tracker's actual supported states and link types.
Do not create a child per implementation step, file, function or routine gate.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Keep the ordered plan in the current work item/session. Post only a new
scope/dependency decision that changes what someone must do. Do not duplicate
an existing plan or announce routine progress. Reference separately tracked
children only when they passed the separate tracking gate.
