"""The station: four charts in abstract space, none of them a map of anywhere.

A globe was considered and rejected. ATProto exposes no geography, so putting
these quantities on a picture of Earth would be inventing the one dimension
the instrument does not have -- and readers trust maps. Every axis used here
is one the data actually has: time, hour of day, and the quantities
themselves.

    A  Conditions      current values against their own hour-of-day cell
    B  Meteogram       quantities over time, min-max banded per column
    C  Intensity map   hour-of-day x day; the diurnal structure, plainly
    D  Field portrait  abstract state space, density-binned, outliers marked

Panel D is the one that carries the "something unusual is happening" reading.
It is a two-dimensional histogram of windows in (velocity, turbulence) space:
ordinary conditions pile into a dense cloud, and a window that sits outside it
is visibly outside it. That is a statement about a measurement, not about a
participant -- there is no one in this picture, because there is no one in the
data.

Every panel is bounded before it is drawn. The meteogram collapses to one
column per rendered pixel and the portrait bins to a fixed grid, so page size
is a function of the chart, not of how long the instrument has been running.
Both disclose what they collapsed. (See `docs/CANDIDATES.md` C4 for what
happens when a chart draws one mark per window and nobody bounds it.)
"""

from __future__ import annotations

import datetime
import html
import math

STYLE = """
:root{--bg:#f7f7f8;--panel:#fff;--ink:#16181d;--muted:#6a7080;--rule:#e2e4ea;
--accent:#3b6ea5;--grid:#e8eaf0;--cloud:#8aa0b8;--mark:#c0632c;
--q1:#3b6ea5;--q2:#4a7f5c;--q3:#c99a3a;--q4:#7b4fa0;--cool:#5a7f9c;}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--panel:#161920;
--ink:#e6e8ee;--muted:#8a90a0;--rule:#262a34;--accent:#7fb2e5;--grid:#20242e;
--cloud:#5c708a;--mark:#e08a4a;--q1:#7fb2e5;--q2:#6fbf8a;
--q3:#e0b45a;--q4:#b48ad8;--cool:#7fa9cc;}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
overflow-wrap:anywhere}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:20px;margin:0 0 2px;letter-spacing:.02em}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;
color:var(--muted);margin:26px 0 10px;font-weight:600}
.sub{color:var(--muted);margin:0 0 16px;font-size:12.5px}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:8px;
padding:14px 16px;margin-bottom:12px}
.grid{display:grid;gap:12px}
.g2{grid-template-columns:repeat(auto-fit,minmax(min(400px,100%),1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(min(210px,100%),1fr))}
.grid>*,.panel{min-width:0}
svg{display:block;max-width:100%;overflow:hidden}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid var(--rule)}
th{color:var(--muted);font-weight:600;text-transform:uppercase;
font-size:10.5px;letter-spacing:.06em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.note{color:var(--muted);font-size:11px;margin-top:6px}
.warn{border-left:3px solid var(--q3);padding-left:11px}
.metric-name{font-size:11.5px;color:var(--muted)}
.metric-val{font-size:19px;font-weight:600}
.metric-unit{font-size:10.5px;color:var(--muted)}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;
border:1px solid currentColor;white-space:nowrap}
.sup-supported{color:var(--q2)}.sup-thin{color:var(--q3)}
.sup-unsupported{color:var(--muted)}
.scroll{overflow-x:auto}
.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;
align-items:center}
@media (max-width:720px){.hero{grid-template-columns:1fr}}
.state{font-size:34px;font-weight:600;letter-spacing:.01em;margin:0 0 4px}
.state-plain{font-size:15px;line-height:1.55;margin:0 0 10px}
.st-calm{color:var(--q2)}.st-active{color:var(--cool)}
.st-turbulent{color:var(--q3)}.st-storm{color:var(--mark)}
.st-severe_storm{color:#c0392b}
@media (prefers-color-scheme:dark){.st-severe_storm{color:#ef6a5a}}
.st-unavailable{color:var(--muted)}
details{margin:10px 0 0}
details>summary{cursor:pointer;color:var(--accent);font-size:13px;
padding:6px 0;list-style:revert}
.why li{margin:6px 0}
.why{padding-left:18px;margin:6px 0}
.cant li{margin:5px 0;color:var(--muted)}
.cant{padding-left:18px;margin:6px 0;font-size:12px}
.crit td{font-size:12px}
.conf{font-size:12px;color:var(--muted);margin-top:8px}
code{font-size:11.5px;color:var(--muted)}
footer{margin-top:30px;padding-top:14px;border-top:1px solid var(--rule);
color:var(--muted);font-size:11.5px}
"""

