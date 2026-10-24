# UI/UX policy for Claude Code and Codex

This is the single canonical policy for UI/UX work done by agents in any
repository that uses this environment (Jira SPE-94). It defines who has
authority over a design decision, which tools may be used, and how a UI change is
verified. It does not install anything by itself; pinned third-party skills
are managed by [`docs/VISUAL-SKILLS.md`](VISUAL-SKILLS.md) and
[`codex/subtraction-skills.json`](../codex/subtraction-skills.json). Do not
duplicate that mechanism or a second manifest here.

Templates: [`DESIGN.md` skeleton](templates/DESIGN.md),
[per-task design brief](templates/DESIGN-BRIEF.md),
[UI QA checklist](templates/UI-QA-CHECKLIST.md).

## Authority order

When two sources disagree, the lower number wins. A third-party skill never
overrides a higher layer.

1. **Product requirements and real users.** Stated requirements, user research, and the people who will actually use the surface.
2. **Product-owned Design System, brand, and Design Constitution.** The product repository's own tokens, components, and written rules. The reference implementation is `horonomy/horonom-site/design/`: `VISUAL_DESIGN_CONSTITUTION.md`, `MOTION_CONSTITUTION.md`, `ACCESSIBILITY_CONTRACT.md`, and `AI_FRONTEND_CHECKLIST.md`. A repository without these starts from the [`DESIGN.md` skeleton](templates/DESIGN.md).
3. **Platform conventions.** Web standards and WCAG, Apple Human Interface Guidelines for iOS and macOS, Material for Android.
4. **Impeccable art direction, critique, and refinement.** Preferred direction layer. Status: verified, not deployed (see [Third-party status](#third-party-status)).
5. **Vercel Web Interface Guidelines review.** A manual review checklist, pinned by commit permalink. Status: skill not deployed (see below).
6. **Optional references.** Everything listed under [Optional, not installed](#optional-not-installed).
7. **Rendered verification.** Layers 1 to 6 describe intent; this layer decides whether the built UI actually meets it. Evidence from a rendered page outranks any claim in a document.

## Tool roles

| Role | Tool | Notes |
|---|---|---|
| Implementation | `frontend-design` plugin (already installed on Claude) | Build with the product's existing components first. |
| Art direction, critique, refinement | Exactly **one** primary skill per task | Impeccable is the preferred choice once deployed; until then use the product Design Constitution and `frontend-design` guidance. Never stack two art-direction skills on one task. |
| Guidelines review | [Vercel Web Interface Guidelines at `434b7f9`](https://github.com/vercel-labs/web-interface-guidelines/blob/434b7f91364665f2f733b310ec54809bf8f37937/command.md) | Read as a checklist. Do not fetch the moving `main` copy. |
| Render verification | Playwright (existing plugin on Claude) | Local screenshots only. |
| Design source | The official **Figma plugin** (`figma@claude-plugins-official` on Claude, `figma@openai-curated` on Codex) | The one preferred Figma route. Framelink (`figma-developer-mcp`) is **non-preferred**: it duplicates the plugin, is launched with an unpinned `npx -y`, and passes the API key as a command-line argument. It remains configured in `.mcp.json` and `codex/config.toml` only because removal is a separate owner-reviewed change; do not use it when the plugin is available. |
| Native Apple build and Simulator | XcodeBuildMCP (task-specific, not installed) | See [Platform QA: iOS](#ios). |

## Anti-generic-AI guidance

These are warning signs, not a ban. Use one when the product's own Design
System or the user's need justifies it, and say why in the design brief.

- Purple-to-blue gradients, glow shadows, and gradient text with no brand reason.
- Identical three-card feature rows, emoji as icons, and decorative glassmorphism.
- Default system or overused fonts chosen because nothing else was considered.
- Centered everything, uniform spacing, and no visual hierarchy.
- Filler copy ("Seamless", "Powerful", "Unlock") instead of the product's real language.
- Motion that exists only to look busy, or that ignores `prefers-reduced-motion`.

## Workflow

1. **Understand users.** Fill in the [design brief](templates/DESIGN-BRIEF.md): who, what job, what constraints.
2. **Inspect the Design System.** Read the product's tokens, components, and constitution documents before writing any UI.
3. **Direction.** Pick the one primary art-direction skill (or none) and record it in the brief.
4. **Implement** with existing components. Add a new component only when none fits.
5. **Render the real UI.** Run it; do not judge from source.
6. **Check desktop and mobile sizes** (at least 375, 768, and 1280 px).
7. **Interaction and accessibility checks.** Keyboard path, visible focus, reduced motion, dark and light, contrast.
8. **One bounded repair and verify cycle.** Fix what steps 5 to 7 found, re-render once, stop. Escalate anything left over instead of looping.
9. **Independent review.** A separate reviewer (agent or human) checks the rendered result against the brief and checklist.
10. **Deliver** with the completed [QA checklist](templates/UI-QA-CHECKLIST.md) and local screenshot paths, not uploaded images.

## Platform QA

### Web

Use Playwright against the running app. Screenshots stay on the local machine
in a gitignored directory (this repository uses `.qa-artifacts/`); never upload
them or commit them.

- Screenshots at **375, 768, and 1280 px** wide.
- No horizontal scroll at any of those widths.
- Keyboard-only pass: every control reachable, order logical, focus always visible, no trap.
- `prefers-reduced-motion: reduce` honoured (no running animations).
- Light and dark color schemes both legible.
- Contrast and text resizing checked against `ACCESSIBILITY_CONTRACT.md` or WCAG 2.2 AA.

A runnable reference is [`tests/ui-smoke.mjs`](../tests/ui-smoke.mjs) with the
synthetic fixture in [`tests/fixtures/ui-smoke/`](../tests/fixtures/ui-smoke/).
It needs an already-installed `playwright` package and never downloads a
browser.

### iOS

Follow the Apple Human Interface Guidelines. Verify in the Simulator at the
smallest and largest supported device sizes, with Dynamic Type, Dark Mode, and
VoiceOver labels. Use XcodeBuildMCP only when the task needs build, run, or
Simulator automation, and only in a pinned form (see below). Android follows
the same shape with Material guidance.

## Host support and parity

Support is stated per host. Parity is not claimed beyond what was observed.

| Capability | Claude Code | Codex |
|---|---|---|
| Implementation (`frontend-design`) | Plugin enabled in `.claude/settings.global-only.json` | No same-named plugin in `codex/config.toml`. Equivalent not verified; `build-web-apps@openai-curated` is configured. |
| Render verification | `playwright@claude-plugins-official` plugin | `browser@openai-bundled` plugin is configured; Playwright equivalence not verified. |
| Figma | `figma@claude-plugins-official` | `figma@openai-curated` |
| Pinned skills via the manifest | Symlinked under `~/.claude/skills` or a native plugin | Copied or symlinked under `~/.agents/skills` |
| Impeccable | Not deployed | Not deployed |

Whether an installed plugin is actually discoverable on a given machine must be
checked by real discovery, never inferred from configuration.

## Third-party status

Verified 2026-10-24 from the official repositories, read-only.

| Source | License | Pin reviewed | Decision |
|---|---|---|---|
| [Impeccable](https://github.com/pbakaus/impeccable) | Apache-2.0 | `main` at `936867543ac2576920d663cc9803597f6044f3ac` (4.7.1); newest tag `skill-v4.7.0` = `aeeb3d8c30b5f95aeeb3e893b0f1a308af8fdb8f` | **Verified, not deployed.** The skill instructs the model to run `scripts/impeccable` each session; that launcher downloads a native binary from GitHub Releases at first use, checked only against a `.sha256` file from the same origin. Its plugin form also registers SessionStart, PostToolUse (Edit and Write), and Stop hooks, and `hooks on` edits `~/.claude/settings.json`. None of that is covered by a tree digest or fits this repository's no-duplicate-hooks rule. |
| [Vercel `web-design-guidelines`](https://github.com/vercel-labs/agent-skills/tree/063bee94c3f4df8453406c830b0a7df0f2860278/skills/web-design-guidelines) | No LICENSE file; the README says MIT but GitHub reports no SPDX license | `agent-skills` at `063bee94c3f4df8453406c830b0a7df0f2860278` | **Not installed.** The 1.2 KB skill fetches `web-interface-guidelines/main/command.md` on every use, so the rules come from an unpinned mutable source and pinning the skill buys nothing. The guidelines repository itself has an MIT license and is used as a pinned manual checklist (layer 5). |
| [XcodeBuildMCP](https://github.com/cameroncooke/XcodeBuildMCP) | MIT | Release `v2.7.1` | **Task-specific, not installed.** Its README now points at `getsentry/MobileBuildMCP` and the `mobilebuildmcp` npm package, and every install form shown uses `@latest`. Use only a version-pinned form and only for native Apple work. |

Follow-up for the owner, in a separate change: Impeccable's launcher checks a
sibling `bin/<os>-<arch>/impeccable` before it downloads anything. Pre-staging
that binary with a per-platform SHA-256 pinned in the manifest, plus an
explicit decision that its hooks stay disabled, would close the gap. Until
then Impeccable is a reference to read, not a skill to load.

### Optional, not installed

21st, UI/UX Pro Max, brandkit, canvas-design, design-taste-frontend, and
remotion-best-practices are **optional and NOT INSTALLED**. They may be read as
references (layer 6) but add no deployed skill, hook, or network behaviour.
