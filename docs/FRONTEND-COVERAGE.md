# Frontend capability coverage

This audit distinguishes what the hosts can discover, what installed software can execute, and what a person actually saw. **VERIFIED** means a stated, bounded result was observed. **SUPPORTED** means reviewed instructions or an installed implementation handles the task, with execution still to be demonstrated. **PARTIAL** identifies a tested subset or an incomplete workflow. **BLOCKED** identifies a concrete constraint on a planned check. **MISSING** means the scoped inventory did not find an approved provider for the stated capability.

## Hosts and evidence

| Provider | Claude Code | Codex |
|---|---|---|
| Official `frontend-design` v1.1.1 | Fresh personal CLI listed the unqualified command once. Sonnet 5.5 emitted Skill and proposed a dominant cobalt tile, type hierarchy and mobile swatch action. | Fresh personal CLI listed one enabled user skill. GPT-6.2 Sol accepted and returned its structured selection and completed a ceramic-studio direction without another tool. |
| First-party `creative-frontend-engineering` v2 | Fresh personal CLI listed it once. Sonnet 5.5 emitted Skill and read `critique.md`, then described pseudo-element and keyboard checks under reduced motion. | Fresh CLI listed one enabled user skill. GPT-6.2 Sol accepted and returned the structured selection, read `critique.md` and correctly required the checked state and focus to survive an immediate transition. |
| First-party `creative-frontend-engineering`, earlier v1 | Fresh personal Claude 2.1.309 discovered it in user scope. A real Skill invocation then read `motion.md` with zero tool errors. | Fresh personal CLI 0.172.0 listed it as an enabled user skill. A native app-server turn accepted and returned the structured skill selection for the installed body, read `motion.md` and completed. |
| Historical OpenAI `frontend-skill` | A disposable home discovered the archived comparison candidate. A subscription session selected it through Claude's Skill tool in a disposable project. | A disposable home discovered the candidate as an enabled user skill. An ephemeral read-only session explicitly requested it and read the installed body. |
| Platform integrations | A fresh neutral catalog exposed Anthropic frontend-design, 16 Figma skills, built-in-browser, chrome-browser and computer-use in the **initial inventory**. Playwright is an enabled MCP plugin. | The fresh neutral CLI exposed system imagegen and skill-installer. Local plugin listing marked build-web-apps and Figma enabled but uninstalled. Browser had a configuration entry. The initiating chat catalog also exposed Sites and Visualize. |
| Local implementation tools | Agent access depends on permissions and current environment | Same condition. Independent process checks below prove host tooling, separately from native skill invocation. |

The final fresh neutral catalogs retained user `acceptance-validation`, sixteen Claude Figma commands, Claude browser and computer-use commands, and Codex system imagegen and skill-installer. Only the former plugin-qualified Claude `frontend-design:frontend-design` was absent from the initial scoped Claude entries. The rejected OpenAI primary and held MengTo recipe were absent. The Playwright and Figma user plugin switches remained enabled. Both live installations matched the complete official and v2 tree digests below. All four actual native skill selections completed. Both Claude answers exceeded a 75-word limit and its primary answer reused the sole intended accent color for its button. These text-only probes establish invocation and bounded task response. The separate benchmark provides the rendered evidence. The final check covered the native CLIs.

Both v1 post-install probes began in an empty neutral directory, with no project skill overlays. The selected user files matched the reviewed `939c67b6e2454b401c791562e7ea97dc76a325f4dea545c9d7f12422495f4fa8` tree on both hosts. The fresh Claude catalog also retained every initially observed scoped command. Codex retained system imagegen and skill-installer. Both listed user `acceptance-validation`. The ChatGPT application shell independently advertised the two first-party skills. These observations concern discovery; only the creative skill was invoked in the post-install smoke.