PANEL_QUANTITIES = ("interaction_velocity", "emission_velocity",
                    "interaction_pressure", "boundary_share")
Q_COLOR = {"interaction_velocity": "var(--q1)", "emission_velocity": "var(--q2)",
           "interaction_pressure": "var(--q3)", "boundary_share": "var(--q4)"}

#: Bounds, applied before drawing. See the module docstring.
METEOGRAM_COLUMNS = 560
PORTRAIT_BINS_X = 60
PORTRAIT_BINS_Y = 34
MAX_MARKED_OUTLIERS = 60


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _fmt(v, digits=3, dash="—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        if abs(v) >= 100:
            return f"{v:,.1f}"
        return f"{v:.{digits}g}"
    return str(v)


def _epoch(iso: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _hour(iso: str) -> int | None:
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).hour
    except (ValueError, AttributeError):
        return None


def _date(iso: str) -> str:
    return iso[:10]


def _vals(obs: list, name: str) -> list:
    return [(o, o["metrics"].get(name)) for o in obs
            if o["metrics"].get(name) is not None]


# --- A: conditions ---------------------------------------------------------

def _percentile_of(value: float, cell: dict) -> str:
    """Where this sits in its own hour cell, in words rather than a score."""
    if not cell or cell.get("p05") is None:
        return "no cell baseline"
    if value > cell["p95"]:
        return "above the 95th of this hour"
    if value < cell["p05"]:
        return "below the 5th of this hour"
    if value > cell["p75"]:
        return "upper quartile for this hour"
    if value < cell["p25"]:
        return "lower quartile for this hour"
    return "typical for this hour"


def _panel_conditions(obs: list, clim: dict) -> str:
    if not obs:
        return '<div class="panel"><p class="sub">No observations.</p></div>'
    latest = obs[-1]
    hour = _hour(latest["ts_start"])
    cells = []
    for name in PANEL_QUANTITIES:
        v = latest["metrics"].get(name)
        qc = clim.get("quantities", {}).get(name, {})
        cell = next((c for c in qc.get("diurnal", []) if c["hour"] == hour), {})
        unit = qc.get("unit", "")
        where = _percentile_of(v, cell) if v is not None else (
            latest["unavailable"].get(name, "unavailable"))
        cells.append(
            f'<div class="panel"><div class="metric-name">{_esc(name)}</div>'
            f'<div class="metric-val" style="color:{Q_COLOR[name]}">'
            f'{_fmt(v)}</div>'
            f'<div class="metric-unit">{_esc(unit)}</div>'
            f'<div class="note">{_esc(where)}</div></div>'
        )
    conf = latest["confidence"]
    sup = conf["support"]
    return (
        f'<div class="grid g4">{"".join(cells)}</div>'
        f'<div class="panel"><table>'
        f'<tr><th>window</th><td>{_esc(latest["ts_start"])} → '
        f'{_esc(latest["ts_end"])}</td></tr>'
        f'<tr><th>coverage</th><td>{conf["coverage"]:.0%} · '
        f'quality {_esc(conf["quality"])}</td></tr>'
        f'<tr><th>baseline</th><td>'
        f'<span class="pill sup-{_esc(sup)}">{_esc(sup)}</span> · '
        f'{conf["baseline_days"]} day replicates for this hour · '
        f'n_eff {_fmt(conf["baseline_n_eff"], 4)}</td></tr>'
        f'<tr><th>note</th><td class="note">{_esc(conf["note"])}</td></tr>'
        f'</table></div>'
    )


# --- B: meteogram ----------------------------------------------------------

def _panel_meteogram(obs: list, width=1080, height=210) -> str:
    series = {n: _vals(obs, n) for n in PANEL_QUANTITIES}
    series = {n: v for n, v in series.items() if v}
    if not series:
        return '<p class="sub">Nothing to plot.</p>'

    t0 = min(_epoch(o["ts_start"]) for o in obs if _epoch(o["ts_start"]))
    t1 = max(_epoch(o["ts_end"]) for o in obs if _epoch(o["ts_end"]))
    span = max(t1 - t0, 1.0)
    pad, lane_h = 26, height // max(len(series), 1)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'height="{height}" role="img" aria-label="field quantities over '
             f'time, one lane per quantity">']

    for i, (name, pairs) in enumerate(series.items()):
        top = i * lane_h
        base = top + lane_h - 6
        lo = min(v for _, v in pairs)
        hi = max(v for _, v in pairs)
        rng = (hi - lo) or 1.0
        # collapse to one column per rendered pixel: bounded, and disclosed
        cols: dict = {}
        for o, v in pairs:
            e = _epoch(o["ts_start"])
            if e is None:
                continue
            c = int((e - t0) / span * (METEOGRAM_COLUMNS - 1))
            lo_hi = cols.setdefault(c, [v, v])
            lo_hi[0] = min(lo_hi[0], v)
            lo_hi[1] = max(lo_hi[1], v)
        step = (width - pad) / METEOGRAM_COLUMNS
        segs = []
        for c, (cmin, cmax) in sorted(cols.items()):
            x = pad + c * step
            y0 = base - (cmax - lo) / rng * (lane_h - 16)
            y1 = base - (cmin - lo) / rng * (lane_h - 16)
            segs.append(f'<rect x="{x:.2f}" y="{y0:.2f}" '
                        f'width="{max(step, 0.9):.2f}" '
                        f'height="{max(y1 - y0, 0.9):.2f}" '
                        f'fill="{Q_COLOR.get(name, "var(--accent)")}" '
                        f'opacity="0.8"/>')
        parts.append(f'<line x1="{pad}" y1="{base}" x2="{width}" y2="{base}" '
                     f'stroke="var(--grid)"/>')
        parts.append("".join(segs))
        parts.append(f'<text x="0" y="{top + 11}" font-size="9.5" '
                     f'fill="var(--muted)">{_esc(name)}</text>')
        parts.append(f'<text x="{width}" y="{top + 11}" font-size="9" '
                     f'text-anchor="end" fill="var(--muted)">'
                     f'{_fmt(lo)}–{_fmt(hi)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --- C: intensity map ------------------------------------------------------

def _panel_intensity(obs: list, name: str, width=1080, cell_h=13) -> str:
    pairs = _vals(obs, name)
    if not pairs:
        return '<p class="sub">Nothing to plot.</p>'
    days = sorted({_date(o["ts_start"]) for o, _ in pairs})
    grid: dict = {}
    for o, v in pairs:
        h = _hour(o["ts_start"])
        d = _date(o["ts_start"])
        if h is None:
            continue
        acc = grid.setdefault((d, h), [0.0, 0])
        acc[0] += v
        acc[1] += 1
    means = [t / n for t, n in grid.values() if n]
    if not means:
        return '<p class="sub">Nothing to plot.</p>'
    lo, hi = min(means), max(means)
    rng = (hi - lo) or 1.0

    left, top = 92, 16
    cw = (width - left - 8) / 24
    height = top + cell_h * len(days) + 22
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'height="{height}" role="img" aria-label="{_esc(name)} by hour '
             f'of day and date">']
    for h in range(0, 24, 3):
        parts.append(f'<text x="{left + h * cw:.1f}" y="{top - 4}" '
                     f'font-size="9" fill="var(--muted)">{h:02d}</text>')
    for r, d in enumerate(days):
        y = top + r * cell_h
        parts.append(f'<text x="0" y="{y + cell_h - 3}" font-size="9" '
                     f'fill="var(--muted)">{_esc(d)}</text>')
        for h in range(24):
            acc = grid.get((d, h))
            x = left + h * cw
            if not acc or not acc[1]:
                parts.append(f'<rect x="{x:.1f}" y="{y}" width="{cw:.1f}" '
                             f'height="{cell_h - 1}" fill="var(--grid)" '
                             f'opacity="0.5"><title>{_esc(d)} {h:02d}:00 · '
                             f'not observed</title></rect>')
                continue
            mean = acc[0] / acc[1]
            o = 0.10 + 0.85 * ((mean - lo) / rng)
            parts.append(f'<rect x="{x:.1f}" y="{y}" width="{cw:.1f}" '
                         f'height="{cell_h - 1}" fill="{Q_COLOR.get(name, "var(--accent)")}" '
                         f'opacity="{o:.3f}"><title>{_esc(d)} {h:02d}:00 · '
                         f'{_fmt(mean)}</title></rect>')
    parts.append(f'<text x="{left}" y="{height - 5}" font-size="9" '
                 f'fill="var(--muted)">low {_fmt(lo)}</text>')
    parts.append(f'<text x="{width}" y="{height - 5}" font-size="9" '
                 f'text-anchor="end" fill="var(--muted)">high {_fmt(hi)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --- D: field portrait -----------------------------------------------------

def _panel_portrait(obs: list, clim: dict, width=1080, height=340) -> str:
    """Abstract state space. Density-binned; outlying windows marked."""
    xs_name, ys_name = "interaction_velocity", "turbulence"
    pts = [(o, o["metrics"].get(xs_name), o["metrics"].get(ys_name))
           for o in obs]
    pts = [(o, x, y) for o, x, y in pts if x is not None and y is not None]
    if len(pts) < 5:
        return ('<p class="sub">Not enough windows carrying both quantities '
                'to draw a portrait.</p>')

    xs = [x for _, x, _ in pts]
    ys = [y for _, _, y in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    xr, yr = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    pad_l, pad_b, pad_t = 58, 30, 12
    pw, ph = width - pad_l - 14, height - pad_b - pad_t

    bins: dict = {}
    for o, x, y in pts:
        bx = min(int((x - x0) / xr * (PORTRAIT_BINS_X - 1)), PORTRAIT_BINS_X - 1)
        by = min(int((y - y0) / yr * (PORTRAIT_BINS_Y - 1)), PORTRAIT_BINS_Y - 1)
        bins[(bx, by)] = bins.get((bx, by), 0) + 1
    peak = max(bins.values())
    cw, ch = pw / PORTRAIT_BINS_X, ph / PORTRAIT_BINS_Y

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'height="{height}" role="img" aria-label="windows in '
             f'velocity-turbulence space, density binned">']
    parts.append(f'<rect x="{pad_l}" y="{pad_t}" width="{pw}" height="{ph}" '
                 f'fill="none" stroke="var(--grid)"/>')
    for (bx, by), n in sorted(bins.items()):
        x = pad_l + bx * cw
        y = pad_t + ph - (by + 1) * ch
        o = 0.12 + 0.8 * math.sqrt(n / peak)
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{cw:.2f}" '
                     f'height="{ch:.2f}" fill="var(--cloud)" '
                     f'opacity="{o:.3f}"/>')

    # Windows outside their own hour cell, drawn individually.
    qc = clim.get("quantities", {}).get(xs_name, {})
    cells = {c["hour"]: c for c in qc.get("diurnal", [])}
    marked = 0
    for o, x, y in pts:
        cell = cells.get(_hour(o["ts_start"]) or -1)
        if not cell or cell.get("p95") is None:
            continue
        if not (x > cell["p95"] or x < cell["p05"]):
            continue
        if marked >= MAX_MARKED_OUTLIERS:
            break
        marked += 1
        cx = pad_l + (x - x0) / xr * pw
        cy = pad_t + ph - (y - y0) / yr * ph
        parts.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="2.6" fill="none" '
            f'stroke="var(--mark)" stroke-width="1.2">'
            f'<title>{_esc(o["ts_start"])} · {xs_name} {_fmt(x)} · '
            f'{ys_name} {_fmt(y)} · outside this hour\'s 5th-95th</title>'
            f'</circle>')

    parts.append(f'<text x="{pad_l}" y="{height - 8}" font-size="9.5" '
                 f'fill="var(--muted)">{xs_name} →  {_fmt(x0)} … {_fmt(x1)}'
                 f'</text>')
    parts.append(f'<text x="0" y="{pad_t + 8}" font-size="9.5" '
                 f'fill="var(--muted)">{ys_name}</text>')
    parts.append(f'<text x="0" y="{pad_t + 20}" font-size="9" '
                 f'fill="var(--muted)">{_fmt(y1)}</text>')
    parts.append(f'<text x="0" y="{pad_t + ph}" font-size="9" '
                 f'fill="var(--muted)">{_fmt(y0)}</text>')
    parts.append("</svg>")
    note = (f'<p class="note">{len(pts):,} windows binned into '
            f'{PORTRAIT_BINS_X}×{PORTRAIT_BINS_Y} cells; darker is denser. '
            f'{marked} window(s) ringed for sitting outside their own '
            f'hour-of-day 5th–95th percentile'
            + (f', capped at {MAX_MARKED_OUTLIERS}' if marked >= MAX_MARKED_OUTLIERS else '')
            + '. A ring means the measurement was unusual for that hour. It '
              'is not an incident and names no one.</p>')
    return "".join(parts) + note


