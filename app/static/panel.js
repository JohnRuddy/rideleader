// Waypoint panel: scan, grouped candidate list, bulk actions.
(function () {
  const panel = document.getElementById('panel');
  const cats = JSON.parse(panel.dataset.categories);
  const catByKey = Object.fromEntries(cats.map(c => [c.key, c]));
  const groupsEl = document.getElementById('groups');
  const statusEl = document.getElementById('scan-status');
  const scanBtn = document.getElementById('scan-btn');
  const countEl = document.getElementById('export-count');
  const open = new Set();   // categories whose <details> are open, kept across re-renders
  let wps = [];
  let status = {};   // waypoint id -> opening-hours evaluation per pace group
  let statusNote = '';

  const api = async (url, method, body) => {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || r.statusText); }
    return r.status === 204 ? null : r.json();
  };
  const el = (tag, attrs = {}, ...kids) => {
    const n = Object.assign(document.createElement(tag), attrs);
    kids.forEach(k => n.append(k));
    return n;
  };
  const fmtKm = m => (m / 1000).toFixed(1);
  // small outline icons for the row actions (instead of emoji, which render differently on every device)
  const IC = {
    clock: 'M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', tag: 'M3 12V4h8l9 9-8 8zM8 8h.01',
    cup: 'M4 8h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5zM16 10h2a2 2 0 0 1 0 4h-2M7 3v2M11 3v2',
    pencil: 'M4 20l1-5L16 4l4 4L9 19zM14 6l4 4', pin: 'M12 17v5M8 3h8l-1 7 3 3H6l3-3z',
    pinoff: 'M12 17v5M8 3h8l-1 7 3 3H6l3-3zM4 4l16 16', trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  };
  const ic = k => `<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${IC[k]}"/></svg>`;

  // search distance inputs
  const optsEl = document.getElementById('scan-opts');
  cats.filter(c => c.scan).forEach(c => {
    const id = 'corr-' + c.key;
    optsEl.append(el('label', { className: 'opt', htmlFor: id },
      el('span', { textContent: c.label }),
      el('input', { id, type: 'number', min: 10, max: 5000, step: 10, value: c.corridor_m, inputMode: 'numeric' }),
      ' m'));
  });

  function setAll(list) {
    wps = list;
    if (window.RLK) RLK.setWaypoints(list);
    render();
  }
  async function load() { setAll(await api(`/api/routes/${panel.dataset.routeId}/waypoints`, 'GET')); await loadStatus(); }

  async function loadStatus() {
    try {
      const r = await api(`/api/routes/${panel.dataset.routeId}/stops-status`, 'GET');
      status = Object.fromEntries(r.stops.map(x => [x.id, x]));
      statusNote = '';
    } catch (e) { status = {}; statusNote = e.message; }
    render();
  }
  document.addEventListener('rlk:ride-changed', loadStatus);
  document.addEventListener('rlk:waypoints-changed', () => load());
  // a regroup was dragged on the map: the server recomputes its distance along the route
  async function moveWaypoint(id, lng, lat) {
    try { await patch(id, { lat, lon: lng }); await loadStatus(); statusEl.textContent = 'Moved (pinned so rescans keep it).'; }
    catch (e) { statusEl.textContent = e.message; await load(); }
  }

  async function patch(id, body) {
    const w = await api(`/api/waypoints/${id}`, 'PATCH', body);
    setAll(wps.map(x => (x.id === id ? w : x)));
    document.dispatchEvent(new Event('rlk:history-changed'));
  }
  async function bulk(kind, action, value) {
    try {
      const r = await api(panel.dataset.bulkUrl, 'POST', { kind, action, value });
      setAll(r.waypoints);
      document.dispatchEvent(new Event('rlk:history-changed'));
      statusEl.textContent = r.changed ? `${r.changed} updated` : 'Nothing to change';
    } catch (e) { statusEl.textContent = e.message; }
  }

  function render() {
    const included = wps.filter(w => w.included).length;
    countEl.textContent = included;
    const limit = Number(panel.dataset.deviceLimit) || null, warn = document.getElementById('limit-warn');
    warn.hidden = !(limit && included > limit);
    if (!warn.hidden) warn.textContent = `${included} in export, but ${panel.dataset.deviceLabel} units are commonly limited to ${limit} course points. Trim some before exporting.`;
    document.getElementById('aed-note').hidden = !wps.some(w => w.kind === 'aed');
    // how full the head unit's course-point list is, and what is ticked, at a glance
    const dm = document.getElementById('devmeter');
    if (dm) {
      dm.hidden = !limit;
      if (limit) {
        document.getElementById('dm-device').textContent = panel.dataset.deviceLabel;
        document.getElementById('dm-count').textContent = included;
        document.getElementById('dm-limit').textContent = limit;
        const f = document.getElementById('dm-fill');
        f.style.width = Math.min(100, (included / limit) * 100) + '%';
        dm.dataset.level = included > limit ? 'over' : included > limit * 0.8 ? 'near' : 'ok';
      }
    }
    const sum = document.getElementById('stop-summary');
    if (sum) {
      sum.replaceChildren();
      const by = {};
      wps.filter(w => w.included).forEach(w => { by[w.kind] = (by[w.kind] || 0) + 1; });
      Object.entries(by).forEach(([k, n]) => {
        const c = catByKey[k] || { label: 'Other', color: '#636366' };
        sum.append(el('li', {}, Object.assign(el('i'), { style: `background:${c.color}` }), c.label.replace(/\s*\(.*\)/, ''), Object.assign(el('b'), { textContent: String(n) })));
      });
    }
    groupsEl.replaceChildren();
    if (statusNote) groupsEl.append(el('p', { className: 'warn small', textContent: `${statusNote} Café opening times show as unknown until it is back.` }));
    if (!wps.length) {
      const empty = el('div', { className: 'empty' });
      empty.innerHTML = '<svg viewBox="0 0 64 64" width="56" height="56" aria-hidden="true"><path d="M8 44c10 0 8-24 18-24s8 24 18 24 6-14 12-14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><circle cx="26" cy="20" r="6" fill="#ffd60a" stroke="currentColor" stroke-width="2.5"/></svg>';
      empty.append(el('p', { className: 'empty-title', textContent: 'No stops on this route yet' }),
        el('p', { className: 'muted small', textContent: 'Press “Find stops along route” and cafés, water, toilets and defibrillators near the line appear here.' }));
      groupsEl.append(empty);
      return;
    }
    const order = [...cats.map(c => c.key), 'generic'];
    order.forEach(kind => {
      const items = wps.filter(w => w.kind === kind);
      if (!items.length) return;
      const label = (catByKey[kind] || { label: 'Other' }).label;
      const sel = items.filter(w => w.included).length;
      const d = el('details', { className: 'group', open: open.has(kind) });
      d.addEventListener('toggle', () => (d.open ? open.add(kind) : open.delete(kind)));
      d.append(el('summary', {}, el('span', { className: 'dot' }), label,
        el('span', { className: 'count', textContent: `${sel}/${items.length}` })));
      d.querySelector('.dot').style.background = items[0].color;
      d.append(toolbar(kind));
      [...items.filter(w => w.club), ...items.filter(w => !w.club)].forEach(w => d.append(row(w)));      // club stops first
      groupsEl.append(d);
    });
  }

  function toolbar(kind) {
    const bar = el('div', { className: 'bulk' });
    bar.append(el('button', { type: 'button', className: 'chip', textContent: 'Select all', onclick: () => bulk(kind, 'add_all') }));
    bar.append(el('button', { type: 'button', className: 'chip', textContent: 'Clear', onclick: () => bulk(kind, 'remove_all') }));
    if (kind === 'cafe') bar.append(el('button', { type: 'button', className: 'chip', textContent: 'Only open on arrival',
      onclick: () => bulk(kind, 'only_open') }));
    [['within', 'Only within', 'm', 100], ['max_n', 'Max', '', 3], ['one_per_km', 'One per', 'km', 10]].forEach(([action, text, unit, def]) => {
      const inp = el('input', { type: 'number', min: 1, value: def, inputMode: 'numeric', ariaLabel: `${text} value` });
      bar.append(el('span', { className: 'chip-group' }, text + ' ', inp, unit && ' ' + unit + ' ',
        el('button', { type: 'button', className: 'chip', textContent: 'Apply', onclick: () => bulk(kind, action, Number(inp.value)) })));
    });
    return bar;
  }

  const ICON = { open: '✓', closes_soon: '⚠', closed: '✕', closed_today: '✕', unknown: '?' };
  function hoursBlock(w) {
    const st = status[w.id];
    if (!st) return w.kind === 'cafe' && !statusNote ? el('span', { className: 'muted small', textContent: 'Checking hours…' }) : '';
    const box = el('div', { className: 'hours' });
    st.groups.forEach(g => {
      let t = `${g.group} · ${g.eta} · ${g.label}`;
      if (g.status === 'closes_soon' && g.closes_at) t += ` (closes ${g.closes_at})`;
      if (g.status === 'closed' && g.opens_at) t += ` (opens ${g.opens_at})`;
      if (g.status === 'closed_today' && g.opens_at) t += ` (next opens ${g.opens_at})`;
      if (g.comment) t += ` · ${g.comment}`;
      box.append(el('div', { className: 'hrs ' + g.status, textContent: `${ICON[g.status]} ${t}` }));
      const alt = st.alternatives[g.group];
      if (alt) box.append(el('div', { className: 'muted small', textContent: `Try ${alt.name} (km ${fmtKm(alt.s_m)}) instead` }));
    });
    const first = st.groups[0];
    const src = st.hours ? `${st.hours_source === 'club' ? 'Club-confirmed' : 'OpenStreetMap'} hours: ${st.hours}` : 'No opening hours on record';
    box.append(el('div', { className: 'muted small', textContent: src + (first && first.hours_today.length ? ` · that day ${first.hours_today.join(', ')}` : '') }));
    if (st.osm_changed_since_club) box.append(el('div', { className: 'warn small', textContent: 'OpenStreetMap hours changed after your club entry. Check and re-confirm.' }));
    if (st.hours_error) box.append(el('div', { className: 'warn small', textContent: 'Those hours could not be read, so they are treated as unknown.' }));
    return box;
  }

  function row(w) {
    const cb = el('input', { type: 'checkbox', checked: w.included, ariaLabel: `Include ${w.name}`,
                             onchange: () => patch(w.id, { included: cb.checked }) });
    const info = el('button', { type: 'button', className: 'wp-name', onclick: () => window.RLK && RLK.focusWaypoint && RLK.focusWaypoint(w) },
      el('strong', { textContent: w.name }), w.club ? el('span', { className: 'badge', textContent: 'club' }) : '',
      el('span', { className: 'muted small',
        textContent: `km ${fmtKm(w.s_m)}` + (w.verified_at ? ` · verified ${w.verified_at.slice(0, 10)}` : '') + (w.origin === 'osm' || w.origin === 'club' ? ` · ${Math.round(w.offset_m)} m ${w.side === 'L' ? 'left' : w.side === 'R' ? 'right' : 'off'}` : '') + (w.note ? ` · ${w.note}` : '') }));
    const main = el('div', { className: 'wp-main' }, info, hoursBlock(w));
    const actions = el('span', { className: 'wp-actions' },
      w.kind === 'cafe' || w.club_hours ? el('button', { type: 'button', className: 'icon', title: 'Set or correct opening hours',
        innerHTML: ic('clock'), onclick: async () => {
          const cur = w.club_hours || w.osm_hours || '';
          const v = prompt('Opening hours in OpenStreetMap format, e.g. Mo-Sa 09:00-17:00; Su 10:00-16:00; PH off\n(leave empty to go back to OpenStreetMap hours)', cur);
          if (v === null) return;
          try { await patch(w.id, { club_hours: v }); await loadStatus(); } catch (e) { alert(e.message); } } }) : '',
      (w.origin === 'osm' || w.origin === 'gpx') && !w.club ? el('button', { type: 'button', className: 'icon', title: 'Save to the club library (verified now)', innerHTML: ic('tag'),
        onclick: async () => { try { await api(`/api/waypoints/${w.id}/save-to-club`, 'POST'); statusEl.textContent = 'Saved to the club library.'; await load(); } catch (e) { alert(e.message); } } }) : '',
      w.kind === 'cafe' ? el('button', { type: 'button', className: 'icon' + (w.planned_stop ? ' on' : ''), ariaPressed: String(!!w.planned_stop),
        title: w.planned_stop ? 'Group stops here (tap to undo)' : 'Group stops here for a break', innerHTML: ic('cup'),
        onclick: async () => { await patch(w.id, { planned_stop: !w.planned_stop }); document.dispatchEvent(new Event('rlk:ride-changed')); } }) : '',
      el('button', { type: 'button', className: 'icon', title: 'Rename or add a note', innerHTML: ic('pencil'), onclick: async () => {
        const name = prompt('Name', w.name); if (name === null) return;
        const note = prompt('Note (optional)', w.note || ''); if (note === null) return;
        try { await patch(w.id, { name, note }); } catch (e) { alert(e.message); } } }),
      el('button', { type: 'button', className: 'icon', title: w.pinned ? 'Pinned: kept through rescans (tap to unpin)' : 'Pin (keep through rescans)', innerHTML: ic(w.pinned ? 'pinoff' : 'pin'),
        onclick: () => patch(w.id, { pinned: !w.pinned }) }),
      el('button', { type: 'button', className: 'icon', title: 'Delete', innerHTML: ic('trash'), onclick: async () => {
        if (!confirm(`Delete "${w.name}"?`)) return;
        await api(`/api/waypoints/${w.id}`, 'DELETE'); setAll(wps.filter(x => x.id !== w.id));
        document.dispatchEvent(new Event('rlk:history-changed')); } }));
    return el('div', { className: 'wp' + (w.included ? ' on' : '') }, cb, main, actions);
  }

  // scanning (runs in the background worker; we poll)
  scanBtn.addEventListener('click', async () => {
    const corridors = {};
    cats.filter(c => c.scan).forEach(c => { corridors[c.key] = Number(document.getElementById('corr-' + c.key).value); });
    scanBtn.disabled = true; statusEl.textContent = 'Queued…';
    try {
      const { job_id } = await api(panel.dataset.scanUrl, 'POST', { corridors });
      for (;;) {
        await new Promise(r => setTimeout(r, 1000));
        const s = await api(`/api/scans/${job_id}`, 'GET');
        if (s.status === 'failed') throw new Error(s.message);
        if (s.status === 'done') {
          const x = s.summary;
          statusEl.textContent = `Found ${Object.values(x.counts).reduce((a, b) => a + b, 0)} candidates (${x.added} new, ${x.removed} removed${x.kept_pinned ? `, ${x.kept_pinned} pinned kept` : ''}).`;
          break;
        }
        statusEl.textContent = `${s.message || 'Working'} (${Math.round(s.progress * 100)}%)`;
      }
      await load();
    } catch (e) { statusEl.textContent = e.message; }
    scanBtn.disabled = false;
  });

  document.addEventListener('rlk:wps-changed', () => load());   // e.g. a cue placed from the map's right-click menu
  const boot = async () => { await load(); if (window.RLK) { RLK.onToggle = (id, inc) => patch(Number(id), { included: inc }); RLK.onMove = moveWaypoint; } };
  boot().then(() => {
    // arriving from a Strava import with "find stops" ticked: start the search once, then tidy the address
    const q = new URLSearchParams(location.search);
    if (q.get('autoscan') === '1') {
      history.replaceState(null, '', location.pathname);
      scanBtn.click();
    }
  });
})();