The first-party implementation and verification skill has four task-specific local references. The earlier primary-direction trial used v1. A disposable project used its five-file snapshot with tree SHA-256 `2b870e0a494c1ecd23c40ec7750a70351419dd949575383894846bc88d33b419`. A neutral keyboard-interruption brief triggered the Claude Skill tool and a read of `motion.md` alone. The answer covered retargeting from the current pose, stale completion, focus return and an immediate reduced-motion state. It assumed linear progress in its numerical example and exceeded the requested 180 words. Actual eased progress must come from the current animation or rendered transform. A keyboard default-action cancellation alone cannot restore focus. The reviewed correction at tree `939c67b6e2454b401c791562e7ea97dc76a325f4dea545c9d7f12422495f4fa8` explicitly samples the rendered transform, restores focus and tests the new endpoint. The forward test measured routing and guidance in a fresh session. The correction received source review, then post-install execution on both personal hosts. Sonnet 5.5 and GPT-6.2 Sol each gave the corrected answer on the eased 110 ms reversal: use the computed pose, return focus before hiding, and commit reduced motion immediately. These were text-only invocation checks. No subsequent browser behavior is inferred. Table cells use their filenames. Candidate status belongs to the canonical manifest. Cached marketplace skills, including Three.js visualization, React best practices and Remotion, do not establish an installed workflow. [First-party entry](../.claude/skills/creative-frontend-engineering/SKILL.md) · [Manifest](../codex/subtraction-skills.json) · [Candidate review](FRONTEND-SOURCE-REVIEW.md)

The selected single art-direction primary is Anthropic `frontend-design` v1.1.1, upstream commit `a7ba5b93db0c7c8d17f3f1d25853adca79598a1a`, complete consumed-tree SHA-256 `cc0bb08818374b7bb74964d2a9869cca785da0c66355382390da0c8268001cda`. Its brand and dashboard treatments passed all 12 browser conditions. First-party v1 used 1,410 fewer input tokens per matched arm and produced a compact dashboard, but its toggle pseudo-element continued animating at all three reduced-motion widths. The revised v2 `creative-frontend-engineering` is reserved for implementation and verification, with an explicit pseudo-element check. Its reviewed source tree is `db031890be488ecdfd9cc28e46aece6286e84d9606fa088a235ad4e39482a0ce`. The measured primary arms used v1. [Comparison and decisions](../evals/frontend/RESULTS.md)

- **Browser evidence.** Eight bounded model generations produced HTML. Ten Chrome 158 screenshots survived from the minimal brand baseline, including keyboard activation and a visible focus ring at narrow widths. They predate the current harness revision. A later admitted session passed all sixteen original B05–B12 component cases, including focus, bounded layout, final state and motion sampling. Their restricted-origin log reported zero rejected requests or page errors. B11 separately passed the reviewed r187/WebGL2 raster check. The original component results test implementation techniques independently of the paired model treatments. The final comparison covered 12 model arms and 72 browser conditions. Sixty-six passed. The six failures were the OpenAI and authored-v1 dashboard toggle pseudo-elements at the three reduced-motion widths. All brand, mobile, motion and hero conditions passed. The unchanged mobile submission and authored brand control passed after corrections to their harness checks. [Fixture matrix](../evals/frontend/MATRIX.md) · [Results](../evals/frontend/RESULTS.md) · [Browser harness](../evals/frontend/render.mjs)
- **Rendering logic.** Node 26.8.1 executed a reusable HTML component, a pure event-driven update, static export, and a three-chunk server-side stream with an asynchronous boundary. Static, streamed and server-template output matched byte-for-byte. The output SHA-256 was `ad4f9ac29684225394fd2003d832c3de012021e30d898c0e25ea11f9c4aeed0c`. The repository B14 fixture also passed three Node tests covering static/request variants, delivery of the useful shell before a failing asynchronous secondary read, and preservation of initial state, reversible toggle and cleanup on an EventTarget. Neither check exercised HTTP or framework/browser hydration. [Portable B14 tests](../evals/frontend/architecture/architecture.test.mjs)
- **Original assets.** Python generated a four-layer orbital illustration under a common phase. System `sips` rasterized eight 96 × 64 frames. The preinstalled Playwright FFmpeg build `n7.0.1-playwright-build-1013` encoded them as a 2,165-byte VP9 WebM and decoded eight PNGs at the expected dimensions. The SVG at phase zero and one was identical. A centroid measurement compared all eight decoded displacements with the source ellipse. The largest error was 0.393 px and the last-to-first error was 0.243 px at the native resolution, both below the 1 px threshold. The decoded contact sheet showed all eight expected positions. Frame zero SVG SHA-256 was `3e454d4ee869a38844d50c41b669b410450cefbc6255faabc1e9adeb17542d00`. The first local WebM SHA-256 was `50ed4bd957b9761bba72e844b5ea400b4a13b75fe0ef20018f42324bec42f8b8`. The portable script reproduced both position errors and byte counts with a different container digest. [Fixture sources and procedure](../evals/frontend/assets/README.md)
- **3D interchange.** Apple USD Tools 0.24.3 compiled a procedural 16-vertex, ten-face wheel with a bound UsdPreviewSurface, a sphere light and three camera position keys. `usdchecker` passed on the binary stage and its USDZ package. The editable USDA SHA-256 was `2ebdc6ff5b686f837c723d7a644d291299cdbd7bb484b2d333601b99a6804ef6`. A separate two-joint, six-weighted-vertex flag with three rotation keys round-tripped through USDC and passed the checker. A reference calculation verified the tip displacement. Rig USDA SHA-256 was `f4d813ea18d7eadadcbca630d6eb1e8d8e11c22a386baa3ed79a49d7c9ef4ef9`. USD validation checks format and dependencies and samples only the first time sample for relevant checks. Neither mesh was presented in a realtime viewport.
- **Tool boundaries.** Chrome 158.0.8127.37, Firefox 89.0.2, Safari 26.6.1 and SafariDriver were present. The recorded browser run used Playwright 1.66.0-alpha-2026-11-09 with Chrome. The local FFmpeg build exposes MJPEG and VP9 decoding, PNG and VP9 encoding. A desktop Figma application and several native graphics SDKs were present. The checked personal tool and application roots lacked Blender, Godot and Unity. The runtime inventory was subsequently extended with a reviewed Three.js r187 camera-only tree. A vendor example containing a bundled library is outside the approved execution path.

