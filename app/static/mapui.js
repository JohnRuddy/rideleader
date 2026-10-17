// Map furniture on the route page: a toolbar (Add cue, Key), the right-click / long-press cue menu, a first-use hint and a toast.
(function () {
  const mapEl = document.getElementById('map');
  const panel = document.getElementById('panel');
  if (!mapEl || !panel || !window.RLK) return;
  const routeId = panel.dataset.routeId;
  const cats = JSON.parse(panel.dataset.categories);

  const ICON = {
    cue_left: 'M19 12H5M11 6l-6 6 6 6', cue_right: 'M5 12h14M13 6l6 6-6 6', cue_straight: 'M12 19V5M6 11l6-6 6 6',
    hazard: 'M12 3l10 18H2zM12 10v5M12 18v.5', water: 'M12 3c4 5 6 8 6 11a6 6 0 0 1-12 0c0-3 2-6 6-11z',
    cafe: 'M4 8h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5zM16 10h2a2 2 0 0 1 0 4h-2M7 3v2M11 3v2',
    aed: 'M9 3h6v6h6v6h-6v6H9v-6H3V9h6z', summit: 'M3 20l7-13 3 5 3-4 5 12z', valley: 'M3 4h18l-6 9-3-4-3 4-3-5zM3 20h18',
    sprint: 'M5 21V4M5 4h12l-2 4 2 4H5', toilets: 'M7 4v16M17 4v16M7 12h10', generic: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  };
  // what the menu offers, in the order Strava lists its own cue types; label is what the leader reads
  const TYPES = [
    ['cue_left', 'Left'], ['cue_right', 'Right'], ['cue_straight', 'Straight'], ['hazard', 'Danger'],
    ['water', 'Water'], ['cafe', 'Food'], ['aed', 'First aid'], ['toilets', 'Toilets'],
    ['summit', 'Summit'], ['valley', 'Valley'], ['sprint', 'Sprint'], ['generic', 'Generic'],
  ];
  const colorOf = k => (cats.find(c => c.key === k) || { color: '#636366' }).color;
  const svg = (k, size = 22) => k === 'toilets'
    ? `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true"><text x="12" y="16" text-anchor="middle" font-size="11" font-weight="700" font-family="Segoe UI, sans-serif" fill="currentColor">WC</text></svg>`
    : `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON[k] || ICON.generic}"/></svg>`;
  const el = (tag, attrs = {}, ...kids) => { const n = Object.assign(document.createElement(tag), attrs); kids.forEach(x => n.append(x)); return n; };

  // ---- toast ----
  const toast = el('div', { className: 'map-toast', role: 'status' });
  toast.setAttribute('aria-live', 'polite');
  mapEl.append(toast);
  let toastTimer;
  function say(text, bad) {
    toast.textContent = text; toast.classList.toggle('bad', !!bad); toast.classList.add('on');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('on'), bad ? 6000 : 3500);
  }

  // ---- where on the route is this point? (display only; the server snaps for real) ----
  function locate(lng, lat) {
    const r = RLK.route; if (!r) return null;
    const k = Math.cos(lat * Math.PI / 180), P = Math.PI / 180 * 6371000;
    let best = Infinity, bi = 0;
    for (let i = 0; i < r.coords.length; i++) {
      const dx = (r.coords[i][0] - lng) * k, dy = r.coords[i][1] - lat, d = dx * dx + dy * dy;
      if (d < best) { best = d; bi = i; }
    }
    return { km: r.dist_m[bi] / 1000, off: Math.sqrt(best) * P, s: r.dist_m[bi] };
  }

  // ---- the cue menu ----
  let menu = null;
  function close() { if (menu) { menu.remove(); menu = null; } }
  function open(lngLat, point) {
    close();
    const where = locate(lngLat.lng, lngLat.lat);
    const tooFar = !where || where.off > 3000;
    menu = el('div', { className: 'cue-menu' });
    menu.setAttribute('role', 'dialog'); menu.setAttribute('aria-label', 'Add a cue');
    const head = el('div', { className: 'cue-head' }, el('strong', { textContent: 'Add a cue here' }),
      el('span', { className: 'muted small', textContent: tooFar ? 'Too far from the route. Right-click closer to the line.'
        : `km ${where.km.toFixed(1)} · ${where.off < 15 ? 'on the route' : Math.round(where.off) + ' m from the route'}` }));
    menu.append(head);
    const body = el('div', { className: 'cue-body' });
    menu.append(body);

    const grid = el('div', { className: 'cue-grid', role: 'group' });
    TYPES.forEach(([kind, label]) => {
      const b = el('button', { type: 'button', className: 'cue-type', disabled: tooFar, title: label });
      b.setAttribute('aria-label', label);
      b.innerHTML = svg(kind); b.style.setProperty('--c', colorOf(kind));
      b.append(el('span', { textContent: label }));
      b.addEventListener('click', () => form(kind, label));
      grid.append(b);
    });
    body.append(grid);
    body.append(el('p', { className: 'muted small cue-foot', textContent: 'Cues are ticked for export and pinned. Rename, unpin or delete them from the stops list.' }));

    function form(kind, label) {
      body.replaceChildren();
      const name = el('input', { type: 'text', maxLength: 80, value: label, ariaLabel: 'Cue name' });
      const note = el('textarea', { rows: 2, maxLength: 500, placeholder: 'Note for the group (optional)' });
      const err = el('p', { className: 'error small', hidden: true });
      const add = el('button', { type: 'submit', className: 'btn small', textContent: 'Add cue' });
      const back = el('button', { type: 'button', className: 'chip', textContent: 'Back', onclick: () => open(lngLat, point) });
      const f = el('form', { className: 'cue-form' },
        el('div', { className: 'cue-picked' }, Object.assign(el('span', { className: 'cue-badge' }), { innerHTML: svg(kind, 18) }), el('span', { textContent: label })),
        el('label', {}, 'Name', name), el('label', {}, 'Note', note), err, el('div', { className: 'cue-actions' }, add, back));
      f.querySelector('.cue-badge').style.background = colorOf(kind);
      f.addEventListener('submit', async ev => {
        ev.preventDefault(); add.disabled = true; err.hidden = true;
        try {
          const r = await fetch(`/api/routes/${routeId}/waypoints`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind, name: name.value, note: note.value, lat: lngLat.lat, lon: lngLat.lng, s_hint: where && where.s }) });
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(j.error || (r.status === 404 || r.status === 403 ? 'Only the route owner can add cues.' : 'Could not add that cue.'));
          close(); say(`Added “${j.name}” at km ${(j.s_m / 1000).toFixed(1)}.`);
          document.dispatchEvent(new CustomEvent('rlk:wps-changed'));
        } catch (e) { err.textContent = e.message; err.hidden = false; add.disabled = false; }
      });
      body.append(f); name.focus(); name.select();
    }
    mapEl.append(menu);
    const w = mapEl.clientWidth, h = mapEl.clientHeight, mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(point.x + 6, w - mw - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(point.y + 6, h - mh - 8)) + 'px';
    const first = menu.querySelector('button:not([disabled])'); if (first) first.focus({ preventScroll: true });
  }

  const map = RLK.map;
  // Listen for the browser's own event rather than MapLibre's copy of it: it is the one that also stops the browser's menu appearing.
  const surface = map.getCanvasContainer();
  surface.addEventListener('contextmenu', e => {
    e.preventDefault();
    const r = surface.getBoundingClientRect(), pt = { x: e.clientX - r.left, y: e.clientY - r.top };
    open(map.unproject([pt.x, pt.y]), pt);
  });
  let press;
  const cancelPress = () => clearTimeout(press);
  map.on('touchstart', e => {
    if (e.originalEvent.touches && e.originalEvent.touches.length > 1) return;
    press = setTimeout(() => open(e.lngLat, e.point), 600);
  });
  ['touchend', 'touchcancel', 'touchmove', 'dragstart', 'zoomstart'].forEach(t => map.on(t, cancelPress));
  map.on('click', close); map.on('movestart', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  document.addEventListener('pointerdown', e => { if (menu && !menu.contains(e.target) && !mapEl.contains(e.target)) close(); });

  // ---- toolbar and key ----
  const bar = el('div', { className: 'map-bar' });
  const addBtn = el('button', { type: 'button', className: 'map-btn', textContent: 'Add cue',
    title: 'Add a cue at the middle of the map (or right-click anywhere)' });
  addBtn.onclick = () => { const c = map.getCenter(); open(c, map.project(c)); };
  const keyBtn = el('button', { type: 'button', className: 'map-btn', textContent: 'Key' });
  keyBtn.setAttribute('aria-expanded', 'false');
  bar.append(addBtn, keyBtn);
  mapEl.append(bar);

  const key = el('div', { className: 'map-key', hidden: true });
  key.append(el('strong', { textContent: 'Map key' }));
  const stops = el('ul', { className: 'key-list' });
  cats.filter(c => c.key !== 'regroup').forEach(c => stops.append(el('li', {}, Object.assign(el('i'), { style: `background:${c.color}` }), c.label)));
  stops.append(el('li', {}, Object.assign(el('i', { className: 'rg' }), { textContent: 'R' }), 'Regroup'));
  key.append(el('span', { className: 'muted small', textContent: 'Stops. Solid and large means it goes on your device; faded means it is only a candidate.' }), stops);
  const lines = el('ul', { className: 'key-list' });
  [['#d70015', 'Closed or closure reported'], ['#ff3b30', 'Headwind'], ['#ff9f0a', 'Crosswind'], ['#34c759', 'Tailwind'], ['#ffcc00', 'Met Éireann warning area']]
    .forEach(([c, t]) => lines.append(el('li', {}, Object.assign(el('i', { className: 'ln' }), { style: `background:${c}` }), t)));
  key.append(el('span', { className: 'muted small', textContent: 'Lines on the route (when the forecast or a warning applies).' }), lines);
  key.append(el('a', { href: '#help', className: 'small', textContent: 'More in Help' }));
  mapEl.append(key);
  keyBtn.onclick = () => { key.hidden = !key.hidden; keyBtn.setAttribute('aria-expanded', String(!key.hidden)); };

  // ---- first-use hint ----
  let seen = false;
  try { seen = localStorage.getItem('rlk-cue-hint') === '1'; } catch (e) { /* ignore */ }
  if (!seen) {
    const hint = el('div', { className: 'map-hint' }, el('span', { textContent: window.matchMedia && matchMedia('(pointer: coarse)').matches ? 'Press and hold the map to add a cue.' : 'Right-click the map to add a cue: a turn, hazard, water or food stop.' }),
      Object.assign(el('button', { type: 'button', textContent: '×', className: 'map-hint-x' }), { ariaLabel: 'Dismiss' }));
    hint.querySelector('button').onclick = () => { hint.remove(); try { localStorage.setItem('rlk-cue-hint', '1'); } catch (e) { /* ignore */ } };
    mapEl.append(hint);
  }
})();
