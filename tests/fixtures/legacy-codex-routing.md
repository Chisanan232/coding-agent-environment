<!-- cost-aware-routing:start -->
# Cost-aware agent routing

Use the root Terra agent for routine implementation and integration; do not delegate tiny work.
Delegate only independent work with a concrete wall-clock or quality benefit, with no more than three concurrent subagents.
Use `luna_worker` only for large, low-risk mechanical work. Escalate to `sol_builder` for evidence-backed difficult implementation, debugging, or cross-component reasoning. Escalate to `astra_architect` only for high-impact architecture, ambiguous design/UX direction, or after two meaningful failed cheaper-tier attempts; return implementation to Terra or Sol once the hard decision is resolved.
Avoid duplicate exploration, repeated large-file reads, redundant agents, repeated full test suites, and verbose progress. Reuse evidence, run targeted validation first, then one broader check when warranted. Do not trade away meaningful validation for token savings.
<!-- cost-aware-routing:end -->
