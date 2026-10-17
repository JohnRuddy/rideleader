// Conditions for the planned ride: warnings banner, day picker, weather along the route, daylight, closures.
// Everything here asks the server about the route's current ride plan, so changing the day or time recalculates it all.
(function () {
  const base = document.getElementById('daypicker').dataset.base;
  const el = (tag, attrs = {}, ...kids) => { const n = Object.assign(document.createElement(tag), attrs); kids.forEach(k => n.append(k)); return n; };
  const api = async (url, method = 'GET', body) => {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const km = m => (m / 1000).toFixed(m < 10000 ? 1 : 0);
  const ready = () => (window.RLK ? RLK.ready : Promise.resolve());

  // ---------- warnings banner ----------
  const bannerEl = document.getElementById('warn-banner');
  function renderBanner(b) {
    bannerEl.replaceChildren();
    bannerEl.hidden = false;
    bannerEl.className = 'warn-banner ' + (b.status === 'unreachable' ? 'lv-unreachable' : b.top_level ? 'lv-' + b.top_level : 'lv-none');
    if (b.status === 'unreachable') bannerEl.append(el('p', { className: 'wb-msg', textContent: b.message }),
      el('p', { className: 'small' }, el('a', { href: b.url, target: '_blank', rel: 'noopener noreferrer', textContent: 'met.ie/warnings' })));
    if (b.status === 'ok' && !b.warnings.length) {
      bannerEl.append(el('p', { className: 'wb-msg', textContent: 'No Met Éireann weather warnings for this ride.' }),
        el('p', { className: 'small', textContent: b.checked_at ? `Checked ${new Date(b.checked_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}. ${b.attribution}` : b.attribution }));
    }
    b.warnings.forEach(w => {
      const box = el('article', { className: 'wb-item lv-' + w.level });
      box.append(el('h3', { textContent: `${w.level.toUpperCase()}: ${w.level_title}, ${w.instruction}` }),
        el('p', { className: 'wb-headline', textContent: w.headline }),
        el('p', { className: 'small', textContent: `${w.type} · ${w.counties.join(', ')} · issued ${w.issued} · valid ${w.valid_from} to ${w.valid_to}` }));
      const desc = el('div', { className: 'wb-desc' }); desc.innerHTML = w.description_html;      // sanitised by the server; text is as issued
      box.append(desc, el('p', { className: 'small' }, el('a', { href: w.url, target: '_blank', rel: 'noopener noreferrer', textContent: 'Full warning on met.ie' })));
      bannerEl.append(box);
    });
    if (b.status === 'ok' && b.warnings.length) bannerEl.append(el('p', { className: 'small wb-attrib', textContent: b.attribution }));
    if (b.postpone_prompt) {
      const act = el('div', { className: 'wb-postpone' }, el('strong', { textContent: 'Consider postponing or changing the route.' }), ' ');
      if (b.review_message) act.append(el('button', { type: 'button', className: 'chip', textContent: 'Copy "spin under review" message for the group', onclick: async e => {
        try { await navigator.clipboard.writeText(b.review_message); e.target.textContent = 'Copied'; } catch (_) { prompt('Copy this message:', b.review_message); } } }));
      bannerEl.append(act);
    }
    ready().then(() => RLK.setWarnSections && RLK.setWarnSections(b.warnings));
  }
  async function loadBanner() {
    try { renderBanner(await api(`${base}/warnings`)); }
    catch (e) { bannerEl.hidden = false; bannerEl.className = 'warn-banner lv-unreachable'; bannerEl.textContent = "Couldn't check Met Éireann weather warnings. Check met.ie/warnings before you ride."; }
  }

  // ---------- day picker ----------
  const daysEl = document.getElementById('days'), noteEl = document.getElementById('dp-note'), timeEl = document.getElementById('dp-time');
  async function setRide(patch) {
    const cur = await api(`${base}/ride`);
    await api(`${base}/ride`, 'PUT', { date: patch.date || cur.date, start_time: patch.start_time || cur.start_time, groups: cur.groups });
    document.dispatchEvent(new Event('rlk:ride-changed'));
  }
  function renderDays(d) {
    daysEl.replaceChildren();
    timeEl.value = d.start_time;
    d.days.forEach(day => {
      const warn = day.warning ? el('span', { className: 'dp-dot lv-' + day.warning, title: `${day.warning} warning`, ariaLabel: `${day.warning} weather warning` }) : '';
      const b = el('button', { type: 'button', role: 'option', ariaSelected: String(day.selected),
        className: 'day' + (day.selected ? ' sel' : '') + (day.today ? ' today' : '') + (day.weekend ? ' weekend' : '') + (day.available ? '' : ' na'),
        onclick: () => { noteEl.textContent = day.note || ''; setRide({ date: day.date }); } },
        el('span', { className: 'dw', textContent: day.today ? 'Today' : day.weekday }), el('span', { className: 'dn', textContent: day.day }),
        el('span', { className: 'di', textContent: day.available ? day.icon : '?', title: day.label }),
        el('span', { className: 'dd', textContent: day.available ? `${day.wind_kmh ?? '–'} km/h` : 'n/a' }),
        el('span', { className: 'dd', textContent: day.available ? `${day.rain_pct ?? 0}% rain` : '' }),
        day.usual ? el('span', { className: 'dp-usual', textContent: 'club spin' }) : '', warn);
      daysEl.append(b);
    });
    const sel = d.days.find(x => x.selected);
    noteEl.textContent = sel ? (sel.note || (sel.available && sel.detail && sel.detail !== 'Detailed' ? `${sel.detail} forecast: less certain this far out.` : '')) : '';
    const s = daysEl.querySelector('.sel'); if (s && s.scrollIntoView) s.scrollIntoView({ block: 'nearest', inline: 'center' });
  }
  async function loadDays() { try { renderDays(await api(`${base}/days`)); } catch (e) { noteEl.textContent = e.message; } }
  timeEl.addEventListener('change', () => timeEl.value && setRide({ start_time: timeEl.value }));

  // ---------- weather ----------
  const wx = document.getElementById('weather-body');
  const ARROW = { head: '⬅ headwind', tail: '➡ tailwind', cross: '↕ crosswind' };
  function renderWeather(w) {
    wx.replaceChildren();
    ready().then(() => { RLK.setWind && RLK.setWind(w.available ? w.segments : []); });
    if (!w.available) { wx.append(el('p', { className: 'muted', textContent: w.reason })); return; }
    wx.append(el('p', { className: 'wx-summary', textContent: w.summary || '' }));
    if (w.detail && w.detail.note) wx.append(el('p', { className: 'warn small', textContent: `${w.detail.class} forecast. ${w.detail.note}` }));
    const dl = w.daylight;
    if (dl && dl.sunrise) wx.append(el('p', { className: 'small', textContent: `☀ Sunrise ${dl.sunrise} · Sunset ${dl.sunset}` }));
    (dl.warnings || []).forEach(t => wx.append(el('p', { className: 'wx-flag', textContent: '🔦 ' + t })));
    if (w.flags.length) {
      const f = el('div', { className: 'wx-flags' }, el('strong', { textContent: 'Advisory flags (ours, not official warnings)' }));
      w.flags.forEach(x => f.append(el('div', { className: 'wx-flag', textContent: '⚠ ' + x.text })));
      wx.append(f);
    }
    if (w.kit.length) {
      const k = el('div', { className: 'wx-kit' }, el('strong', { textContent: 'Kit to suggest' }));
      w.kit.forEach(x => k.append(el('div', { textContent: `• ${x.item} (${x.why})` })));
      wx.append(k);
    }
    w.groups.forEach(g => {
      const det = el('details', { className: 'wx-group' }, el('summary', { textContent: `Group ${g.name}: along the route` }));
      const t = el('table', { className: 'wx-table' }, el('thead', {}, el('tr', {}, ...['km', 'time', '', '°C (feels)', 'wind', 'rain'].map(h => el('th', { textContent: h })))));
      const tb = el('tbody');
      g.samples.forEach(r => {
        if (!r.available) { tb.append(el('tr', {}, el('td', { textContent: km(r.s_m) }), el('td', { textContent: r.eta }), el('td', { colSpan: 4, className: 'muted', textContent: 'no forecast yet' }))); return; }
        const wind = `${Math.round(r.wind_kmh)} ${r.wind_from} (gust ${Math.round(r.gust_kmh ?? r.wind_kmh)}) ${ARROW[r.wind_class]}`;
        tb.append(el('tr', { className: 'wc-' + r.wind_class }, el('td', { textContent: `${km(r.s_m)} ${r.label.startsWith('km') ? '' : r.label}` }), el('td', { textContent: r.eta }),
          el('td', { textContent: r.icon, title: r.label_text }), el('td', { textContent: `${Math.round(r.temp_c)} (${Math.round(r.feels_c)})` }),
          el('td', { textContent: wind }), el('td', { textContent: `${(r.rain_mm_h ?? 0).toFixed(1)} mm/h · ${Math.round(r.rain_prob ?? 0)}%` })));
      });
      t.append(tb); det.append(el('div', { className: 'tablewrap' }, t)); wx.append(det);
    });
    wx.append(el('p', { className: 'small muted', textContent: `Forecast updated ${w.updated}. ${w.attribution.join(' · ')}` }));
  }
  async function loadWeather() {
    wx.textContent = 'Checking the forecast…';
    try { const w = await api(`${base}/weather`); renderWeather(w); if (w.warnings) renderBanner(w.warnings); }
    catch (e) { wx.textContent = e.message; }
  }
  document.getElementById('wind-toggle').addEventListener('change', e => ready().then(() => RLK.showWind && RLK.showWind(e.target.checked)));

  // ---------- closures ----------
  const clEl = document.getElementById('closures-body');
  function renderClosures(c) {
    clEl.replaceChildren();
    ready().then(() => RLK.setClosures && RLK.setClosures(c.hits));
    if (!c.hits.length) clEl.append(el('p', { className: 'muted', textContent: c.disclaimer }));
    c.hits.forEach(h => {
      const row = el('div', { className: 'cl-hit' }, el('strong', { textContent: `⛔ Km ${km(h.s_start)}–${km(h.s_end)}: ${h.description}` }),
        el('div', { className: 'small muted', textContent: `${h.starts_at ? 'from ' + h.starts_at.slice(0, 10) : ''}${h.ends_at ? ' until ' + h.ends_at.slice(0, 10) : ' (no end date given)'} · ${h.source}` }));
      if (h.url) row.append(el('a', { href: h.url, target: '_blank', rel: 'noopener noreferrer', className: 'small', textContent: 'details' }));
      row.append(el('button', { type: 'button', className: 'chip', textContent: 'Reroute around this', onclick: async e => {
        e.target.disabled = true; e.target.textContent = 'Finding a way round…';
        try {
          const r = await api(`${base}/closures/reroute`, 'POST', { closure_id: h.id });
          document.dispatchEvent(new Event('rlk:route-changed')); document.dispatchEvent(new Event('rlk:waypoints-changed')); document.dispatchEvent(new Event('rlk:history-changed'));
          if (window.RLK) { RLK.setRoute(await (await fetch(document.getElementById('map').dataset.routeUrl)).json()); }
          clEl.prepend(el('p', { className: r.still_blocked ? 'warn' : 'small', textContent: r.still_blocked ? 'The new route still touches this closure. Check it on the map.' : `Rerouted. The route is now ${km(r.length_m)} km.` }));
        } catch (err) { e.target.disabled = false; e.target.textContent = 'Reroute around this'; clEl.prepend(el('p', { className: 'warn', textContent: err.message })); }
      } }));
      clEl.append(row);
    });
    const cov = el('details', { className: 'cl-cov' }, el('summary', { textContent: 'What was checked' }));
    c.coverage.forEach(s => cov.append(el('p', { className: 'small' }, el('strong', { textContent: s.label + (s.ok ? '' : ' (unavailable right now)') }),
      el('br'), `${s.coverage}${s.checked_at ? ' Checked ' + new Date(s.checked_at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) + '.' : ''}${s.error ? ' ' + s.error : ''}`)));
    cov.append(el('p', { className: 'small' }, el('strong', { textContent: 'Not checked: ' }), c.not_checked.join('; ') + '.'));
    clEl.append(cov);
    if (c.hits.length) clEl.append(el('p', { className: 'small muted', textContent: c.disclaimer }));
  }
  async function loadClosures() { try { renderClosures(await api(`${base}/closures`)); } catch (e) { clEl.textContent = e.message; } }

  // entering a closure: click the map where it is, then fill in a few details
  const addBtn = document.getElementById('cl-add'), form = document.getElementById('cl-form');
  if (addBtn) addBtn.addEventListener('click', () => {
    addBtn.textContent = 'Click the map where the road is closed…'; addBtn.disabled = true;
    ready().then(() => RLK.map.once('click', e => {
      form.hidden = false; form.dataset.lat = e.lngLat.lat; form.dataset.lon = e.lngLat.lng;
      addBtn.textContent = 'Add a closure…'; addBtn.disabled = false;
      const t = new Date(); form.querySelector('[name=starts]').value = t.toISOString().slice(0, 10);
    }));
  });
  if (form) form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const f = new FormData(form), msg = form.querySelector('.cl-msg');
    try {
      await api('/api/closures', 'POST', { lat: Number(form.dataset.lat), lon: Number(form.dataset.lon), radius_m: Number(f.get('radius')),
        description: f.get('description'), starts_at: f.get('starts'), ends_at: f.get('ends') });
      form.hidden = true; form.reset(); msg.textContent = ''; loadClosures();
    } catch (e) { msg.textContent = e.message; }
  });

  function refreshAll() { loadBanner(); loadDays(); loadWeather(); loadClosures(); }
  document.addEventListener('rlk:ride-changed', refreshAll);
  document.addEventListener('rlk:route-changed', refreshAll);
  refreshAll();
})();
