# Frontend benchmark results

**Routing decision.** Retain the verified Anthropic incumbent as the single primary for the tested application and premium-brand briefs. Route the separately authored engineering guidance only for a concrete implementation or verification need. Hold the historical OpenAI skill outside the general-purpose allowlist. Its dashboard treatment failed reduced motion and small-text contrast. Keep the MengTo word reveal outside active discovery. Both hero treatments completed the interaction, and the recipe added context without a clear advantage. The eight original generated specimens and the original motion fixtures reached the browser.

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

## Rendered comparisons

A new memory admission and disk reservation allowed a complete serial capture in sandboxed Chrome 158.0.8127.37 using Playwright 1.66.0-alpha-2026-11-09. The original eight specimens ran at 375, 768 and 1280 pixels under normal and reduced motion. Every named control was keyboard-reachable, and none of the 48 views overflowed horizontally. No unexpected page request or JavaScript error was recorded. App/browser background traffic was outside the assessment. An initial ten-image baseline capture was interrupted when its disk reservation granted before memory admission. It remains separate from the matched evidence.

An agent independent of the generator inspected actual desktop and mobile screens, changed states and motion frames. The reviewer knew the treatment labels. Scores below are bounded judgments on the rubric's 0–2 scale, ordered as composition, typography, art direction, interaction, usability and accessibility. A score of 2 applies only to the observed brief and checks. No screen-reader or physical-device session contributed to the scores.

| Task and arm | Six scores | Evidence behind the judgment |
|---|---|---|
| Brand baseline | 1 / 2 / 2 / 2 / 1 / 2 | The oversized serif title, studio mark and framed print have a clear hierarchy. On mobile a roughly 161 px gap separates the edition button from the artwork. The detail toggle and focus ring remain clear. |
| Brand OpenAI | 2 / 2 / 2 / 2 / 2 / 2 | The mobile button and print sit about 23 px apart. The extra space moves above the title. Both arms resolve to similar two-column desktop compositions. These captures show a local spacing improvement. |
| Dashboard baseline | 1 / 2 / 1 / 1 / 2 / 2 | All three routes and times are legible. Repeated outlined rows and left rules compete for attention, and the filter gives less feedback. Its mobile image header occupies about 111 px. |
| Dashboard OpenAI | 1 / 2 / 1 / 2 / 2 / 0 | The ruled list and isolated rust exception make the delay easy to scan. The toggle and live result count clarify filtering. The illustrated mobile cover consumes 190 px. A 14.4 px bold rust status on paper computes to 4.36:1, below 4.5:1. One switch-knob transition remained in reduced motion at every width. |
| Hero OpenAI | 2 / 2 / 2 / 2 / 1 / 2 | The full-bleed desktop image anchors the uppercase title. The entry frames show the second word following the first. At 375 px the tight cover crop removes the sun. Zoom, focus, no-JS heading and reduced motion passed. |
| Hero OpenAI + recipe | 1 / 2 / 2 / 2 / 1 / 2 | The complete print remains visible on mobile and the large two-line desktop title is easy to read. The mobile action sits roughly 400 px below the print. The shorter word delay changes the cadence. The primary alone already achieved a clear reveal and usable zoom. |

A second reviewer independently inspected the two 375 px hero after-states. Both showed a focus ring and a detail label. That reviewer also found the roughly 394 px gap between the recipe treatment’s artwork and focused action, and saw no clear net advantage in that pair. Those stills do not establish the rhythm of the entrance. The first review also checked the entry and middle frames.

The rubric has no composite artistic score. OpenAI improved two specific decisions, mobile brand grouping and dashboard filter feedback. The dashboard's accessibility failures bar general-purpose promotion in the zero-repair protocol. The small status label is subject to [WCAG 2.2 SC 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html). The word reveal's larger prompt, longer generation and mobile action gap leave its incremental benefit unproven here. Retain the local reference for a future brief that specifically calls for this cadence.

The single-arm exhibition transition passed all six conditions. The image moves to a detail crop while the scene state remains available in the control bar. The middle capture shows the scene name sliding as the crop changes. The mobile pickup view puts a 60 px-high full-width action near the bottom. Its first six captures could not confirm submission because the harness sandbox also suppressed the locally canceled submit event. After the harness allowed local form events with `sandbox allow-scripts allow-forms`, the unchanged HTML passed all six cases. It confirmed “Saturday, 11:30” after changing the default slot. The rerender retained `form-action 'none'`, request interception and Chromium's process sandbox. The initial failed records were preserved.

