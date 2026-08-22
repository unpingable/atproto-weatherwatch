"""HTML fragment for the published report's social observations section.

Takes a `SocialProjection` and returns markup. It is given no database
connection and no path, so the rendering layer structurally cannot reach past
the read model into a collector or detector table.

The section always renders, including when nothing is enabled and nothing was
detected. "Edge custody: OFF" is a receipt, and a receipt that only appears
when the answer is interesting is not a receipt.
"""

from __future__ import annotations

import html

from .. import timeutil
from .projection import SocialProjection

#: Appended to the report's stylesheet. Band colours are their own tokens
#: rather than reusing the weather conditions palette: these are effect-size
#: bands on a different scale, and sharing a colour would imply they share a
#: meaning.
STYLE_ADDITION = """
:root{--mag-info:#5a7f9c;--mag-low:#4a7f5c;--mag-med:#c99a3a;
      --mag-high:#c0632c;--mag-critical:#b03a3a;--mag-grid:#e8eaf0;}
@media (prefers-color-scheme:dark){:root{--mag-info:#7fa9cc;--mag-low:#6fbf8a;
      --mag-med:#e0b45a;--mag-high:#e08a4a;--mag-critical:#ef6a5a;
      --mag-grid:#20242e;}}
.mag-info{color:var(--mag-info)}.mag-low{color:var(--mag-low)}
.mag-med{color:var(--mag-med)}.mag-high{color:var(--mag-high)}
.mag-critical{color:var(--mag-critical)}
.seismo{width:100%;height:auto}
.lanelab{font-size:9.5px;fill:var(--muted)}
"""

