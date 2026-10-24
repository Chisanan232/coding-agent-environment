() => {
  // Single arrow-function expression: evaluated in the page by tests/ui-smoke.mjs or any browser tool.
  const doc = document.documentElement;
  const active = document.activeElement;
  const style = active && active !== document.body ? getComputedStyle(active) : null;
  const pulse = document.querySelector('.pulse');
  return {
    horizontalOverflow: doc.scrollWidth > doc.clientWidth,
    scrollWidth: doc.scrollWidth,
    clientWidth: doc.clientWidth,
    focusedTag: active ? active.tagName : null,
    focusedLabel: active ? (active.textContent || '').trim().slice(0, 40) : null,
    focusOutlineWidthPx: style ? parseFloat(style.outlineWidth) : 0,
    focusOutlineStyle: style ? style.outlineStyle : 'none',
    runningAnimations: pulse ? pulse.getAnimations().filter((a) => a.playState === 'running').length : -1,
    animationName: pulse ? getComputedStyle(pulse).animationName : null,
    colorScheme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
  };
}
