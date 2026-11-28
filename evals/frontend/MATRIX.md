# Frontend and motion evaluation matrix

The primary comparison uses twelve saved model responses across five briefs, including four additional matched treatments of the brand and dashboard. Eight original component fixtures exercise higher-risk browser behavior on one shared page. Each URL opens only the named fixture. Fixture success demonstrates the measurement and implementation technique. A candidate earns a routing role through a comparable candidate response and rendered review. The exact model, prompts, limits and initial results are in the [benchmark protocol](README.md) and [generation results](RESULTS.md).

| Case | Subject and evidence | Acceptance observation |
|---|---|---|
| B01 Premium brand website | Matched baseline, OpenAI, incumbent and authored print-studio treatments | The studio, print and edition action remain distinct on mobile and desktop. Inspect how the print crop and whitespace support the purchase decision. |
| B02 Product application UI | OpenAI pickup scheduler | Select the second slot by keyboard and confirm the correct time. Inspect focus, the primary action and any image displacing the form. |
| B03 Professional dashboard | Matched dispatch desks | Find the delayed route, read route times and toggle the filter in both directions. Examine whether decoration consumes space needed by the operator. |
| B04 Responsive interface | All five briefs at 375, 768 and 1280 CSS pixels | Check overflow, scan order, wrapping, touch-target bounds, essential action placement and the actual changed state. |
| B05 Refined micro-interaction | Original light-table toggle | Tab and Enter visibly change the switch, its text and pressed state. Inspect feedback timing, travel and its immediate reduced-motion state. |
| B06 Seamless component or page transition | Original horizontal print-proof change and generated exhibition transition | Follow the same artwork between scales. Check continuity, clipping, stable layout and final state, including the reduced-motion preference. |
| B07 Scroll-driven storytelling | Original three-chapter coastal crop | Real wheel movement changes crop and chapter. Home restores the first chapter. Reduced motion preserves each chapter label with a static crop. |
| B08 Living interactive environment | Original canvas wind field | A local pointer displacement and keyboard shift bend the ink marks. Sample the field over time and pause it by keyboard. Reduced motion redraws only on input. |
| B09 Physics-driven organic interaction | Original counterweight and rail | Keyboard and pointer retarget the anchor. Measure screen-space overshoot, forward and return velocity, settling time and endpoint. Reduced motion places the weight directly. |
| B10 Cinematic hero experience | Paired crop-viewer/word-reveal treatments and an original two-view camera move | Inspect title legibility, moving crop, focus and the hierarchy of the image. Compare the same recipe-free and recipe-assisted treatment at rest and in motion. |
| B11 Interactive 3D object | Original six-face CSS print block and optional WebGL2 color/depth raster probe | Perspective and face occlusion must establish volume. Keyboard rotation and pointer dragging change the visible face. Reduced motion updates orientation directly. |
| B12 Generative art and procedural graphics | Original seeded contour field | Changing the seed should produce another coherent drawing. Returning to the first seed must restore the same rendered pixel signature in the same browser. |
| B13 Visual and motion quality review | Generated comparisons, fixture PNGs and timestamped browser samples | Review composition, typography, art direction, interaction, usability and accessibility independently of the generator. Examine intermediate states and note frame timing with browser and viewport. |
| B14 Frontend architecture and maintainability | Static audit of the generated HTML, fixture-source review and [server rendering and state fixture](architecture/) | Verify dependency boundaries, landmark/ID integrity, interaction state ownership, input equivalence and reduced-motion paths. Record unresolved risks and avoid extrapolation to production-scale architecture. |

## Run and interpret

Run the existing model-output capture first. After a fresh memory admission and disk reservation, run the original fixtures sequentially in a single sandboxed Chromium process. The same Playwright and browser environment overrides as the generated-output runner apply. Use a new private output directory.

```sh
node evals/frontend/check-fixtures.mjs "$run_dir/fixture-capture"
python3 evals/frontend/audit-html.py "$run_dir"/*/index.html > "$run_dir/source-audit.json"
```

For each fixture the harness uses a desktop viewport with normal motion and a mobile viewport with reduced motion. It saves actual screenshots, timestamped animation-frame samples and a JSON verdict. Frame values come from browser layout, computed transforms or a 32 × 16 sample of painted canvas pixels. Milliseconds are wall-clock measurements on the test host. The fixtures give higher-variance effects narrow checks such as overshoot and return velocity. Their frame intervals include harness and screenshot overhead and cannot establish a device performance target. A normal-vs-reduced performance comparison would require matched viewport sizes.

B11 optionally accepts `FRONTEND_THREE_CAMERA` pointing to the local r187 camera-only tree documented in the [reference review](../../docs/FRONTEND-REFERENCES.md). The browser harness checks the complete tree, verifies the exact in-memory bytes it will serve and exposes only fourteen reviewed ES modules. The fixture owns its vertex and fragment shaders and block geometry. It checks actual framebuffer coverage, distinct face colors, an orientation change and return to the original pixels. The GPU path records SKIPPED when the subset or WebGL2 is unavailable. The CSS 3D fixture runs independently.

The static HTML audit counts declared resource references and reads no scripts as executable code. All twelve model responses had one main landmark, a reduced-motion media query, zero duplicate IDs, zero unresolved static ID references and zero external asset declarations. The local print was the only declared asset. Inline script sizes ranged from 149 to 446 bytes. Reviewing the scripts found local toggle or form state, without framework, build or runtime fetch dependencies. The word-reveal treatment carries both a JavaScript-ready class and a reduced-motion override. The recipe-free hero relies on script and CSS jointly to expose its detail label. Both deserve a regression check if markup changes. The bounded one-file briefs provide no evidence about design-system reuse or long-term maintenance of a full application.

**Current evidence.** B01, B03, B04, B06, B10 and B13 have rendered model-output evidence. B02 passed the alternate-slot confirmation at all three widths under both motion preferences after a local-form harness correction. B05–B12 passed all sixteen original fixture conditions. The optional B11 WebGL2 branch also passed. B14 has a twelve-output source audit and a separate offline rendering/state exercise. The [results](RESULTS.md) explain why a successful technique fixture does not establish an active skill role.
