"""Color e-paper PNG rendering; all source reads come from the hub cache."""
from __future__ import annotations

import io
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1264, 1680
COLORS = {"printers": "#0963ee", "plex": "#7939db", "status": "#009c9c", "alerts": "#c82077", "traffic": "#e37c00"}


def font(size, bold=False):
    for name in (("DejaVuSans-Bold.ttf", "arialbd.ttf") if bold else ("DejaVuSans.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def text(draw, xy, value, size=26, fill="#102035", bold=False, width=1100, lines=2):
    value = str(value).replace("\n", " ")[:1200]
    f = font(size, bold)
    words = value.split()
    rendered = []
    line = ""
    for word in words:
        test = (line + " " + word).strip()
        if draw.textlength(test, font=f) > width and line:
            rendered.append(line)
            line = word
        else:
            line = test
    if line:
        rendered.append(line)
    if len(rendered) > lines:
        rendered = rendered[:lines]
        rendered[-1] = rendered[-1].rstrip(".") + "…"
    for i, row in enumerate(rendered):
        draw.text((xy[0], xy[1] + i * (size + 8)), row, font=f, fill=fill)


def render(hub, tab="printers") -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f4f7fc")
    draw = ImageDraw.Draw(image)
    color = COLORS.get(tab, COLORS["status"])
    draw.rectangle((0, 0, WIDTH, 210), fill=color)
    title = {"printers": "PRINT STATUS", "plex": "PLEX / MEDIA", "alerts": "ALERT INBOX", "traffic": "WEBSITE TRAFFIC"}.get(tab, "SERVICE STATUS")
    text(draw, (48, 35), title, 52, "white", True)
    text(draw, (50, 112), hub.config["name"], 27, "white")
    snapshot = hub.snapshot()
    text(draw, (50, 160), "Updated " + snapshot["generated_at"] + " · Source timestamps shown below", 21, "white")
    y = 238
    if tab == "printers":
        printers = hub.printers()["printers"][:2]
        if not printers:
            draw.rounded_rectangle((35, y, WIDTH - 35, y + 280), 18, fill="white", outline="#d5deeb", width=2)
            text(draw, (65, y + 25), "Connect your printers", 36, bold=True)
            text(draw, (65, y + 100), "Add a Bambu module to config.json, place its credentials in secrets.json, then run inkboard check.", 27, lines=3)
        for printer in printers:
            bottom = y + 550
            draw.rounded_rectangle((35, y, WIDTH - 35, bottom), 18, fill="white", outline=color, width=3)
            draw.rectangle((35, y + 20, 45, bottom - 20), fill=color)
            text(draw, (70, y + 25), printer["name"], 39, bold=True)
            state = str(printer.get("state", "unknown")).upper()
            online = printer.get("online", False)
            badge = "#168647" if online else "#b32132"
            draw.rounded_rectangle((900, y + 25, 1190, y + 88), 12, fill=badge)
            text(draw, (918, y + 38), state if online else "OFFLINE / STALE", 21, "white", True, width=265, lines=1)
            progress = printer.get("progress", 0)
            if not isinstance(progress, (int, float)):
                progress = 0
            progress = min(100, max(0, progress))
            text(draw, (70, y + 110), f"{progress:g}%", 80, color, True)
            text(draw, (70, y + 220), printer.get("job_name", "No job reported"), 29, lines=2)
            draw.rounded_rectangle((70, y + 315, 1180, y + 345), 10, fill="#e6edf8")
            if progress:
                draw.rounded_rectangle((70, y + 315, 70 + int(1110 * progress / 100), y + 345), 10, fill=color)
            metrics = [("TIME LEFT", str(printer.get("remaining_minutes", printer.get("remaining", "—"))) + " min"),
                       ("NOZZLE", str(printer.get("nozzle_temperature", printer.get("nozzle", "—"))) + "°C"),
                       ("BED", str(printer.get("bed_temperature", printer.get("bed", "—"))) + "°C")]
            for i, (label, value) in enumerate(metrics):
                x = 75 + i * 370
                text(draw, (x, y + 385), label, 20, "#53677f")
                text(draw, (x, y + 420), value, 34, bold=True, width=330)
            hint = "Tap for controls" if printer.get("control_link_ready") else "Read-only · Enable controls only after reviewing your printer's security settings"
            text(draw, (75, y + 495), hint, 20, "#53677f", width=1080, lines=1)
            y = bottom + 26
    else:
        modules = snapshot["modules"]
        if tab == "plex":
            modules = [m for m in modules if m.get("type") == "plex"]
        elif tab == "alerts":
            modules = [m for m in modules if not m.get("available") or m.get("stale")]
        elif tab == "traffic":
            modules = []
        else:
            modules = [m for m in modules if m.get("type") == "http_status"]
        if not modules:
            heading = "All sources healthy" if tab == "alerts" else "This module is not configured"
            text(draw, (55, y + 30), heading, 40, color, True)
            message = "No current source failures. The hub checks source freshness on every read." if tab == "alerts" else "Choose the integrations you want in config.json. Disabled modules do not require credentials."
            if tab == "traffic":
                message = "Request analytics is an optional extension. HTTP uptime checks cannot measure visits; no traffic numbers are invented."
            text(draw, (55, y + 110), message, 28, lines=5)
        for module in modules[:5]:
            draw.rounded_rectangle((35, y, WIDTH - 35, y + 235), 18, fill="white", outline=color, width=2)
            text(draw, (65, y + 20), module["name"], 34, bold=True)
            state = "READY" if module.get("available") and not module.get("stale") else "NEEDS ATTENTION"
            text(draw, (65, y + 73), state, 24, "#168647" if state == "READY" else "#b32132", True)
            text(draw, (65, y + 112), module.get("summary", ""), 24, lines=2)
            text(draw, (65, y + 187), "Last source observation: " + str(module.get("observed_at") or "not yet received"), 20, "#53677f")
            y += 260
    draw.rectangle((0, HEIGHT - 110, WIDTH, HEIGHT), fill="white")
    text(draw, (45, HEIGHT - 88), "Inkboard · Private dashboard · Cached data remains visible during disconnections", 22, "#53677f")
    text(draw, (45, HEIGHT - 50), "A stale source disables controls. Check Health for the last successful client refresh.", 21, "#53677f")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