## A. Frontend engineering

| Capability | Grade | Provider and boundary |
|---|---|---|
| Frontend architecture | PARTIAL | `direction.md` and the Node fixture establish state ownership and rendering boundaries in a small example. Application-scale integration remains open. |
| Component architecture | VERIFIED | The Node component rendered two state values through one template. Component-library composition remains outside that probe. |
| Design system engineering | SUPPORTED | `direction.md` defines palette primitives, semantic roles and interaction states. Claude Figma library and code-connect skills were discovered. No design-library sync ran. |
| State management | VERIFIED | A Node reducer applied an action with a bounded result and preserved the original model. The brand baseline independently exposed an open-details state in Chrome. |
| Rendering strategies | PARTIAL | `direction.md` supplies the selection criteria. The Node probe exercised three output paths for one small state. B14 also tested request variants and asynchronous failure behavior. |
| SSR | PARTIAL | Node generated complete HTML from input. B14 exercised request-specific filtering and initial ARIA state. No HTTP server, cache behavior or framework lifecycle was exercised. |
| CSR | VERIFIED | The baseline's client-side action changed visible product details in Chrome. This is one client-side update. |
| SSG | VERIFIED | The static export wrote a complete HTML file whose content matched the input model and server template. |
| Streaming | PARTIAL | The in-process stream yielded ordered HTML. B14 proved the shell arrived before a delayed secondary read and an error preserved the queue. Browser progressive paint and network buffering remain unmeasured. |
| Hydration | PARTIAL | B14 attaches handlers to initial state in an EventTarget fixture, toggles both ways and cleans up. Real DOM/framework hydration, event replay and mismatch recovery remain unverified. |
| Frontend development | VERIFIED | Both benchmark conditions produced self-contained frontend artifacts. One baseline supported keyboard interaction in Chrome. |
| Frontend debugging | PARTIAL | Browser traces identified a dashboard toggle that still animated under reduced motion at three widths. A prospective repair and regression test remain open. |
| Responsive engineering | PARTIAL | Both brand and hero treatments were exercised at 375, 768 and 1280 CSS px under two motion preferences. Physical devices and zoomed text remain open. |
| Accessibility engineering | PARTIAL | Keyboard operation, focus, responsive bounds and reduced-motion behavior were tested. The dashboard treatment retained an unwanted animation and a small status label measured 4.36:1 contrast. Screen-reader and physical-device validation remain open. |
| Frontend performance | PARTIAL | The Chromium fixture runner collected requestAnimationFrame intervals. B05 desktop median and p95 were both about 16.7 ms. Screenshot and harness overhead are included. Core Web Vitals, resource profiling and representative device testing remain open. |
| Automated tests | VERIFIED | The local Node assertions and USD checks ran. The repository also contains browser assertions for keyboard, state, overflow and reduced motion. Browser matrix completion is tracked separately. |

