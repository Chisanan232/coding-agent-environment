# Bounded frontend skill benchmark

The [case matrix](MATRIX.md) maps the five briefs, eight motion fixtures, optional shader probe and architecture review to B01–B14.

The five briefs cover a premium print studio, a dispatch dashboard, a crop viewer, a two-scene transition and a mobile pickup form. Each arm receives identical copy, interaction requirements and an original coastal print. The artwork also supplies a common color reference for the application briefs.

## Paired generation

Use fresh, tool-free sessions on an included subscription. Record the host version, effective model ID, requested effort, token ceiling, elapsed time and all input, cache and output token counts for each call. Keep auth, account metadata, generated HTML and screenshots in a private evaluation directory. A tool-free prompt with appended reviewed skill text measures the instruction body. Verify installation and native invocation separately.

Run a minimal baseline first or counterbalance order across cases. Add one pinned primary to the same brief. For a short editorial reveal, pair that primary with and without the approved recipe. Admit candidates to this comparison only after source and license review. Use one response per arm, the same system prompt, a 3,200-output-token ceiling, 6,500-byte HTML budget, 170-second timeout and zero repair rounds. A failed arm keeps its result.

Generate the prompt from the repository root. Supply local paths to verified skill bodies in primary-then-recipe order.

```sh
python3 evals/frontend/prompt.py dashboard > "$run_dir/prompt.txt"
python3 evals/frontend/prompt.py dashboard "$primary/SKILL.md" > "$run_dir/treatment.txt"
```

For the Claude Code condition use `claude -p --safe-mode --restricted --tools '' --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence --output-format json`, with explicit `--model`, `--effort medium`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS=3200` and the system prompt “Complete the supplied frontend benchmark. Return the requested artifact only.” Pass the generated prompt on stdin from a new empty working directory. Accept a single completed response on the selected model. Extract its HTML without rewriting it and remove an outer Markdown code fence if present. Inspect the generated code before rendering.

## Browser capture

Use a locally installed Playwright package and Chromium. The runner uses fresh browser contexts and serves the HTML and reference art from memory. The page gets a restrictive content policy, and the harness intercepts page requests, allowing only the synthetic document and its local artwork. Chromium must start with its process sandbox enabled. Browser-level background traffic is outside the page-request measurement. Screenshots include settled views, interactive states and desktop entry and transition samples. The middle frame is diagnostic, so keep the actual browser version with the result. Run inside the workstation's admitted browser QA lane.

```sh
node evals/frontend/render.mjs dashboard "$run_dir/index.html" "$run_dir/capture"
```

The output directory must be new. Set `FRONTEND_PLAYWRIGHT_MODULE` to a local Playwright ES module and `FRONTEND_BROWSER` to a local Chromium executable if needed. The runner returns nonzero for an oversized response, missing keyboard action, failed state change or reversal, overflow, page error, network/policy violation or detected reduced-motion animation. Inspect the screenshots for visual defects even when it returns zero.

## Critique and decision

Compare equivalent widths and states side by side, preferably with treatment labels hidden. Give a short observable reason per axis. Use 0 for failure, 1 for passable, 2 for clearly effective. Score composition, typography, art direction, interaction quality, task usability and accessibility. Check copy and contrast manually alongside the recorded focus styles, action bounds and motion state. Record token and wall-clock deltas separately. A small single-model sample supports a bounded routing decision only. A practical improvement must survive the essential interaction, responsive and accessibility checks for its intended task. Record unrun arms, deficits and the reviewer relationship explicitly.

### Perceptual evidence control

Run `node evals/frontend/perceptual-control.mjs` with the existing local Playwright and browser (same `FRONTEND_PLAYWRIGHT_MODULE` and `FRONTEND_BROWSER` overrides as the rendering harness). This independent fixture samples three displaced poses. A child moves behind an opaque foreground; the measured transform changes and its final pixels stay identical to rest. Reject a visible-motion claim for those frames. Removing the occluder makes the frames differ and verifies sensitivity of the control. Ordinary-playback perception and artistic quality remain unverified by that positive result. The fixture needs no generators, downloads or full campaign.
