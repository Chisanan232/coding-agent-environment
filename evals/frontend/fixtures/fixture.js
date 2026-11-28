// Original, dependency-free motion probes. Each load runs exactly one scene.
'use strict';
const id = new URL(location.href).searchParams.get('case');
const scene = document.getElementById(id);
if (!scene || scene.tagName !== 'SECTION') throw new Error('Choose a fixture case B05–B12');
scene.hidden = false;
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const stage = scene.querySelector('.stage');
const output = scene.querySelector('output');
const button = scene.querySelector('button');
const marker = scene.querySelector('.marker');

function toggle(first, second, update) {
  let active = false;
  button.addEventListener('click', () => {
    active = !active;
    scene.classList.toggle('alternate', active);
    output.value = active ? second : first;
    update?.(active);
  });
}

function horizontalFraction(event, target) {
  const bounds = target.getBoundingClientRect();
  return Math.max(0, Math.min(1, (event.clientX - bounds.x) / bounds.width));
}

function scrollStudy() {
  const viewport = scene.querySelector('.scroll');
  const caption = scene.querySelector('.chapter');
  viewport.addEventListener('scroll', () => {
    const progress = viewport.scrollTop / (viewport.scrollHeight - viewport.clientHeight);
    const chapter = ['Shore', 'Ridge', 'Strata'][Math.min(2, Math.floor(progress * 3))];
    marker.style.transform = reduced ? 'none' : 'scale(' + (1 + progress * .35) + ')';
    caption.textContent = output.value = chapter;
  }, { passive: true });
}

function windStudy() {
  const ctx = scene.querySelector('canvas').getContext('2d');
  const animate = scene.querySelector('input');
  animate.checked = !reduced;
  animate.disabled = reduced;
  let center = .24, frame = 0;
  function draw(time) {
    const phase = reduced ? 0 : time / 1300;
    ctx.fillStyle = '#1f3531';
    ctx.fillRect(0, 0, 480, 260);
    ctx.lineWidth = 2;
    for (let y = 12; y < 260; y += 14) {
      for (let x = 12; x < 480; x += 16) {
        const dx = x - center * 480, dy = y - 135;
        const influence = Math.exp(-(dx * dx + dy * dy) / 12000);
        const angle = .22 * Math.sin(phase + y / 38) + 2.7 * influence;
        ctx.strokeStyle = influence > .65 ? '#d98b63' : '#e0d7ad';
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x + 11 * Math.cos(angle), y + 11 * Math.sin(angle));
        ctx.stroke();
      }
    }
    if (animate.checked) frame = requestAnimationFrame(draw);
  }
  function setCenter(fraction) {
    center = fraction;
    output.value = center > .5 ? 'East' : 'West';
    if (!animate.checked) draw(0);
  }
  animate.addEventListener('change', () => {
    cancelAnimationFrame(frame);
    if (animate.checked) draw(performance.now());
  });
  stage.addEventListener('pointermove', event => setCenter(horizontalFraction(event, stage)));
  button.addEventListener('click', () => setCenter(center > .5 ? .24 : .76));
  draw(0);
}

function springStudy() {
  // Solve the underdamped step analytically so position and velocity stay continuous on retarget.
  let target = 0, start = 0, displacement = 0, initialVelocity = 0, frame = 0;
  const decay = 6, frequency = 13;
  const rail = scene.querySelector('.rail');
  function sample(time) {
    const seconds = Math.max(0, (time - start) / 1000);
    const a = displacement, b = (initialVelocity + decay * a) / frequency;
    const sin = Math.sin(frequency * seconds), cos = Math.cos(frequency * seconds);
    const envelope = Math.exp(-decay * seconds);
    return [target + envelope * (a * cos + b * sin),
      envelope * ((frequency * b - decay * a) * cos - (frequency * a + decay * b) * sin)];
  }
  function draw(time) {
    const [position, velocity] = sample(time);
    const settled = Math.abs(position - target) < .001 && Math.abs(velocity) < .012;
    marker.style.transform = 'translateX(' + (settled ? target : position) * rail.clientWidth + 'px)';
    frame = settled ? 0 : requestAnimationFrame(draw);
  }
  function retarget(next) {
    const time = performance.now();
    const [position, velocity] = sample(time);
    start = time;
    target = next;
    displacement = reduced ? 0 : position - target;
    initialVelocity = reduced ? 0 : velocity;
    output.value = target > .5 ? 'Far' : 'Near';
    cancelAnimationFrame(frame);
    draw(time);
  }
  stage.addEventListener('pointerdown', event => retarget(horizontalFraction(event, rail)));
  button.addEventListener('click', () => retarget(target > .5 ? 0 : 1));
}

function blockStudy() {
  let angle = 25, press = null;
  function orient(next) {
    angle = next;
    marker.style.transform = 'rotateX(-18deg) rotateY(' + angle + 'deg)';
    const face = ((Math.round((angle - 25) / 90) % 4) + 4) % 4;
    output.value = 'Face ' + (face + 1);
  }
  stage.addEventListener('pointerdown', event => {
    press = { x: event.clientX, angle };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener('pointermove', event => {
    if (press) orient(press.angle + (event.clientX - press.x) / 2);
  });
  stage.addEventListener('lostpointercapture', () => { press = null; });
  button.addEventListener('click', () => orient(angle + 90));
}

function contourStudy() {
  const ctx = scene.querySelector('canvas').getContext('2d');
  let seed = 1;
  function draw() {
    let n = seed;
    function random() {
      n = (Math.imul(1664525, n) + 1013904223) >>> 0;
      return n / 4294967296;
    }
    const phase = random() * 6, offset = random() * 6, height = 18 + random() * 25;
    ctx.fillStyle = '#eee9d7';
    ctx.fillRect(0, 0, 480, 260);
    for (let row = 0; row < 26; row++) {
      ctx.beginPath();
      ctx.strokeStyle = row % 7 === 0 ? '#a54125' : '#31463f';
      ctx.lineWidth = row % 7 === 0 ? 3 : 1;
      for (let x = 0; x <= 480; x += 3) {
        const y = row * 11 + Math.sin(x / 73 + phase + row * .13) * height + Math.cos(x / 49 + offset) * 11;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    output.value = 'Seed ' + seed;
  }
  button.addEventListener('click', () => { seed = seed === 1 ? 2 : 1; draw(); });
  draw();
}

switch (id) {
  case 'B05': toggle('Off', 'On', value => button.setAttribute('aria-pressed', String(value))); break;
  case 'B06': toggle('Full print', 'Detail proof'); break;
  case 'B07': scrollStudy(); break;
  case 'B08': windStudy(); break;
  case 'B09': springStudy(); break;
  case 'B10': toggle('Horizon', 'Ridge', value => { scene.querySelector('.caption').textContent = value ? 'Ridge' : 'Horizon'; }); break;
  case 'B11': blockStudy(); break;
  case 'B12': contourStudy(); break;
}
