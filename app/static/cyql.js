// "Create this ride in Cyql": the fields Cyql's ride form asks for, each one tap to copy.
(function () {
  const box = document.getElementById('cyql');
  if (!box) return;
  const list = document.getElementById('cyql-fields'), msg = document.getElementById('cyql-msg');
  document.getElementById('cyql-gpx').href = box.dataset.gpx;
  const say = t => { msg.textContent = t; clearTimeout(say.t); say.t = setTimeout(() => (msg.textContent = ''), 3500); };
  const copy = async (text, what) => {
    try { await navigator.clipboard.writeText(text); say(`Copied ${what}.`); }
    catch (e) { say('Could not copy. Select the text and copy it by hand.'); }
  };
  const row = (label, value) => {
    const dt = Object.assign(document.createElement('dt'), { textContent: label });
    const dd = document.createElement('dd');
    dd.append(Object.assign(document.createElement('span'), { textContent: value }));
    const b = Object.assign(document.createElement('button'), { type: 'button', className: 'chip', textContent: 'Copy', ariaLabel: `Copy ${label}` });
    b.onclick = () => copy(value, label.toLowerCase());
    dd.append(b);
    const wrap = document.createElement('div'); wrap.append(dt, dd);
    return wrap;
  };
  async function load() {
    const r = await (await fetch(box.dataset.ride)).json();
    const title = document.getElementById('route-title').textContent.trim();
    const dist = document.getElementById('stat-dist').textContent.trim();
    const up = document.getElementById('stat-up').textContent.trim();
    const fast = r.groups && r.groups.length ? r.groups[0] : null;
    const d = new Date(r.date + 'T' + r.start_time);
    const when = isNaN(d) ? `${r.date} ${r.start_time}` : d.toLocaleDateString('en-IE', { weekday: 'long', day: 'numeric', month: 'long' }) + ' at ' + r.start_time;
    list.replaceChildren(
      row('Name', title),
      row('Ride type', 'Road'),
      row('Date and start time', when),
      row('Start location', r.meeting_name || 'Route start (no meeting point set yet)'),
      row('Distance', dist),
      row('Climbing', up),
      row('Estimated speed', r.groups && r.groups.length ? r.groups.map(g => `${g.name} ${Number(g.speed_kmh)} km/h`).join(', ') : 'Not set'),
    );
    if (!r.meeting_name) say('Set a meeting point on the Checks tab so Cyql riders know where to be.');
  }
  document.getElementById('cyql-copy-desc').addEventListener('click', async () => {
    try {
      const t = await (await fetch(box.dataset.summary)).text();
      await copy(t, 'the ride description');
    } catch (e) { say('Could not build the description just now.'); }
  });
  load();
  ['rlk:ride-changed', 'rlk:history-changed'].forEach(n => document.addEventListener(n, () => setTimeout(load, 300)));
})();