# --- hero: state and diurnal radar -----------------------------------------

def _radar(obs: list, clim: dict, size=360) -> str:
    """Diurnal radar. Angle is hour of day; radius is ratio to that hour's
    typical level. Both axes are quantities the data actually has.

    The grey annulus is the usual range for each hour (p25-p75 as a ratio to
    that hour's median), so it wobbles with the daily cycle rather than being
    a circle. The trace is the last 24 hours. A bulge past the outer ring is a
    stretch of time that did not look like that time of day usually looks.
    """
    name = "interaction_velocity"
    q = clim.get("quantities", {}).get(name, {})
    cells = {c["hour"]: c for c in q.get("diurnal", [])}
    if not cells:
        return ""

    cx = cy = size / 2
    r_max = size / 2 - 34

    #: Radius is LOGARITHMIC in the ratio, with "typical" at mid-radius.
    #:
    #: A linear scale capped at 5x squashes the entire interesting region --
    #: roughly 0.8x to 1.3x -- into a disc a few pixels across, and the usual
    #: range becomes an invisible dot. Log spacing gives quiet conditions
    #: somewhere to go (inward) as well as busy ones (outward), which matters
    #: for an instrument that reports lulls as readily as storms.
    LO, HI = 0.25, 8.0
    log_lo, log_hi = math.log2(LO), math.log2(HI)

    def radius(ratio: float) -> float:
        r = min(max(ratio, LO), HI)
        return (math.log2(r) - log_lo) / (log_hi - log_lo) * r_max

    def xy(hour: float, ratio: float):
        rr = radius(ratio)
        ang = (hour / 24.0) * 2 * math.pi - math.pi / 2
        return cx + rr * math.cos(ang), cy + rr * math.sin(ang)

    parts = [f'<svg viewBox="0 0 {size} {size}" width="{size}" '
             f'height="{size}" role="img" aria-label="conditions by hour of '
             f'day relative to typical">']
    for ratio, label in ((0.5, "half"), (1.0, "typical"), (2.0, "2x"),
                         (4.0, "4x"), (8.0, "8x+")):
        rr = radius(ratio)
        typical = ratio == 1.0
        stroke = "var(--cool)" if typical else "var(--grid)"
        dash = "" if typical else ' stroke-dasharray="2 3"'
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{rr:.1f}" fill="none" '
                     f'stroke="{stroke}"{dash}/>')
        parts.append(f'<text x="{cx + 7}" y="{cy - rr - 2:.1f}" font-size="8" '
                     f'fill="var(--muted)">{label}</text>')
    for h in range(0, 24, 6):
        x, y = xy(h, HI)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                     f'stroke="var(--grid)"/>')
        # Outside the plot, computed directly: routing this through xy() sent
        # it through the ratio clamp, which pinned every hour label onto the
        # outer ring where it collided with the ring labels.
        ang = (h / 24.0) * 2 * math.pi - math.pi / 2
        lx = cx + (r_max + 15) * math.cos(ang)
        ly = cy + (r_max + 15) * math.sin(ang) + 3
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="8.5" '
                     f'text-anchor="middle" fill="var(--muted)">'
                     f'{h:02d}h</text>')

    band = []
    for h in range(25):
        c = cells.get(h % 24, {})
        med, p75 = c.get("p50"), c.get("p75")
        band.append(xy(h, (p75 / med) if med and p75 else 1.0))
    for h in range(24, -1, -1):
        c = cells.get(h % 24, {})
        med, p25 = c.get("p50"), c.get("p25")
        band.append(xy(h, (p25 / med) if med and p25 else 1.0))
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in band)
    parts.append(f'<polygon points="{pts}" fill="var(--cloud)" '
                 f'opacity="0.42" stroke="var(--cloud)" stroke-width="0.8" '
                 f'stroke-opacity="0.8"/>')

    recent = obs[-24:]
    trace = []
    clamped = []
    for o in recent:
        h = _hour(o["ts_start"])
        v = o["metrics"].get(name)
        if h is None or v is None:
            continue
        med = (cells.get(h) or {}).get("p50")
        if not med:
            continue
        ratio = v / med
        pt = xy(h, ratio)
        trace.append(pt)
        if ratio > HI:
            clamped.append((pt[0], pt[1], ratio))
    if len(trace) >= 2:
        tp = " ".join(f"{x:.1f},{y:.1f}" for x, y in trace)
        parts.append(f'<polyline points="{tp}" fill="none" '
                     f'stroke="var(--q1)" stroke-width="1.8" '
                     f'stroke-linejoin="round"/>')
    if trace:
        x, y = trace[-1]
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                     f'fill="var(--mark)"><title>most recent window</title>'
                     f'</circle>')
    for x, y, ratio in clamped:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="none" '
                     f'stroke="var(--mark)" stroke-width="1" '
                     f'stroke-dasharray="2 2"><title>{ratio:.1f}x typical — '
                     f'beyond the outer ring</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def _panel_hero(cond: dict, obs: list, clim: dict) -> str:
    state = cond.get("state", "unavailable")
    reasons = "".join(f"<li>{_esc(r['plain'])}</li>"
                      for r in cond.get("reasons", []))
    cant = "".join(f"<li>{_esc(c)}</li>" for c in cond.get("cannot_see", []))
    crit = "".join(
        f'<tr><td>{_esc(label)}</td><td>{_esc(text)}</td></tr>'
        for label, text in cond.get("criteria_table", [])
    )
    return f"""<div class="panel"><div class="hero">
<div>
<p class="state st-{_esc(state)}">{_esc(cond.get("headline", ""))}</p>
<p class="state-plain">{_esc(cond.get("plain", ""))}</p>
<p class="conf">{_esc(cond.get("confidence_plain", ""))}</p>
<p class="conf">Conditions as of {_esc(cond.get("as_of", "—"))}.</p>
</div>
<div>{_radar(obs, clim)}
<p class="note" style="max-width:20em;text-align:center;margin:2px auto 0">
Last 24 hours. Angle is hour of day; distance from centre is activity against
what that hour usually looks like. Shaded band = usual range.</p></div>
</div>

<details open><summary>Why these conditions?</summary>
<ul class="why">{reasons or "<li>No comparison was possible.</li>"}</ul>
<p class="note">Rule applied: {_esc(cond.get("criteria", ""))}</p>
<p class="note" style="margin-top:10px"><strong>What this instrument cannot
see</strong>, in case you were about to assume otherwise:</p>
<ul class="cant">{cant}</ul>
</details>

<details><summary>How conditions are decided</summary>
<div class="scroll"><table class="crit">
<tr><th>state</th><th>issued when</th></tr>{crit}</table></div>
<p class="note">These criteria are published so the label can be checked
rather than trusted. A state is a statement about measured interaction
conditions — never about a person, a group, or anyone's intent.</p>
</details>
</div>"""