## Incumbent and authored comparison

Four additional single-response treatments used the identical model, medium effort, reference, task prompts, limits and scoring protocol. The incumbent input came from the exact locally verified Anthropic marketplace leaf at version 1.1.1. The authored input comprised the v1 `creative-frontend-engineering` entry and its direction reference. The five-file authored snapshot reviewed for this experiment was `939c67b6e2454b401c791562e7ea97dc76a325f4dea545c9d7f12422495f4fa8`. An improved successor needs its own evidence. [Source identities](../../docs/FRONTEND-SOURCE-REVIEW.md)

| Task | Arm | Input including cache | Output tokens | HTML bytes | Seconds |
|---|---|---:|---:|---:|---:|
| Brand | Anthropic incumbent | 4,237 | 1,345 | 2,788 | 9.59 |
| Brand | Authored v1 | 2,827 | 1,434 | 3,041 | 10.18 |
| Dashboard | Authored v1 | 2,798 | 1,642 | 3,550 | 11.08 |
| Dashboard | Anthropic incumbent | 4,208 | 1,544 | 3,301 | 11.05 |

| Task and arm | Six scores | Rendered assessment |
|---|---|---|
| Brand incumbent | 2 / 2 / 2 / 2 / 2 / 2 | The two-line title balances the print at desktop width. Its folded mark, italic edition line and rust action relate to the product. Mobile keeps the title, action and full print in one group, with the edition size stated once. The disclosure opened and closed in all six cases. |
| Brand authored v1 | 1 / 2 / 1 / 2 / 2 / 2 | The rectangular action, white mat and serif title form a legible study. The masthead and hero repeat the edition size. A large gap follows the narrow masthead on mobile. An exact-name selector missed the native disclosure because its generated “+” joined its accessible name. A bounded selector also accepted the exact label plus its state glyph on an initially collapsed disclosure. The unchanged specimen then passed opening, focus and reversal in all six cases. |
| Dashboard incumbent | 1 / 2 / 1 / 2 / 2 / 2 | Time, status and route are distinct on desktop. The mobile rows spend more vertical space on aligning the time. The exception's small rust text on the panel computes to 4.87:1. Keyboard filtering, reversal and zero running reduced-motion transitions passed at all widths. |
| Dashboard authored v1 | 2 / 2 / 1 / 2 / 2 / 0 | A shallow mobile art band, compact rows and a high-contrast exception badge make this the fastest board to scan visually. The badge computes to 5.90:1. A pseudo-element color transition remained in all three reduced-motion cases, failing the predeclared zero-animation gate. |

Across twelve generated specimens and six conditions per specimen, 66 of 72 final behavioral checks passed. The six failures were the three reduced-motion widths in each of the OpenAI and authored-v1 dashboards. Two harness corrections were rerun on unchanged HTML, and the original failures were preserved. The incumbent passed both matched briefs without a repair. The authored v1 used 1,410 fewer input tokens per pair, made a more compact dashboard and still failed a mandatory accessibility check. Keep its engineering and motion-verification material available when implementation calls for it. For conventional and premium brand tasks in the evaluated range, load the incumbent alone as primary, subject to that host's verified installation and source ownership. A single print-studio sample cannot establish immersive-site quality in general.

## Original fixtures

B05–B12 passed all 16 desktop-normal and mobile-reduced fixture conditions. The separately vetted Three.js camera subset also produced a WebGL2 framebuffer with 10,109 colored pixels in the initial orientation and 8,956 after rotation. Returning to the original orientation restored the original pixel signature. The wind field changed around the pointer, froze on keyboard pause and stayed static under reduced motion until input. The counterweight overshot its measured anchor by 23.4%, reached +3,203 px/s going forward and −745 px/s on return, and settled within 2% in 614 ms. The reduced version moved directly to the endpoint. Seed changes produced visibly different contour bands and reproduced the first drawing when reversed. Captured frame-interval p95 values ranged from 16.7 to 16.8 ms in this browser run.

These hand-authored studies establish technique and verification coverage. They provide no treatment comparison. The admitted captures retained 150 original-matrix PNGs, 63 supplemental PNGs, 14 disclosure-rerun PNGs and 16 frame traces. These totals include earlier harness outcomes and are separate from the ten images in the interrupted first capture. The original HTML and raw evidence remain local. See the [fourteen-case matrix](MATRIX.md) for the fixture roles and static review.