BAND_ORDER = ("info", "low", "med", "high", "critical")
BAND_FILL = {b: f"var(--mag-{b})" for b in BAND_ORDER}
BAND_MEANING = {
    "info": "under 1.5x",
    "low": "1.5x - 2x",
    "med": "2x - 4x",
    "high": "4x - 11x",
    "critical": "11x and above",
}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _fmt(v, digits=2, dash="—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _seismogram(proj: SocialProjection, width=1100, lane_h=34) -> str:
    """One lane per episode type, one bar per episode, height by magnitude."""
    eps = proj.episodes
    if not eps:
        return ""
    spans = []
    for e in eps:
        a = timeutil.to_epoch(e.ts_start)
        b = timeutil.to_epoch(e.ts_end)
        if a is None or b is None:
            continue
        spans.append((a, max(b, a + 1.0), e))
    if not spans:
        return ""

    t0 = min(s[0] for s in spans)
    t1 = max(s[1] for s in spans)
    span = max(t1 - t0, 1.0)
    types = sorted({e.type for _, _, e in spans})
    height = lane_h * len(types) + 22
    peak = max((abs(e.magnitude) for _, _, e in spans), default=1.0) or 1.0
    label_w = 168

    out = [f'<svg class="seismo" viewBox="0 0 {width} {height}" '
           f'preserveAspectRatio="xMidYMid meet" role="img" '
           f'aria-label="episode magnitude over time, one lane per event type">']
    for i, t in enumerate(types):
        y = i * lane_h
        base = y + lane_h - 2
        out.append(f'<line x1="{label_w}" y1="{base}" x2="{width}" y2="{base}" '
                   f'stroke="var(--mag-grid)" stroke-width="1"/>')
        out.append(f'<text class="lanelab" x="0" y="{base - 2}">{_esc(t)}</text>')
        for a, b, e in spans:
            if e.type != t:
                continue
            x0 = label_w + (a - t0) / span * (width - label_w - 4)
            w = max((b - a) / span * (width - label_w - 4), 1.6)
            h = max(3.0, min(abs(e.magnitude) / peak, 1.0) * (lane_h - 10))
            out.append(
                f'<rect x="{x0:.2f}" y="{base - h:.2f}" width="{w:.2f}" '
                f'height="{h:.2f}" fill="{BAND_FILL.get(e.band)}" '
                f'opacity="0.85"><title>{_esc(e.type)} · {_esc(e.ts_start)} · '
                f'magnitude {e.magnitude:.2f} ({_esc(e.band)}) · '
                f'{_fmt(e.rate_ratio)}x baseline</title></rect>')
    out.append(f'<text class="lanelab" x="{label_w}" y="{height - 4}">'
               f'{_esc(timeutil.us_to_iso(int(t0 * 1_000_000)))}</text>')
    out.append(f'<text class="lanelab" x="{width}" y="{height - 4}" '
               f'text-anchor="end">'
               f'{_esc(timeutil.us_to_iso(int(t1 * 1_000_000)))}</text>')
    out.append("</svg>")
    return "".join(out)


def _legend() -> str:
    items = "".join(
        f'<span><i class="swatch" style="background:{BAND_FILL[b]}"></i>'
        f'{_esc(b)} · {_esc(BAND_MEANING[b])}</span>'
        for b in BAND_ORDER
    )
    return f'<div class="legend">{items}</div>'


def _receipt_panel(proj: SocialProjection) -> str:
    r = proj.sink_receipt or {}
    known = bool(r)
    enabled = bool(r.get("enabled"))
    state = ("ON" if enabled else "OFF") if known else "not recorded"
    cols = ", ".join(r.get("collections") or []) or "—"
    return f"""<div class="panel">
<dl class="kv">
<dt>Aggregate episode sensor</dt><dd>always available — derived from the
minute counters above, which contain no actor and no target</dd>
<dt>Edge custody (identity-bearing)</dt><dd><strong>{_esc(state)}</strong>
{'' if known else ' — no run has recorded a configuration receipt yet'}</dd>
<dt>Collections retained</dt><dd>{_esc(cols)}</dd>
<dt>Retention horizon</dt><dd>{_esc(r.get("retention") or "—")}</dd>
<dt>Configured by</dt><dd>{_esc(r.get("config_source", "—"))} ·
config <code>{_esc(r.get("config_hash", "—"))}</code></dd>
<dt>Published from</dt><dd>{_esc(", ".join(proj.source.get(
    "detector_allowlist") or ["all detectors"]))}</dd>
</dl>
<p class="note">Edge custody feeds structural detectors (concentration,
target-set overlap, temporal compression) whose findings are actor-level and
stay on the collecting host. They are never projected here, whatever this
switch says.</p>
</div>"""


def _summary_panel(proj: SocialProjection) -> str:
    s = proj.summary
    bands = s.get("by_band", {})
    cats = s.get("by_category", {})
    mag = s.get("magnitude", {})
    band_cells = "".join(
        f'<div class="panel"><div class="metric-name">{_esc(b)}</div>'
        f'<div class="metric-val mag-{_esc(b)}">{bands.get(b, 0)}</div>'
        f'<div class="metric-unit">{_esc(BAND_MEANING[b])}</div></div>'
        for b in BAND_ORDER
    )
    cat_rows = "".join(
        f'<tr><td>{_esc(k)}</td><td class="num">{v}</td></tr>'
        for k, v in sorted(cats.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2">none</td></tr>'
    dirs = s.get("by_direction", {})
    return f"""<div class="grid g3">{band_cells}</div>
<div class="grid g2" style="margin-top:12px">
<div class="panel"><div class="scroll"><table>
<tr><th>event family</th><th>episodes</th></tr>{cat_rows}</table></div></div>
<div class="panel"><dl class="kv">
<dt>episodes</dt><dd>{s.get("n_episodes", 0)}</dd>
<dt>excess / deficit</dt><dd>{dirs.get("excess", 0)} / {dirs.get("deficit", 0)}</dd>
<dt>magnitude min / med / max</dt>
<dd>{_fmt(mag.get("min"))} / {_fmt(mag.get("median"))} / {_fmt(mag.get("max"))}</dd>
<dt>first / last</dt><dd>{_esc(s.get("first_ts") or "—")}<br>
{_esc(s.get("last_ts") or "—")}</dd>
<dt>detector</dt><dd>{_esc(", ".join(s.get("detectors") or []) or "—")}</dd>
</dl></div></div>"""


#: Episodes below this magnitude are kept out of the table. At minute
#: resolution over a week the deployed instrument produced ~270 episodes/day,
#: most of them small: a MAD baseline on a smooth series makes a 3% change
#: statistically enormous, and a table of those buries the ones that moved.
#: The floor is a *display* choice -- `social.json` carries everything in the
#: window -- and the section states how many it held back.
DEFAULT_TABLE_FLOOR = 1.0

#: Hard row cap after the floor, so one very busy interval cannot produce a
#: multi-megabyte page. Also disclosed.
MAX_TABLE_ROWS = 250


def _episode_rows(proj: SocialProjection, shown) -> str:
    rows = []
    for e in shown:
        quality = ", ".join(e.window_quality) or "—"
        rows.append(
            "<tr>"
            f"<td>{_esc(e.ts_start)}</td>"
            f"<td>{_esc(e.type)}</td>"
            f"<td>{_esc(e.direction)}</td>"
            f'<td class="num">{e.magnitude:.2f}</td>'
            f'<td><span class="pill mag-{_esc(e.band)}">{_esc(e.band)}</span></td>'
            f'<td class="num">{_fmt(e.rate_ratio)}&times;</td>'
            f'<td class="num">{_fmt(e.peak_z, 1)}</td>'
            f'<td class="num">{_fmt(e.n_windows, 0)}</td>'
            f'<td class="num">{_fmt(e.events_in_episode, 0)}</td>'
            f'<td class="num">{_fmt(e.baseline_rate_eps)}</td>'
            f'<td class="num">{_fmt(e.extreme_rate_eps)}</td>'
            f"<td>{_esc(quality)}</td>"
            f"<td><code>{_esc(e.receipt_hash[:10])}</code></td>"
            "</tr>"
        )
    return "".join(rows)


def render(proj: SocialProjection, floor: float = DEFAULT_TABLE_FLOOR) -> str:
    """The whole section, always renderable."""
    span = ""
    if proj.summary.get("first_ts"):
        span = (f' Covering <strong>{_esc(proj.summary["first_ts"])}</strong> '
                f'to <strong>{_esc(proj.summary["last_ts"])}</strong>.')
    head = f"""<h2>E · Social observations — episodes</h2>
<p class="sub">A seismograph, not a dossier. These are <strong>intervals in
which an aggregate event rate departed from its own trailing baseline</strong>
— when the departure began, how long it lasted, how far it went. The subject
of every row is the episode. No account is named, counted, ranked or scored
here, and none of these figures is about any particular account: they are
derived from the same minute counters shown above, which never contained an
actor or a target in the first place.{span}</p>
<p class="sub warn"><strong>Observation is not causation, and magnitude is not
a verdict.</strong> An episode says the ground moved; it says nothing about
why, who, or whether anyone should have done anything differently. Bands are
<em>provisional</em> effect-size cuts — round numbers chosen before any
calibration against a long observation, not thresholds derived from one. Every
row carries its ratio and its z so the bands can be ignored.</p>"""

    if not proj.available:
        return f"""{head}
{_receipt_panel(proj)}
<div class="panel"><p class="sub">No episodes projected — {_esc(proj.reason)}.
</p></div>"""

    total = len(proj.episodes)
    above = [e for e in proj.episodes if e.magnitude >= floor]
    shown = sorted(above, key=lambda e: -e.magnitude)[:MAX_TABLE_ROWS]
    shown = sorted(shown, key=lambda e: e.ts_start)
    held_back = total - len(above)
    capped = len(above) - len(shown)

    disclosure = (
        f"Table shows {len(shown)} of {total} episodes in this window. "
        f"{held_back} fall below magnitude {floor:g} "
        f"(under {2 ** floor:.3g}&times; baseline)"
        + (f" and {capped} more exceed the {MAX_TABLE_ROWS}-row cap"
           if capped else "")
        + ". The chart above plots all of them, and "
        "<code>social.json</code> carries every one with its full envelope — "
        "nothing here is dropped without being counted."
    )

    return f"""{head}
{_receipt_panel(proj)}
<div class="panel">{_seismogram(proj)}{_legend()}
<p class="note">Bar height is magnitude: log2 of the departure ratio, so one
unit is one doubling. Bar width is the episode's duration. Lanes are event
types; a lane is not a severity ordering.</p></div>
{_summary_panel(proj)}
<div class="panel"><div class="scroll wide"><table>
<tr><th>start</th><th>type</th><th>dir</th><th>mag</th><th>band</th>
<th>ratio</th><th>peak z</th><th>win</th><th>events</th>
<th>baseline /s</th><th>extreme /s</th><th>window quality</th>
<th>receipt</th></tr>
{_episode_rows(proj, shown)}
</table></div>
<p class="note">{disclosure}</p>
<p class="note">Magnitude is the size of the departure; z is what decided
there was one. They are reported separately because a very smooth series can
make a 3% change statistically enormous — on this instrument's own data a
1.03&times; like-rate departure cleared any z gate while remaining a 1.03&times;
change. <code>receipt</code> is the sealed detection's hash; the full envelope,
including the evidence commitment, is in <code>social.json</code>.</p></div>"""