## B. Product design and UX

| Capability | Grade | Provider and boundary |
|---|---|---|
| Product design | PARTIAL | `direction.md`, OpenAI candidate and the benchmark produced an enterprise dashboard and pickup flow. Product validation with users is outstanding. |
| Information architecture | SUPPORTED | `direction.md` orders information by the user's decision. The corpus fixes dispatch rows, filters and meaningful statuses. |
| User journeys | SUPPORTED | `direction.md` asks for arrival, outcome and return. The mobile brief specifies selecting a time and confirming it. |
| User flows | PARTIAL | The print details, reversible dashboard filter, hero detail action and confirmation of Saturday 11:30 from the mobile scheduler ran in Chromium. Product validation across complete user journeys is outstanding. |
| Usability | PARTIAL | Independent visual critique identified an action separated from its product on mobile. Task completion by representative users has not been studied. |
| Design tokens | SUPPORTED | `direction.md` separates palette primitives and semantic roles. Figma code-connect and library skills offer a design-source integration on Claude. |
| Component states | PARTIAL | Chrome captured baseline states before and after activation. Error, stale, disabled and busy states remain design guidance. |
| Platform conventions | SUPPORTED | The UI policy specifies platform QA. Claude's Figma SwiftUI skill is discoverable. No simulator or real-device run is in evidence. |
| Responsive patterns | PARTIAL | The brand baseline reflows at narrow width. Independent review identified a vertical gap at 375 px. |
| Accessible patterns | PARTIAL | Semantic controls and visible focus appear in the baseline. The direction and motion references specify keyboard equivalents and reduced-motion paths. |

## C. Art direction

| Capability | Grade | Provider and boundary |
|---|---|---|
| Creative direction | PARTIAL | The common Low Tide print brief and fixed reference led to two independently inspected hero compositions. The added reveal recipe showed no clear practical benefit in these samples. |
| Art direction | PARTIAL | The selected incumbent passed all 12 brand and dashboard conditions and its exception color measured 4.87:1. A separate reviewer compared matched mobile hero images against the hierarchy and proximity requirements. |
| Visual hierarchy | VERIFIED | The baseline critique identified the dominant coastal print and the purchase action in actual desktop and mobile images. |
| Typography | PARTIAL | At 375 px the recipe-free hero used a single uppercase title. The recipe used title case and grouped the one-line description directly below it. Typeface loading and wider reading studies remain open. |
| Composition | VERIFIED | Independent inspection located the visual subject, action and problematic whitespace at a named width. |
| Color theory | PARTIAL | The layered orbital palette rasterized as intended. A separate dashboard review found a 4.36:1 small status label. Color-vision and larger palette studies remain open. |
| Brand identity | PARTIAL | The synthetic print studio has a bounded brand brief, artwork and rendered baseline. A reusable, multi-surface identity has not been evaluated. |
| Editorial design | PARTIAL | The brand baseline used editorial type and image layout. No sustained reading or multi-page rhythm evaluation was performed. |
| Image-led design | VERIFIED | The original coastal print occupied the dominant visual area in the inspected desktop baseline. |
| Environmental storytelling | SUPPORTED | `direction.md` and `scenes.md` connect material, silhouette and recurring subject across shots. The actual multi-shot result remains unreviewed. |
| Artistic consistency | PARTIAL | The same original print and copy were used across the paired hero treatments. Both retained a recognizable coastal motif and their own consistent palettes. No multisurface brand system was tested. |
| Evidence-based critique | VERIFIED | The independent initial review cites a specific viewport, visible feature, user consequence and focus state. |

## D. Motion

