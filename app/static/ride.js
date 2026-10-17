// Ride plan: date, start time, pace groups -> finish times. Changing it recalculates every arrival time.
(function () {
  const box = document.getElementById('ride');
  const url = box.dataset.url;
  const dateEl = document.getElementById('ride-date'), timeEl = document.getElementById('ride-time');
  const groupsEl = document.getElementById('ride-groups'), summaryEl = document.getElementById('ride-summary');
  const msg = document.getElementById('ride-msg');
  let groups = [];

  const el = (tag, attrs = {}, ...kids) => { const n = Object.assign(document.createElement(tag), attrs); kids.forEach(k => n.append(k)); return n; };
  const dur = s => { const m = Math.round(s / 60); return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`; };

  function field(label, type, value, onInput, extra = {}) {
    const input = el('input', Object.assign({ type, value, inputMode: type === 'number' ? 'decimal' : 'text' }, extra));
    input.addEventListener('input', () => onInput(input.value));
    return el('label', {}, label, input);
  }
  function renderGroups() {
    groupsEl.replaceChildren();
    groups.forEach((g, i) => {
      const rm = el('button', { type: 'button', className: 'icon', title: `Remove group ${g.name}`, ariaLabel: `Remove group ${g.name}`,
                                textContent: '✕', disabled: groups.length === 1, onclick: () => { groups.splice(i, 1); renderGroups(); } });
      groupsEl.append(el('div', { className: 'grp' },
        field('Group', 'text', g.name, v => (g.name = v), { maxLength: 20 }),
        field('Avg km/h', 'number', g.speed_kmh, v => (g.speed_kmh = v), { min: 8, max: 60, step: 0.5 }),
        field('Stop min', 'number', g.stop_min, v => (g.stop_min = v), { min: 0, max: 180, step: 5 }), rm));
    });
  }
  function renderSummary(r) {
    summaryEl.replaceChildren();
    r.summary.forEach(s => summaryEl.append(el('li', {},
      el('strong', { textContent: `Group ${s.name}` }),
      el('span', { textContent: `${dur(s.total_s)} · finish ${s.finish_local}` + (s.stops_s ? ` (incl. ${dur(s.stops_s)} stopped)` : '') }))));
    if (!r.planned_stops) summaryEl.append(el('li', { className: 'muted small', textContent: 'Tap ☕ on a café to plan a stop there. Stop time is then added to later arrivals.' }));
  }
  function show(r) {
    dateEl.value = r.date; timeEl.value = r.start_time;
    document.getElementById('ride-meeting').value = r.meeting_name || '';
    document.getElementById('ride-phone').value = r.leader_phone || '';
    document.getElementById('ride-notes').value = r.notes || '';
    document.dispatchEvent(new Event('rlk:ride-loaded'));
    groups = r.groups.map(g => ({ ...g }));
    renderGroups(); renderSummary(r);
  }
  async function refresh() { show(await (await fetch(url)).json()); }

  // quick lines a leader says out loud before most rides; one tap adds one to the word
  const notesEl = document.getElementById('ride-notes'), countEl = document.getElementById('ride-notes-count'), chipsEl = document.getElementById('ride-chips');
  const LINES = ['Mudguards on please.', 'Nobody gets left behind.', 'Lights on, it will be dark before we finish.', 'We regroup at the top of every climb.',
                 'Bring a spare tube and a pump.', 'Call me if you are running late.'];
  const updateCount = () => { countEl.textContent = `${notesEl.value.length} of 1000`; };
  LINES.forEach(t => chipsEl.append(Object.assign(document.createElement('button'), { type: 'button', className: 'chip', textContent: t, onclick: () => {
    const v = notesEl.value.trim();
    if (v.includes(t)) return;
    notesEl.value = (v ? v + ' ' : '') + t;
    updateCount(); notesEl.focus();
  } })));
  notesEl.addEventListener('input', updateCount);
  document.addEventListener('rlk:ride-loaded', updateCount);
  document.getElementById('ride-abc').addEventListener('click', () => {
    groups = [{ name: 'A', speed_kmh: 28, stop_min: 20 }, { name: 'B', speed_kmh: 24, stop_min: 20 }, { name: 'C', speed_kmh: 20, stop_min: 20 }];
    renderGroups();
  });
  document.getElementById('ride-add').addEventListener('click', () => {
    if (groups.length >= 6) return;
    const used = new Set(groups.map(g => g.name));
    const name = 'ABCDEF'.split('').find(c => !used.has(c)) || String(groups.length + 1);
    groups.push({ name, speed_kmh: 22, stop_min: 20 }); renderGroups();
  });
  document.getElementById('ride-save').addEventListener('click', async () => {
    msg.textContent = 'Updating…';
    const body = { date: dateEl.value, start_time: timeEl.value, meeting_name: document.getElementById('ride-meeting').value,
                   leader_phone: document.getElementById('ride-phone').value, notes: document.getElementById('ride-notes').value,
                   groups: groups.map(g => ({ name: g.name, speed_kmh: Number(g.speed_kmh), stop_min: Number(g.stop_min) })) };
    const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await r.json();
    if (!r.ok) { msg.textContent = data.error; return; }
    msg.textContent = 'Times updated.'; show(data);
    document.dispatchEvent(new Event('rlk:ride-changed'));
  });
  // a planned stop was toggled elsewhere: only the summary needs refreshing
  document.addEventListener('rlk:ride-changed', () => { if (!msg.textContent.startsWith('Updating')) refresh(); });
  document.addEventListener('rlk:route-changed', refresh);   // distance changed, so finish times did
  refresh();
})();
