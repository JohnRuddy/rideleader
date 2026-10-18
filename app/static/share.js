// Share card: create or stop the ride-sheet link, copy it, copy the WhatsApp summary.
(function () {
  const box = document.getElementById('share');
  const base = box.dataset.base, canShare = box.dataset.can === 'yes', signedIn = box.dataset.signedIn === 'yes';
  const stateEl = document.getElementById('share-state'), msg = document.getElementById('share-msg');
  const create = document.getElementById('share-create'), copy = document.getElementById('share-copy'), stop = document.getElementById('share-stop');
  const native = document.getElementById('share-native'), art = box.querySelector('.share-art');
  let url = null;
  const api = async (method, body) => {
    const r = await fetch(`${base}/share`, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (r.status === 409 && data.needs_confirm) { const e = new Error(data.error); e.needsConfirm = data; throw e; }
    if (!r.ok) throw new Error(data.error || (r.status === 401 ? 'Sign in first to share a ride.' : r.statusText));
    return data;
  };
  const clip = async (text, ok) => { try { await navigator.clipboard.writeText(text); msg.textContent = ok; } catch (e) { prompt('Copy this:', text); } };

  function render(s) {
    url = s.url;
    stateEl.replaceChildren();
    if (s.makes_copy) {
      stateEl.textContent = 'This route came from your Strava. Sharing makes your own copy, with your stops, cues and notes, and shares that. Your Strava route itself is never shown to anyone else.';
      create.hidden = !signedIn; copy.hidden = stop.hidden = true; return;
    }
    if (s.shared) {
      const a = document.createElement('a'); a.href = s.url; a.textContent = s.url;
      stateEl.append('Anyone with this link can view the ride: ', a);
    } else stateEl.textContent = signedIn ? 'Not shared yet.' : 'Sign in to share this ride.';
    create.hidden = s.shared || !signedIn; copy.hidden = stop.hidden = !s.shared;
    native.hidden = !(s.shared && navigator.share);
    if (art) art.hidden = !!s.shared;                  // the drawing is for before there is a link
  }
  async function load() { try { render(await (await fetch(`${base}/share`)).json()); } catch (e) { stateEl.textContent = e.message; } }

  create.addEventListener('click', async () => {
    try { const d = await api('POST'); render({ shared: true, url: d.url, can_share: true }); msg.textContent = 'Link created.'; }
    catch (e) {
      if (!e.needsConfirm) { msg.textContent = e.message; return; }
      if (!confirm(e.needsConfirm.consent_text + '\n\nMake the copy and share it?')) return;
      try { const d = await api('POST', { confirm_own: true }); location.href = d.copy_url; } catch (e2) { msg.textContent = e2.message; }
    }
  });
  stop.addEventListener('click', async () => {
    if (!confirm('Stop sharing? The old link will stop working for everyone.')) return;
    try { await api('DELETE'); render({ shared: false, url: null, can_share: true }); msg.textContent = 'Sharing stopped.'; } catch (e) { msg.textContent = e.message; }
  });
  copy.addEventListener('click', () => url && clip(url, 'Link copied.'));
  if (native) native.addEventListener('click', async () => {      // the phone's own share sheet: WhatsApp, Messages, email...
    try {
      const text = await (await fetch(`${base.replace('/api', '')}/summary.txt`)).text();
      await navigator.share({ title: document.getElementById('route-title').textContent, text, url });
    } catch (e) { if (e.name !== 'AbortError') msg.textContent = 'Couldn\'t open sharing: ' + e.message; }
  });
  document.getElementById('share-wa').addEventListener('click', async () => {
    msg.textContent = 'Working it out…';
    try { const t = await (await fetch(`${base.replace('/api', '')}/summary.txt`)).text(); clip(t, 'Copied: paste it into the group.'); } catch (e) { msg.textContent = e.message; }
  });
  const sponsorSel = document.getElementById('sponsor-choice');
  if (sponsorSel) sponsorSel.addEventListener('change', async () => {
    const note = document.getElementById('sponsor-msg'), [mode, id] = sponsorSel.value.split(':');
    try {
      const r = await fetch(`${box.closest('#pane-share').querySelector('#sponsor-ride').dataset.base}/sponsor`, { method: 'PUT',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(mode === 'campaign' ? { mode, campaign_id: Number(id) } : { mode }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.statusText);
      note.textContent = mode === 'none' ? 'This ride will be published without a sponsor panel.' : 'Saved.';
    } catch (e) { note.textContent = e.message; }
  });
  const vis = document.getElementById('club-visible');
  if (vis) vis.addEventListener('change', async () => {
    try {
      const r = await fetch(`${base}/visibility`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ visibility: vis.checked ? 'club' : 'private' }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.statusText);
      msg.textContent = vis.checked ? 'Now visible to your club.' : 'Now private to you.';
    } catch (e) { vis.checked = !vis.checked; msg.textContent = e.message; }
  });
  load();
})();

// "Next" prompt: jump to the tab it names
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-goto]');
  if (!b) return;
  const tab = document.getElementById('tab-' + b.dataset.goto);
  if (tab) { tab.click(); tab.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
});
