"""The printable cue card: A5 portrait PDF built from the ride sheet. Warnings first, then the ordered list of stops with
distances, regroups, hazards and closures, then emergency details. Uses the standard PDF fonts, so text the fonts
can't draw (emoji, some accents) is folded to plain letters rather than printed as boxes."""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .export.names import ascii_fold

LEVEL_COLORS = {"yellow": colors.HexColor("#ffd60a"), "orange": colors.HexColor("#ff9500"), "red": colors.HexColor("#d70015")}


def _t(text) -> str:
    """Text safe for the PDF's built-in font: keep what Latin-1/Windows-1252 can show, fold the rest."""
    out = []
    for ch in str(text if text is not None else ""):
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(ascii_fold(ch) or "?")
    return escape("".join(out))


def rows_for(s: dict) -> list[tuple[float, str, str, str]]:
    """(distance m, km text, what, details) for everything on the route, in ride order."""
    rows = []
    cafes = {c["id"]: c for c in s["cafes"]}
    for c in s["climbs"]:
        extra = {"ease_back": " Ease back after the top.", "stop": " Regroup after the top."}.get(c["regroup"], "")
        rows.append((c["s_start"], f"{c['s_start'] / 1000:.1f}", "CLIMB" + (f" cat {c['category']}" if c["category"] else ""),
                     f"{c['length_km']} km, +{c['gain_m']} m, {c['avg']}% avg, {c['max']}% max.{extra}"))
    for st in s["stops"]:
        if st["kind"] == "regroup":
            what, det = "REGROUP", st["name"] + (f". {st['note']}" if st["note"] else "")
        else:
            what = ("STOP " if st["planned_stop"] else "") + st["kind_label"].upper().rstrip("S")
            det = st["name"] + (f". {st['note']}" if st["note"] else "")
            c = cafes.get(st["id"])
            if c and c["groups"]:
                det += ". " + "; ".join(f"{g['group']} {g['eta']} {g['label'].lower()}" for g in c["groups"])
                if c.get("hours"):
                    det += f" ({c['hours']})"
        rows.append((st["s_m"], st["km"], what, det))
    for h in s["hazards"]:
        rows.append((h["s_m"], h["km"], "HAZARD", h["name"] + (f". {h['note']}" if h["note"] else "")))
    for c in s["closures"]["hits"]:
        rows.append((c["s_start"], f"{c['s_start'] / 1000:.1f}", "CLOSURE", f"{c['description']} (km {c['s_start'] / 1000:.1f} to {c['s_end'] / 1000:.1f})"))
    for b in s["bailouts"]:
        rows.append((b["s_m"], b["km"], "BAIL-OUT", f"{b['name']} ({b['offset_m'] or 0:.0f} m off the route)"))
    return sorted(rows, key=lambda r: r[0])


def build_pdf(s: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=14 * mm,
                            title=f"Cue card: {s['title']}", author="Ride Leader Kit")
    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=14, leading=16, spaceAfter=2)
    body = ParagraphStyle("body", parent=base["BodyText"], fontName="Helvetica", fontSize=8.5, leading=10.5)
    small = ParagraphStyle("small", parent=body, fontSize=7, leading=8.5, textColor=colors.HexColor("#444444"))
    bold = ParagraphStyle("bold", parent=body, fontName="Helvetica-Bold")
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=9.5)
    story = []
    banner = s["banner"]
    if banner["status"] == "unreachable":
        story.append(_box("COULDN'T CHECK MET EIREANN WARNINGS. Check met.ie/warnings before you ride.", colors.HexColor("#fff3cd"), body))
    for w in banner["warnings"]:
        story.append(_box(f"{w['level'].upper()} {_t(w['type']).upper()} WARNING: {_t(w['headline'])}. Valid {_t(w['valid_from'])} to {_t(w['valid_to'])}.",
                          LEVEL_COLORS[w["level"]], body, light_text=w["level"] == "red"))
    if banner["status"] == "unreachable" or banner["warnings"]:
        story.append(Spacer(1, 3))
    story += [Paragraph(_t(s["title"]), h1),
             Paragraph(f"<b>{_t(s['date_long'])}</b>, meet {_t(s['meeting']['name'])} at <b>{_t(s['start_time'])}</b>", body)]
    story.append(Paragraph(f"{s['distance_km']} km" + (f", {s['ascent_m']} m climbing" if s["ascent_m"] is not None else "") + ". Back by: "
                           + ", ".join(f"{_t(g['name'])} {g['finish']} ({g['speed_kmh']:g} km/h)" for g in s["groups"]), body))
    story.append(Spacer(1, 3))
    if s["notes"]:
        story += [_box("FROM YOUR LEADER: " + _t(s["notes"]), colors.HexColor("#fff4c2"), body), Spacer(1, 3)]

    wx = s["weather"]
    story.append(Paragraph(_t(wx["summary"]) if wx.get("available") and wx.get("summary") else _t(wx.get("reason") or "No forecast yet."), body))
    for note in s["daylight"].get("warnings", []):
        story.append(Paragraph("LIGHTS: " + _t(note), bold))
    for f in wx.get("flags", []):
        story.append(Paragraph("Advisory: " + _t(f["text"]), body))
    story.append(Spacer(1, 4))

    data = [[Paragraph("<b>km</b>", cell), Paragraph("<b>What</b>", cell), Paragraph("<b>Details</b>", cell)]]
    for _, km, what, det in rows_for(s):
        data.append([Paragraph(_t(km), cell), Paragraph(f"<b>{_t(what)}</b>", cell), Paragraph(_t(det), cell)])
    if len(data) == 1:
        data.append([Paragraph("", cell), Paragraph("", cell), Paragraph("Nothing marked on this route.", cell)])
    t = Table(data, colWidths=[11 * mm, 24 * mm, None], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999999")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e5ea")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [t, Spacer(1, 5), Paragraph("EMERGENCY", bold), Paragraph(_t(s["emergency"]), body)]
    if s["leader_phone"]:
        story.append(Paragraph(f"Ride leader: <b>{_t(s['leader_phone'])}</b>", body))
    aed = [f"{_t(sec['label'])}: {_t(sec['item']['name'])} at km {sec['item']['km']}, {sec['item']['offset_m'] or 0:.0f} m {'left' if sec['item']['side'] == 'L' else 'right' if sec['item']['side'] == 'R' else 'off'}"
           + (" (club verified)" if sec["item"]["verified_at"] else "") if sec["item"] else f"{_t(sec['label'])}: none known nearby"
           for sec in s["aed_sections"]]
    story += [Paragraph("Nearest defibrillator by section", bold)] + [Paragraph(a, small) for a in aed] + [Paragraph(_t(s["aed_note"]), small)]
    story.append(Spacer(1, 4))
    story.append(Paragraph(_t(" · ".join(s["attribution"])), small))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(10 * mm, 7 * mm, _plain(f"{s['title']} · printed {s['generated_at']}"))
        canvas.drawRightString(A5[0] - 10 * mm, 7 * mm, f"Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _plain(text: str) -> str:
    return "".join(c if ord(c) < 256 else (ascii_fold(c) or "?") for c in text)


def _box(text, bg, style, light_text: bool = False):
    st = ParagraphStyle("box", parent=style, textColor=colors.white if light_text else colors.black, fontName="Helvetica-Bold")
    t = Table([[Paragraph(text, st)]])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t
