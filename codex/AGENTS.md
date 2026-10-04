
<!-- codebase-memory-mcp:start -->
# Codebase Knowledge Graph (codebase-memory-mcp)

This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase.
ALWAYS prefer MCP graph tools over grep/glob/file-search for code discovery.

## Priority Order
1. `search_graph` — find functions, classes, routes, variables by pattern
2. `trace_path` — trace who calls a function or what it calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

## When to fall back to grep/glob
- Searching for string literals, error messages, config values
- Searching non-code files (Dockerfiles, shell scripts, configs)
- When MCP tools return insufficient results

## Examples
- Find a handler: `search_graph(name_pattern=".*OrderHandler.*")`
- Who calls it: `trace_path(function_name="OrderHandler", direction="inbound")`
- Read source: `get_code_snippet(qualified_name="pkg/orders.OrderHandler")`
<!-- codebase-memory-mcp:end -->

<!-- coding-agent-environment:signal-first:start -->
# Signal-first engineering

Create only what has a current need. Search before creating; reuse or extend
only matching semantic responsibilities, and reference canonical truth before
repeating it. Avoid speculative abstractions; deletion/consolidation can improve
an existing system.

Code is primary implementation truth. Prefer clear names, types and boundaries.
Comments/docstrings explain non-obvious why, constraints, invariants, safety,
compatibility or external limitations—not obvious mechanics. Docs and tickets
store durable decisions/outcomes without duplicating code or another source.

Communicate semantic outcomes, not file/command chronology. Support material
claims with evidence; keep inference and unknown/unverified state explicit.
Use $evidence-first-briefing for persistent/shared engineering records, preserving
decision-critical evidence, risks and action in roughly 10–30 seconds of reading.

Use canonical $requirement-zero for NEW unvalidated scope and $codebase-zero for
EXISTING artifact audits. Never reopen already-approved work through these skills.
<!-- coding-agent-environment:signal-first:end -->
