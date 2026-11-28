import test from 'node:test';
import assert from 'node:assert/strict';
import { attachFilter, orders, rowsFor } from './app.mjs';
import { renderPage, streamPage } from './render.mjs';

test('static output and request variants preserve identity and the initial filter', async () => {
  const build = await renderPage();
  assert.equal(build, await renderPage({ delayedOnly: false }));
  assert.equal((build.match(/data-order=/g) || []).length, orders.length);
  const filtered = await renderPage({ delayedOnly: true });
  assert.match(filtered, /aria-pressed="true"/);
  for (const order of orders) {
    assert.ok(filtered.includes(`data-order="${order.id}"${order.delayed ? '' : ' hidden'}`));
  }
});

test('first content streams before the secondary read; a failure leaves the queue usable', async () => {
  let rejectRead;
  const freshness = new Promise((_, reject) => { rejectRead = reject; });
  const chunks = streamPage({ freshness });
  const shell = await chunks.next();
  assert.match(shell.value, /Mill Lane/);
  assert.doesNotMatch(shell.value, /Last update unavailable/);
  const end = chunks.next();
  rejectRead(new Error('fixture unavailable'));
  assert.match((await end).value, /Last update unavailable/);
  assert.equal((await chunks.next()).done, true);
});

test('client attachment preserves rendered state, toggles both ways and cleans up', () => {
  // EventTarget exercises the state contract without claiming browser coverage.
  const toggle = new EventTarget();
  let pressed = 'true';
  toggle.getAttribute = () => pressed;
  toggle.setAttribute = (_, value) => { pressed = value; };
  const rows = orders.map(o => ({ dataset: { order: o.id }, hidden: !o.delayed }));
  const dispose = attachFilter({ querySelector: () => toggle, querySelectorAll: () => rows });
  const visible = () => rows.filter(r => !r.hidden).map(r => r.dataset.order);
  const delayed = rowsFor(true).map(r => r.id);
  assert.deepEqual(visible(), delayed);
  toggle.dispatchEvent(new Event('click'));
  assert.equal(pressed, 'false');
  assert.deepEqual(visible(), orders.map(o => o.id));
  toggle.dispatchEvent(new Event('click'));
  assert.deepEqual(visible(), delayed);
  dispose();
  toggle.dispatchEvent(new Event('click'));
  assert.deepEqual(visible(), delayed);
});
