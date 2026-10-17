// Share card: create or stop the ride-sheet link, copy it, copy the WhatsApp summary.
(function () {
  const box = document.getElementById('share');
  const base = box.dataset.base, canShare = box.dataset.can === 'yes', signedIn = box.dataset.signedIn === 'yes';
  const stateEl = document.getElementById('share-state'), msg = document.getElementById('share-msg');
  const create = document.getElementById('share-create'), copy = document.getElementById('share-copy'), stop = document.getElementById('share-stop');
  let url = null;
  const api = async (method) => {
    const r = await fetch(`${base}/share`, { method });
    const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || (r.status === 401 ? 'Sign in first to share a ride.' : r.statusText));
    return data;
  };
  const clip = async (text, ok) => { try { await navigator.clipboard.writeText(text); msg.textContent = ok; } catch (e) { prompt('Copy this:', text); } };

  function render(s) {
    url = s.url;
    stateEl.replaceChildren();
    if (!s.can_share) {
      stateEl.textContent = "This route came from your Strava, so it can't be shared as it is. Use “Share a copy with your club…” at the top of the page first, then share that copy.";
      create.hidden = copy.hidden = stop.hidden = true; return;
    }
    if (s.shared) {
      const a = document.createElement('a'); a.href = s.url; a.textContent = s.url;
      stateEl.append('Anyone with this link can view the ride: ', a);
    } else stateEl.textContent = signedIn ? 'Not shared yet.' : 'Sign in to share this ride.';
    create.hidden = s.shared || !signedIn; copy.hidden = stop.hidden = !s.shared;
  }
  async function load() { try { render(await (await fetch(`${base}/share`)).json()); } catch (e) { stateEl.textContent = e.message; } }

  create.addEventListener('click', async () => { try { const d = await api('POST'); render({ shared: true, url: d.url, can_share: true }); msg.textContent = 'Link created.'; } catch (e) { msg.textContent = e.message; } });
  stop.addEventListener('click', async () => {
    if (!confirm('Stop sharing? The old link will stop working for everyone.')) return;
    try { await api('DELETE'); render({ shared: false, url: null, can_share: true }); msg.textContent = 'Sharing stopped.'; } catch (e) { msg.textContent = e.message; }
  });
  copy.addEventListener('click', () => url && clip(url, 'Link copied.'));
  document.getElementById('share-wa').addEventListener('click', async () => {
    msg.textContent = 'Working it out…';
    try { const t = await (await fetch(`${base.replace('/api', '')}/summary.txt`)).text(); clip(t, 'Copied: paste it into the group.'); } catch (e) { msg.textContent = e.message; }
  });
  load();
})();
