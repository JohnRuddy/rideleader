// Route editor: drag the line to reroute, click to add a via point, tap a via point to remove it, undo / redo.
(function () {
  const bar = document.getElementById('editbar');
  const base = bar.dataset.base;                       // /api/routes/<id>
  const toggle = document.getElementById('edit-toggle');
  const undoBtn = document.getElementById('undo'), redoBtn = document.getElementById('redo');
  const msg = document.getElementById('edit-msg'), help = document.getElementById('edit-help');
  const optBusy = document.getElementById('opt-busy'), optUnpaved = document.getElementById('opt-unpaved');
  const tools = document.getElementById('edit-tools');
  let editing = false, busy = false, grab = null, suppressClick = false, hist = null;
  const viaMarkers = new Map();

  const api = async (url, method, body) => {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const say = (t, cls = '') => { msg.textContent = t; msg.className = 'small ' + (cls || 'muted'); };
  const km = m => (m / 1000).toFixed(1);

  function setHistory(h) {
    hist = h;
    undoBtn.disabled = busy || !h.can_undo;
    redoBtn.disabled = busy || !h.can_redo;
    undoBtn.title = h.undo_label ? `Undo: ${h.undo_label}` : 'Nothing to undo';
    redoBtn.title = h.redo_label ? `Redo: ${h.redo_label}` : 'Nothing to redo';
  }
  function setBusy(on, text) { busy = on; if (hist) setHistory(hist); toggle.disabled = on; if (text) say(text); }

  function updateStats(d) {
    document.getElementById('stat-dist').textContent = `${km(d.length_m)} km`;
    document.getElementById('stat-up').textContent = d.ascent_m == null ? 'n/a' : `${Math.round(d.ascent_m)} m`;
    document.getElementById('stat-down').textContent = d.descent_m == null ? 'n/a' : `${Math.round(d.descent_m)} m`;
  }

  // ---- via point markers ----
  function showAnchors(anchors) {
    const vias = anchors.filter(a => a.kind === 'via');
    for (const [id, m] of viaMarkers) if (!vias.find(a => a.id === id)) { m.remove(); viaMarkers.delete(id); }
    vias.forEach((a, i) => {
      let m = viaMarkers.get(a.id);
      if (!m) {
        const node = document.createElement('button');
        node.type = 'button'; node.className = 'via-marker';
        m = new maplibregl.Marker({ element: node, draggable: true }).setLngLat([a.lon, a.lat]).addTo(RLK.map);
        m.on('dragstart', () => { suppressClick = true; });
        m.on('dragend', () => { const p = m.getLngLat(); setTimeout(() => (suppressClick = false), 80); doEdit({ op: 'move_via', anchor_id: a.id, lat: p.lat, lon: p.lng }); });
        node.addEventListener('click', ev => {
          ev.stopPropagation();
          if (suppressClick) return;
          const box = document.createElement('div');
          box.append(Object.assign(document.createElement('strong'), { textContent: `Via point ${node.textContent}` }), document.createElement('br'));
          const rm = Object.assign(document.createElement('button'), { type: 'button', className: 'btn small', textContent: 'Remove via point',
            onclick: () => { popup.remove(); doEdit({ op: 'delete_via', anchor_id: a.id }); } });
          box.append(rm);
          const popup = new maplibregl.Popup({ offset: 14 }).setLngLat(m.getLngLat()).setDOMContent(box).addTo(RLK.map);
        });
        viaMarkers.set(a.id, m);
      }
      m.setLngLat([a.lon, a.lat]);
      m.getElement().textContent = String(i + 1);
      m.getElement().title = `Via point ${i + 1}: drag to move, tap to remove`;
      m.getElement().hidden = !editing;
    });
  }

  // ---- doing an edit ----
  async function afterChange(d) {
    updateStats(d); showAnchors(d.anchors); setHistory(d.history);
    RLK.setRoute(await (await fetch(document.getElementById('map').dataset.routeUrl)).json());
    document.dispatchEvent(new Event('rlk:route-changed'));
    document.dispatchEvent(new Event('rlk:waypoints-changed'));
  }
  async function pollScan(id) {
    say('Updating stops along the new line…');
    for (let i = 0; i < 120; i++) {
      await new Promise(r => setTimeout(r, 1500));
      const s = await api(`/api/scans/${id}`, 'GET');
      if (s.status === 'done') { document.dispatchEvent(new Event('rlk:waypoints-changed')); say('Stops updated along the new line.'); return; }
      if (s.status === 'failed') { say(`Couldn't update stops: ${s.message}`, 'warn'); return; }
    }
  }
  async function doEdit(body) {
    if (busy) return;
    setBusy(true, 'Finding a cycling route…');
    try {
      const was = RLK.route.length_m;
      const d = await api(`${base}/edit`, 'POST', body);
      await afterChange(d);
      const diff = d.length_m - was;
      say(d.warnings && d.warnings.length ? d.warnings.join(' ')
        : `Route updated: ${diff >= 0 ? '+' : '−'}${km(Math.abs(diff))} km, now ${km(d.length_m)} km.`);
      if (d.scan_job_id) pollScan(d.scan_job_id);
    } catch (e) { say(e.message, 'warn'); await reload(); }
    setBusy(false);
  }
  async function reload() {      // put markers and numbers back to what the server has
    try { const d = await api(`${base}/editor`, 'GET'); showAnchors(d.anchors); setHistory(d.history); updateStats(d); } catch (e) { /* leave as is */ }
  }
  async function step(which) {
    if (busy) return;
    setBusy(true, which === 'undo' ? 'Undoing…' : 'Redoing…');
    try {
      const d = await api(`${base}/history/${which}`, 'POST');
      await afterChange(d);
      say(which === 'undo' ? 'Undone.' : 'Redone.');
    } catch (e) { say(e.message, 'warn'); }
    setBusy(false);
  }
  undoBtn.addEventListener('click', () => step('undo'));
  redoBtn.addEventListener('click', () => step('redo'));
  document.addEventListener('keydown', e => {
    if (e.target.closest('input, textarea, select, [contenteditable]')) return;
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    if (e.key.toLowerCase() === 'z') { e.preventDefault(); step(e.shiftKey ? 'redo' : 'undo'); }
    else if (e.key.toLowerCase() === 'y') { e.preventDefault(); step('redo'); }
  });
  // other parts of the page changed something undoable (a waypoint, a climb choice)
  document.addEventListener('rlk:history-changed', async () => { try { setHistory(await api(`${base}/history`, 'GET')); } catch (e) { /* ignore */ } });

  // ---- grabbing the line ----
  function nearestS(lngLat) {
    const r = RLK.route, cs = r.coords, k = Math.cos(lngLat.lat * Math.PI / 180);
    let best = 0, bd = Infinity;
    for (let i = 0; i < cs.length; i++) {
      const dx = (cs[i][0] - lngLat.lng) * k, dy = cs[i][1] - lngLat.lat, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = i; }
    }
    return r.dist_m[best];
  }
  function startGrab(e) {
    if (!editing || busy) return;
    e.preventDefault();                                    // stop the map from panning while we drag the line
    const node = document.createElement('div'); node.className = 'ghost-marker';
    grab = { s: nearestS(e.lngLat), start: e.point, last: e.lngLat, ghost: new maplibregl.Marker({ element: node }).setLngLat(e.lngLat).addTo(RLK.map) };
  }
  function moveGrab(e) { if (grab) { grab.last = e.lngLat; grab.ghost.setLngLat(e.lngLat); } }
  function endGrab(e) {
    if (!grab) return;
    const g = grab; grab = null; g.ghost.remove();
    const p = e.point || g.start;
    if (Math.hypot(p.x - g.start.x, p.y - g.start.y) < 10) return;     // a tap, not a drag
    suppressClick = true; setTimeout(() => (suppressClick = false), 80);
    doEdit({ op: 'add_via', s_m: g.s, lat: g.last.lat, lon: g.last.lng });
  }

  RLK.ready.then(() => {
    const map = RLK.map;
    map.on('mousedown', 'route-hit', startGrab);
    map.on('touchstart', 'route-hit', startGrab);
    map.on('mousemove', moveGrab); map.on('touchmove', moveGrab);
    map.on('mouseup', endGrab); map.on('touchend', endGrab);
    map.on('mouseenter', 'route-hit', () => { if (editing) map.getCanvas().style.cursor = 'grab'; });
    map.on('mouseleave', 'route-hit', () => { if (editing) map.getCanvas().style.cursor = ''; });
    map.on('click', e => {
      if (!editing || busy || suppressClick) return;
      if (map.queryRenderedFeatures(e.point, { layers: ['wps'] }).length) return;     // that tap was on a stop
      doEdit({ op: 'add_via', lat: e.lngLat.lat, lon: e.lngLat.lng });
    });
  });

  toggle.addEventListener('click', () => {
    editing = !editing;
    toggle.textContent = editing ? 'Done editing' : 'Edit route';
    toggle.setAttribute('aria-pressed', String(editing));
    tools.hidden = help.hidden = !editing;
    document.body.classList.toggle('editing', editing);
    viaMarkers.forEach(m => (m.getElement().hidden = !editing));
    say('');
  });
  [optBusy, optUnpaved].forEach(box => box.addEventListener('change', async () => {
    try { await api(`${base}/editor/options`, 'PUT', { avoid_busy: optBusy.checked, allow_unpaved: optUnpaved.checked }); say('Routing preference saved for new edits.'); }
    catch (e) { say(e.message, 'warn'); }
  }));

  (async function init() {
    try {
      const d = await api(`${base}/editor`, 'GET');
      optBusy.checked = d.options.avoid_busy; optUnpaved.checked = d.options.allow_unpaved;
      setHistory(d.history);
      RLK.ready.then(() => showAnchors(d.anchors));
    } catch (e) { say(e.message, 'warn'); }
  })();
})();
