// Club stop library page: add, verify, remove.
(function () {
  const api = async (url, method, body) => {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const msg = document.getElementById('stop-msg');
  document.getElementById('stop-form').addEventListener('submit', async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api('/api/club/stops', 'POST', { name: f.get('name'), kind: f.get('kind'), lat: Number(f.get('lat')), lon: Number(f.get('lon')),
                                              hours: f.get('hours'), note: f.get('note'), verified: f.get('verified') === 'on' });
      location.reload();
    } catch (err) { msg.textContent = err.message; }
  });
  document.querySelectorAll('.js-verify').forEach(b => b.addEventListener('click', async () => {
    try { await api(`/api/club/stops/${b.closest('li').dataset.id}/verify`, 'POST'); location.reload(); } catch (err) { alert(err.message); }
  }));
  document.querySelectorAll('.js-delete').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('Remove this stop from the club library?')) return;
    try { await api(`/api/club/stops/${b.closest('li').dataset.id}`, 'DELETE'); location.reload(); } catch (err) { alert(err.message); }
  }));
})();
