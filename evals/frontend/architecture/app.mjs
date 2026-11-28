// A small shared state model for the B14 server-first dispatch queue.
export const orders = Object.freeze([
  { id: 'road', label: 'Access Road', delayed: false },
  { id: 'mill', label: 'Mill Lane', delayed: true },
  { id: 'dock', label: 'East Dock', delayed: false },
]);

export function rowsFor(delayedOnly) {
  return delayedOnly ? orders.filter(order => order.delayed) : orders;
}

export function attachFilter(root) {
  const toggle = root.querySelector('button');
  const rows = root.querySelectorAll('[data-order]');
  function toggleFilter() {
    const next = toggle.getAttribute('aria-pressed') !== 'true';
    const visible = new Set(rowsFor(next).map(order => order.id));
    for (const row of rows) row.hidden = !visible.has(row.dataset.order);
    toggle.setAttribute('aria-pressed', String(next));
  }
  toggle.addEventListener('click', toggleFilter);
  return () => toggle.removeEventListener('click', toggleFilter);
}