| Capability | Grade | Provider and boundary |
|---|---|---|
| Motion systems | PARTIAL | A shared fixture runner verified eight different effects under normal desktop and reduced mobile preferences, with explicit labels and keyboard alternatives. Production-scale orchestration remains open. |
| Timing | VERIFIED | B09 measured a 614 ms settle within 2% of the endpoint. B05 and B06 distinguished normal travel from immediate reduced-motion endpoints. |
| Easing | PARTIAL | B06 sampled 27 distinct normal-motion positions. B09 measured a nonlinear spring trajectory. The live-host reversal guidance uses the current pose. A mid-transition reversal has not been exercised in the browser. |
| Micro-interactions | VERIFIED | B05 reached the On state after keyboard activation. Its desktop trace recorded 12 positions and the reduced-motion trace recorded two. |
| Springs and damping | VERIFIED | B09 measured a 23.4% overshoot, about 614 ms to settle within 2% and peak forward and return velocities of +3,203 and −745 px/s. The mobile reduced-motion trace moved immediately to its endpoint. |
| Choreography | SUPPORTED | `motion.md` relates a staged cause to its consequence. `scenes.md` extends this to shots and holds. |
| Shared-element transitions | PARTIAL | B06 recorded continuous motion to a detail proof with 27 distinct desktop positions. The fixture uses a transformed print view. Cross-document shared-element transitions and interrupted FLIP remain open. |
| Masked reveals | SUPPORTED | `motion.md` covers clip or mask reveals and accessible fallback. The external masked-reveal candidate is held. |
| SVG morphing | SUPPORTED | `motion.md` covers path correspondence, winding and crossfading incompatible shapes. No morph was rendered. |
| Loops | VERIFIED | The original procedural orbit produced eight video frames and an identical source pose at the next cycle. The decoded displacement at the seam stayed within a pixel of the reference step. |
| Scroll interactions | VERIFIED | B07 drove the chapter strip with real browser wheel events. Home restored Shore on both viewports. Reduced motion held the crop fixed as the chapter changed. |
| Cinematic scroll | PARTIAL | B07 couples a three-chapter strip to crop scale and caption. Automated wheel and Home interactions passed. No long-form cinematic sequence or comparative pacing judgment was tested. |
| Interruptible animation | PARTIAL | Both live hosts used the motion reference, selected the current eased pose and prescribed focus return before hiding. Actual browser interruption and focus restoration remain unverified. |
| Animation state machines | SUPPORTED | `motion.md` supplies opening, open, closing and closed states with stale-completion protection. No timed UI state trace is available. |

## E. Interaction and environment

| Capability | Grade | Provider and boundary |
|---|---|---|
| Pointer input | VERIFIED | Playwright mouse input moved the B08 wind field, retargeted B09 and dragged the B11 block to a new face. |
| Touch input | SUPPORTED | Native Pointer Events and `motion.md` cover coarse input and native pan arbitration. No physical touch sample ran. |
| Gestures | PARTIAL | B11 captured pointer input across an eight-step drag, producing a changed face after release. Multitouch and native mobile scroll arbitration remain open. |
| Drag and release | VERIFIED | B11 received mouse-down, eight pointer steps and mouse-up. It exposed a different face afterwards. Velocity-based throwing and cancellation were outside the case. |
| Physics | VERIFIED | A one-dimensional B09 damped oscillator overshot its target and reversed. The measured browser trace settled within the stated tolerance. Contact or collision physics was outside the case. |
| Reactive environments | VERIFIED | B08 rendered a wind field responsive to pointer and keyboard actions, with visible frame changes and a working pause. |
| Organic motion | PARTIAL | B09 demonstrates a retargetable analytical spring with measured overshoot. B08 has a time-varying field. Perceived naturalness is a separate critique criterion. |
| Spatial interaction | VERIFIED | B11 changed the orientation of a six-face CSS block with a keyboard action and pointer dragging in both tested contexts. |
| Interactive dioramas | PARTIAL | B11 provides an interactive shaded block. A separate validated USD scene contains lighting and camera keys. No environmental diorama with multiple interactive subjects was tested. |
| Environmental reactivity | VERIFIED | B08 changed its ink directions in response to pointer position and keyboard input. Desktop playback paused on request and reduced motion redrew on input. |
| Accessible alternatives | PARTIAL | B08, B09 and B11 passed keyboard alternatives to pointer controls. B07 retained the narrative labels with a static crop under reduced motion. Touch cancellation and assistive-technology review remain open. |

