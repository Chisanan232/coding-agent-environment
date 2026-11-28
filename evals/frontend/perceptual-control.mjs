// A negative control for interpreting motion traces. Use the existing local browser.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const modulePath = process.env.FRONTEND_PLAYWRIGHT_MODULE;
const { chromium } = await import(modulePath ? pathToFileURL(resolve(modulePath)).href : 'playwright');
const browser = await chromium.launch({
  chromiumSandbox: true,
  executablePath: process.env.FRONTEND_BROWSER || undefined,
});
try {
  for (const width of [375, 1280]) {
    const page = await browser.newPage({ viewport: { width, height: 480 }, deviceScaleFactor: 1 });
    await page.setContent('<style>*{margin:0}canvas{display:block}</style><canvas></canvas>');
    async function pose(relativeX, covered) {
      const actualOffset = await page.evaluate(({ relativeX, covered }) => {
        const canvas = document.querySelector('canvas');
        canvas.width = innerWidth;
        canvas.height = innerHeight;
        const paint = canvas.getContext('2d');
        paint.fillStyle = '#ddd3c1';
        paint.fillRect(0, 0, canvas.width, canvas.height);
        paint.translate(canvas.width / 2, canvas.height / 2);
        paint.save();
        paint.translate(relativeX, 0);
        paint.fillStyle = '#8e331c';
        paint.fillRect(-6, -6, 12, 12);
        const offset = paint.getTransform().e - canvas.width / 2;
        paint.restore();
        if (covered) {
          paint.fillStyle = '#4e5849';
          paint.fillRect(-42, -12, 84, 24);
        }
        return offset;
      }, { relativeX, covered });
      assert.equal(actualOffset, relativeX);
      return page.screenshot();
    }
    for (const covered of [true, false]) {
      const rest = await pose(0, covered);
      for (const offset of [-24, 16, -4]) {
        const frame = await pose(offset, covered);
        assert.equal(rest.equals(frame), covered, 'Unexpected final composite');
      }
    }
    console.log(width, 'covered displacement: identical frames; exposed control: different frames');
    await page.close();
  }
} finally {
  await browser.close();
}
