// Map + elevation profile, linked. Exposes window.RLK so the panels and the editor can update both.
(function () {
  const mapEl = document.getElementById('map');
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue('--route').trim() || css.getPropertyValue('--accent').trim() || '#0071e3';
  const RLK = window.RLK = { route: null, wps: [], onToggle: null };
  let resolveReady; RLK.ready = new Promise(r => (resolveReady = r));

  const map = new maplibregl.Map({
    container: 'map',
    style: { version: 8,
      sources: { osm: { type: 'raster', tiles: [mapEl.dataset.tileUrl], tileSize: 256,
                        attribution: '© OpenStreetMap contributors' } },
      layers: [{ id: 'osm', type: 'raster', source: 'osm' }] },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
  // 'load' fires once. Waiting for it only after the route has been fetched misses it on a quick refresh, so listen from the start.
  const mapReady = new Promise(resolve => map.on('load', resolve));
  // The box changes size after first paint (late-loading panels, scrollbars, fonts); MapLibre only listens to window resizes.
  if (window.ResizeObserver) new ResizeObserver(() => map.resize()).observe(mapEl);
  const cursor = new maplibregl.Marker({ color: '#1d1d1f', scale: 0.8 });
  RLK.map = map;

  const lineFeature = r => ({ type: 'Feature', geometry: { type: 'LineString', coordinates: r.coords } });

  function wpGeoJSON() {
    return { type: 'FeatureCollection', features: RLK.wps.map(w => ({
      type: 'Feature', geometry: { type: 'Point', coordinates: [w.lon, w.lat] },
      properties: { id: w.id, kind: w.kind, name: w.name, km: (w.s_m / 1000).toFixed(1), included: w.included ? 1 : 0, color: w.color } })) };
  }
  // Regroups are DOM markers so the leader can drag them; everything else is a dot on the map layer.
  const regroupMarkers = new Map();
  function syncRegroupMarkers() {
    const want = new Map(RLK.wps.filter(w => w.kind === 'regroup').map(w => [w.id, w]));
    for (const [id, m] of regroupMarkers) if (!want.has(id)) { m.remove(); regroupMarkers.delete(id); }
    for (const [id, w] of want) {
      let m = regroupMarkers.get(id);
      if (!m) {
        const node = document.createElement('div');
        node.className = 'rg-marker';
        node.textContent = 'R';
        m = new maplibregl.Marker({ element: node, draggable: true }).setLngLat([w.lon, w.lat]).addTo(map);
        m.on('dragend', () => { const p = m.getLngLat(); RLK.onMove && RLK.onMove(id, p.lng, p.lat); });
        regroupMarkers.set(id, m);
      }
      m.setLngLat([w.lon, w.lat]);
      m.getElement().classList.toggle('off', !w.included);
      m.getElement().title = `${w.name} (km ${(w.s_m / 1000).toFixed(1)}). Drag to move.`;
    }
  }
  RLK.setWaypoints = function (list) {
    RLK.wps = list;
    if (map.getSource('wps')) map.getSource('wps').setData(wpGeoJSON());
    syncRegroupMarkers();
    if (RLK.drawProfile) RLK.drawProfile();
  };

  // Climbs: shaded bands on the profile and a coloured stretch of the route on the map.
  RLK.climbs = [];
  RLK.setClimbs = function (list) {
    RLK.climbs = list;
    if (RLK.route && map.getSource('climbs')) map.getSource('climbs').setData(climbGeoJSON());
    if (RLK.drawProfile) RLK.drawProfile();
  };
  function climbGeoJSON() {
    const r = RLK.route, D = r.dist_m;
    return { type: 'FeatureCollection', features: RLK.climbs.map(c => {
      const i0 = D.findIndex(d => d >= c.s_start_m), i1 = D.findIndex(d => d >= c.s_end_m);
      return { type: 'Feature', properties: { id: c.id },
               geometry: { type: 'LineString', coordinates: r.coords.slice(Math.max(i0, 0), (i1 < 0 ? D.length - 1 : i1) + 1) } };
    }) };
  }

  // Overlays: wind-coloured stretches, warning sections and closures, drawn on the map and as bands on the profile.
  function sliceLine(s0, s1) {
    const r = RLK.route, D = r.dist_m;
    const i0 = Math.max(D.findIndex(d => d >= s0), 0), j = D.findIndex(d => d >= s1), i1 = j < 0 ? D.length - 1 : j;
    return r.coords.slice(i0, i1 + 1);
  }
  const WIND_COLOR = { head: '#ff3b30', cross: '#ff9f0a', tail: '#34c759' };
  const LEVEL_COLOR = { yellow: '#ffcc00', orange: '#ff9500', red: '#ff3b30' };
  RLK.strip = []; RLK.warnBands = []; RLK.windOn = true; RLK.windSegs = []; RLK.warnSecs = []; RLK.closureHits = [];
  function setOverlay(id, features) {
    if (map.getSource(id)) map.getSource(id).setData({ type: 'FeatureCollection', features });
  }
  function paintOverlays() {
    if (!RLK.route) return;
    setOverlay('wind', RLK.windOn ? RLK.windSegs.map(s => ({ type: 'Feature', properties: { color: WIND_COLOR[s.class] },
      geometry: { type: 'LineString', coordinates: sliceLine(s.s0, s.s1) } })) : []);
    setOverlay('warnsec', RLK.warnSecs.map(s => ({ type: 'Feature', properties: { color: LEVEL_COLOR[s.level] },
      geometry: { type: 'LineString', coordinates: sliceLine(s.s0, s.s1) } })));
    setOverlay('closures', RLK.closureHits.map(h => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: sliceLine(h.s_start, h.s_end) } })));
    RLK.strip = RLK.windOn ? RLK.windSegs.map(s => ({ s0: s.s0, s1: s.s1, color: WIND_COLOR[s.class] })) : [];
    RLK.warnBands = RLK.warnSecs.map(s => ({ s0: s.s0, s1: s.s1, color: LEVEL_COLOR[s.level] }));
    if (RLK.drawProfile) RLK.drawProfile();
  }
  RLK.setWind = segs => { RLK.windSegs = segs || []; paintOverlays(); };
  RLK.showWind = on => { RLK.windOn = on; paintOverlays(); };
  RLK.setWarnSections = warnings => {
    RLK.warnSecs = [];
    (warnings || []).forEach(w => (w.sections || []).forEach(([a, b]) => RLK.warnSecs.push({ s0: a, s1: b, level: w.level })));
    paintOverlays();
  };
  RLK.setClosures = hits => { RLK.closureHits = hits || []; paintOverlays(); };

  // ---- profile ----
  const canvas = document.getElementById('profile');
  const readout = document.getElementById('profile-readout');
  const pad = { l: 44, r: 8, t: 8, b: 34 };
  let D = [], E = null, total = 0, minE = 0, maxE = 0, hoverIdx = null;

  function loadArrays() {
    const r = RLK.route;
    D = r.dist_m; E = r.ele_m; total = r.length_m; hoverIdx = null;
    if (E) { minE = Math.min(...E); maxE = Math.max(...E); }
    canvas.parentElement.hidden = !E;
  }
  function nearest(d) {
    let lo = 0, hi = D.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; D[m] < d ? lo = m + 1 : hi = m; }
    return lo > 0 && d - D[lo - 1] < D[lo] - d ? lo - 1 : lo;
  }
  function grad(i) {
    let a = i, b = i;
    while (a > 0 && D[i] - D[a] < 50) a--;
    while (b < D.length - 1 && D[b] - D[i] < 50) b++;
    return D[b] === D[a] ? 0 : ((E[b] - E[a]) / (D[b] - D[a])) * 100;
  }
  // Same bands as the key under the profile.
  function gradeColor(g) {
    return g <= -6 ? '#3f7fb0' : g <= -2 ? '#8ec1da' : g < 2 ? '#a8cfa0' : g < 4 ? '#e6d35f'
         : g < 7 ? '#f0a23c' : g < 10 ? '#e0572d' : '#9c2127';
  }
  RLK.drawProfile = function draw() {
    if (!E) return;
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const c = canvas.getContext('2d'); c.scale(dpr, dpr);
    const x = d => pad.l + (d / total) * (w - pad.l - pad.r);
    const span = Math.max(maxE - minE, 20), lo = minE - span * 0.05;
    const y = e => h - pad.b - ((e - lo) / (span * 1.1)) * (h - pad.t - pad.b);
    const fg = css.getPropertyValue('--muted').trim();
    c.font = '11px sans-serif'; c.fillStyle = fg; c.strokeStyle = css.getPropertyValue('--line').trim();
    c.textAlign = 'right';
    for (let i = 0; i <= 3; i++) {
      const e = lo + (span * 1.1 * i) / 3, yy = y(e);
      c.beginPath(); c.moveTo(pad.l, yy); c.lineTo(w - pad.r, yy); c.stroke();
      c.fillText(Math.round(e) + ' m', pad.l - 6, yy + 4);
    }
    c.textAlign = 'center';
    const step = total > 60000 ? 10000 : total > 20000 ? 5000 : 2000;
    for (let d = 0; d <= total; d += step) c.fillText((d / 1000) + ' km', x(d), h - 5);
    (RLK.warnBands || []).forEach(b => { c.fillStyle = b.color + '55'; c.fillRect(x(b.s0), pad.t, Math.max(2, x(b.s1) - x(b.s0)), 7); });
    (RLK.strip || []).forEach(b => { c.fillStyle = b.color; c.fillRect(x(b.s0), h - pad.b + 3, Math.max(1, x(b.s1) - x(b.s0)), 8); });   // wind under the axis
    (RLK.climbs || []).forEach(cl => { c.fillStyle = '#ff3b3033'; c.fillRect(x(cl.s_start_m), pad.t, Math.max(2, x(cl.s_end_m) - x(cl.s_start_m)), h - pad.t - pad.b); });
    // categorised climbs get the little round numbered flag a stage profile has
    (RLK.climbs || []).filter(cl => cl.category).forEach(cl => {
      const cx = Math.min(Math.max(x(cl.s_start_m) + 12, pad.l + 10), w - pad.r - 10), cy = pad.t + 14;
      c.fillStyle = '#fff'; c.strokeStyle = '#d6006f'; c.lineWidth = 2;
      c.beginPath(); c.arc(cx, cy, 11, 0, 7); c.fill(); c.stroke();
      c.fillStyle = '#101418'; c.font = '700 11px sans-serif'; c.textAlign = 'center'; c.fillText(String(cl.category), cx, cy + 4);
    });
    // area under the line, one slice per stretch of route, coloured by how steep it is
    const base = h - pad.b;
    for (let i = 1; i < D.length; i++) {
      c.fillStyle = gradeColor(grad(i));
      c.beginPath(); c.moveTo(x(D[i - 1]), base); c.lineTo(x(D[i - 1]), y(E[i - 1])); c.lineTo(x(D[i]), y(E[i])); c.lineTo(x(D[i]), base);
      c.closePath(); c.fill();
    }
    c.beginPath(); D.forEach((d, i) => (i ? c.lineTo(x(d), y(E[i])) : c.moveTo(x(d), y(E[i]))));
    c.strokeStyle = css.getPropertyValue('--text').trim() || '#222'; c.lineWidth = 1.5; c.stroke();
    // waypoints: candidates small and faint, included ones solid
    [...RLK.wps].sort((a, b) => a.included - b.included).forEach(wp => {
      c.globalAlpha = wp.included ? 1 : 0.45; c.fillStyle = wp.color;
      c.beginPath(); c.arc(x(wp.s_m), y(E[nearest(wp.s_m)]), wp.included ? 5 : 3, 0, 7); c.fill();
      if (wp.included) { c.strokeStyle = '#fff'; c.lineWidth = 1.5; c.stroke(); }
    });
    c.globalAlpha = 1;
    if (hoverIdx !== null) {
      c.strokeStyle = fg; c.lineWidth = 1; c.beginPath();
      c.moveTo(x(D[hoverIdx]), pad.t); c.lineTo(x(D[hoverIdx]), h - pad.b); c.stroke();
    }
  };
  function onMove(ev) {
    if (!E) return;
    const r = canvas.getBoundingClientRect(), px = ev.clientX - r.left;
    const d = Math.min(Math.max(((px - pad.l) / (r.width - pad.l - pad.r)) * total, 0), total);
    hoverIdx = nearest(d);
    cursor.setLngLat(RLK.route.coords[hoverIdx]).addTo(map);
    readout.textContent = `km ${(D[hoverIdx] / 1000).toFixed(1)} · ${Math.round(E[hoverIdx])} m · ${grad(hoverIdx).toFixed(1)}%`;
    RLK.drawProfile();
  }
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerleave', () => { hoverIdx = null; cursor.remove(); RLK.drawProfile(); });
  window.addEventListener('resize', () => RLK.drawProfile());

  // Replace the route after an edit or undo: line, climbs, profile.
  RLK.setRoute = function (r) {
    RLK.route = r;
    loadArrays();
    cursor.remove();
    paintOverlays();
    if (map.getSource('route')) map.getSource('route').setData(lineFeature(r));
    if (map.getSource('climbs')) map.getSource('climbs').setData(climbGeoJSON());
    RLK.drawProfile();
  };

  (async function init() {
    const route = RLK.route = await (await fetch(mapEl.dataset.routeUrl)).json();
    RLK.wps = route.waypoints;
    loadArrays();
    const lons = route.coords.map(c => c[0]), lats = route.coords.map(c => c[1]);
    const bounds = [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]];

    mapReady.then(() => {
      map.resize();
      map.fitBounds(bounds, { padding: 40, animate: false });
      map.addSource('route', { type: 'geojson', data: lineFeature(RLK.route) });
      map.addLayer({ id: 'route-casing', type: 'line', source: 'route',
        paint: { 'line-color': '#fff', 'line-width': 8 }, layout: { 'line-cap': 'round', 'line-join': 'round' } });
      map.addLayer({ id: 'route-line', type: 'line', source: 'route',
        paint: { 'line-color': accent, 'line-width': 5 }, layout: { 'line-cap': 'round', 'line-join': 'round' } });
      // a wide, almost invisible copy of the line: the target for grabbing it in the editor (easy to hit with a finger)
      map.addLayer({ id: 'route-hit', type: 'line', source: 'route',
        paint: { 'line-color': '#000', 'line-width': 28, 'line-opacity': 0.01 }, layout: { 'line-cap': 'round', 'line-join': 'round' } });
      for (const [id, width, extra] of [['warnsec', 11, { 'line-opacity': 0.45 }], ['wind', 6, { 'line-opacity': 0.95 }],
                                         ['closures', 9, { 'line-opacity': 0.9, 'line-dasharray': [1, 1.2] }]]) {
        map.addSource(id, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addLayer({ id, type: 'line', source: id, paint: { 'line-color': id === 'closures' ? '#d70015' : ['get', 'color'], 'line-width': width, ...extra },
                       layout: { 'line-cap': 'butt', 'line-join': 'round' } }, 'route-hit');
      }
      paintOverlays();
      map.addSource('climbs', { type: 'geojson', data: climbGeoJSON() });
      map.addLayer({ id: 'climbs', type: 'line', source: 'climbs', paint: { 'line-color': '#ff3b30', 'line-width': 5, 'line-opacity': 0.9 },
                     layout: { 'line-cap': 'round', 'line-join': 'round' } });
      map.addSource('wps', { type: 'geojson', data: wpGeoJSON() });
      map.addLayer({ id: 'wps', type: 'circle', source: 'wps', filter: ['!=', ['get', 'kind'], 'regroup'], paint: {
        'circle-color': ['get', 'color'],
        'circle-radius': ['case', ['==', ['get', 'included'], 1], 9, 6],
        'circle-opacity': ['case', ['==', ['get', 'included'], 1], 1, 0.55],
        'circle-stroke-width': ['case', ['==', ['get', 'included'], 1], 3, 1.5],
        'circle-stroke-color': '#fff' } });
      const showWps = document.getElementById('show-wps');
      const applyWps = () => { const on = !showWps || showWps.checked; map.setLayoutProperty('wps', 'visibility', on ? 'visible' : 'none'); };
      if (showWps) {
        try { if (localStorage.getItem('rlk-show-wps') === '0') showWps.checked = false; } catch (e) { /* storage blocked: default on */ }
        showWps.addEventListener('change', () => { applyWps(); try { localStorage.setItem('rlk-show-wps', showWps.checked ? '1' : '0'); } catch (e) { /* ignore */ } });
        applyWps();
      }
      map.on('click', 'wps', e => {
        const f = e.features[0].properties;
        const box = document.createElement('div');
        box.innerHTML = '<strong></strong><br><span class="muted"></span><br>';
        box.querySelector('strong').textContent = f.name;
        box.querySelector('span').textContent = `km ${f.km}`;
        const b = document.createElement('button');
        b.className = 'btn small'; b.type = 'button';
        b.textContent = f.included == 1 ? 'Remove from export' : 'Add to export';
        b.onclick = () => { RLK.onToggle && RLK.onToggle(f.id, !(f.included == 1)); popup.remove(); };
        box.appendChild(b);
        const popup = new maplibregl.Popup().setLngLat(e.lngLat).setDOMContent(box).addTo(map);
      });
      map.on('mouseenter', 'wps', () => (map.getCanvas().style.cursor = 'pointer'));
      map.on('mouseleave', 'wps', () => (map.getCanvas().style.cursor = ''));
      syncRegroupMarkers();
      resolveReady(RLK);
    });
    RLK.focusWaypoint = w => map.easeTo({ center: [w.lon, w.lat], zoom: Math.max(map.getZoom(), 14) });
    RLK.drawProfile();
  })();
})();
