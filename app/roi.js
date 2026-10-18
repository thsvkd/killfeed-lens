'use strict';
// ROI editor: boxes behave like gui.RoiBox (edges resize, inside drags, Enter confirms, Esc
// cancels). Rects stay normalized to this window, which covers the whole game monitor.
(() => {
  const GRIP = 8; // px band at the edge that resizes (gui.GRIP)
  const MIN = 24; // smallest box side (gui.ROI_MIN)
  const CURSOR = { n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize', nw: 'nwse-resize',
                   se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', '': 'move' };
  const $ = (id) => document.getElementById(id);
  const boxes = { killfeed: $('killfeed'), alive: $('alive') };
  let rects = null; // key -> [x, y, w, h], normalized
  let scale = 1; // physical px per CSS px, for the size readout
  // The monitor's size, not innerWidth/innerHeight: Windows may keep the window off the taskbar,
  // and ROIs are normalized to the whole captured monitor.
  let SW = innerWidth;
  let SH = innerHeight;
  let active = 'killfeed';
  let drag = null;
  let sent = false;

  const px = ([x, y, w, h]) => [x * SW, y * SH, w * SW, h * SH];
  function norm([x, y, w, h]) {
    const r = (v) => Math.round(v * 1e6) / 1e6;
    const nx = r(x / SW);
    const ny = r(y / SH);
    return [nx, ny, Math.min(r(w / SW), 1 - nx), Math.min(r(h / SH), 1 - ny)];
  }

  /** gui.edge_at: which edge/corner ("n", "se", ...) a point in a w x h box is on; "" = inside. */
  const edgeAt = (x, y, w, h) =>
    (y < GRIP ? 'n' : y >= h - GRIP ? 's' : '') + (x < GRIP ? 'w' : x >= w - GRIP ? 'e' : '');

  /** gui.drag_box: the box after dragging `edge` by (dx, dy) ("" moves it), kept on screen. */
  function dragBox([x, y, w, h], edge, dx, dy) {
    if (!edge) return [Math.min(Math.max(0, x + dx), SW - w), Math.min(Math.max(0, y + dy), SH - h), w, h];
    let [l, t, r, b] = [x, y, x + w, y + h];
    if (edge.includes('w')) l = Math.min(Math.max(0, l + dx), r - MIN);
    if (edge.includes('e')) r = Math.max(Math.min(SW, r + dx), l + MIN);
    if (edge.includes('n')) t = Math.min(Math.max(0, t + dy), b - MIN);
    if (edge.includes('s')) b = Math.max(Math.min(SH, b + dy), t + MIN);
    return [l, t, r - l, b - t];
  }

  function draw(key) {
    const el = boxes[key];
    const [x, y, w, h] = px(rects[key]);
    Object.assign(el.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
    el.classList.toggle('tag-in', y < 28);
    el.querySelector('.size').textContent = `${Math.round(w * scale)}×${Math.round(h * scale)}`;
  }

  function activate(key) {
    active = key;
    for (const [k, el] of Object.entries(boxes)) el.classList.toggle('active', k === key);
  }

  const useAlive = () => $('use-alive').checked;

  function finish(channel, data) {
    if (sent) return;
    sent = true;
    kfl.send(channel, data);
  }
  const confirm = () => finish('roi:done', { killfeed: rects.killfeed, alive: useAlive() ? rects.alive : null });
  const cancel = () => finish('roi:cancel');

  for (const [key, el] of Object.entries(boxes)) {
    const edgeOf = (e) => {
      const r = el.getBoundingClientRect();
      return edgeAt(e.clientX - r.left, e.clientY - r.top, r.width, r.height);
    };
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      activate(key);
      drag = { key, edge: edgeOf(e), x: e.clientX, y: e.clientY, box: px(rects[key]) };
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener('pointermove', (e) => {
      if (!drag) { el.style.cursor = CURSOR[edgeOf(e)]; return; }
      rects[key] = norm(dragBox(drag.box, drag.edge, e.clientX - drag.x, e.clientY - drag.y));
      draw(key);
    });
    el.addEventListener('pointerup', () => { drag = null; });
  }

  addEventListener('keydown', (e) => {
    if (!rects) return;
    if (e.key === 'Escape') return cancel();
    if (e.key === 'Enter') {
      if (!e.target.closest?.('button, input')) confirm(); // a focused button handles its own Enter
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      if (useAlive()) activate(active === 'killfeed' ? 'alive' : 'killfeed');
      return;
    }
    const step = e.shiftKey ? 10 : 1;
    const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!d) return;
    e.preventDefault();
    rects[active] = norm(dragBox(px(rects[active]), '', ...d));
    draw(active);
  });

  $('ok').addEventListener('click', confirm);
  $('cancel').addEventListener('click', cancel);
  $('use-alive').addEventListener('change', () => {
    boxes.alive.hidden = !useAlive();
    activate(useAlive() ? 'alive' : 'killfeed');
  });

  kfl.on('roi:init', (d) => {
    rects = { killfeed: d.killfeed, alive: d.alive };
    scale = d.scale;
    [SW, SH] = d.size;
    $('use-alive').checked = d.useAlive;
    boxes.alive.hidden = !d.useAlive;
    $('policy').hidden = !d.guide;
    $('warn').hidden = !d.monitor;
    $('demo').hidden = !d.demo;
    draw('killfeed');
    draw('alive');
    activate('killfeed');
    document.body.classList.add('ready');
  });
})();