# --- page ------------------------------------------------------------------

def _truncation(meta: dict, shown: int) -> str:
    """Say so when the archive is larger than what is drawn."""
    total = meta.get("observations_in_store")
    if not total or total <= shown:
        return ""
    return (f" (most recent of {total:,} in the archive; "
            f"{total - shown:,} older not drawn)")


def render_page(observations: list, climatology: dict, meta: dict,
                conditions: dict | None = None) -> str:
    """Three tiers: what the weather is, why, and the machinery behind it.

    A visitor who reads only the first screen should come away with a correct
    impression. A visitor who wants the numbers can open them. Nobody has to
    meet a z-score to find out whether the environment is calm.
    """
    obs = sorted(observations, key=lambda o: o["ts_start"])
    clim = climatology or {}
    n_days = clim.get("n_days", 0)
    how_note = clim.get("hour_of_week_note", "")
    cond = conditions or {}

    support_rows = "".join(
        f'<tr><td>{_esc(n)}</td>'
        f'<td><span class="pill sup-{_esc(q["support"])}">'
        f'{_esc(q["support"])}</span></td>'
        f'<td class="num">{_fmt(q["overall"].get("p50"))}</td>'
        f'<td class="num">{_fmt(q["overall"].get("p95"))}</td>'
        f'<td class="num">{_fmt(q["overall"].get("n_eff_residual"), 4)}</td>'
        f'<td class="num">{_fmt(q["overall"].get("lag1_r_residual"), 2)}</td>'
        f'<td class="note">{_esc(q["non_claim"])}</td></tr>'
        for n, q in sorted(clim.get("quantities", {}).items())
    ) or '<tr><td colspan="7">no climatology</td></tr>'

    absences = "".join(
        f'<tr><td>{_esc(k)}</td><td class="note">{_esc(v)}</td></tr>'
        for k, v in sorted(
            (obs[0]["structural_absences"] if obs else {}).items())
    ) or '<tr><td colspan="2">—</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>social weather · current conditions</title>
