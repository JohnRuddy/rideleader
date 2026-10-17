// Climbs card: detected climbs, a regroup choice per climb, and the thresholds that drive the proposals.
(function () {
  const box = document.getElementById('climbs');
  const base = box.dataset.routeUrl;
  const list = document.getElementById('climb-list');
  const msg = document.getElementById('climb-msg');
  const el = (tag, attrs = {}, ...kids) => { const n = Object.assign(document.createElement(tag), attrs); kids.forEach(k => n.append(k)); return n; };
  const km = m => (m / 1000).toFixed(1);
  const api = async (url, method, body) => {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const MODES = [['off', 'No regroup'], ['ease_back', 'Ease back'], ['stop', 'Stop']];
  let state = null;

  function render(data, note) {
    state = data;
    window.RLK && RLK.ready.then(() => RLK.setClimbs(data.climbs));
    list.replaceChildren();
    const s = data.settings;
    document.getElementById('set-gain').value = s.min_gain_m;
    document.getElementById('set-mode').value = s.default_mode;
    document.getElementById('set-ease').value = s.ease_km;
    document.getElementById('set-gap').value = s.gap_km;
    if (!data.has_elevation) { list.append(el('p', { className: 'muted', textContent: 'This route has no elevation data, so climbs cannot be detected yet.' })); return; }
    if (!data.climbs.length) list.append(el('p', { className: 'muted', textContent: 'No significant climbs on this route.' }));
    data.climbs.forEach((c, i) => {
      const seg = el('div', { className: 'seg', role: 'radiogroup', ariaLabel: `Regroup after climb ${i + 1}` });
      MODES.forEach(([mode, label]) => seg.append(el('button', {
        type: 'button', className: 'seg-btn' + (c.regroup_mode === mode ? ' on' : ''), textContent: label,
        role: 'radio', ariaChecked: String(c.regroup_mode === mode),
        onclick: () => setMode(c, mode) })));
      const ease = c.regroup_mode === 'ease_back' ? el('label', { className: 'ease' }, 'Ease back ',
        el('input', { type: 'number', min: 0.5, max: 10, step: 0.5, value: c.ease_km, inputMode: 'decimal',
                      onchange: e => patch(c, { ease_km: Number(e.target.value) }) }), ' km') : '';
      list.append(el('div', { className: 'climb' },
        el('div', { className: 'climb-head' },
          el('strong', { textContent: `Climb ${i + 1} · km ${km(c.s_start_m)}–${km(c.s_end_m)}` }),
          c.category ? el('span', { className: 'badge', textContent: c.category === 'HC' ? 'HC' : `Cat ${c.category}` }) : ''),
        el('div', { className: 'muted small', textContent: `${km(c.length_m)} km · +${Math.round(c.gain_m)} m · ${c.avg_grade}% average · ${c.max_grade}% max` }),
        seg, ease));
    });
    msg.textContent = note || '';
  }
  async function load() { try { render(await api(`${base}/climbs`, 'GET')); } catch (e) { msg.textContent = e.message; } }
  async function patch(c, body) {
    try {
      const d = await api(`/api/climbs/${c.id}`, 'PATCH', body);
      render(d, d.warning || '');
      document.dispatchEvent(new Event('rlk:waypoints-changed'));
      document.dispatchEvent(new Event('rlk:history-changed'));
    } catch (e) { msg.textContent = e.message; }
  }
  const setMode = (c, mode) => patch(c, { regroup_mode: mode });

  document.getElementById('climb-apply').addEventListener('click', async () => {
    if (state && state.climbs.length && !confirm('Apply these settings to every climb? This resets your per-climb choices.')) return;
    try {
      const d = await api(`${base}/climb-settings`, 'PUT', {
        min_gain_m: Number(document.getElementById('set-gain').value), default_mode: document.getElementById('set-mode').value,
        ease_km: Number(document.getElementById('set-ease').value), gap_km: Number(document.getElementById('set-gap').value) });
      render(d, (d.warnings || []).join(' ') || 'Regroups updated.');
      document.dispatchEvent(new Event('rlk:waypoints-changed'));
    } catch (e) { msg.textContent = e.message; }
  });
  document.getElementById('climb-refresh').addEventListener('click', async () => {
    msg.textContent = 'Working…';
    try {
      const d = await api(`${base}/climbs/detect`, 'POST');
      render(d, (d.warnings || []).join(' ') || `Refreshed: ${d.detected} climb${d.detected === 1 ? '' : 's'}, ${d.suggestions} extra suggestion${d.suggestions === 1 ? '' : 's'}.`);
      document.dispatchEvent(new Event('rlk:waypoints-changed'));
    } catch (e) { msg.textContent = e.message; }
  });
  document.addEventListener('rlk:route-changed', load);     // the line was edited or undone: climbs may have changed
  load();
})();
