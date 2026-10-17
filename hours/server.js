// Evaluates OSM opening_hours strings. The Python app does all the date/ETA maths and classification;
// this service only answers "what does this rule say at these instants, and what are the opening
// intervals on this day".
//
// POST /evaluate  { items: [{ id, rules, lat, lon, country_code, state, tz, arrivals: [ISO...], day_from, day_to }] }
// GET  /health
const http = require('http');
const OH = require('opening_hours');

const MAX_BODY = 1024 * 1024;
const MAX_ITEMS = 500;
const iso = d => (d ? d.toISOString() : null);

function evaluate(it) {
  // opening_hours.js reads wall-clock fields in the process time zone, so set it per item.
  process.env.TZ = it.tz || 'Europe/Dublin';
  let oh;
  try {
    oh = new OH(String(it.rules), {
      address: { country_code: String(it.country_code || 'ie').toLowerCase(), state: it.state || '' },
      lat: it.lat, lon: it.lon,
    });
  } catch (e) {
    return { id: it.id, error: String((e && e.message) || e) };
  }
  const out = { id: it.id, warnings: oh.getWarnings(), arrivals: [], day: null };
  for (const at of it.arrivals || []) {
    const d = new Date(at);
    const open = oh.getState(d);
    const nc = oh.getNextChange(d);
    let nextOpen = null;
    if (!open) { // walk forward to the next change that is open (bounded)
      let cur = d;
      for (let i = 0; i < 20; i++) {
        const n = oh.getNextChange(cur);
        if (!n) break;
        if (oh.getState(n)) { nextOpen = n; break; }
        cur = n;
      }
    }
    out.arrivals.push({
      at, open, unknown: oh.getUnknown(d), comment: oh.getComment(d) || null,
      next_change: iso(nc), next_change_open: nc ? oh.getState(nc) : null, next_open: iso(nextOpen),
    });
  }
  if (it.day_from && it.day_to) {
    out.day = oh.getOpenIntervals(new Date(it.day_from), new Date(it.day_to))
      .map(([a, b, unknown, comment]) => [iso(a), iso(b), !!unknown, comment || null]);
  }
  return out;
}

http.createServer((req, res) => {
  const send = (code, body) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); };
  if (req.method === 'GET' && req.url === '/health') return send(200, { ok: true });
  if (req.method !== 'POST' || req.url !== '/evaluate') return send(404, { error: 'not found' });
  let raw = '';
  req.on('data', c => { raw += c; if (raw.length > MAX_BODY) { send(413, { error: 'too large' }); req.destroy(); } });
  req.on('end', () => {
    let body;
    try { body = JSON.parse(raw); } catch (e) { return send(400, { error: 'bad json' }); }
    if (!Array.isArray(body.items) || body.items.length > MAX_ITEMS) return send(400, { error: 'items required (max 500)' });
    send(200, { results: body.items.map(evaluate) });
  });
}).listen(3000, '0.0.0.0', () => console.log('hours sidecar on :3000'));
