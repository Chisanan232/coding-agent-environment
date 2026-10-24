// Local rendered-UI smoke test. Requires an already-installed `playwright` package and browser;
// it never downloads anything. Screenshots go to a gitignored directory and are never uploaded.
//   node tests/ui-smoke.mjs [outDir]
import { readFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = pathToFileURL(join(here, 'fixtures/ui-smoke/index.html')).href;
const checks = readFileSync(join(here, 'fixtures/ui-smoke/checks.js'), 'utf8');
const outDir = resolve(process.argv[2] ?? join(here, '../.qa-artifacts/ui-smoke'));
mkdirSync(outDir, { recursive: true });

let chromium;
try {
  ({ chromium } = await import('playwright'));
} catch {
  console.error('playwright is not resolvable from this directory; install it locally or run the checks via a browser tool.');
  process.exit(2);
}

const failures = [];
const expect = (ok, message) => { if (!ok) failures.push(message); };
const browser = await chromium.launch();
try {
  for (const { width, height } of [{ width: 375, height: 812 }, { width: 1280, height: 800 }]) {
    for (const scheme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, reducedMotion: 'reduce' });
      const page = await context.newPage();
      await page.goto(fixture);
      await page.keyboard.press('Tab');
      const result = await page.evaluate(`(${checks})()`);
      const label = `${width}px/${scheme}`;
      expect(!result.horizontalOverflow, `${label}: horizontal scroll (${result.scrollWidth} > ${result.clientWidth})`);
      expect(result.focusedTag !== null && result.focusOutlineStyle !== 'none' && result.focusOutlineWidthPx >= 2, `${label}: focus indicator not visible`);
      expect(result.reducedMotion && result.runningAnimations === 0, `${label}: reduced motion not honoured`);
      expect(result.colorScheme === scheme, `${label}: color scheme not applied`);
      await page.screenshot({ path: join(outDir, `smoke-${width}-${scheme}.png`), fullPage: true });
      await context.close();
    }
  }
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: 'no-preference' });
  const page = await context.newPage();
  await page.goto(fixture);
  const motion = await page.evaluate(`(${checks})()`);
  expect(motion.runningAnimations > 0, 'control: animation should run without a reduced-motion preference');
  await context.close();
} finally {
  await browser.close();
}
if (failures.length) { console.error(failures.join('\n')); process.exit(1); }
console.log('ui-smoke: all checks passed');
