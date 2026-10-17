// Route page: tabs, "Ready to ride?" checks, and renaming.
(function () {
  const panel = document.getElementById('panel');
  const routeId = panel && panel.dataset.routeId;
  if (!routeId) return;
  const el = (tag, attrs = {}, ...kids) => { const n = Object.assign(document.createElement(tag), attrs); kids.forEach(k => n.append(k)); return n; };

  // ---- tabs: one set of tools at a time, the map and profile stay put ----
  const tabs = [...document.querySelectorAll('.tabs [role="tab"]')];
  const panes = [...document.querySelectorAll('.pane')];
  const store = { get() { try { return sessionStorage.getItem('rlk-tab-' + routeId); } catch (e) { return null; } },
                  set(v) { try { sessionStorage.setItem('rlk-tab-' + routeId, v); } catch (e) { /* private mode */ } } };
  function show(id, focusTab) {
    if (!panes.some(p => p.id === id)) id = 'pane-checks';
    panes.forEach(p => { p.hidden = p.id !== id; });
    tabs.forEach(t => { const on = t.dataset.pane === id; t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1; if (on && focusTab) t.focus(); });
    store.set(id);
  }
  function reveal(target, smooth = true) {
    const pane = target && target.closest('.pane');
    if (pane) show(pane.id);
    if (target) requestAnimationFrame(() => target.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' }));
  }
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => { show(t.dataset.pane); });
    t.addEventListener('keydown', e => {
      const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (d) { e.preventDefault(); const n = tabs[(i + d + tabs.length) % tabs.length]; show(n.dataset.pane, true); }
    });
  });
  document.querySelectorAll('.actionbar a[data-pane]').forEach(a => a.addEventListener('click', e => {
    e.preventDefault(); show(a.dataset.pane);
    const tabsEl = document.querySelector('.tabs'); if (tabsEl) tabsEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  // anything linking to an id inside a tab (readiness "Go", help, old bookmarks) opens that tab first
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#"]'); if (!a || a.dataset.pane || a.getAttribute('href') === '#help') return;
    const t = document.getElementById(a.getAttribute('href').slice(1));
    if (t && t.closest('.pane')) { e.preventDefault(); reveal(t); }
  });
  const hashTarget = location.hash.length > 1 ? document.getElementById(location.hash.slice(1)) : null;
  if (hashTarget && hashTarget.closest('.pane')) reveal(hashTarget, false); else show(store.get() || 'pane-checks');

  // ---- ready to ride? ----
  const rd = document.getElementById('readiness');
  const WORD = { ok: 'OK', warn: 'Check', bad: 'Fix', unknown: 'Unknown' };
  const dot = document.getElementById('tab-checks-dot');
  async function loadReadiness() {
    if (!rd) return;
    const head = rd.querySelector('.rd-headline'), list = rd.querySelector('.rd-list'), meta = rd.querySelector('.rd-meta');
    rd.dataset.state = 'loading'; head.textContent = 'Checking the ride…';
    try {
      const r = await fetch(`/api/routes/${routeId}/readiness`);
      if (!r.ok) throw new Error('not ok');
      const d = await r.json();
      rd.dataset.state = d.status;
      head.textContent = d.headline;
      meta.textContent = `Checked ${d.checked_at}`;
      if (dot) { dot.hidden = d.status === 'ok'; dot.dataset.state = d.status; }
      list.replaceChildren();
      const open = d.items.filter(i => i.status !== 'ok'), fine = d.items.filter(i => i.status === 'ok');
      const row = i => el('li', { className: 'rd-row st-' + i.status },
        el('span', { className: 'rd-tag', textContent: WORD[i.status] }),
        el('span', { className: 'rd-what' }, el('strong', { textContent: i.label }), ' ', el('span', { textContent: i.text })),
        i.anchor ? el('a', { className: 'rd-go', href: '#' + i.anchor, textContent: 'Go', ariaLabel: `Go to ${i.label}` }) : '');
      open.forEach(i => list.append(row(i)));
      if (fine.length) {
        const det = el('details', { className: 'rd-fine' }, el('summary', { textContent: `${fine.length} thing${fine.length === 1 ? '' : 's'} look fine` }));
        const ul = el('ul', { className: 'rd-list' }); fine.forEach(i => ul.append(row(i))); det.append(ul);
        list.append(el('li', {}, det));
      }
    } catch (e) {
      rd.dataset.state = 'unknown';
      head.textContent = "Couldn't run the checks";
      if (dot) dot.hidden = false;
      list.replaceChildren(el('li', { className: 'rd-row st-unknown', textContent: 'Try again in a moment. Until then, check the weather and warnings by hand.' }));
    }
  }
  if (rd) {
    rd.querySelector('.rd-refresh').addEventListener('click', loadReadiness);
    loadReadiness();
    let t;
    const again = () => { clearTimeout(t); t = setTimeout(loadReadiness, 600); };
    ['rlk:ride-changed', 'rlk:wps-changed', 'rlk:history-changed'].forEach(n => document.addEventListener(n, again));
  }

  // ---- stop count on the Stops tab ----
  const countEl = document.getElementById('export-count'), tabCount = document.getElementById('tab-stops-count');
  if (countEl && tabCount && window.MutationObserver) {
    const sync = () => { const n = Number(countEl.textContent) || 0; tabCount.textContent = n ? String(n) : ''; };
    new MutationObserver(sync).observe(countEl, { childList: true, characterData: true, subtree: true }); sync();
  }

  // ---- rename ----
  const title = document.getElementById('route-title'), btn = document.getElementById('rename-route');
  if (title && btn) {
    btn.addEventListener('click', async () => {
      const name = prompt('Name this ride (the group will see this)', title.textContent.trim());
      if (name === null || !name.trim()) return;
      const r = await fetch(`/api/routes/${routeId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: name }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { alert(j.error || 'Could not rename.'); return; }
      title.textContent = j.title; document.title = `${j.title} · Ride Leader Kit`;
    });
  }
})();
