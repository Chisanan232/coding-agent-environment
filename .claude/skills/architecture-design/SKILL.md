---
name: architecture-design
description: "Inspect significant architecture, design, migration or cross-component tradeoffs and return scoped implementation boundaries. Use isolated stronger reasoning only when complexity warrants it."
---

# Architecture and design

Read the relevant code, accepted requirements and constraints before designing.
Challenge complexity and scope beyond the request. Choose the simplest design
that satisfies the contract; identify real risks and tradeoffs. Define concrete
implementation boundaries, dependency order and observable acceptance.

Use isolated read-only reasoning for a consequential ambiguous design or a hard
problem where additional capability improves the result. Routine implementation
stays with the main agent. Select the host's available reasoning/model capability
using its cost-aware routing; model names and advisor UI are host-specific.
Return a distilled decision and plan to the implementer and continue authorized
work. Finishing a plan is not an approval gate. Escalate only an actual unresolved
product, authority, cost, security or irreversible-action decision.

Use `task-decomposition` for independent tracking decisions, `cross-repo-coordinator`
for integrated outcomes and `engineering-workflow` for independent review.
