"""M7 — static dark dashboard.

Plain HTML plus inline SVG. No framework, no CDN, no JavaScript application,
no external requests of any kind. Output is a directory that can be served by
any static file server, or opened straight off disk.

Generation is atomic: everything is written to a sibling temp directory and
swapped in, so a reader never sees a half-written report.

The visual rules that matter more than the aesthetics:

* **Unobserved time never looks like zero.** A gap in observation breaks the
  line and is hatched; a window we watched with no activity draws a point on
  the floor. Those must never be confusable.
* **Degraded and gapped windows are not smoothed over.** They are shaded in
  place, at their real position.
* **The exact source endpoint and the observation interval are always on
  screen**, not in a tooltip and not in a footnote.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import shutil
import sqlite3
from pathlib import Path

from . import COLLECTOR_VERSION, db, derive, health, query, timeutil
from .query import Series, WindowPoint
from .social import section as _social_section
from .social import api as _social_api
from .social import projection as _social_projection
from .social.config import RECEIPT_META_KEY as _SOCIAL_RECEIPT_KEY

#: Window budget for the report's own queries.
#:
#: `query.series` caps a densified range at 20,000 windows so an *accidental*
#: unbounded query cannot silently truncate a series into one that merely
#: looks complete. The report's range is not accidental: it asks for the whole
#: observed interval on purpose, and it discloses that interval, its gaps and
#: its coverage on the page. So it raises the cap for itself rather than
#: narrowing what it reports.
#:
#: This is a repair, not a design, and it is filed as such: see
#: `docs/CANDIDATES.md` C4 for the measurements and the options. Continuous
#: 60s collection crossed 20,000
#: *span* windows (observed plus gap) on 2026-08-22 and publication began
#: failing with QueryTooLarge; the last good render was 19,821 windows. The
#: value below buys ~139 days at 60s. Past that the page needs an actual
#: windowing decision -- a trailing interval, or coarser buckets for the long
#: tail -- because an unboundedly growing dashboard is a defect either way.
REPORT_MAX_WINDOWS = 200_000

#: A per-window ratio is only shown as an extreme when its denominator is at
#: least this large.
#:
#: Ratios are a two-body system: `block/follow` reaches 22.75 when four
#: follows happen to land in a window, and the number is arithmetic rather
#: than weather. The page already says so in prose, but **a disclaimer does
#: not travel with a screenshot** — somebody caption-crops the 22.75 and the
#: caveat stays behind. So the guard is structural: extremes are selected only
#: from windows with a real denominator, and the windows excluded are counted
#: on the page rather than silently dropped.
MIN_RATIO_DENOMINATOR = 30

#: The deployed publisher runs every five minutes. Freshness is considered
#: current for two missed publication intervals plus one source bucket. This
#: is an operational, explicitly provisional threshold, not a statement about
#: event statistics or relay completeness.
PUBLICATION_INTERVAL_S = 5 * 60

# --- what to show ----------------------------------------------------------

#: (label, metric-or-metrics). Order is display order; a tuple of metrics is
#: summed at read time for presentation and is never persisted.
#:
#: Laid out to fall as a 4x4 at ordinary desktop width:
#:   creation      | posts    replies   quotes      reposts
#:   engagement    | likes    follows   blocks      unblocks
#:   removals      | post del like del  repost del  follow del
#:   churn/account | list mut profile   account     identity
PRIMITIVES: tuple[tuple[str, str | tuple[str, ...]], ...] = (
    ("Posts", "post.create"),
    ("Replies", "post.create.reply"),
    ("Quotes", "post.create.quote"),
    ("Reposts", "repost.create"),

    ("Likes", "like.create"),
    ("Follows", "follow.create"),
    ("Blocks", "block.create"),
    ("Unblocks", "block.delete"),

    ("Post deletes", "post.delete"),
    ("Like deletes", "like.delete"),
    ("Repost deletes", "repost.delete"),
    ("Follow deletes", "follow.delete"),

    ("List mutations", ("listitem.create", "listitem.delete")),
    ("Profile updates", "profile.update"),
    ("Account events", "account.event"),
    ("Identity events", "identity.event"),
)

#: Optional hover text, for cards whose label is shorter than its meaning.
#: Descriptive of the observed events only — no relationship state inferred.
CARD_HELP: dict[str, str] = {
    "Unblocks": ("app.bsky.graph.block delete events — a block record was "
                 "removed. Nothing is inferred about the relationship."),
    "List mutations": ("aggregate list membership churn: app.bsky.graph.listitem "
                       "creates + deletes. No lists, members or identities."),
}


def _metric_keys(spec) -> tuple[str, ...]:
    """The persisted key(s) a card reads. Composites list their components."""
    m = spec[1]
    return (m,) if isinstance(m, str) else tuple(m)


def _card_series(series_map: dict[str, Series], spec) -> Series | None:
    """Resolve a card spec to a Series, summing components if composite."""
    keys = _metric_keys(spec)
    parts = [series_map[k] for k in keys if k in series_map]
    if len(parts) != len(keys):
        return None
    if len(parts) == 1:
        return parts[0]
    return query.sum_series(parts, "+".join(keys))

QUALITY_COLORS = {
    "clean": "var(--ok)",
    "seam": "var(--seam)",
    "lagged": "var(--lagged)",
    "warming_up": "var(--warming)",
    "partial": "var(--partial)",
    "degraded": "var(--degraded)",
    "loss": "var(--loss)",
    "gap": "var(--gap)",
    "unobserved": "var(--unobserved)",
}

QUALITY_HELP = {
    "clean": "full window, no loss",
    "seam": "reconnect seam; interval reconstructed by cursor+1 replay",
    "lagged": "complete data, behind real time (replay or backlog)",
    "warming_up": "collector baseline not yet established",
    "partial": "observed for less than the full window",
    "degraded": "health gate tripped for a coverage reason",
    "loss": "instrumented loss in this window",
    "gap": "stream discontinuity inside the window",
    "unobserved": "nobody was watching — NOT zero activity",
}

CONDITION_COLORS = {
    "surging": "var(--surging)",
    "elevated": "var(--elevated)",
    "normal": "var(--normal)",
    "quiet": "var(--quiet)",
    "degrading": "var(--degrading)",
    "unknown": "var(--muted)",
}

STYLE = """
:root {
  --bg:#f7f7f8; --panel:#ffffff; --ink:#16181d; --muted:#6a7080;
  --rule:#e2e4ea; --accent:#3b6ea5;
  --ok:#3f8f5f; --seam:#7a6fd0; --lagged:#4f8fbf; --warming:#8a8f9c;
  --partial:#c99a3a; --degraded:#c0632c; --loss:#b03a3a; --gap:#8d2b2b;
  --unobserved:#c9ccd4;
  --surging:#c0392b; --elevated:#c98a2c; --normal:#4a7f5c;
  --quiet:#5a7f9c; --degrading:#7b4fa0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0e1014; --panel:#161920; --ink:#e6e8ee; --muted:#8a90a0;
    --rule:#262a34; --accent:#7fb2e5;
    --ok:#5fbf85; --seam:#a99bf0; --lagged:#6fb2e0; --warming:#9aa0ae;
    --partial:#e0b45a; --degraded:#e08a4a; --loss:#e05c5c; --gap:#c04545;
    --unobserved:#343945;
    --surging:#ef6a5a; --elevated:#e0aa4a; --normal:#6fbf8a;
    --quiet:#7fa9cc; --degrading:#b48ad8;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; padding:24px; background:var(--bg); color:var(--ink);
  font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  /* Long operational strings (endpoint URLs, run ids) must never widen the
     page. `anywhere` breaks only when a break is actually required, unlike
     `break-all`, which happily splits ordinary numbers mid-digit. */
  overflow-wrap:anywhere;
}
.wrap { max-width:1180px; margin:0 auto; }

