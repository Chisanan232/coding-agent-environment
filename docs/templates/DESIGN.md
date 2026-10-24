# DESIGN.md skeleton

Copy this file into a product repository as `DESIGN.md` (or into its `design/`
directory) and replace every bracketed placeholder. It is layer 2 of
[`docs/UI-UX-POLICY.md`](../UI-UX-POLICY.md): once filled in, it outranks any
third-party design skill.

## Product and users

- Product: [name and one-line purpose]
- Primary users and their main jobs: [list]
- Surfaces covered: [web, iOS, Android, desktop]

## Design principles

[3 to 5 principles, each with one sentence on what it forbids.]

## Visual system

- Color tokens: [names, values, light and dark, where defined in code]
- Typography: [families, scale, line heights]
- Spacing and layout: [scale, grid, breakpoints]
- Components: [path to the component library; reuse before adding]

## Motion

- Purpose of motion: [what it communicates]
- Durations and easings: [tokens]
- Reduced motion: [required behaviour under `prefers-reduced-motion`]

## Accessibility contract

- Target: [for example WCAG 2.2 AA]
- Keyboard, focus, contrast, target size, screen reader labels: [requirements]

## Voice and copy

[Tone, terms to use, terms to avoid.]

## Review checklist for AI-assisted changes

[Short list a reviewer can tick; link to the product's own checklist if any.]