## F. Cinematography and technical art

| Capability | Grade | Provider and boundary |
|---|---|---|
| Cinematography | PARTIAL | `scenes.md` specifies camera intent and shot design. The USD camera and light are valid; the shot has not been rendered and judged. |
| Shot composition | PARTIAL | The 2D clip layers a foreground line, subject and background. The independent brand critique applies to one still composition. |
| Camera choreography | PARTIAL | A three-key USD camera move compiled. B10 sampled 39 distinct desktop transform values in an image crop from Horizon to Ridge and two states with reduced motion. A multishot 3D camera sequence remains open. |
| Cinematic storytelling | SUPPORTED | `scenes.md` specifies an opening, turning point and final hold. No edited narrative sequence was reviewed. |
| 2D | VERIFIED | Original layered SVG frames were generated, rasterized, encoded and decoded. |
| 2.5D | SUPPORTED | `scenes.md` covers depth planes, parallax limits, overscan and stable typography. No parallax sample ran. |
| Real-time 3D | VERIFIED | B11 rotated a six-face CSS block by keyboard and pointer. A separate WebGL2 probe rasterized a depth-tested solid, changed its rendered face colors and restored the original orientation. |
| Three.js | PARTIAL | The pinned r187 camera subset projected eight vertices under Node, matching independent calculations at two aspect ratios and a quarter turn. In Chromium the same camera math drove an authored WebGL2 block, changing coverage and restoring its initial framebuffer signature. Full Three.js rendering, loaders and scene tooling remain outside the reviewed subset. [Reference and digest](FRONTEND-REFERENCES.md#verified-threejs-camera-subset) |
| WebGL | VERIFIED | B11 compiled local vertex and fragment shaders, rendered a depth-tested block, rotated it and returned to exactly the initial sampled framebuffer signature. Visible coverage changed from 10,109 to 8,956 pixels. |
| Technical art | PARTIAL | The procedural mesh, material and rig passed USD structural checks. B11 separately verified a custom shaded block in Chromium. Runtime rigging, advanced lighting and cross-engine material fidelity remain open. |
| Procedural geometry | VERIFIED | The wheel generator produced indexed cap and side faces. USDC and USDZ checks passed. |
| Particles | SUPPORTED | `scenes.md` covers buffer reuse, instancing, count and resolution budgets. No particle simulation was exercised. |
| Shader programming | VERIFIED | B11 compiled and linked authored WebGL2 vertex and fragment shaders, rendered multiple face colors in two orientations and verified the restored output. WGSL and production shader pipelines remain unmeasured. |
| Generative art | VERIFIED | A deterministic orbit generated eight original layered frames. The separate browser B12 seeded contour field changed output and reproduced the initial pixel signature after returning to its original seed. |
| Spatial navigation | PARTIAL | The B11 block offered keyboard and pointer orientation changes. Traversal or keyboard waypoint navigation through a multi-object world has not been tested. |
| Rendering optimization | SUPPORTED | `direction.md` and `scenes.md` cover batching, pixel ratio, capped buffers and profiling. No before/after GPU or frame-time measurement. |

## G. Asset production

| Capability | Grade | Provider and boundary |
|---|---|---|
| Original assets | VERIFIED | The first-party illustration, VP9 clip, procedural model and rig were constructed from original primitives. |
| Image generation | PARTIAL | Codex system `imagegen` is discoverable and the chat surface supplies an ImageGen tool. The original procedural art probe completed. No model-generated bitmap or provider-specific cost verification from this probe. |
| Layered illustration | VERIFIED | Four named SVG groups kept the ground, orbit, moving object and foreground separately editable through rasterization. |
| 3D modeling | VERIFIED | The original polygonal wheel compiled to binary USD and packaged successfully. Interactive sculpting, retopology and Blender authoring were not demonstrated. |
| Materials | PARTIAL | A bound UsdPreviewSurface material with explicit diffuse color and roughness passed structural validation. Its rendered appearance remains unverified. |
| Lighting | PARTIAL | A positioned, powered SphereLight with explicit radius passed USD validation. Illumination and shadows remain unverified. |
| Character animation | PARTIAL | A simple articulated flag exercises joints and weights. No performance, locomotion, humanoid character or rendered deformation was tested. |
| Skeletal animation and rigging | PARTIAL | A two-bone rig with weighted vertices and timed joint rotations round-tripped through USDC. A CPU reference verified one offset vertex. Runtime skin deformation and artistic rig quality are open. |
| Video production | VERIFIED | FFmpeg encoded and decoded an original eight-frame VP9 WebM. This path covers short silent clips. Full editing, audio mixing and general-purpose transcoding were not exercised. |
| Compositing | VERIFIED | The four editable source layers were composited into each rasterized orbital frame. Alpha video, mattes and multi-pass 3D compositing remain open. |
| Asset optimization | PARTIAL | A 14,669-byte input JPEG stream became a 2,165-byte video. Comparable-quality codec trials, texture budgets and perceptual validation remain open. |
| Licensing and provenance | VERIFIED | The source review records license decisions, consumed paths, commit SHAs and tree digests. The original probe records editable source and output checksums. No third-party art was imported. [Reproducible original asset probes](../evals/frontend/assets/README.md) |

## H. Visual verification

| Capability | Grade | Provider and boundary |
|---|---|---|
| Browser automation | VERIFIED | Playwright drove the minimal baseline in actual Chrome, captured state and interacted through the keyboard. |
| Screenshot inspection | VERIFIED | Independent inspection of saved desktop and mobile baseline screenshots produced concrete findings. |
| Visual regression | PARTIAL | Matching viewport, preference and action states were captured across both hero, dashboard and brand arms. A rotated WebGL block and a seeded contour field restored the initial pixel signatures. The generated designs lack a blessed visual baseline or perceptual threshold. |
| Interaction recording | VERIFIED | B05–B12 saved before/after screenshots plus timestamped samples of positions, transforms or canvas pixel signatures. Each desktop case also saved an in-motion screenshot. These are sampled records of particular interactions. |
| Motion verification | VERIFIED | All 16 B05–B12 cases passed at desktop normal motion and mobile reduced motion. Frame traces include spring overshoot and return, pause, bounded crop travel and reproducible canvas states. |
| Transition verification | VERIFIED | B06 reached its detail-proof state in both motion modes. The desktop trace recorded 27 positions, the reduced-motion trace two. B10 reached Ridge with 39 and two distinct transform samples respectively. |
| Loop continuity | VERIFIED | On the eight-frame 2D clip, the decoded last-to-first displacement differs from the expected vector by 0.243 px. The contact sheet corroborated the positions. UI playback scheduling and character-animation seams remain open. |
| Cross-browser testing | PARTIAL | Chrome was used. Safari, SafariDriver and Firefox are available, but no second-engine run was verified. |
| Mobile testing | PARTIAL | Chrome viewport emulation was captured at 375 px. No physical phone or touch-device run was verified. |
| Accessibility testing | PARTIAL | Keyboard and focus checks ran across the component fixtures. The generated dashboard retained a toggle animation under reduced motion at 375, 768 and 1280 px. Its small status text measured 4.36:1. Screen-reader and physical touch validation remain open. |
| Rendering performance measurement | PARTIAL | The Chrome fixture runner recorded per-case frame intervals during 1.5-second windows. B09 desktop median and p95 intervals were about 16.7 ms. Screenshot overhead is included. GPU costs and cross-device budgets remain unverified. |
| Independent critique | VERIFIED | An independent agent inspected the baseline and another reviewed both 375 px hero detail states. The recipe placed the end of the artwork near y=348 and its focused action near y=742, leaving roughly 394 px of empty space. Both arms exposed a focused control and a pigment-detail tag. |
| Reference comparison | VERIFIED | The paired hero samples used the same original Low Tide reference, copy and budgets. At 375 px the recipe-free version fills the viewport with the artwork and groups the description with its action. The recipe keeps the top heading, description and smaller print together but separates the action. This bounded comparison offers no clear advantage for its additional 2,084 input tokens. |

The missing authoring environment is specific. No approved Blender or equivalent interactive character DCC was observed. The USD utilities provide an interchange and procedural path. Browser, frame and DCC claims require actual target output before their grades can advance. Re-run the affected probes after changing a pin, host version or browser harness. [UI policy](UI-UX-POLICY.md) · [Benchmark procedure](../evals/frontend/README.md)