/* Grid and flex children default to min-width:auto, so a child with an
   intrinsic width (an SVG, a wide table) refuses to shrink and punches out
   of its box. This one line is the actual fix for the chart overflow. */
.grid > *, .panel { min-width:0; }

/* Charts size to their container and scale via viewBox. No fixed pixel
   width anywhere: a chart must never assume one viewport. */
svg { display:block; max-width:100%; overflow:hidden; }
.spark { width:100%; height:44px; }
.strip { width:100%; height:26px; }
h1 { font-size:20px; margin:0 0 2px; letter-spacing:.02em; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.09em;
     color:var(--muted); margin:28px 0 10px; font-weight:600; }
.sub { color:var(--muted); margin:0 0 18px; font-size:12.5px; }
.panel { background:var(--panel); border:1px solid var(--rule);
         border-radius:8px; padding:14px 16px; }
.grid { display:grid; gap:12px; }
/* minmax(Npx, 1fr) has a HARD floor of Npx: below that the track keeps its
   width and pushes the page sideways. min(Npx, 100%) lets the floor collapse
   to the container on narrow viewports while behaving identically above it. */
.g2 { grid-template-columns:repeat(auto-fit,minmax(min(330px,100%),1fr)); }
.g3 { grid-template-columns:repeat(auto-fit,minmax(min(232px,100%),1fr)); }
.kv { display:grid; grid-template-columns:auto 1fr; gap:3px 14px; }
.kv dt { color:var(--muted); }
.kv dd { margin:0; min-width:0; }
@media (max-width:560px) {
  /* Below this the label column starves the value column, and an endpoint
     URL wraps to one or two characters per line. Stack instead. */
  .kv { grid-template-columns:1fr; gap:0; }
  .kv dt { margin-top:9px; }
  body { padding:14px; }
}
.metric-name { font-size:12px; color:var(--muted); }
.metric-val { font-size:19px; font-weight:600; }
.metric-unit { font-size:11px; color:var(--muted); font-weight:400; }
table { border-collapse:collapse; width:100%; font-size:12.5px; }
th,td { text-align:left; padding:5px 9px; border-bottom:1px solid var(--rule); }
th { color:var(--muted); font-weight:600; text-transform:uppercase;
     font-size:10.5px; letter-spacing:.06em; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
/* Condition badges: outline pills, deliberately not filled alert chips.
   These are z-score cuts against a short trailing baseline, not calibrated
   alarms, and they should not read as authoritative. */
.pill { display:inline-block; padding:1px 8px; border-radius:9px;
        font-size:11px; line-height:1.55; border:1px solid currentColor;
        white-space:nowrap; letter-spacing:.03em; vertical-align:baseline; }
td .pill { min-width:5.6em; text-align:center; }
.note { color:var(--muted); font-size:11px; margin-top:3px; }
.legend { display:flex; flex-wrap:wrap; gap:12px; font-size:11.5px;
          color:var(--muted); margin-top:9px; }
.legend span { display:flex; align-items:center; gap:5px; }
.swatch { width:11px; height:11px; border-radius:2px; display:inline-block; }
/* Dense tables get a LOCAL horizontal scroller. The min-width keeps columns
   legible and makes the scroller actually engage, instead of the table
   squeezing itself into unreadable slivers or shoving the page sideways. */
.scroll { overflow-x:auto; }
.scroll table { min-width:32rem; }
.scroll.wide table { min-width:46rem; }
.ratio-expression { white-space:nowrap; font-variant-numeric:tabular-nums; }
footer { margin-top:34px; padding-top:14px; border-top:1px solid var(--rule);
         color:var(--muted); font-size:11.5px; }
.warn { border-left:3px solid var(--partial); padding-left:11px; }
.freshness { margin:12px 0 18px; border-left:4px solid var(--accent); }
.freshness strong { text-transform:uppercase; letter-spacing:.05em; }
.beef { text-align:center; padding:20px; color:var(--muted); }
.beef .big { font-size:17px; letter-spacing:.13em; color:var(--ink);
             opacity:.55; }
""" + _social_section.STYLE_ADDITION

HATCH_DEF = """
<defs>
  <pattern id="unobs" width="6" height="6" patternUnits="userSpaceOnUse"
           patternTransform="rotate(45)">
    <rect width="6" height="6" fill="var(--unobserved)" opacity="0.35"/>
    <line x1="0" y1="0" x2="0" y2="6" stroke="var(--unobserved)"
          stroke-width="3"/>
  </pattern>
</defs>
"""


#: Deliberately short and negative-first. An unfurl card is often ALL the
#: context a reader gets, it is cached by whoever unfurls it, and it is seen by
#: people who never open the page — so the denial has to be on the card, not
#: just behind the link.
SHARE_TITLE = "weatherwatch — platform weather"
SHARE_DESCRIPTION = (
    "Aggregate ATProto event rates and observation health, observed from one "
    "named Jetstream source. Does not measure conflict, sentiment, users, or "
    "content."
)
#: Static image, never regenerated with live figures: a share card outlives
#: the numbers on it, and a cached card showing stale rates would mislead.
SHARE_IMAGE = Path(__file__).resolve().parents[2] / "assets" / "og-card.png"


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _share_meta(public_url: str | None) -> str:
    """Open Graph / Twitter tags, emitted only when a canonical URL is given.

    Default is none at all: the page stays entirely self-contained for local
    viewing, and nothing advertises a location it may not be served from.
    These affect how a link someone deliberately shares renders; they do not
    make the page discoverable, and `noindex, nofollow, noarchive` still
    governs crawling.
    """
    if not public_url:
        return ""
    base = public_url.rstrip("/")
    return f"""
<meta name="description" content="{_esc(SHARE_DESCRIPTION)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="weatherwatch">
<meta property="og:title" content="{_esc(SHARE_TITLE)}">
<meta property="og:description" content="{_esc(SHARE_DESCRIPTION)}">
<meta property="og:url" content="{_esc(base + '/')}">
<meta property="og:image" content="{_esc(base + '/og-card.png')}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{_esc(SHARE_DESCRIPTION)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_esc(SHARE_TITLE)}">
<meta name="twitter:description" content="{_esc(SHARE_DESCRIPTION)}">
<meta name="twitter:image" content="{_esc(base + '/og-card.png')}">"""


def _fmt(v, digits=2, dash="—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return f"{v:,}"


def _iso(us: int | None) -> str:
    return timeutil.us_to_iso(us).replace("+00:00", "Z") if us else "—"


def _fmt_lag(v: float | None) -> str:
    """Format a lag reading, never presenting the clamp as a measurement.

    `health.record_event_time` clamps every sample to LAG_CLAMP_MAX_S before
    it reaches the EWMA, so a window that sat behind real time for an hour
    records exactly 600.000s — the same number as one that sat behind for ten
    minutes. Printing that as an exact value is a lie of precision, so a
    saturated reading renders as "≥600s" instead.
    """
    if v is None:
        return "—"
    if v >= health.LAG_CLAMP_MAX_S:
        return f"≥{health.LAG_CLAMP_MAX_S:.0f}s"
    return f"{v:,.3f}s"


def _freshness(health_points, generated_at: str, bucket_width: int) -> dict:
    """Classify the static view without ever turning missing data into calm."""
    generated = datetime.datetime.fromisoformat(
        generated_at.replace("Z", "+00:00"))
    observed = [point for point in health_points if point.observed]
    complete = [
        point for point in observed
        if point.observed_duration_us >= point.bucket_width * 1_000_000
        and not (point.flags & {
            query.FLAG_PARTIAL, query.FLAG_GAP, query.FLAG_LOSS,
            query.FLAG_DEGRADED,
        })
    ]
    newest_observed = max(
        observed, key=lambda point: point.bucket_start + point.bucket_width,
        default=None)
    newest_complete = max(
        complete, key=lambda point: point.bucket_start + point.bucket_width,
        default=None)
    allowance = 2 * PUBLICATION_INTERVAL_S + bucket_width

    if newest_observed is None:
        state = "unavailable"
        age_s = None
    else:
        observed_end = newest_observed.bucket_start + newest_observed.bucket_width
        age_s = max(0.0, generated.timestamp() - observed_end)
        if age_s > allowance:
            state = "stale"
        elif newest_complete is None or newest_complete is not newest_observed:
            state = "partial"
        else:
            state = "current"

    return {
        "state": state,
        "newest_observation_end": _iso(
            ((newest_observed.bucket_start + newest_observed.bucket_width)
             * 1_000_000) if newest_observed else None),
        "newest_complete_observation_end": _iso(
            ((newest_complete.bucket_start + newest_complete.bucket_width)
             * 1_000_000) if newest_complete else None),
        "age_seconds": age_s,
        "current_threshold_seconds": allowance,
        "basis": ("provisional: two 5-minute publication intervals plus one "
                  "source bucket"),
    }


def _freshness_panel(freshness: dict, first: int | None,
                     last: int | None) -> str:
    state = freshness["state"]
    meanings = {
        "current": "newest observed window is complete and within the refresh budget",
        "partial": "newest observation is incomplete; do not read it as current conditions",
        "stale": "newest observation is older than the refresh budget",
        "unavailable": "no observed window is available; unavailable is not calm",
    }
    return f"""<div class="panel freshness" data-freshness="{_esc(state)}">
<strong>{_esc(state)}</strong> — {_esc(meanings[state])}<br>
Report interval: {_esc(_iso(first * 1_000_000 if first is not None else None))}
→ {_esc(_iso(last * 1_000_000 if last is not None else None))} ·
newest complete observation: {_esc(freshness['newest_complete_observation_end'])} ·
newest observation: {_esc(freshness['newest_observation_end'])}
<div class="note">Freshness budget: {_fmt(freshness['current_threshold_seconds'], 0)}s
({_esc(freshness['basis'])}). This is a static report, never a live gauge.</div>
</div>"""


# --- SVG -------------------------------------------------------------------

def _sparkline(points: list[WindowPoint], width=300, height=44) -> str:
    """Rate over time. Unobserved windows break the line and are hatched.

    A hole in observation must be visually impossible to mistake for a run of
    zeros, so it gets both treatments: no line, plus a hatched band.

    `width`/`height` define the viewBox coordinate space only — they are NOT
    emitted as pixel attributes. The rendered size comes from CSS (`.spark`,
    width:100%), so the chart scales to whatever the card gives it. All
    geometry below is computed inside [0,width] x [0,height], and the SVG
    viewport clips anything that isn't.
    """
    if not points:
        return f'<svg class="spark" viewBox="0 0 {width} {height}"></svg>'

    n = len(points)
    step = width / max(n, 1)
    rates = [p.rate for p in points if p.rate is not None]
    top = max(rates) if rates else 1.0
    top = top if top > 0 else 1.0
    pad = 3

    def y(v: float) -> float:
        return height - pad - (v / top) * (height - 2 * pad)

    bands, segments, cur = [], [], []
    for i, p in enumerate(points):
        x = i * step
        if p.rate is None:
            bands.append(f'<rect x="{x:.1f}" y="0" width="{step:.2f}" '
                         f'height="{height}" fill="url(#unobs)"/>')
            if cur:
                segments.append(cur)
                cur = []
            continue
        if p.quality in ("degraded", "gap", "loss", "partial"):
            bands.append(f'<rect x="{x:.1f}" y="0" width="{step:.2f}" '
                         f'height="{height}" fill="{QUALITY_COLORS[p.quality]}" '
                         f'opacity="0.16"/>')
        cur.append((x + step / 2, y(p.rate)))
    if cur:
        segments.append(cur)

    paths = "".join(
        '<polyline fill="none" stroke="var(--accent)" stroke-width="1.6" '
        'stroke-linejoin="round" points="%s"/>'
        % " ".join(f"{px:.1f},{py:.1f}" for px, py in seg)
        for seg in segments if len(seg) > 1
    )
    dots = "".join(
        f'<circle cx="{seg[0][0]:.1f}" cy="{seg[0][1]:.1f}" r="1.7" '
        f'fill="var(--accent)"/>' for seg in segments if len(seg) == 1
    )
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img">'
            f'{HATCH_DEF}{"".join(bands)}{paths}{dots}</svg>')


def _health_strip(points: list[WindowPoint], width=1100, height=26) -> str:
    """One cell per window, coloured by quality. Gaps stay visible in place."""
    if not points:
        return ""
    n = len(points)
    step = width / n
    cells = []
    for i, p in enumerate(points):
        q = p.quality
        fill = "url(#unobs)" if q == "unobserved" else QUALITY_COLORS.get(q, "var(--ok)")
        when = _iso(p.bucket_start * 1_000_000)
        title = f"{when} — {q}: {QUALITY_HELP.get(q, '')}"
        cells.append(
            f'<rect x="{i * step:.2f}" y="0" width="{max(step - 0.5, 0.6):.2f}" '
            f'height="{height}" fill="{fill}"><title>{_esc(title)}</title></rect>'
        )
    return (f'<svg class="strip" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img">'
            f'{HATCH_DEF}{"".join(cells)}</svg>')


def _legend(keys) -> str:
    out = []
    for k in keys:
        sw = ('background:repeating-linear-gradient(45deg,var(--unobserved),'
              'var(--unobserved) 2px,transparent 2px,transparent 4px)'
              if k == "unobserved" else f"background:{QUALITY_COLORS[k]}")
        out.append(f'<span><i class="swatch" style="{sw}"></i>{_esc(k)}'
                   f'<span style="opacity:.65"> · {_esc(QUALITY_HELP[k])}</span></span>')
    return f'<div class="legend">{"".join(out)}</div>'


# --- sections --------------------------------------------------------------

def _section_status(runs, health_points, latest, metric_totals) -> str:
    lag_vals = [p.lag_ewma_s for p in health_points if p.lag_ewma_s is not None]
    lag_max = [p.lag_max_s for p in health_points if p.lag_max_s is not None]
    obs_s = sum(p.observed_seconds for p in health_points if p.observed)
    first = min((p.bucket_start for p in health_points), default=None)
    last = max((p.bucket_start + p.bucket_width for p in health_points), default=None)
    # Denominator is the SPAN of the reported interval, not the sum of window
    # widths. Consecutive runs can each hold a partial piece of the same
    # wall-clock minute (a clean shutdown commits a partial window; the next
    # run recounts the remainder under its own run_id), so summing widths
    # would count that minute twice and quietly overstate the interval.
    nominal = (last - first) if (first is not None and last is not None) else 0

    counts: dict[str, int] = {}
    for p in health_points:
        counts[p.quality] = counts.get(p.quality, 0) + 1

    rows = "".join(
        f"<tr><td>{_esc(r.run_id)}</td><td>{_esc(r.status)}</td>"
        f"<td>{_esc(r.started_at[:19])}</td>"
        f"<td>{_esc((r.ended_at or 'open')[:19])}</td>"
        f"<td class='num'>{_fmt(r.windows, 0)}</td>"
        f"<td class='num'>{_fmt(r.partial_windows, 0)}</td>"
        f"<td class='num'>{_fmt(r.reconnects, 0)}</td>"
        f"<td class='num'>{_fmt(r.gap_us / 1e6, 2)}</td>"
        f"<td class='num'>{_fmt(r.events, 0)}</td></tr>"
        for r in runs
    )

    quality_line = " · ".join(
        f"{_esc(k)} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )

    # Collection states are separate prospectively: an unsupported NSID is
    # deliberate scope (`untracked.collection`), while a missing/invalid value
    # is malformed input (`malformed.collection`). Historical databases may
    # contain the old ambiguous `unclassified.collection` key; retain it in the
    # displayed scope total and state the compatibility assumption explicitly.
    unknown_schema = sum(
        metric_totals.get(k, 0)
        for k in ("unclassified.operation", "unclassified.kind",
                  "malformed.commit", "malformed.collection")
    )
    legacy_untracked = metric_totals.get("unclassified.collection", 0)
    untracked = metric_totals.get("untracked.collection", 0) + legacy_untracked
    total_events = sum(r.events for r in runs)
    untracked_pct = (f" events ({100 * untracked / total_events:.2f}% of observed)"
                     if total_events else " events")
    legacy_note = (
        " Counted under the legacy <code>unclassified.collection</code> key, "
        "which would also capture a commit carrying no collection — a case not "
        "yet observed."
    ) if legacy_untracked else ""

    saturated = any(v >= health.LAG_CLAMP_MAX_S for v in lag_vals + lag_max)
    lag_note = (
        f'<div class="note">≥{health.LAG_CLAMP_MAX_S:.0f}s means the reading '
        f'hit the {health.LAG_CLAMP_MAX_S:.0f}s clamp — the true lag was that '
        f'or greater. Replaying backlog from a resumed cursor saturates this '
        f'while missing no events.</div>'
    ) if saturated else ""

    return f"""
<h2>A · Observation status</h2>
<div class="grid g2">
  <div class="panel">
    <dl class="kv">
      <dt>Observation source</dt><dd>{_esc(latest.endpoint)}</dd>
      <dt>Collector</dt><dd>v{_esc(latest.collector_version)}</dd>
      <dt>Runs in view</dt><dd>{len(runs)}</dd>
      <dt>Latest run</dt><dd>{_esc(latest.run_id)} ({_esc(latest.status)})</dd>
      <dt>Data interval</dt>
      <dd>{_esc(_iso(first * 1_000_000 if first else None))}<br>
          → {_esc(_iso(last * 1_000_000 if last else None))}</dd>
      <dt>Observed</dt>
      <dd>{_fmt(obs_s, 1)}s of {_fmt(nominal, 0)}s nominal
          ({_fmt(100 * obs_s / nominal if nominal else None, 1)}%)</dd>
    </dl>
  </div>
  <div class="panel">
    <dl class="kv">
      <dt>Windows</dt><dd>{len(health_points)} × {latest.bucket_width}s</dd>
      <dt>Window quality</dt><dd>{quality_line or '—'}</dd>
      <dt>Reconnects</dt><dd>{sum(r.reconnects for r in runs)}</dd>
      <dt>Seams</dt><dd>{sum(r.seam_windows for r in runs)} (reconstructed)</dd>
      <dt>Gaps</dt><dd>{_fmt(sum(r.gap_us for r in runs) / 1e6, 2)}s</dd>
      <dt>Lag (EWMA)</dt>
      <dd>med {_fmt_lag(sorted(lag_vals)[len(lag_vals) // 2] if lag_vals else None)} ·
          max {_fmt_lag(max(lag_max) if lag_max else None)}
          {lag_note}</dd>
      <dt>Ingest accounting</dt>
      <dd>parse errors {sum(r.parse_errors for r in runs)} ·
          no time_us {sum(r.rejected_no_time_us for r in runs)} ·
          unknown schema {_fmt(unknown_schema, 0)} ·
          late events {sum(r.late_events for r in runs)}
          <div class="note">Events the observer failed to account for: could
          not parse, could not place on the observation clock, could not
          understand, or arrived after its window had closed.</div></dd>
      <dt>Untracked collection</dt>
      <dd>{_fmt(untracked, 0)}{untracked_pct}
          <div class="note">Valid records from ATProto collections outside the
          tracked metric vocabulary. Deliberate scope, <strong>not</strong>
          loss — the observer read them and chose not to count them.{legacy_note}</div></dd>
    </dl>
  </div>
</div>
<div class="panel scroll wide" style="margin-top:12px">
<table><thead><tr><th>run</th><th>status</th><th>started</th><th>ended</th>
<th>win</th><th>partial</th><th>recon</th><th>gap s</th><th>events</th></tr></thead>
<tbody>{rows}</tbody></table>
</div>"""


def _section_weather(series_map: dict[str, Series]) -> str:
    cards = []
    for spec in PRIMITIVES:
        label = spec[0]
        s = _card_series(series_map, spec)
        if s is None or not s.observed_points:
            continue
        pts = list(s.points)
        help_text = CARD_HELP.get(label)
        title = f' title="{_esc(help_text)}"' if help_text else ""
        cards.append(f"""
<div class="panel">
  <div class="metric-name"{title}>{_esc(label)}</div>
  <div class="metric-val">{_fmt(s.mean_rate, 2)}
    <span class="metric-unit">/s mean · {_fmt(s.total, 0)} total</span></div>
  {_sparkline(pts)}
</div>""")
    return ("<h2>B · Activity weather</h2>"
            '<p class="sub">Each figure is the <strong>mean rate over the '
            'observed interval</strong>, not the current rate. The sparkline '
            'shows the per-window series it averages.</p>'
            f'<div class="grid g3">{"".join(cards)}</div>')


def _section_conditions(conn, run_ids, series_map, totals_series) -> str:
    width = totals_series.bucket_width
    rows = []
    for label, num, den in derive.STANDARD_RATIOS:
        a, b = series_map.get(num), series_map.get(den)
        if a is None or b is None:
            continue
        pts = derive.ratio_series(a, b)
        valued = [p for p in pts if p.value is not None]
        # Structural guard, not a caveat: an extreme drawn from a four-event
        # denominator is arithmetic wearing weather's clothes.
        eligible = [p for p in valued
                    if (p.denominator or 0) >= MIN_RATIO_DENOMINATOR]
        thin = len(valued) - len(eligible)
        overall = derive.ratio(a.total, b.total)
        low = min(eligible, key=lambda point: point.value) if eligible else None
        high = max(eligible, key=lambda point: point.value) if eligible else None

        def expression(numerator, denominator, value):
            if value is None:
                return "—"
            return (
                '<span class="ratio-expression">'
                f'{_fmt(numerator, 0)} {_esc(num)} / '
                f'{_fmt(denominator, 0)} {_esc(den)} = '
                f'<strong>{_fmt(value, 4)}</strong></span>'
            )

        rows.append(
            f"<tr><td>{_esc(label)}</td>"
            f"<td>{expression(a.total, b.total, overall)}</td>"
            f"<td>{expression(low.numerator, low.denominator, low.value) if low else '—'}</td>"
            f"<td>{expression(high.numerator, high.denominator, high.value) if high else '—'}</td>"
            f"<td class='num'>{len(eligible)}</td>"
            f"<td class='num'>{thin or ''}</td></tr>"
        )

    dep_rows = []
    for spec in PRIMITIVES:
        label = spec[0]
        s = _card_series(series_map, spec)
        if s is None or not s.observed_points:
            continue
        deps = derive.rolling_departures(s)
        last = next((d for d in reversed(deps) if d.value is not None), None)
        if last is None:
            continue
        colour = CONDITION_COLORS.get(last.condition, "var(--muted)")
        dep_rows.append(
            f"<tr><td>{_esc(label)}</td>"
            f"<td class='num'>{_fmt(last.value, 2)}</td>"
            f"<td class='num'>{_fmt(last.baseline_mean, 2)}</td>"
            f"<td class='num'>{_fmt(last.z, 2)}</td>"
            f"<td class='num'>{_fmt(100 * last.pct_change if last.pct_change is not None else None, 1)}%</td>"
            f"<td><span class='pill' style='color:{colour}'>"
            f"{_esc(last.condition)}</span></td>"
            f"<td class='num'>{last.baseline_n}</td></tr>"
        )

    total_dep = derive.rolling_departures(totals_series)
    tlast = next((d for d in reversed(total_dep) if d.value is not None), None)
    total_line = (
        f"All events {_fmt(tlast.value, 1)}/s · baseline {_fmt(tlast.baseline_mean, 1)}/s"
        f" · z {_fmt(tlast.z, 2)} · <span class='pill' "
        f"style='color:{CONDITION_COLORS.get(tlast.condition)}'>"
        f"{_esc(tlast.condition)}</span>"
        if tlast else "All events — insufficient baseline"
    )

    return f"""
<h2>C · Derived conditions</h2>
<div class="panel" style="margin-bottom:12px">{total_line}</div>
<div class="grid g2">
  <div class="panel scroll">
    <table><thead><tr><th>ratio</th><th>overall: numerator / denominator = ratio</th>
    <th>min window: numerator / denominator = ratio</th>
    <th>max window: numerator / denominator = ratio</th>
    <th>windows scored</th><th>windows too thin</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
  </div>
  <div class="panel scroll">
    <table><thead><tr><th>metric</th><th>latest /s</th><th>baseline</th><th>z</th>
    <th>Δ</th><th>condition</th><th>n</th></tr></thead>
    <tbody>{''.join(dep_rows)}</tbody></table>
  </div>
</div>
<p class="sub" style="margin-top:9px"><strong>latest /s</strong> is the most
recent <em>observed</em> {width}-second window — not an instantaneous reading,
not a live gauge, and not an average over the run. <strong>baseline</strong> is
the mean of the last {derive.DEFAULT_BASELINE_N} <em>eligible</em> windows
before it; partial, gapped, lossy and coverage-degraded windows are skipped
rather than filled.</p>
<p class="sub">Conditions are threshold cuts on that z-score (surging z≥3,
elevated z≥1.5, quiet z≤−1.5, degrading z≤−3; unknown below
{derive.MIN_BASELINE_SAMPLES} samples). They are not calibrated against
anything and carry no statistical warrant beyond “this window looked different
from the recent past”.</p>
<p class="sub">Every ratio is a two-body system: <em>a/b</em> can move because
<em>a</em> rose, because <em>b</em> fell, or because both did — and it means
little when <em>b</em> is small. The ratio is the hint; the primitive cards
above are the receipts, and each ratio's numerator and denominator appear
there as their own cards.</p>
    <p class="note"><strong>Extremes are drawn only from windows whose
    denominator reached {MIN_RATIO_DENOMINATOR}.</strong> A ratio is a
    two-body system: divide by four and it will happily report 22&times;,
    which is arithmetic rather than weather. Saying so in prose is not enough
    — a caveat does not travel with a screenshot — so the thin windows are
    excluded from the min and max columns and counted in the last one instead
    of being quietly dropped. <strong>{MIN_RATIO_DENOMINATOR} is a legibility
    floor, not a statistical one</strong>: no power calculation says a ratio
    becomes sound at {MIN_RATIO_DENOMINATOR} events, only that a single-digit
    denominator should not be allowed to set a headline.</p>"""


def _section_health_strip(health_points) -> str:
    present = []
    for p in health_points:
        if p.quality not in present:
            present.append(p.quality)
    order = [k for k in QUALITY_COLORS if k in present]
    return f"""
<h2>D · Observation health</h2>
<div class="panel">
  {_health_strip(health_points)}
  {_legend(order)}
  <p class="sub warn" style="margin:12px 0 0">Unobserved time is hatched and
  is <strong>not</strong> zero activity — it is time nobody was watching.
  Degraded and gapped windows are shaded where they happened; nothing is
  smoothed across them and nothing is interpolated.</p>
</div>"""


def _section_beef() -> str:
    return """
<h2>F · Beef conditions</h2>
<div class="panel beef">
  <div class="big">GLOBAL BEEF INDEX</div>
  <div style="margin-top:6px">undefined — calibration not assumed</div>
  <div style="margin-top:10px;font-size:11.5px;opacity:.75">
    No composite has been defined. Primitive conditions above remain
    authoritative.
  </div>
</div>"""


# --- assembly --------------------------------------------------------------

def _build_html(conn, run_ids, runs, latest, series_map, totals_series,
                health_points, metric_totals, generated_at,
                public_url, social_projection=None, freshness=None) -> str:
    first = min((point.bucket_start for point in health_points), default=None)
    last = max((point.bucket_start + point.bucket_width for point in health_points),
               default=None)
    freshness = freshness or _freshness(
        health_points, generated_at, latest.bucket_width)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>weatherwatch · platform weather</title>{_share_meta(public_url)}
<style>{STYLE}</style>
</head><body><div class="wrap">
<h1>weatherwatch · platform weather</h1>
<p class="sub warn"><strong>Despite the name, this does not measure conflict,
sentiment, users, or content.</strong> It counts how fast aggregate ATProto
events occur — posts, likes, follows, blocks, deletes — and how well it is
observing them. Public artifacts contain no account identifiers, no social
graph, read no post text, and detect no dispute. Disclosure-limited social
periods are not claimed anonymous. <em>Global Beef Index</em> is a joke name for
a composite that does not exist.</p>
{_freshness_panel(freshness, first, last)}
<p class="sub">Cortisol accounting for the ATProto firehose — event velocity,
not affect. Aggregate activity observed from
<strong>{_esc(latest.endpoint)}</strong> during the stated observation
interval. Counts describe what this endpoint delivered; they are not a claim
about the network's total activity, and no relay is authoritative or
complete.</p>
<p class="sub warn">This is measured, not hypothetical. A controlled probe
(2026-08-08) compared two same-region public Jetstream endpoints over one
interval and found their post volumes differing by <strong>~1.61&times;</strong>,
with a same-endpoint self-control of 1.000 — so relays are demonstrably not
interchangeable. Rates here are <strong>as observed at
{_esc(latest.endpoint)}</strong> and are not estimates of total-network
activity. That ratio is an <em>inter-observer comparison</em>, not a coverage
or completeness figure: neither observer has a canonical denominator.</p>
{_section_status(runs, health_points, latest, metric_totals)}
{_section_weather(series_map)}
{_section_conditions(conn, run_ids, series_map, totals_series)}
{_section_health_strip(health_points)}
{_social_section.render(social_projection) if social_projection else ""}
{_section_beef()}
<footer>
Generated {_esc(generated_at)} · collector v{_esc(COLLECTOR_VERSION)} ·
public artifacts contain no DIDs, handles, record keys, CIDs, event-supplied AT
URIs or text.
The bounded local edge custody stated above is not published. Monotonic stream
time is not evidence of complete observation.
</footer>
</div></body></html>"""


def _summary_json(runs, latest, series_map, totals_series, health_points,
                  generated_at, freshness=None) -> dict:
    first = min((p.bucket_start for p in health_points), default=None)
    last = max((p.bucket_start + p.bucket_width for p in health_points),
               default=None)
    span_s = (last - first) if (first is not None and last is not None) else 0
    observed_s = sum(p.observed_seconds for p in health_points if p.observed)
    freshness = freshness or _freshness(
        health_points, generated_at, latest.bucket_width)
    return {
        "interval": {
            "first_bucket_start": first,
            "last_bucket_end": last,
            "span_seconds": span_s,
            "observed_seconds": observed_s,
            "coverage_ratio": (observed_s / span_s) if span_s else None,
        },
        "generated_at": generated_at,
        "freshness": freshness,
        "collector_version": COLLECTOR_VERSION,
        "claim": ("Aggregate activity observed from this Jetstream source "
                  "during the stated observation interval."),
        "measures": ("rates of aggregate ATProto events (posts, likes, "
                     "follows, blocks, deletes, account/identity events) and "
                     "the health of the observation itself"),
        "does_not_measure": [
            "conflict or disputes", "sentiment or affect", "individual users",
            "post content or text", "the social graph", "any identity",
        ],
        "source_endpoint": latest.endpoint,
        "runs": [
            {
                "run_id": r.run_id, "status": r.status,
                "started_at": r.started_at, "ended_at": r.ended_at,
                "stop_reason": r.stop_reason, "windows": r.windows,
                "partial_windows": r.partial_windows,
                "degraded_windows": r.degraded_windows,
                "lagged_windows": r.lagged_windows,
                "seam_windows": r.seam_windows, "gap_us": r.gap_us,
                "reconnects": r.reconnects, "events": r.events,
                "observed_duration_us": r.observed_duration_us,
                "nominal_duration_us": r.nominal_duration_us,
                "replayed_from_cursor": r.replayed,
            } for r in runs
        ],
        "windows": [
            {"bucket_start": p.bucket_start, "quality": p.quality,
             "flags": sorted(p.flags), "events_seen": p.events_seen,
             "observed_duration_us": p.observed_duration_us}
            for p in health_points
        ],
        "metrics": {
            m: {
                "total": s.total,
                "mean_rate_per_s": s.mean_rate,
                "observed_seconds": s.observed_seconds,
                "observed_windows": len(s.observed_points),
                "unobserved_windows": len(s.points) - len(s.observed_points),
            } for m, s in series_map.items()
        },
        "total_events": {
            "total": totals_series.total,
            "mean_rate_per_s": totals_series.mean_rate,
        },
        "notes": [
            "Unobserved windows are omitted from every rate; nothing is "
            "interpolated across them.",
            "Rates divide by observed duration, not nominal window width.",
            "No relay is treated as complete or authoritative.",
            "Rates are as observed at this endpoint and are not estimates of "
            "total-network activity. A controlled probe (2026-08-08) compared "
            "two same-region public Jetstream endpoints over one interval and "
            "found post volumes differing by ~1.61x (same-endpoint "
            "self-control 1.000): relays are not interchangeable.",
            "That ratio is an inter-observer comparison, not a coverage or "
            "completeness figure: there is no canonical denominator.",
        ],
    }


def _load_social(conn, social_db, window_s: int | None = None) -> "_social_projection.SocialProjection":
    """Build the read model. The report never queries the episode store itself.

    A missing store, an empty one, or a detection run that never happened all
    resolve to an unavailable projection rather than an error: the section
    still renders, and still states the retention posture, because "nothing
    was detected" and "nothing was enabled" are different facts and a reader
    is entitled to both.
    """
    raw = db.get_meta(conn, _SOCIAL_RECEIPT_KEY)
    receipt = None
    if raw:
        try:
            receipt = json.loads(raw)
        except (TypeError, ValueError):
            receipt = None
    if social_db is None:
        return _social_projection.SocialProjection(
            audience=_social_projection.AUDIENCE_PUBLIC, available=False,
            reason="no episode store configured for this report",
            source={"audience": _social_projection.AUDIENCE_PUBLIC,
                    "detector_allowlist":
                        sorted(_social_projection.PUBLIC_DETECTORS),
                    # Stated even with nothing to apply it to: "no rows" and
                    # "no policy" are different facts.
                    "disclosure_policy":
                        _social_projection.public_disclosure_policy()},
            sink_receipt=receipt)
    since = None
    if window_s:
        since = timeutil.us_to_iso(timeutil.now_us() - window_s * 1_000_000)
    return _social_projection.load(
        social_db, audience=_social_projection.AUDIENCE_PUBLIC,
        since=since, sink_receipt=receipt)


def generate_report(
    conn: sqlite3.Connection,
    out_dir: str | Path,
    run_ids: list[str] | None = None,
    now: datetime.datetime | None = None,
    public_url: str | None = None,
    social_db: str | Path | None = None,
    social_window: str | None = "48h",
) -> dict:
    """Render the dashboard into `out_dir`, atomically.

    Without `run_ids`, uses the most recent compatible sequence of runs on the
    endpoint of the latest run — never a mixture of endpoints.
    """
    out_dir = Path(out_dir)
    generated_at = (now or timeutil.now_utc()).isoformat().replace("+00:00", "Z")

    if run_ids is None:
        latest_id = query.latest_run_id(conn)
        if latest_id is None:
            raise ValueError("no observation runs recorded")
        endpoint = query.run_summary(conn, latest_id).endpoint
        run_ids = query.compatible_runs(conn, endpoint)
    if not run_ids:
        raise ValueError("no compatible runs to report on")

    runs = [query.run_summary(conn, r) for r in run_ids]
    latest = max(runs, key=lambda r: r.started_at)
    health_points = query.observation_window_health(conn, run_ids)
    totals_series = query.total_events_series(
        conn, run_ids, max_points=REPORT_MAX_WINDOWS)

    series_map: dict[str, Series] = {}
    wanted = {k for spec in PRIMITIVES for k in _metric_keys(spec)}
    wanted |= {n for _, n, _ in derive.STANDARD_RATIOS}
    wanted |= {d for _, _, d in derive.STANDARD_RATIOS}
    for metric in sorted(wanted):
        series_map[metric] = query.series(
            conn, run_ids, metric, max_points=REPORT_MAX_WINDOWS)

    metric_totals = query.metric_totals(conn, run_ids)
    window_s = int(timeutil.parse_duration(social_window)) if social_window \
        else None
    social = _load_social(conn, social_db, window_s)
    freshness = _freshness(health_points, generated_at, latest.bucket_width)
    html_doc = _build_html(conn, run_ids, runs, latest, series_map,
                           totals_series, health_points, metric_totals,
                           generated_at, public_url, social, freshness)
    summary = _summary_json(runs, latest, series_map, totals_series,
                            health_points, generated_at, freshness)

    tmp = out_dir.parent / f".{out_dir.name}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    (tmp / "index.html").write_text(html_doc, encoding="utf-8")
    (tmp / "summary.json").write_text(json.dumps(summary, indent=2,
                                                 default=str), encoding="utf-8")
    # Static read side, beside the one that already exists. `build()` asserts
    # identity-freedom once more before these bytes are written.
    _social_api.write(social, tmp, generated_at=generated_at)
    if public_url and SHARE_IMAGE.exists():
        shutil.copy(SHARE_IMAGE, tmp / "og-card.png")

    # Atomic-ish swap: rename the old tree aside, move the new one in, then
    # delete. A reader sees either the whole old report or the whole new one.
    prev = out_dir.parent / f".{out_dir.name}.prev-{os.getpid()}"
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    if out_dir.exists():
        os.rename(out_dir, prev)
    try:
        os.rename(tmp, out_dir)
    except Exception:
        if prev.exists():
            os.rename(prev, out_dir)
        raise
    finally:
        if prev.exists():
            shutil.rmtree(prev, ignore_errors=True)

    return {
        "out_dir": str(out_dir),
        "runs": len(runs),
        "windows": len(health_points),
        "metrics": len(series_map),
        "html_bytes": len(html_doc.encode("utf-8")),
        "generated_at": generated_at,
        "social_episodes": len(social.episodes),
        "social_available": social.available,
        "social_sink_enabled": bool(
            (social.sink_receipt or {}).get("enabled")),
    }
