# UI QA checklist

Complete for every UI change and attach the result to the delivery. Screenshots
stay local and are referenced by path only. See
[`docs/UI-UX-POLICY.md`](../UI-UX-POLICY.md).

## Web

- [ ] Rendered the real UI (not only source review)
- [ ] Screenshots at 375, 768, and 1280 px (local path: [path])
- [ ] No horizontal scroll at any checked width
- [ ] Keyboard-only pass: reachable, logical order, visible focus, no trap
- [ ] `prefers-reduced-motion: reduce` honoured
- [ ] Light and dark schemes both legible
- [ ] Contrast and text resizing meet the stated accessibility target
- [ ] Existing Design System components reused; new ones justified

## iOS

- [ ] Human Interface Guidelines conventions followed
- [ ] Simulator run at smallest and largest supported sizes
- [ ] Dynamic Type, Dark Mode, and VoiceOver labels checked
- [ ] XcodeBuildMCP used only if the task required it, in a pinned form

## Process

- [ ] Design brief completed; exactly one primary art-direction skill (or none)
- [ ] One bounded repair and verify cycle run; leftovers escalated
- [ ] Independent review done by someone other than the author
- [ ] Items not verified are listed explicitly: [list or "none"]
