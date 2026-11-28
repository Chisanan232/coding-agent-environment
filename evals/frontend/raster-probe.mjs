// B11 optional real rasterization. Source bytes are verified in memory before any browser receives them.
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open, readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const root = dirname(fileURLToPath(import.meta.url));
const names = [
  'LICENSE', 'src/cameras/Camera.js', 'src/cameras/PerspectiveCamera.js', 'src/constants.js',
  'src/core/EventDispatcher.js', 'src/core/Layers.js', 'src/core/Object3D.js', 'src/math/Euler.js',
  'src/math/MathUtils.js', 'src/math/Matrix3.js', 'src/math/Matrix4.js', 'src/math/Quaternion.js',
  'src/math/Vector2.js', 'src/math/Vector3.js', 'src/utils.js'
];

export async function cameraResources(tree) {
  const folder = resolve(tree);
  const { stdout } = await promisify(execFile)('bash', [join(root,'three-projection.sh'),'--verify-only',folder]);
  const expected = stdout.trim();
  const entries = new Map();
  const digest = createHash('sha256');
  for (const name of names.toSorted()) {
    const handle = await open(join(folder,name), constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const info = await handle.stat();
      if (!info.isFile() || info.size > 2_000_000) throw new Error('Unexpected camera source file');
      const bytes = await handle.readFile();
      const mode = info.mode & 0o111 ? '0755' : '0644';
      digest.update(name).update('\0').update(mode).update('\0');
      digest.update(createHash('sha256').update(bytes).digest()).update('\0');
      if (name !== 'LICENSE') entries.set('/three/'+name,{ contentType:'text/javascript',body:bytes });
    } finally {
      await handle.close();
    }
  }
  if (digest.digest('hex') !== expected) throw new Error('Served camera bytes differ from the reviewed tree');
  return entries;
}

function readPixels() {
  const canvas = document.querySelector('canvas');
  const gl = canvas.getContext('webgl2');
  const pixels = new Uint8Array(canvas.width * canvas.height * 4);
  gl.readPixels(0,0,canvas.width,canvas.height,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
  let signature = 2166136261, covered = 0;
  const colors = new Set();
  for (let i=0; i<pixels.length; i+=4) {
    const rgb = pixels[i]*65536 + pixels[i+1]*256 + pixels[i+2];
    signature = Math.imul(signature ^ rgb,16777619);
    if (rgb === 0xffffff) continue;
    covered++;
    colors.add(rgb);
  }
  return { signature: signature>>>0, covered, colors:colors.size, error:gl.getError() };
}

export async function rasterProbe(browser,out,tree) {
  if (!tree) return { status:'SKIPPED', reason:'Reviewed local camera subset was not supplied' };
  const resources = await cameraResources(tree);
  for (const [file,contentType] of [['raster.html','text/html'],['raster.mjs','text/javascript']]) {
    resources.set('/'+file,{ contentType,body:await readFile(join(root,'fixtures',file)) });
  }
  const origin = 'https://raster.invalid';
  const context = await browser.newContext({
    viewport:{ width:480,height:400 }, reducedMotion:'reduce', acceptDownloads:false, serviceWorkers:'block'
  });
  try {
    const errors = [], blocked = [];
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      const file = url.origin === origin && resources.get(url.pathname);
      if (file) return route.fulfill({ ...file, headers:{
        'content-security-policy': "default-src 'none'; script-src 'self'; base-uri 'none'; form-action 'none'"
      } });
      blocked.push(route.request().resourceType());
      return route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(4000);
    page.on('pageerror', e => errors.push(e.name));
    await page.goto(origin+'/raster.html');
    const state = await page.locator('output').innerText();
    if (!errors.length && !blocked.length && state === 'WebGL2 unavailable') {
      return { status:'SKIPPED',reason:'The sandboxed Chromium context did not provide WebGL2' };
    }
    if (state !== 'Front') return { status:'FAIL',reason:'First mesh did not render',errors,blocked };
    const before = await page.evaluate(readPixels);
    await page.screenshot({path:join(out,'B11-raster-before.png')});
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    const after = await page.evaluate(readPixels);
    await page.screenshot({path:join(out,'B11-raster-turned.png')});
    const turned = await page.locator('output').innerText();
    await page.keyboard.press('Enter');
    const restored = await page.evaluate(readPixels);
    const pass = [before,after,restored].every(r => !r.error && r.colors >= 2 && r.covered > 250)
      && before.signature !== after.signature && before.signature === restored.signature
      && turned === 'Turned' && !errors.length && !blocked.length;
    return { status:pass ? 'PASS':'FAIL',before,after,restored,errors,blocked };
  } finally {
    await context.close();
  }
}