<style>{STYLE}</style></head><body><div class="wrap">
<h1>social weather</h1>
<p class="sub">Conditions in a public interaction field, the way a weather
service reports conditions in the air. It tells you what the network is
experiencing. It cannot report who caused it, and that is a property of the
data rather than a policy on top of it: every figure here comes from aggregate
counters that never contained an actor, a target, a record body or a
location.</p>

{_panel_hero(cond, obs, clim)}

<details><summary>Technical detail — measurements, climatology and
provenance</summary>

<p class="sub warn"><strong>This is a climatology, not an alarm system.</strong>
Nothing below is a detector. Values are placed against the distribution of the
same measurement in the same hour of day, and where the history is too short
to support that, the panel says <em>unsupported</em> instead of guessing.
{_esc(how_note)}</p>

<h2>A · Conditions</h2>
{_panel_conditions(obs, clim)}

<h2>B · Meteogram</h2>
<div class="panel">{_panel_meteogram(obs)}
<p class="note">One column per rendered pixel; each column spans the min–max
of the windows falling in it, so a spike is never averaged away and the chart
does not grow with the archive.</p></div>

<h2>C · Diurnal intensity — interaction velocity</h2>
<div class="panel">{_panel_intensity(obs, "interaction_velocity")}
<p class="note">Hour of day across, date down. Unobserved cells are flat grey,
never zero. Diurnal structure is why conditions are compared within an hour
rather than against a flat average.</p></div>

