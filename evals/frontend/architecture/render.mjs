import { orders, rowsFor } from './app.mjs';

const head = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dispatch desk</title><body>';
const tail = '</main></body></html>';

export function queueMarkup(delayedOnly = false) {
  const visible = new Set(rowsFor(delayedOnly).map(row => row.id));
  const rows = orders.map(row =>
    `<li data-order="${row.id}"${visible.has(row.id) ? '' : ' hidden'}>${row.label} · ${row.delayed ? 'Delayed' : 'On time'}</li>`
  );
  return `<section id="queue" aria-label="Dispatch queue"><button type="button" aria-pressed="${delayedOnly}">Delayed only</button><ul>${rows.join('')}</ul></section>`;
}

// Yield the useful queue before the independent freshness read has settled.
export async function* streamPage({ delayedOnly = false, freshness = Promise.resolve(true) } = {}) {
  yield head + '<main><h1>Dispatch desk</h1>' + queueMarkup(delayedOnly);
  let fresh;
  try {
    fresh = await freshness;
  } catch {
    fresh = false;
  }
  yield `<small>${fresh ? 'Queue updated' : 'Last update unavailable'}</small>` + tail;
}

// Build-time and request-time callers can consume the same representation.
export async function renderPage(options) {
  const chunks = [];
  for await (const chunk of streamPage(options)) chunks.push(chunk);
  return chunks.join('');
}
