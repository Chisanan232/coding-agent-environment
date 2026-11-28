# Initial frontend benchmark

**Decision.** The eight scheduled generations completed. Only the minimal brand baseline has browser screenshots. Comparative visual value remains unverified for both the primary and the recipe.

The runner used Claude Code 2.1.309 on an included subscription with additional spending and model tools disabled. Each recorded response used only `claude-sonnet-5-5` at requested medium effort. No server web search or fetch was reported. Each arm received the same system prompt, artwork, task-specific copy, single generation, 170-second deadline, 3,200-output-token ceiling and 6,500-byte HTML target. The baseline received no skill body. Treatment prompts included the pinned skill text. This measured instruction-body use in fresh sessions. Native host invocation is a separate check.

| Task | Arm | Input including cache | Output tokens | HTML bytes | Seconds |
|---|---|---:|---:|---:|---:|
| Dashboard | Baseline | 1,356 | 1,222 | 2,591 | 8.75 |
| Dashboard | OpenAI | 4,009 | 1,738 | 3,684 | 12.50 |
| Brand | OpenAI | 4,038 | 1,254 | 2,612 | 9.73 |
| Brand | Baseline | 1,385 | 1,328 | 2,800 | 9.22 |
| Interactive hero | OpenAI | 4,007 | 1,490 | 3,080 | 9.86 |
| Interactive hero | OpenAI + word reveal | 6,091 | 2,087 | 3,162 | 15.42 |
| Motion | OpenAI | 3,966 | 2,144 | 3,228 | 16.79 |
| Mobile | OpenAI | 3,983 | 1,291 | 2,801 | 9.92 |

The primary added 2,653 input tokens in each matched primary/baseline pair. The recipe added 2,084 input tokens and 597 output tokens in the hero pair. Each pair has one sample per arm. Motion and mobile have single-arm coverage only. Source review held Awwwards before generation. The instruction bodies came from [OpenAI's archived frontend skill](https://github.com/openai/skills/tree/82d2c5b44ac234ec0f204f647562a4349d90ef43/skills/.curated/frontend-skill) and [MengTo's word reveal](https://github.com/MengTo/Skills/tree/3a8517e09e9a258a8b8a1dfdb71579abe75f1c22/agent-skills/web-design/staggered-word-reveal).

## Rendered evidence

A disk reservation started the baseline renderer while the separate memory gate still refused heavy QA. The run was interrupted when that discrepancy was identified. Ten PNGs survived from Chrome 158.0.8127.37 and Playwright 1.66.0-alpha-2026-11-09. They cover 375 and 768 pixels before and after the action under both motion preferences, plus the before and after states at 1280 pixels with reduced motion. The capture preceded the harness update that explicitly enables Chromium process sandboxing and checks both excluded dashboard rows and the initial pickup time. Rerender with the current harness for comparable results. The full matrix requires both gates to admit. Production of further screenshots stopped at the five-refusal boundary. No generated page or screenshot was published.

An agent separate from the generator inspected desktop and mobile screenshots. The framed coastal print dominates the desktop layout. The dark purchase button and large serif title read clearly against the warm paper. At 375 px the art starts below a wide gap after the button, which separates the product from the decision. Activating the button exposes the print dimensions and leaves a clearly visible keyboard focus ring. The captured evidence establishes that interaction in the baseline. A full accessibility audit, motion sequence, cross-arm visual scores and comparative recommendation remain open.

**Acceptance status.** Model generation PASS. Baseline visual critique PARTIAL. Paired browser comparison, motion evidence and comparative approval BLOCKED. Resume with the saved HTML after a new resource-admission grant. Rerender both arms using the same harness revision.