<h2>D · Field portrait</h2>
<div class="panel">{_panel_portrait(obs, clim)}</div>

<h2>E · Climatology and what each quantity refuses</h2>
<div class="panel"><div class="scroll"><table>
<tr><th>quantity</th><th>support</th><th>p50</th><th>p95</th>
<th>n_eff (resid)</th><th>lag-1 r (resid)</th><th>does not measure</th></tr>
{support_rows}</table></div>
<p class="note">n_eff is the AR(1)-corrected sample size of the
<em>deseasonalised</em> residual. Adjacent windows are strongly correlated and
the daily cycle dominates the raw series, so reporting n alone — or even raw
n_eff — would overstate every baseline here.</p></div>

<h2>F · What this instrument cannot measure</h2>
<div class="panel"><table>{absences}</table>
<p class="note">These are design properties, not gaps awaiting work. Each is
absent because measuring it would require retention this instrument
refuses.</p></div>

</details>

<footer>
{_esc(meta.get("generated_at", ""))} ·
{len(obs):,} observations shown{_truncation(meta, len(obs))} · climatology
<code>{_esc(str(clim.get("climatology_id", "—"))[:12])}</code> over
{n_days} day(s) ·
aggregate counters only — no identities, no text, no geography.
Observation is not causation.
</footer>
</div></body></html>"""
