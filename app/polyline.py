"""Google encoded-polyline format, as used by Strava's map.summary_polyline (precision 5)."""


def encode(points: list[tuple[float, float]], precision: int = 5) -> str:
    """points are (lat, lon)."""
    factor, out, prev = 10 ** precision, [], (0, 0)
    for lat, lon in points:
        cur = (round(lat * factor), round(lon * factor))
        for delta in (cur[0] - prev[0], cur[1] - prev[1]):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = cur
    return "".join(out)


def decode(s: str, precision: int = 5) -> list[tuple[float, float]]:
    factor, i, lat, lon, out = 10 ** precision, 0, 0, 0, []
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + vals[0], lon + vals[1]
        out.append((lat / factor, lon / factor))
    return out
