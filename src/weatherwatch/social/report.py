"""The seismogram. A local file, not a page.

Deliberately not a dashboard and deliberately not deployed. It renders what
was detected, on one time axis, at magnitude — and it has no lookup box, no
ranking, no account view, and nothing to paste into an argument. There is no
DID anywhere in the output: the envelopes do not carry one, so the renderer
could not print one if it tried.

It reuses weatherwatch's report idiom (monospace, CSS variables, dark-mode
media query, inline SVG sized by viewBox) so the two look like one estate,
without importing its layout code — the weather page's sections are about
observation health and rates, and none of them is what this shows.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .. import timeutil
from .envelope import DetectionEnvelope, envelope_to_dict

STYLE = """
:root{--bg:#f7f7f8;--panel:#fff;--ink:#16181d;--muted:#6a7080;--rule:#e2e4ea;
--accent:#3b6ea5;--info:#5a7f9c;--low:#4a7f5c;--med:#c98a2c;--high:#c0632c;
--critical:#b03a3a;--grid:#e8eaf0;}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--panel:#161920;
--ink:#e6e8ee;--muted:#8a90a0;--rule:#262a34;--accent:#7fb2e5;--info:#7fa9cc;
--low:#6fbf8a;--med:#e0aa4a;--high:#e08a4a;--critical:#ef6a5a;--grid:#20242e;}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
overflow-wrap:anywhere}
.wrap{max-width:1180px;margin:0 auto}
.grid>*,.panel{min-width:0}
svg{display:block;max-width:100%;overflow:hidden}
h1{font-size:20px;margin:0 0 2px;letter-spacing:.02em}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;
color:var(--muted);margin:28px 0 10px;font-weight:600}
.sub{color:var(--muted);margin:0 0 18px;font-size:12.5px}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:8px;
padding:14px 16px;margin-bottom:12px}
.note{border-left:3px solid var(--accent);padding-left:10px;color:var(--muted);
font-size:12.5px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid var(--rule);
vertical-align:top}
th{color:var(--muted);font-weight:600;text-transform:uppercase;font-size:10.5px;
letter-spacing:.06em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.lane{font-size:11px;color:var(--muted)}
code{font-size:11.5px;color:var(--muted)}
details{margin-top:6px}
summary{cursor:pointer;color:var(--accent);font-size:12px}
.sev-info{color:var(--info)}.sev-low{color:var(--low)}.sev-med{color:var(--med)}
.sev-high{color:var(--high)}.sev-critical{color:var(--critical)}
"""

SEV_FILL = {
    "info": "var(--info)", "low": "var(--low)", "med": "var(--med)",
    "high": "var(--high)", "critical": "var(--critical)",
}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _seismogram(envs: list[DetectionEnvelope], width=1100, lane_h=42) -> str:
    """One lane per episode type; a bar per episode, height by magnitude."""
    if not envs:
        return '<p class="sub">No episodes in range.</p>'

    spans = []
    for e in envs:
        a = timeutil.to_epoch(e.ts_start)
        b = timeutil.to_epoch(e.ts_end)
        if a is None or b is None:
            continue
        spans.append((a, max(b, a + 1.0), e))
    if not spans:
        return '<p class="sub">No episodes with usable timestamps.</p>'

    t0 = min(s[0] for s in spans)
    t1 = max(s[1] for s in spans)
    span = max(t1 - t0, 1.0)
    types = sorted({e.type for _, _, e in spans})
    height = lane_h * len(types) + 26
    peak = max((e.score for _, _, e in spans), default=1.0) or 1.0

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="episode seismogram">'
    ]
    for i, t in enumerate(types):
        y = i * lane_h
        parts.append(
            f'<line x1="0" y1="{y + lane_h - 1}" x2="{width}" '
            f'y2="{y + lane_h - 1}" stroke="var(--grid)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="2" y="{y + 11}" font-size="10" fill="var(--muted)" '
            f'font-family="ui-monospace,monospace">{_esc(t)}</text>'
        )
        for a, b, e in spans:
            if e.type != t:
                continue
            x0 = (a - t0) / span * (width - 4)
            w = max((b - a) / span * (width - 4), 1.5)
            mag = min(abs(e.score) / peak, 1.0)
            h = max(4, mag * (lane_h - 16))
            fill = SEV_FILL.get(e.severity, "var(--info)")
            parts.append(
                f'<rect x="{x0:.2f}" y="{y + lane_h - 2 - h:.2f}" '
                f'width="{w:.2f}" height="{h:.2f}" fill="{fill}" '
                f'opacity="0.85"><title>{_esc(e.type)} '
                f'{_esc(e.ts_start)} magnitude {e.score:.2f} '
                f'({_esc(e.severity)})</title></rect>'
            )
    parts.append(
        f'<text x="0" y="{height - 4}" font-size="10" fill="var(--muted)" '
        f'font-family="ui-monospace,monospace">'
        f'{_esc(timeutil.us_to_iso(int(t0 * 1_000_000)))}</text>'
    )
    parts.append(
        f'<text x="{width}" y="{height - 4}" font-size="10" text-anchor="end" '
        f'fill="var(--muted)" font-family="ui-monospace,monospace">'
        f'{_esc(timeutil.us_to_iso(int(t1 * 1_000_000)))}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _episode_rows(envs: list[DetectionEnvelope]) -> str:
    rows = []
    for e in envs:
        d = envelope_to_dict(e)
        ex = {k: v for k, v in e.explain.items()}
        rows.append(
            "<tr>"
            f"<td>{_esc(e.ts_start)}</td>"
            f"<td>{_esc(e.type)}</td>"
            f'<td class="num">{e.score:.3f}</td>'
            f'<td class="sev-{_esc(e.severity)}">{_esc(e.severity)}</td>'
            f'<td class="num">{_esc(ex.get("n_windows") or ex.get("n_actors") or "—")}</td>'
            f"<td><code>{_esc(e.subject.value[:12])}</code></td>"
            f"<td><details><summary>evidence</summary>"
            f"<pre style='white-space:pre-wrap;font-size:11px'>"
            f"{_esc(json.dumps(d, indent=2, sort_keys=True))}</pre>"
            f"</details></td>"
            "</tr>"
        )
    return "".join(rows)


def _cooccurrence_rows(pairs: list[dict]) -> str:
    return "".join(
        "<tr>"
        f"<td>{_esc(p['start'])}</td>"
        f"<td>{_esc(p['a_type'])}</td>"
        f"<td>{_esc(p['b_type'])}</td>"
        f'<td class="num">{_esc(p["overlap_s"])}</td>'
        "</tr>"
        for p in pairs
    )


def build_html(
    envs: list[DetectionEnvelope], pairs: list[dict], meta: dict,
) -> str:
    counts: dict[str, int] = {}
    for e in envs:
        counts[e.type] = counts.get(e.type, 0) + 1
    summary = "".join(
        f"<tr><td>{_esc(k)}</td><td class='num'>{v}</td></tr>"
        for k, v in sorted(counts.items())
    ) or "<tr><td colspan='2'>none</td></tr>"

    meta_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in sorted(meta.items())
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>social seismogram · local</title>
<style>{STYLE}</style></head><body><div class="wrap">
<h1>social seismogram</h1>
<p class="sub">Episodes detected over the weatherwatch observation layer.
The subject of every row is an <strong>episode</strong> — a bounded stretch of
observed activity — never an account. Magnitude is a departure measurement,
like a Richter reading: it says something moved, not that anyone was wrong.</p>

<div class="note">This file is local. It carries no DID, no handle, no post
text and no ranking of accounts, and it is not built to be published. Actor
tokens are salted per store and are not comparable across stores or reversible
without that salt.</div>

<h2>A · Run</h2>
<div class="panel"><table>{meta_rows}</table></div>

<h2>B · Seismogram</h2>
<div class="panel scroll">{_seismogram(envs)}</div>

<h2>C · Episodes by type</h2>
<div class="panel"><table>
<tr><th>type</th><th>count</th></tr>{summary}</table></div>

<h2>D · Episodes</h2>
<div class="panel scroll"><table>
<tr><th>start</th><th>type</th><th>magnitude</th><th>band</th>
<th>n</th><th>episode</th><th>receipts</th></tr>
{_episode_rows(envs) or "<tr><td colspan='7'>none</td></tr>"}
</table></div>

<h2>E · Co-occurring episodes</h2>
<div class="panel">
<div class="note">Overlapping intervals of <em>different</em> types. Shown
because simultaneity is worth a reader's attention; asserted as nothing more.
These are not merged into single episodes and carry no score.</div>
<div class="scroll"><table>
<tr><th>from</th><th>type a</th><th>type b</th><th>overlap s</th></tr>
{_cooccurrence_rows(pairs) or "<tr><td colspan='4'>none</td></tr>"}
</table></div></div>
</div></body></html>"""


def generate(
    envs: list[DetectionEnvelope], pairs: list[dict], meta: dict, out_dir: Path,
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "seismogram.html"
    path.write_text(build_html(envs, pairs, meta), encoding="utf-8")
    (out_dir / "episodes.json").write_text(
        json.dumps(
            {"meta": meta,
             "episodes": [envelope_to_dict(e) for e in envs],
             "co_occurrence": pairs},
            indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
