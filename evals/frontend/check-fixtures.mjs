// Measure original fixture motion in the browser. Raw samples and PNGs stay in the caller's new directory.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { rasterProbe } from './raster-probe.mjs';
import { analyzeFrames } from './motion-metrics.mjs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const out = process.argv[2];
if (!out) throw new Error('Usage: node check-fixtures.mjs NEW_PRIVATE_DIRECTORY');
await mkdir(out);
const root = dirname(fileURLToPath(import.meta.url));
const resources = new Map();
for (const [name, mime] of Object.entries({
  'index.html': 'text/html', 'fixture.js': 'text/javascript', 'fixture.css': 'text/css', 'reference.svg': 'image/svg+xml'
})) {
  const source = name === 'reference.svg' ? root : join(root, 'fixtures');
  resources.set('/' + name, { contentType: mime, body: await readFile(join(source, name)) });
}
const modulePath = process.env.FRONTEND_PLAYWRIGHT_MODULE;
const { chromium } = await import(modulePath ? pathToFileURL(resolve(modulePath)).href : 'playwright');
const browser = await chromium.launch({ chromiumSandbox: true, executablePath: process.env.FRONTEND_BROWSER || undefined });
const origin = 'https://fixtures.invalid';
const expected = { B05: 'On', B06: 'Detail proof', B07: 'Strata', B08: 'East', B09: 'Far', B10: 'Ridge', B11: 'Face 2', B12: 'Seed 2' };
const evidence = [];
let raster;
const csp = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'";

// This function is serialized by Playwright. Values come from browser layout, computed transforms and painted canvas pixels.
function beginSamples(caseId) {
  const scene = document.getElementById(caseId);
  const marker = scene.querySelector('.marker');
  const canvas = scene.querySelector('canvas');
  let downsample;
  if (canvas) {
    downsample = document.createElement('canvas');
    downsample.width = 32;
    downsample.height = 16;
  }
  function value() {
    if (canvas) {
      const ctx = downsample.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(canvas, 0, 0, 32, 16);
      let hash = 2166136261;
      for (const byte of ctx.getImageData(0, 0, 32, 16).data) hash = Math.imul(hash ^ byte, 16777619);
      return hash >>> 0;
    }
    if (['B05','B06','B09'].includes(caseId)) return marker.getBoundingClientRect().x;
    const transform = getComputedStyle(marker).transform;
    return transform === 'none' ? 1 : new DOMMatrixReadOnly(transform).m11;
  }
  window.fixtureValue = value;
  window.fixtureSamples = [];
  window.addEventListener(caseId === 'B07' ? 'wheel' : 'keydown', () => {
    const start = performance.now();
    const sample = now => {
      window.fixtureSamples.push({ ms: Math.max(0, now - start), value: value() });
      if (now - start < 1500) requestAnimationFrame(sample);
      else window.fixtureComplete = true;
    };
    sample(start);
  }, { once: true, capture: true });
}

async function pointerCheck(page, id, section) {
  if (!['B08','B09','B11'].includes(id)) return undefined;
  const box = await section.locator('.stage').boundingBox();
  const y = box.y + box.height/2;
  if (id === 'B11') {
    await page.mouse.move(box.x + box.width*.15, y);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width*.85, y, { steps: 8 });
    await page.mouse.up();
  } else if (id === 'B09') {
    await page.mouse.click(box.x + box.width*.15, y);
  } else {
    await page.mouse.move(box.x + box.width*.15, y);
  }
  const label = await section.locator('output').innerText();
  return id === 'B11' ? label !== 'Face 2' : label === (id === 'B08' ? 'West' : 'Near');
}

try {
  for (const id of Object.keys(expected)) {
    for (const reduced of [false, true]) {
      const width = reduced ? 375 : 1280;
      const context = await browser.newContext({
        viewport: { width, height: 800 }, reducedMotion: reduced ? 'reduce' : 'no-preference',
        acceptDownloads: false, serviceWorkers: 'block'
      });
      const errors = [], blocked = [];
      const row = { id, width, reduced, errors, blocked };
      evidence.push(row);
      try {
        await context.route('**/*', route => {
          const url = new URL(route.request().url());
          const asset = url.origin === origin && resources.get(url.pathname);
          if (asset) return route.fulfill({ ...asset, headers: { 'content-security-policy': csp } });
          blocked.push(route.request().resourceType());
          return route.abort();
        });
        const page = await context.newPage();
        page.setDefaultTimeout(3500);
        page.on('pageerror', error => errors.push(error.name));
        await page.goto(origin + '/index.html?case=' + id);
        const section = page.locator('#' + id);
        const input = section.locator(id === 'B07' ? '.scroll' : 'button');
        await page.keyboard.press('Tab');
        row.focus = await input.evaluate(el => el === document.activeElement && getComputedStyle(el).outlineStyle !== 'none');
        row.overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
        const filename = [id, width].join('-');
        await page.screenshot({ path: join(out, filename + '-before.png') });
        await page.evaluate(beginSamples, id);
        const initialValue = await page.evaluate(() => window.fixtureValue());
        if (id === 'B07') {
          await input.hover();
          await page.mouse.wheel(0, 120);
          await page.waitForTimeout(120);
          await page.mouse.wheel(0, 1200);
        } else {
          await page.keyboard.press('Enter');
        }
        if (!reduced) {
          await page.waitForTimeout(95);
          await page.screenshot({ path: join(out, filename + '-in-motion.png') });
        }
        await page.waitForFunction(() => window.fixtureComplete, undefined, { timeout: 3500 });
        const samples = await page.evaluate(() => window.fixtureSamples);
        await writeFile(join(out, filename + '-frames.json'), JSON.stringify(samples));
        const travel = id === 'B09' ? await section.locator('.rail').evaluate(el => el.clientWidth) : undefined;
        row.motion = analyzeFrames(samples, id, reduced, travel);
        row.state = await section.locator('output').innerText();
        await page.screenshot({ path: join(out, filename + '-after.png') });
        row.pointer = await pointerCheck(page, id, section);
        if (id === 'B08' && !reduced) {
          const animate = section.getByRole('checkbox', { name: 'Animate wind' });
          await animate.focus();
          await page.keyboard.press('Space');
          const frozen = await page.evaluate(() => window.fixtureValue());
          await page.waitForTimeout(160);
          row.windPauses = !(await animate.isChecked()) && frozen === await page.evaluate(() => window.fixtureValue());
        }
        if (id === 'B07') {
          await input.focus();
          await page.keyboard.press('Home');
          await page.waitForFunction(() => document.querySelector('#B07 output').value === 'Shore');
          row.scrollRestored = true;
        }
        if (id === 'B12') {
          await page.keyboard.press('Enter');
          row.seedReproduced = await page.evaluate(() => window.fixtureValue()) === initialValue;
        }
        row.pass = row.focus && !row.overflow && row.motion.pass && row.state === expected[id]
          && row.pointer !== false && row.windPauses !== false && row.seedReproduced !== false && !blocked.length && !errors.length;
      } catch (error) {
        row.pass = false;
        row.failure = error.message.split('\n')[0];
      } finally {
        await context.close();
      }
    }
  }
  try {
    raster = await rasterProbe(browser, out, process.env.FRONTEND_THREE_CAMERA);
  } catch (error) {
    raster = { status: 'FAIL', reason: error.message.split('\n')[0] };
  }
} finally {
  await browser.close();
}
await writeFile(join(out,'fixture-results.json'),JSON.stringify({ browser: browser.version(), evidence, raster },null,2)+'\n');
if (evidence.some(row => !row.pass) || raster.status === 'FAIL') process.exitCode = 1;
