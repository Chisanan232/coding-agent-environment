// Capture a single, local benchmark response. The browser sees only these two synthetic URLs.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [task, input, output] = process.argv.slice(2);
const root = dirname(fileURLToPath(import.meta.url));
const corpus = JSON.parse(await readFile(join(root, 'corpus.json'), 'utf8'));
if (!corpus.tasks[task] || !input || !output) {
  throw new Error('Usage: node render.mjs TASK RESULT.html NEW_OUTPUT_DIRECTORY');
}
const spec = corpus.tasks[task];
const html = await readFile(input, 'utf8');
if (Buffer.byteLength(html) > 65536) throw new Error('Response exceeds the harness safety limit');
const art = await readFile(join(root, 'reference.svg'), 'utf8');
const out = resolve(output);
await mkdir(out, { recursive: false });
const modulePath = process.env.FRONTEND_PLAYWRIGHT_MODULE;
const { chromium } = await import(modulePath ? pathToFileURL(resolve(modulePath)).href : 'playwright');
const browser = await chromium.launch({ chromiumSandbox: true, executablePath: process.env.FRONTEND_BROWSER || undefined });
const origin = 'https://frontend-benchmark.invalid';
// A sandboxed form needs allow-forms even to dispatch a local, canceled submit event.
// form-action 'none' still prohibits submission destinations and the route allowlist stays in force.
const csp = "default-src 'none'; img-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; sandbox allow-scripts allow-forms";
const results = [];
try {
  for (const width of [375, 768, 1280]) {
    for (const motion of ['reduce', 'no-preference']) {
      const label = width + '-' + motion;
      const context = await browser.newContext({
        viewport: { width, height: width === 375 ? 812 : 800 },
        reducedMotion: motion, acceptDownloads: false, serviceWorkers: 'block'
      });
      const errors = [];
      const blocked = [];
      let allowScript = true;
      const page = await context.newPage();
      page.setDefaultTimeout(3500);
      page.on('pageerror', error => errors.push(error.name));
      await context.route('**/*', async route => {
        const url = route.request().url();
        if (url === origin + '/index.html' && route.request().resourceType() === 'document') {
          return route.fulfill({ contentType: 'text/html', body: html, headers: { 'content-security-policy': allowScript ? csp : csp.replace("script-src 'unsafe-inline'", "script-src 'none'") } });
        }
        if (url === origin + '/reference.svg' && route.request().resourceType() === 'image') {
          return route.fulfill({ contentType: 'image/svg+xml', body: art });
        }
        blocked.push(route.request().resourceType());
        return route.abort();
      });
      const record = { width, motion, blocked, errors };
      results.push(record);
      try {
        await page.addInitScript(() => {
          window.policyViolations = [];
          document.addEventListener('securitypolicyviolation', e => window.policyViolations.push(e.violatedDirective));
        });
        await page.goto(origin + '/index.html', { waitUntil: 'load' });
        // Entry frame plus the settled view. Timing is diagnostic and varies by browser and machine.
        if (motion === 'no-preference' && width === 1280) {
          await page.screenshot({ path: join(out, 'entry.png') });
          if (task === 'hero') {
            await page.waitForTimeout(210);
            await page.screenshot({ path: join(out, 'entry-mid.png') });
          }
        }
        await page.waitForTimeout(1050);
        const initial = await page.locator('body').innerText();
        if (task === 'mobile') {
          // Check the alternate slot so a hard-coded confirmation cannot pass.
          record.defaultSlot = await page.getByRole('radio', { name: /10:00/ }).isChecked();
          await page.getByRole('radio', { name: /11:30/ }).check();
        }
        record.initialState = task === 'dashboard'
          ? ['Access Road', 'Mill Lane', 'East Dock', '08:30', '08:45', '09:10'].every(t => initial.includes(t))
          : task === 'motion' ? initial.includes('Exposure') : !initial.includes(spec.after);
        record.overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
        await page.screenshot({ path: join(out, label + '-before.png') });
        const action = page.getByRole('button', { name: spec.action, exact: true })
          .or(page.getByRole('checkbox', { name: spec.action, exact: true }))
          .or(page.getByRole('switch', { name: spec.action, exact: true }))
          // Generated +/- disclosure indicators can enter the accessible name.
          // Accept a state glyph only for an initially collapsed native disclosure.
          .or(page.getByRole('button', { name: spec.action + ' +', exact: true, expanded: false }));
        record.actionCount = await action.count();
        if (record.actionCount !== 1) throw new Error('expected one named action');
        record.actionBounds = await action.boundingBox();
        let found = false;
        for (let i = 0; i < 16 && !found; i++) {
          await page.keyboard.press('Tab');
          found = await action.evaluate(el => el === document.activeElement);
        }
        record.keyboardReachable = found;
        if (!found) throw new Error('action unreachable with Tab');
        record.focusStyle = await action.evaluate(el => {
          const s = getComputedStyle(el);
          return { outline: s.outlineStyle, width: s.outlineWidth, shadow: s.boxShadow };
        });
        const checkable = await action.evaluate(el => el.matches('input[type=checkbox],[role="switch"],[role="checkbox"]'));
        const key = checkable ? 'Space' : 'Enter';
        await page.keyboard.press(key);
        record.animationsAfterAction = await page.evaluate(() => document.getAnimations().filter(a => a.playState === 'running').length);
        if (width === 1280 && motion === 'no-preference') {
          await page.waitForTimeout(140);
          await page.screenshot({ path: join(out, 'transition.png') });
        }
        await page.waitForTimeout(650);
        const bodyText = await page.locator('body').innerText();
        record.expectedState = bodyText !== initial && bodyText.includes(spec.after) && !(spec.hiddenAfter || []).some(text => bodyText.includes(text));
        if (task === 'mobile') {
          const confirmation = await page.getByText(/Pickup confirmed/i).last().innerText();
          record.confirmationText = confirmation;
          record.expectedState &&= confirmation.includes('11:30');
        }
        record.policyViolations = await page.evaluate(() => window.policyViolations);
        await page.screenshot({ path: join(out, label + '-after.png') });
        if (task !== 'mobile') {
          // A product may put an explicit Close button inside a dialog.
          const close = page.getByRole('button', { name: /^(close|close details)$/i });
          if (await close.isVisible()) await close.click();
          else await page.keyboard.press(key);
          await page.waitForTimeout(650);
          record.reversible = (await page.locator('body').innerText()) === initial;
        }
        if (task === 'hero' && motion === 'reduce') {
          allowScript = false;
          await page.reload({ waitUntil: 'load' });
          record.staticHeading = await page.locator('h1').evaluate(heading => {
            const visible = el => {
              const s = getComputedStyle(el);
              return Number(s.opacity) > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            return heading.textContent.replace(/\s+/g, '').trim() === 'LowTide'
              && [heading, ...heading.querySelectorAll('*')].every(visible);
          });
        }
      } catch (error) {
        record.failure = error.message.split('\n')[0];
      } finally {
        await context.close();
      }
    }
  }
} finally {
  await browser.close();
}
const outputRecord = { task, bytes: Buffer.byteLength(html), browser: browser.version(), results };
await writeFile(join(out, 'render.json'), JSON.stringify(outputRecord, null, 2) + '\n');
const failed = results.some(r => r.failure || r.overflow || !r.initialState || !r.expectedState || r.reversible === false || r.staticHeading === false || r.defaultSlot === false || r.blocked.length ||
  r.errors.length || r.policyViolations?.length || (r.motion === 'reduce' && r.animationsAfterAction));
if (failed || outputRecord.bytes > 6500) process.exitCode = 1;
