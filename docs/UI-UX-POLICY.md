# UI/UX policy for Claude Code and Codex

This is the single canonical policy for UI/UX work done by agents in any
repository that uses this environment (Jira SPE-94). It defines who has
authority over a design decision, which tools may be used, and how a UI change is
verified. It does not install anything by itself; pinned third-party skills
are managed by [`docs/VISUAL-SKILLS.md`](https://github.com/Chisanan232/coding-agent-environment/blob/main/docs/VISUAL-SKILLS.md) and
[`codex/subtraction-skills.json`](https://github.com/Chisanan232/coding-agent-environment/blob/main/codex/subtraction-skills.json). Do not
duplicate that mechanism or a second manifest here.

Templates: [`DESIGN.md` skeleton](templates/DESIGN.md),
[per-task design brief](templates/DESIGN-BRIEF.md),
[UI QA checklist](templates/UI-QA-CHECKLIST.md).

## Authority order

When two sources disagree, the lower number wins among layers 1 to 6. A third-party skill never
overrides a higher layer. Layers 4 and 5 are advisory until their tools are deployed (see Third-party status); layer 7 is a gate, not a rank.

1. **Product requirements and real users.** Stated requirements, user research, and the people who will actually use the surface.
2. **Product-owned Design System, brand, and Design Constitution.** Preserve the existing product language for maintenance. For greenfield work or an explicitly authorized creative reboot, identify the constraints the owner kept and the directions they rejected. Start a new [design brief](templates/DESIGN-BRIEF.md) when the desired visual direction changes.
3. **Platform conventions.** Web standards and WCAG, Apple Human Interface Guidelines for iOS and macOS, Material for Android.
4. **Task-specific art direction.** Select a single active primary from the frontend section of the visual manifest. An unfilled role falls back to the approved brief and references.
5. **Vercel Web Interface Guidelines review.** A manual review checklist, pinned by commit permalink. Status: skill not deployed (see below).
6. **Optional references.** Owner-supplied visual references and the vetted material selected for this brief. Check [excluded routes](#excluded-from-design-routing) before selecting an additional source.
7. **Rendered verification.** A gate, not a rank: layers 1 to 6 set the intent, and this layer checks whether the built UI actually meets it. A rendered failure sends the work back to the layer that was violated; it never changes what that layer requires.

## Tool roles

| Role | Tool | Notes |
|---|---|---|
| Implementation | Product components, browser and local project tooling | Follow the brief for the task mode below. |
| Art direction, critique, refinement | Exactly **one** primary skill per task when its role has an approved active package | The manifest owns activation. Read only the selected primary and a recipe required by the interaction. |
| Guidelines review | [Vercel Web Interface Guidelines at `434b7f9`](https://github.com/vercel-labs/web-interface-guidelines/blob/434b7f91364665f2f733b310ec54809bf8f37937/command.md) | Read as a checklist. Do not fetch the moving `main` copy. |
| Render verification | Playwright (`playwright@claude-plugins-official`, the preferred route on Claude) | Local screenshots only. `.mcp.json` also configures `npx -y @playwright/mcp@latest`; it is unpinned and non-preferred, so use the plugin. |
| Design source | The official **Figma plugin** (`figma@claude-plugins-official` on Claude, `figma@openai-curated` on Codex) | The one preferred Figma route. Framelink (`figma-developer-mcp`) is **non-preferred**: it duplicates the plugin, is launched with an unpinned `npx -y`, and in `.mcp.json` only passes the API key as a command-line argument (`codex/config.toml` passes it through the environment). It remains configured in `.mcp.json` and `codex/config.toml` only because removal is a separate owner-reviewed change; do not use it when the plugin is available. |
| Native Apple build and Simulator | XcodeBuildMCP (task-specific, not installed) | See [Platform QA: iOS](#ios). |

## Choose a direction

| Task | Direction and evidence |
|---|---|
| Existing-product maintenance | Record the applicable tokens, behavior and components. Compare the change with the existing rendered surface. |
| Greenfield design | Start from the audience, content and a visual thesis. Select concrete references for composition, typography and imagery when fidelity matters. |
| Full creative reboot | Record explicit authorization, the rejected direction and the constraints that survive. Explore a new composition in the browser. An internal style frame or concept image may clarify the brief. |
| Brand website | For an immersive brand story use the active creative primary if approved. When that role is vacant, use the owner's references and a browser prototype. |
| Application UI | Use the active general primary for conventional websites, apps and dashboards. Give the user's next action and information hierarchy priority. |
| Motion design | Specify the trigger, transition, end state, keyboard behavior and reduced-motion equivalent. Consult one approved recipe only for its matching effect. |
| Creative development | Choose CSS, Canvas, WebGL or a library for a specific interaction and device budget. Judge composition and usability in the actual render. |
| Visual verification | Inspect the running prototype at target viewports and capture representative motion frames. Review the result against the brief. |

A request for direct implementation can proceed straight to the browser prototype. Separate static mockup approval applies only when the owner asks for it. The number of animations, use of 3D and choice of framework carry no artistic score. For backend work skip design skills entirely. Plugin availability alone never selects an art director.

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
2. **Inspect the Design System.** Identify the existing or newly authorized visual direction and relevant product constraints.
3. **Direction.** Pick the one primary art-direction skill (or none) and record it in the brief.
4. **Implement** the agreed direction as a runnable prototype. Reuse product components where the brief calls for continuity.
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

A runnable reference is [`tests/ui-smoke.mjs`](https://github.com/Chisanan232/coding-agent-environment/blob/main/tests/ui-smoke.mjs) with the
synthetic fixture in [`tests/fixtures/ui-smoke/`](https://github.com/Chisanan232/coding-agent-environment/tree/main/tests/fixtures/ui-smoke).
It needs an already-installed `playwright` package and never downloads a
browser. It is a manual check and is not gated in CI: CI cannot assume Playwright, so only the policy test runs there.

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
| Primary design skills | User skill links from the verified frontend snapshot | User skill links from the same snapshot |
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

### Excluded from design routing

Impeccable, 21st, Elaya AI Design Skills, UI/UX Pro Max, brandkit, canvas-design, design-taste-frontend, remotion-best-practices and generic prompt packs have no active primary or recipe role. A marketplace entry or a cached copy is insufficient evidence of active discovery. Preserve independent accessibility, engineering, diagramming, browser, Figma and testing tools.

See the [creative candidate review](https://github.com/Chisanan232/coding-agent-environment/blob/main/docs/FRONTEND-SOURCE-REVIEW.md) and [frontend installation and rollback procedure](https://github.com/Chisanan232/coding-agent-environment/blob/main/docs/VISUAL-SKILLS.md#frontend-design-skills).
