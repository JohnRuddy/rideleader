// Draw each route's outline from the encoded polyline in our own database. No Strava imagery or map tiles are used.
(function () {
  function decode(s) {
    let i = 0, lat = 0, lon = 0; const out = [];
    while (i < s.length) {
      const v = [0, 0];
      for (let k = 0; k < 2; k++) {
        let shift = 0, result = 0, b;
        do { b = s.charCodeAt(i++) - 63; result |= (b & 31) << shift; shift += 5; } while (b >= 32);
        v[k] = result & 1 ? ~(result >> 1) : result >> 1;
      }
      lat += v[0]; lon += v[1]; out.push([lat / 1e5, lon / 1e5]);
    }
    return out;
  }
  document.querySelectorAll('svg.thumb[data-poly]').forEach(svg => {
    const pts = decode(svg.dataset.poly || '');
    if (pts.length < 2) return;
    const k = Math.cos(pts[0][0] * Math.PI / 180);
    const xy = pts.map(p => [p[1] * k, -p[0]]);
    const xs = xy.map(p => p[0]), ys = xy.map(p => p[1]);
    const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
    const scale = Math.min(92 / ((maxx - minx) || 1), 52 / ((maxy - miny) || 1));
    const ox = (100 - (maxx - minx) * scale) / 2, oy = (60 - (maxy - miny) * scale) / 2;
    const d = xy.map((p, i) => `${i ? 'L' : 'M'}${((p[0] - minx) * scale + ox).toFixed(1)} ${((p[1] - miny) * scale + oy).toFixed(1)}`).join('');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d); path.setAttribute('fill', 'none'); path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2'); path.setAttribute('stroke-linecap', 'round'); path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
  });
})();

// The "find stops" tick box applies to whichever Import button is pressed.
(function () {
  const box = document.getElementById('auto-scan');
  if (!box) return;
  const apply = () => document.querySelectorAll('input[name=scan]').forEach(i => { i.disabled = !box.checked; });
  box.addEventListener('change', apply);
  apply();
})();
