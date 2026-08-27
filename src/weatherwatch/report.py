"""Static Weather Watch observatory and finding publication.

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

from . import (COLLECTOR_VERSION, archive, db, derive, findings, health, query,
               timeutil)
from .query import Series, WindowPoint
from .social import section as _social_section
from .social import api as _social_api
from .social import projection as _social_projection
from .social import store as _social_store
from .social.config import RECEIPT_META_KEY as _SOCIAL_RECEIPT_KEY
from .social.field import conditions as _conditions
from .social.field import hero as _hero
from .social.field import observation as _field_obs

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
/* Typography carries most of the difference between an instrument and a
   console. Prose, labels and headings are set in the reader's UI face; the
   monospace is reserved for things that are literally machine text —
   endpoints, run ids, timestamps, metric keys — and for figures, where
   tabular numerals make columns line up. Setting the whole page in mono made
   every sentence look like log output, which is what a visitor then assumed
   it was. No webfont is loaded: this page makes no external requests. */
:root {
  --font-sans: system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",
               Arial,"Noto Sans",sans-serif;
  --font-serif: Georgia,"Times New Roman",Times,serif;
  --font-mono: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --bg:#f4f5f7; --panel:#ffffff; --ink:#15181e; --muted:#666d7c;
  --rule:#e0e3e9; --accent:#2f6094;
  --ok:#3f8f5f; --seam:#7a6fd0; --lagged:#4f8fbf; --warming:#8a8f9c;
  --partial:#c99a3a; --degraded:#c0632c; --loss:#b03a3a; --gap:#8d2b2b;
  --unobserved:#c9ccd4;
  --surging:#c0392b; --elevated:#c98a2c; --normal:#4a7f5c;
  --quiet:#5a7f9c; --degrading:#7b4fa0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0d0f13; --panel:#151920; --ink:#e7e9ef; --muted:#8d94a4;
    --rule:#242832; --accent:#7fb2e5;
    --ok:#5fbf85; --seam:#a99bf0; --lagged:#6fb2e0; --warming:#9aa0ae;
    --partial:#e0b45a; --degraded:#e08a4a; --loss:#e05c5c; --gap:#c04545;
    --unobserved:#343945;
    --surging:#ef6a5a; --elevated:#e0aa4a; --normal:#6fbf8a;
    --quiet:#7fa9cc; --degrading:#b48ad8;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; padding:26px 24px 40px; background:var(--bg); color:var(--ink);
  font:15px/1.6 var(--font-sans);
  -webkit-font-smoothing:antialiased;
  /* Long operational strings (endpoint URLs, run ids) must never widen the
     page. `anywhere` breaks only when a break is actually required, unlike
     `break-all`, which happily splits ordinary numbers mid-digit. */
  overflow-wrap:anywhere;
}
.wrap { max-width:1120px; margin:0 auto; }

/* Grid and flex children default to min-width:auto, so a child with an
   intrinsic width (an SVG, a wide table) refuses to shrink and punches out
   of its box. This one line is the actual fix for the chart overflow. */
.grid > *, .panel { min-width:0; }

/* Charts size to their container and scale via viewBox. No fixed pixel
   width anywhere: a chart must never assume one viewport. */
svg { display:block; max-width:100%; overflow:hidden; }
.spark { width:100%; height:44px; }
.strip { width:100%; height:26px; }

/* --- masthead ---------------------------------------------------------- */
.mast { border-bottom:2px solid var(--ink); padding-bottom:12px;
        margin-bottom:16px; }
h1 { font-size:26px; margin:0; letter-spacing:-.015em; font-weight:680;
     line-height:1.15; }
h1 .mark { color:var(--accent); }
.tagline { font-size:14.5px; color:var(--muted); margin:5px 0 0;
           max-width:62ch; }
.mast-grid { display:grid; gap:22px; align-items:start;
             grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr)); }
.scope { margin:0; padding:12px 15px; border:1px solid var(--rule);
         border-left:4px solid var(--partial); border-radius:0 6px 6px 0;
         background:var(--panel); font-size:13.5px; line-height:1.55;
         max-width:78ch; }
.scope strong { color:var(--ink); }

/* --- observatory front page -------------------------------------------- */
.mast.observatory { border:0; margin:0; padding:10px 0 26px; }
.brand-kicker { margin:0; color:var(--accent); font-size:12px; font-weight:760;
                letter-spacing:.15em; text-transform:uppercase; }
.brand-title { margin:5px 0 0; font:700 clamp(30px,5vw,52px)/1
               var(--font-serif); letter-spacing:-.035em; }
.brand-line { margin:10px 0 0; color:var(--muted); font-size:16px;
              line-height:1.45; }
.brand-line strong { color:var(--ink); font-weight:650; }
.brand-boundary { margin:5px 0 0; color:var(--ink); font-size:13px;
                  font-weight:680; letter-spacing:.015em; }

.finding-hero { border-top:2px solid var(--ink); border-bottom:1px solid var(--ink);
                padding:20px 0 26px; }
.section-eyebrow { display:flex; justify-content:space-between; gap:18px;
                   align-items:baseline; color:var(--muted); font-size:11px;
                   font-weight:730; letter-spacing:.11em; text-transform:uppercase; }
.finding-title { max-width:19ch; margin:17px 0 8px;
                 font:700 clamp(30px,5.2vw,58px)/1.02 var(--font-serif);
                 letter-spacing:-.035em; text-transform:uppercase; }
.finding-claim { max-width:64ch; margin:0; color:var(--muted);
                 font-size:17px; line-height:1.55; }
.finding-layout { display:grid; grid-template-columns:minmax(0,1fr) minmax(230px,.52fr);
                  gap:clamp(28px,6vw,78px); align-items:center; margin-top:26px; }
.observer-bars { display:grid; gap:13px; }
.observer-row { display:grid; grid-template-columns:minmax(145px,.7fr) minmax(120px,1fr) 3.2em;
                gap:12px; align-items:center; }
.observer-name { font:12px/1.25 var(--font-mono); color:var(--muted); }
.observer-track { height:13px; background:var(--rule); }
.observer-fill { display:block; height:100%; background:var(--accent); }
.observer-row:nth-child(2) .observer-fill { opacity:.68; }
.observer-relative { text-align:right; font:650 13px var(--font-mono); }
.finding-result { border-left:1px solid var(--rule); padding-left:28px; }
.finding-number { font:700 clamp(44px,7vw,78px)/.9 var(--font-serif);
                  letter-spacing:-.055em; }
.finding-number-label { margin-top:10px; color:var(--muted); font-size:12px;
                        letter-spacing:.08em; text-transform:uppercase; }
.control { margin-top:18px; font-size:13px; }
.finding-implication { max-width:68ch; margin:24px 0 0; font-size:18px;
                       line-height:1.45; }
.finding-actions, .text-links { display:flex; flex-wrap:wrap; gap:9px 18px;
                               margin-top:20px; }
.action { display:inline-block; padding:8px 13px; border:1px solid var(--ink);
          color:var(--ink); text-decoration:none; font-size:13px; font-weight:650; }
.action:hover { background:var(--ink); color:var(--panel); }
.action.secondary { border-color:var(--rule); color:var(--accent); }

.editorial-section { margin-top:42px; padding-top:18px; border-top:1px solid var(--ink); }
.editorial-heading { display:flex; justify-content:space-between; align-items:baseline;
                     gap:18px; margin-bottom:16px; }
.editorial-heading h2 { margin:0; color:var(--ink); font-size:13px; }
.conditioned-source { color:var(--muted); font-size:12px; text-align:right; }
.now-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
            border:1px solid var(--rule); background:var(--rule); gap:1px; }
.now-metric { background:var(--panel); padding:16px; min-width:0; }
.now-label { color:var(--muted); font-size:12px; }
.now-value { margin:4px 0 7px; font:650 clamp(25px,4vw,39px)/1 var(--font-mono);
             letter-spacing:-.045em; }
.now-unit { font:12px var(--font-sans); color:var(--muted); letter-spacing:0; }
.now-foot { color:var(--muted); font-size:11px; }
.status-line { display:flex; flex-wrap:wrap; gap:10px 22px; align-items:center;
               margin:13px 0; padding:11px 0; border-bottom:1px solid var(--rule); }
.status-item { display:flex; align-items:center; gap:8px; font-size:12px; }
.status-label { color:var(--muted); }
.status-chip { display:inline-block; border:1px solid currentColor; border-radius:999px;
               padding:2px 8px; font-size:10px; font-weight:780; letter-spacing:.08em; }
.status-present { color:var(--ok); }
.status-degraded, .status-refused { color:var(--degraded); }
.status-stale { color:var(--loss); }
.status-unknown, .status-absent { color:var(--muted); }

.finding-list { border-top:1px solid var(--rule); }
.finding-list a { display:grid; grid-template-columns:minmax(0,1fr) auto;
                  gap:18px; padding:14px 2px; border-bottom:1px solid var(--rule);
                  color:var(--ink); text-decoration:none; }
.finding-list a:hover .finding-list-title { color:var(--accent); }
.finding-list-title { font-family:var(--font-serif); font-size:17px; }
.finding-list-result { font:650 14px var(--font-mono); }
.how-read { max-width:78ch; font-size:18px; line-height:1.55; }
.how-read strong { font-weight:700; }

/* The permanent finding is a paper, not a second dashboard. */
.paper { max-width:850px; margin:0 auto; }
.paper-nav { margin-bottom:35px; font-size:13px; }
.paper-nav a { color:var(--accent); }
.paper-lead { font:22px/1.48 var(--font-serif); max-width:62ch; }
.paper-section { margin-top:38px; padding-top:18px; border-top:1px solid var(--rule); }
.paper-section h2 { margin:0 0 12px; color:var(--ink); font-size:12px; }
.paper-section p, .paper-section li { max-width:72ch; }
.paper-table td:first-child { font-family:var(--font-mono); }
.paper-caveat { border-left:3px solid var(--partial); padding:3px 0 3px 15px; }

@media (max-width:720px) {
  .finding-layout { grid-template-columns:1fr; }
  .finding-result { border-left:0; border-top:1px solid var(--rule);
                    padding:20px 0 0; }
  .observer-row { grid-template-columns:minmax(110px,.8fr) minmax(80px,1fr) 3em; }
  .now-grid { grid-template-columns:1fr; }
  .conditioned-source { text-align:left; }
  .editorial-heading { display:block; }
}

h2 { font-family:var(--font-sans); font-size:11.5px; text-transform:uppercase;
     letter-spacing:.11em; color:var(--muted); margin:30px 0 10px;
     font-weight:700; }
h3 { font-size:15px; margin:0 0 6px; font-weight:650; }
.sub { color:var(--muted); margin:0 0 16px; font-size:13px; max-width:78ch; }
.panel { background:var(--panel); border:1px solid var(--rule);
         border-radius:7px; padding:14px 16px; }
.grid { display:grid; gap:12px; }
/* minmax(Npx, 1fr) has a HARD floor of Npx: below that the track keeps its
   width and pushes the page sideways. min(Npx, 100%) lets the floor collapse
   to the container on narrow viewports while behaving identically above it. */
.g2 { grid-template-columns:repeat(auto-fit,minmax(min(330px,100%),1fr)); }
.g3 { grid-template-columns:repeat(auto-fit,minmax(min(232px,100%),1fr)); }
.kv { display:grid; grid-template-columns:auto 1fr; gap:3px 16px;
      font-size:13.5px; }
.kv dt { color:var(--muted); }
.kv dd { margin:0; min-width:0; }
@media (max-width:560px) {
  /* Below this the label column starves the value column, and an endpoint
     URL wraps to one or two characters per line. Stack instead. */
  .kv { grid-template-columns:1fr; gap:0; }
  .kv dt { margin-top:9px; }
  body { padding:16px 14px 30px; }
  h1 { font-size:21px; }
  /* The masthead must not push the finding off a phone screen. Labels and
     supporting scope text do not need desktop leading to remain legible. */
  .tagline { font-size:13px; }
  .scope { font-size:12.5px; line-height:1.5; padding:10px 12px; }
  .mast { padding-bottom:9px; margin-bottom:12px; }
  .mast-grid { gap:14px; }

}
/* Machine text and figures. Everything else is prose. */
.mono, code, .kv dd, td.num, .metric-val, .ratio-expression {
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.kv dd { font-size:12.8px; }
.metric-name { font-size:12.5px; color:var(--muted); font-family:var(--font-sans); }
.metric-val { font-size:20px; font-weight:600; letter-spacing:-.01em; }
.metric-unit { font-size:11px; color:var(--muted); font-weight:400;
               font-family:var(--font-sans); }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--rule); }
th { color:var(--muted); font-weight:650; text-transform:uppercase;
     font-size:10.5px; letter-spacing:.07em; font-family:var(--font-sans); }
td.num { text-align:right; }
/* Condition badges: outline pills, deliberately not filled alert chips.
   These are z-score cuts against a short trailing baseline, not calibrated
   alarms, and they should not read as authoritative. */
.pill { display:inline-block; padding:1px 8px; border-radius:9px;
        font-size:11px; line-height:1.55; border:1px solid currentColor;
        white-space:nowrap; letter-spacing:.03em; vertical-align:baseline;
        font-family:var(--font-sans); }
td .pill { min-width:5.6em; text-align:center; }
.note { color:var(--muted); font-size:12px; margin-top:5px; max-width:78ch; }
.legend { display:flex; flex-wrap:wrap; gap:12px; font-size:12px;
          color:var(--muted); margin-top:9px; }
.legend span { display:flex; align-items:center; gap:5px; }
.swatch { width:11px; height:11px; border-radius:2px; display:inline-block;
          flex:0 0 auto; }
/* Dense tables get a LOCAL horizontal scroller. The min-width keeps columns
   legible and makes the scroller actually engage, instead of the table
   squeezing itself into unreadable slivers or shoving the page sideways. */
/* A local scroller a thumb can find. `local` gradients scroll with the
   content and `scroll` ones stay put, so the shadow appears only on the side
   that actually has more table — six of these were on the page at 375px with
   nothing indicating any of them moved. */
.scroll {
  overflow-x:auto;
  background:
    linear-gradient(to right, var(--panel) 30%, transparent) left center/28px 100% no-repeat local,
    linear-gradient(to left, var(--panel) 30%, transparent) right center/28px 100% no-repeat local,
    radial-gradient(farthest-side at 0 50%, rgba(0,0,0,.22), transparent) left center/12px 100% no-repeat scroll,
    radial-gradient(farthest-side at 100% 50%, rgba(0,0,0,.22), transparent) right center/12px 100% no-repeat scroll;
  overscroll-behavior-x:contain;
}
.scroll table { min-width:32rem; }
.scroll.wide table { min-width:46rem; }
/* Four ratio expressions across, each `white-space:nowrap` by design so a
   numerator never detaches from its denominator. Side by side in a two-column
   grid these two tables starved their own label columns until
   `overflow-wrap:anywhere` shredded the word "ratio" down one character per
   line. They get the full width and a real local scroller instead. */
.scroll.wider table { min-width:70rem; }
/* Below this the ratio table is 1782px against a 313px viewport — a scroller
   is an affordance, not an excuse for a five-screen-wide table. Stack each
   row into a block and label the cells from the header, which keeps every
   numerator physically attached to its denominator (the one thing the ratio
   presentation must never lose) while removing the width entirely. */
@media (max-width:700px) {
  .scroll.wider.stackable, .scroll.wider.stackable table { min-width:0; }
  .scroll.stackable thead { position:absolute; width:1px; height:1px;
                     overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
  .scroll.stackable tr { display:block; padding:9px 0;
                  border-bottom:1px solid var(--rule); }
  .scroll.stackable td { display:grid; grid-template-columns:9.5em 1fr; gap:10px;
                  border:0; padding:3px 0; white-space:normal; }
  .scroll.stackable td::before { content:attr(data-label); color:var(--muted);
                          font-size:11.5px; text-transform:uppercase;
                          letter-spacing:.06em; font-family:var(--font-sans); }
  .scroll.stackable td.num { text-align:left; }
  .scroll.stackable .ratio-expression { white-space:normal; }
}
/* The label column has to be allowed to keep its word. Its siblings are
   `white-space:nowrap` by design, so they claim their full intrinsic width
   first and the label is left with whatever remains — and `overflow-wrap:
   anywhere` on the body then obligingly breaks "ratio" into six lines of one
   letter. The labels are short; let them be nowrap too and let the local
   scroller carry the extra width, which is what it is for. */
.scroll.wider th:first-child, .scroll.wider td:first-child,
.scroll.wide th:first-child, .scroll.wide td:first-child {
  white-space:nowrap; }
/* Same failure, other end of the table: with three nowrap ratio expressions
   claiming 1,343px of a 1,549px table, the two count columns were left 31px
   each and "43,056" came out as six lines of one character. Inside a table
   that already has its own scroller, a number and a column header both stay
   on one line and the scroller carries the width. */
.scroll th, .scroll td.num { white-space:nowrap; }
.ratio-expression { white-space:nowrap; }
footer { margin-top:38px; padding-top:14px; border-top:1px solid var(--rule);
         color:var(--muted); font-size:12px; }
.warn { border-left:3px solid var(--partial); padding-left:12px; }

/* --- station bar: freshness, loudly ------------------------------------ */
/* 160px, not 180px: the panel's own 1px borders take the content box to
   360px on a 390px viewport, and two 180px tracks plus the 1px gap need 361.
   The bar fell back to a single column and cost ~110px of vertical space
   above the reading. */
.station { display:grid; gap:1px; background:var(--rule);
           grid-template-columns:repeat(auto-fit,minmax(min(160px,100%),1fr));
           border:1px solid var(--rule); border-radius:7px; overflow:hidden;
           margin:0 0 8px; }
.station > div { background:var(--panel); padding:10px 14px; }
.station .k { font-size:10px; text-transform:uppercase; letter-spacing:.09em;
              color:var(--muted); font-weight:700; margin-bottom:3px; }
.station .v { font-size:13.5px; font-family:var(--font-mono);
              font-variant-numeric:tabular-nums; }
.station .state { font-size:15px; font-weight:700; font-family:var(--font-sans);
                  letter-spacing:.02em; text-transform:uppercase; }
.station .flag { box-shadow:inset 4px 0 0 var(--fresh-ink,var(--accent)); }
.fresh-current { --fresh-ink:var(--ok); }
.fresh-partial { --fresh-ink:var(--partial); }
.fresh-stale   { --fresh-ink:var(--degraded); }
.fresh-unavailable { --fresh-ink:var(--muted); }
.station .flag .state { color:var(--fresh-ink); }

/* --- receipts deck ----------------------------------------------------- */
.deck { margin:44px 0 0; padding-top:16px; border-top:2px solid var(--ink); }
/* An <h2> for the document outline, but not the small uppercase label the
   other h2s are: this one titles a deck, not a section. */
.deck-title { font-size:17px; font-weight:680; margin:0 0 4px;
              text-transform:none; letter-spacing:-.01em; color:var(--ink); }
details.rc { border:1px solid var(--rule); border-radius:7px;
             background:var(--panel); margin:0 0 10px; }
details.rc > summary { cursor:pointer; padding:11px 15px; font-size:13.5px;
                       list-style:none; display:flex; gap:10px;
                       align-items:baseline; flex-wrap:wrap; }
details.rc > summary::-webkit-details-marker { display:none; }
details.rc > summary::before { content:"\\25B8"; color:var(--muted);
                               flex:0 0 auto; }
details.rc[open] > summary::before { content:"\\25BE"; }
details.rc > summary:hover { color:var(--accent); }
details.rc > summary:focus-visible { outline:2px solid var(--accent);
                                     outline-offset:-2px; }
details.rc > summary h3 { display:inline; font-size:13.5px; font-weight:650;
                          margin:0; }
details.rc > summary .hint { color:var(--muted); font-size:12.5px; }
details.rc .body { padding:0 15px 15px; }
/* Keyboard focus must be visible everywhere, not only on summaries. */
a:focus-visible, summary:focus-visible, details:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px; }
@media (prefers-reduced-motion: reduce) {
  * { animation:none !important; transition:none !important; }
}

.beef { text-align:center; padding:22px; color:var(--muted); }
.beef .big { font-size:17px; letter-spacing:.14em; color:var(--ink);
             opacity:.5; font-weight:650; }
.freshness { margin:12px 0 18px; border-left:4px solid var(--accent); }
.freshness strong { text-transform:uppercase; letter-spacing:.05em; }

/* --- narrow viewports --------------------------------------------------
   Last in the sheet on purpose. An earlier version of this block sat above
   the station-bar and receipts rules it was trying to override, so at equal
   specificity the desktop values won and the type floor, the touch targets
   and the table stacking all silently did nothing while measuring as applied.
   Everything here is a measured failure at 320-430px, not a general pass. */
@media (max-width:560px) {
  /* Type floor: 71 elements measured under 12.5px, three of them at 10px.
     Small uppercase labels are the worst offenders, because the tracking
     makes them look larger than they read. */
  .note, .station .note { font-size:12px; }
  .station .k { font-size:11.5px; }
  .wx-pairs .hd, .wx-hist .t { font-size:11.5px; }
  th { font-size:11.5px; }
  .metric-name { font-size:12.5px; }
  .metric-unit { font-size:11.5px; }
  .kv { font-size:13px; }
  .kv dd { font-size:12.5px; }
  .legend { font-size:12px; }
  footer { font-size:12px; }
  details.rc > summary .hint { font-size:12px; }

  /* Two primitive cards per row rather than one: the 232px floor collapsed
     to a single column and made section B most of the page's height. A
     sparkline is still legible at ~150px. */
  /* 165px, not 150: two cards fit at 375px and above, and at 320px it falls
     back to one rather than squeezing two into 145px each, where the labels
     wrap enough to make the section TALLER than the single column it
     replaced. Measured both ways. */
  .g3 { grid-template-columns:repeat(auto-fit,minmax(min(165px,100%),1fr)); }

  /* 44px touch targets. The receipts summaries measured 33px. */
  details.rc > summary { padding:13px 15px; min-height:44px;
                         align-items:center; }
}
""" + _hero.STYLE + _social_section.STYLE_ADDITION

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
SHARE_TITLE = "Jetstream observers disagree: 1.61× — Weather Watch"
SHARE_DESCRIPTION = (
    "A controlled concurrent probe found 1.61× different post volumes from "
    "same-region public observers; self-control was 1.000×. Weather Watch "
    "does not measure conflict, sentiment, users, or content."
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


#: What the station bar says each freshness state means, in one clause a
#: visitor can act on. Deliberately parallel to `_freshness_panel`: the bar is
#: the same fact at the top of the page, not a second opinion.
FRESHNESS_SHORT = {
    "current": "the newest window is complete and recent",
    "partial": "the newest window is still filling — not current conditions",
    "stale": "no recent window; this page is behind the stream",
    "unavailable": "no observed window at all — which is not calm",
}


def _clock(iso: str) -> str:
    """`2026-08-25T19:52:00Z` -> `2026-08-25 19:52 UTC`."""
    if not iso or iso == "—":
        return "—"
    text = str(iso)
    if len(text) >= 16 and text[10] in "Tt":
        return f"{text[:10]} {text[11:16]} UTC"
    return text


def _station_bar(freshness: dict, latest, generated_at: str) -> str:
    """Freshness, at the top, in four cells nobody has to go looking for.

    The stale-query bug is why this is a band across the page rather than a
    line in the methodology: for a week the live page reported conditions from
    a fortnight earlier and nothing on it disagreed. A reader must be able to
    answer *is this now?* before reading anything else, and the four facts that
    answer it — what state the observation is in, when the last complete
    window closed, when the page was built, and how wide a window is — are the
    four cells here.

    Nothing here is called "live". The page is a static artifact published on
    a timer, and it says so.
    """
    state = freshness["state"]
    newest = freshness.get("newest_complete_observation_end") or "—"
    age = _hero.age_phrase(newest, generated_at) if newest != "—" else ""
    partial = " · a further window is still filling" if state == "partial" else ""
    return f"""<div class="station fresh-{_esc(state)}"
     data-freshness="{_esc(state)}" role="group" aria-label="observation freshness">
  <div class="flag"><div class="k">observation</div>
    <div class="state">{_esc(state)}</div>
    <div class="note" style="margin-top:2px">{_esc(FRESHNESS_SHORT[state])}</div></div>
  <div><div class="k">newest complete observation</div>
    <div class="v">{_esc(_clock(newest))}</div>
    <div class="note" style="margin-top:2px">{_esc(age.lstrip(" —") or "—")}
    {_esc(partial)}</div></div>
  <div><div class="k">this page was published</div>
    <div class="v">{_esc(_clock(generated_at))}</div>
    <div class="note" style="margin-top:2px">static artifact, rebuilt every
    {_fmt(PUBLICATION_INTERVAL_S // 60, 0)} minutes — never a live gauge</div></div>
  <div><div class="k">observation window</div>
    <div class="v">{_esc(latest.bucket_width)} s</div>
    <div class="note" style="margin-top:2px">every rate on this page is per
    window, not instantaneous</div></div>
</div>"""


def _receipt(title: str, hint: str, body: str, open_: bool = False,
             id_: str | None = None) -> str:
    """One collapsible section of the receipts deck.

    Progressive disclosure, not deletion: every figure that was on the page
    before is still on the page and still in the DOM, so a reader who wants
    the paperwork gets all of it and a reader who wants the weather is not
    made to scroll past it first.
    """
    # The title is a real heading, not a bold span. A reader navigating by
    # heading should be able to reach every section of the deck; `<h3>` inside
    # `<summary>` is valid and keeps the disclosure keyboard-operable.
    anchor = f' id="{_esc(id_)}"' if id_ else ""
    return (f'<details class="rc"{anchor}{" open" if open_ else ""}>'
            f'<summary><h3>{_esc(title)}</h3>'
            f'<span class="hint">{_esc(hint)}</span></summary>'
            f'<div class="body">{body}</div></details>')


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

#: Column budgets. Every chart is bounded BEFORE it is drawn, so page size is
#: a function of the chart rather than of how long the instrument has been
#: running.
#:
#: This is C4 in `docs/CANDIDATES.md`, observed rather than predicted: the live
#: page reached **11.5 MB** — 70,425 `<rect>` and 24,169 `<title>` elements —
#: because the health strip drew one mark per window and each of sixteen
#: sparklines drew one shading rect per bad window. Nothing was wrong with the
#: data; the chart simply had no ceiling. A reader on a phone downloaded eleven
#: megabytes to look at a strip 1,100 pixels wide.
#:
#: The collapse is deterministic (fixed column count, integer window→column
#: assignment, no sampling) and disclosed on the page.
STRIP_COLUMNS = 1100
SPARK_COLUMNS = 300

#: Worst-first. A column that collapses many windows takes the **worst**
#: quality among them, never the commonest and never the mean: a strip whose
#: job is to make gaps impossible to miss must not let a gap be outvoted by
#: its clean neighbours. `unobserved` outranks everything because "nobody was
#: watching" is the one state that must never be confusable with data.
QUALITY_SEVERITY = ("unobserved", "gap", "loss", "degraded", "partial",
                    "warming_up", "seam", "lagged", "clean")


def _worst(qualities) -> str:
    for q in QUALITY_SEVERITY:
        if q in qualities:
            return q
    return "clean"


class _Column:
    """One rendered column, summarising the windows that fall in it."""

    __slots__ = ("lo", "hi", "quality", "unobserved", "n", "start")

    def __init__(self, start):
        self.lo = None
        self.hi = None
        self.quality = []
        self.unobserved = False
        self.n = 0
        self.start = start


def _collapse(points, columns: int) -> tuple[list, int]:
    """Bin windows into at most `columns` columns, worst-case preserving.

    Returns `(columns, windows_per_column)`. Rates keep their min and max so a
    spike is never averaged away; quality keeps the worst so a fault is never
    smoothed over. Assignment is `index * columns // n`, which is integer and
    stable — the same store renders the same columns every time.

    Below the budget nothing is collapsed and every window is its own column,
    so short intervals are unaffected and the chart degrades to exactly what it
    drew before.
    """
    n = len(points)
    if n == 0:
        return [], 1
    width = max(1, -(-n // columns))          # ceil, so we never exceed budget
    out: list = []
    for i, p in enumerate(points):
        slot = i // width
        if slot >= len(out):
            out.append(_Column(p.bucket_start))
        col = out[slot]
        col.n += 1
        if p.rate is None:
            col.unobserved = True
        else:
            col.lo = p.rate if col.lo is None else min(col.lo, p.rate)
            col.hi = p.rate if col.hi is None else max(col.hi, p.rate)
        col.quality.append(p.quality)
    for col in out:
        col.quality = _worst(col.quality)
    return out, width


def _sparkline(points: list[WindowPoint], width=SPARK_COLUMNS,
               height=44, label: str = "") -> str:
    """Rate over time. Unobserved windows break the line and are hatched.

    A hole in observation must be visually impossible to mistake for a run of
    zeros, so it gets both treatments: no line, plus a hatched band.

    Above `SPARK_COLUMNS` windows the series is collapsed one column per
    rendered pixel and the column's **max** carries the line, with the min–max
    range drawn behind it as a band. Averaging would erase exactly the spikes
    a sparkline exists to show; taking the max alone would hide a collapse to
    zero. Both bounds are drawn, so neither is lost.

    `width`/`height` define the viewBox coordinate space only — they are NOT
    emitted as pixel attributes. The rendered size comes from CSS (`.spark`,
    width:100%), so the chart scales to whatever the card gives it. All
    geometry below is computed inside [0,width] x [0,height], and the SVG
    viewport clips anything that isn't.
    """
    if not points:
        return f'<svg class="spark" viewBox="0 0 {width} {height}"></svg>'

    cols, per = _collapse(points, width)
    n = len(cols)
    step = width / max(n, 1)
    tops = [c.hi for c in cols if c.hi is not None]
    top = max(tops) if tops else 1.0
    top = top if top > 0 else 1.0
    pad = 3

    def y(v: float) -> float:
        return height - pad - (v / top) * (height - 2 * pad)

    bands, segments, lows, cur, curlo = [], [], [], [], []
    for i, c in enumerate(cols):
        x = i * step
        if c.unobserved:
            bands.append(f'<rect x="{x:.1f}" y="0" width="{step:.2f}" '
                         f'height="{height}" fill="url(#unobs)"/>')
        if c.quality in ("degraded", "gap", "loss", "partial"):
            bands.append(f'<rect x="{x:.1f}" y="0" width="{step:.2f}" '
                         f'height="{height}" fill="{QUALITY_COLORS[c.quality]}" '
                         f'opacity="0.16"/>')
        if c.hi is None:
            if cur:
                segments.append(cur); lows.append(curlo)
                cur, curlo = [], []
            continue
        cur.append((x + step / 2, y(c.hi)))
        curlo.append((x + step / 2, y(c.lo)))
    if cur:
        segments.append(cur); lows.append(curlo)

    # min-max range, one filled path per unbroken segment
    ranges = "".join(
        '<polygon fill="var(--accent)" opacity="0.18" points="%s"/>'
        % " ".join(f"{px:.1f},{py:.1f}"
                   for px, py in list(seg) + list(reversed(lo)))
        for seg, lo in zip(segments, lows)
        if len(seg) > 1 and any(a[1] != b[1] for a, b in zip(seg, lo))
    ) if per > 1 else ""
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
    # A chart with `role="img"` and no name is announced as "image" and
    # nothing else. The label carries what the picture carries: what is
    # plotted, over how many windows, and its range.
    unobs = sum(1 for c in cols if c.unobserved)
    alt = (f"{label or 'rate'} per window over {len(points):,} windows, "
           f"ranging from {_fmt(min((c.lo for c in cols if c.lo is not None), default=None), 2)} "
           f"to {_fmt(top, 2)} events per second"
           + (f"; {unobs} column{'' if unobs == 1 else 's'} contain "
              f"unobserved windows, drawn hatched with the line broken"
              if unobs else "; every window in range was observed"))
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img" aria-label="{_esc(alt)}">'
            f'{HATCH_DEF}{"".join(bands)}{ranges}{paths}{dots}</svg>')


def _health_strip(points: list[WindowPoint],
                  width=STRIP_COLUMNS, height=26) -> tuple[str, int]:
    """One cell per column, coloured by quality. Gaps stay visible in place.

    Returns `(svg, windows_per_column)` so the caller can disclose the
    collapse rather than let the reader assume one cell is one window.

    A column carries the worst quality among its windows — see
    `QUALITY_SEVERITY`. That is the only collapse that keeps the strip's
    promise: unobserved time and faults stay visible at their real position,
    and the cost is that a column may look worse than most of its interval,
    which is the safe direction to be wrong in.
    """
    if not points:
        return "", 1
    cols, per = _collapse(points, width)
    n = len(cols)
    step = width / n
    cells = []
    for i, c in enumerate(cols):
        q = c.quality
        fill = "url(#unobs)" if q == "unobserved" else QUALITY_COLORS.get(q, "var(--ok)")
        when = _iso(c.start * 1_000_000)
        span = f" (worst of {c.n} windows)" if c.n > 1 else ""
        title = f"{when} — {q}: {QUALITY_HELP.get(q, '')}{span}"
        cells.append(
            f'<rect x="{i * step:.2f}" y="0" width="{max(step - 0.5, 0.6):.2f}" '
            f'height="{height}" fill="{fill}"><title>{_esc(title)}</title></rect>'
        )
    return (f'<svg class="strip" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img" aria-label="observation '
            f'quality for each of {n} columns across the reported interval">'
            f'{HATCH_DEF}{"".join(cells)}</svg>', per)


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

    return f"""<div class="grid g2">
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
        # Rendered, not hovered. These sentences say what the card does NOT
        # claim about a relationship, and a refusal that only exists in a
        # `title` attribute does not exist on a phone, for a screen reader, or
        # in a screenshot.
        help_text = CARD_HELP.get(label)
        note = f'<p class="note">{_esc(help_text)}</p>' if help_text else ""
        cards.append(f"""
<div class="panel">
  <div class="metric-name">{_esc(label)}</div>
  <div class="metric-val">{_fmt(s.mean_rate, 2)}
    <span class="metric-unit">/s mean · {_fmt(s.total, 0)} total</span></div>
  {_sparkline(pts, label=label)}
  {note}
</div>""")
    windows = max((len(_card_series(series_map, spec).points)
                   for spec in PRIMITIVES
                   if _card_series(series_map, spec) is not None), default=0)
    per = max(1, -(-windows // SPARK_COLUMNS)) if windows else 1
    collapse = (
        f'<p class="sub">Above {SPARK_COLUMNS:,} windows a sparkline is drawn '
        f'one column per rendered pixel; here each column spans <strong>{per} '
        f'windows</strong>, with the line following the column maximum and the '
        f'shaded range showing its minimum to maximum. Averaging would erase '
        f'the spikes a sparkline exists to show, so neither bound is dropped.</p>'
    ) if per > 1 else ""
    return ('<p class="sub">Each figure is the <strong>mean rate over the '
            'observed interval</strong>, not the current rate. The sparkline '
            'shows the per-window series it averages.</p>'
            + collapse
            + f'<div class="grid g3">{"".join(cards)}</div>')


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

        # `data-label` is what lets the row stack into a labelled block on a
        # narrow screen without a second markup path; on desktop it is inert.
        rows.append(
            f"<tr><td data-label='ratio'>{_esc(label)}</td>"
            f"<td data-label='overall'>{expression(a.total, b.total, overall)}</td>"
            f"<td data-label='min window'>{expression(low.numerator, low.denominator, low.value) if low else '—'}</td>"
            f"<td data-label='max window'>{expression(high.numerator, high.denominator, high.value) if high else '—'}</td>"
            f"<td class='num' data-label='windows scored'>{len(eligible)}</td>"
            f"<td class='num' data-label='windows too thin'>{thin or ''}</td></tr>"
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

    return f"""<div class="panel" style="margin-bottom:12px">{total_line}</div>
<div class="grid">
  <div class="panel scroll wider stackable">
    <table><thead><tr><th>ratio</th><th>overall: numerator / denominator = ratio</th>
    <th>min window: numerator / denominator = ratio</th>
    <th>max window: numerator / denominator = ratio</th>
    <th>windows scored</th><th>windows too thin</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
  </div>
  <div class="panel scroll wide">
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
    strip, per = _health_strip(health_points)
    collapse = (
        f'<p class="note">Each column spans <strong>{per} windows</strong> and '
        f'shows the <strong>worst</strong> quality among them — {len(health_points):,} '
        f'windows across {STRIP_COLUMNS} columns. A column therefore never looks '
        f'better than its interval, only worse: a fault outvoted by clean '
        f'neighbours would defeat the point of the strip.</p>'
    ) if per > 1 else ""
    return f"""<div class="panel">
  {strip}
  {_legend(order)}
  {collapse}
  <p class="sub warn" style="margin:12px 0 0">Unobserved time is hatched and
  is <strong>not</strong> zero activity — it is time nobody was watching.
  Degraded and gapped windows are shaded where they happened; nothing is
  smoothed across them and nothing is interpolated.</p>
</div>"""


def _section_beef() -> str:
    return """<div class="panel beef">
  <div class="big">GLOBAL BEEF INDEX</div>
  <div style="margin-top:6px">undefined — calibration not assumed</div>
  <div style="margin-top:10px;font-size:11.5px;opacity:.75">
    No composite has been defined. Primitive conditions above remain
    authoritative.
  </div>
</div>"""


# --- observatory front page -----------------------------------------------

FRESHNESS_STATUS = {
    "current": "PRESENT",
    "partial": "DEGRADED",
    "stale": "STALE",
    "unavailable": "UNKNOWN",
}


def _status_chip(state: str) -> str:
    css = state.lower().replace("_", "-")
    return (f'<span class="status-chip status-{_esc(css)}">'
            f'{_esc(state)}</span>')


def _quality_status(quality: str | None) -> str:
    if quality in {"clean", "seam", "lagged", "recovering"}:
        return "PRESENT"
    if quality in {"gap", "loss", "degraded", "partial"}:
        return "DEGRADED"
    return "UNKNOWN"


def _compact_rate(rate_per_s: float | None) -> str:
    if rate_per_s is None:
        return "—"
    per_minute = rate_per_s * 60
    if abs(per_minute) >= 1_000_000:
        return f"{per_minute / 1_000_000:.1f}m"
    if abs(per_minute) >= 1_000:
        return f"{per_minute / 1_000:.1f}k"
    if abs(per_minute) >= 100:
        return f"{per_minute:,.0f}"
    return f"{per_minute:,.1f}"


def _latest_point(series_map: dict[str, Series], metric: str) -> WindowPoint | None:
    series = series_map.get(metric)
    return series.points[-1] if series and series.points else None


def _finding_figure() -> str:
    finding = findings.observer_divergence()
    result = finding["result"]
    return f"""<div class="finding-layout" role="group"
 aria-label="Observed posts in one concurrent 120-second interval">
  <div class="observer-bars">
    <div class="observer-row">
      <span class="observer-name">jetstream1.us-east</span>
      <span class="observer-track"><span class="observer-fill" style="width:100%"></span></span>
      <span class="observer-relative">1.00</span>
    </div>
    <div class="observer-row">
      <span class="observer-name">jetstream2.us-east</span>
      <span class="observer-track"><span class="observer-fill" style="width:62.1%"></span></span>
      <span class="observer-relative">0.62</span>
    </div>
    <p class="note">7,088 vs 4,400 post events delivered. Same host, same
    120-second wall-clock interval; higher volume is not labelled truth.</p>
  </div>
  <div class="finding-result">
    <div class="finding-number">{_esc(result['display_ratio'])}</div>
    <div class="finding-number-label">observed difference</div>
    <div class="control">Same-observer control:
      <strong class="mono">{_esc(result['display_control_ratio'])}</strong></div>
  </div>
</div>"""


def _latest_finding() -> str:
    finding = findings.observer_divergence()
    slug = finding["slug"]
    return f"""<section class="finding-hero" id="latest-finding">
  <div class="section-eyebrow"><span>Latest finding</span><time>Aug 2026</time></div>
  <h2 class="finding-title">{_esc(finding['headline'])}</h2>
  <p class="finding-claim">{_esc(finding['claim'])}</p>
  {_finding_figure()}
  <p class="finding-implication">Your firehose study may not have a stable
  denominator: observed volume depended on observer choice in this probe.</p>
  <div class="finding-actions">
    <a class="action" href="findings/{_esc(slug)}/">Read the finding</a>
    <a class="action secondary" href="findings/{_esc(slug)}/#receipts">See the receipts</a>
  </div>
</section>"""


def _network_now(series_map: dict[str, Series], health_points,
                 latest, freshness: dict, generated_at: str) -> str:
    cards = []
    for label, metric in (("Posts", "post.create"),
                          ("Replies", "post.create.reply"),
                          ("Post deletes", "post.delete")):
        point = _latest_point(series_map, metric)
        points = list(series_map[metric].points[-1440:])
        quality = point.quality if point else "unobserved"
        value = _compact_rate(point.rate if point else None)
        cards.append(f"""<div class="now-metric">
  <div class="now-label">{_esc(label)}</div>
  <div class="now-value">{_esc(value)} <span class="now-unit">/ min</span></div>
  {_sparkline(points, label=f'{label} observed rate')}
  <div class="now-foot">latest source window · {_esc(quality)}</div>
</div>""")

    newest_health = health_points[-1] if health_points else None
    quality = newest_health.quality if newest_health else None
    observation_state = FRESHNESS_STATUS[freshness["state"]]
    coverage_state = _quality_status(quality)
    return f"""<section class="editorial-section" id="network-now">
  <div class="editorial-heading">
    <h2>Network weather — now</h2>
    <div class="conditioned-source">Observed at
      <span class="mono">{_esc(latest.endpoint)}</span> · not a network total</div>
  </div>
  <div class="now-grid">{''.join(cards)}</div>
  <div class="status-line">
    <div class="status-item"><span class="status-label">Observation</span>
      {_status_chip(observation_state)}</div>
    <div class="status-item"><span class="status-label">Coverage</span>
      {_status_chip(coverage_state)} <span>conditioned · latest window:
      {_esc(quality or 'unobserved')}</span></div>
  </div>
  {_station_bar(freshness, latest, generated_at)}
</section>"""


def _recent_findings() -> str:
    slug = findings.OBSERVER_DIVERGENCE_SLUG
    return f"""<section class="editorial-section" id="recent-findings">
  <div class="editorial-heading"><h2>Recent findings</h2></div>
  <div class="finding-list">
    <a href="findings/{slug}/"><span class="finding-list-title">Observer divergence</span>
      <span class="finding-list-result">1.61×</span></a>
    <a href="findings/{slug}/#continuity"><span class="finding-list-title">Reconnect monotonicity did not imply completeness</span>
      <span class="finding-list-result">0 decreasing</span></a>
    <a href="findings/{slug}/#cursor-resume"><span class="finding-list-title">Cursor T+1 produced exact continuation</span>
      <span class="finding-list-result">6/6 trials</span></a>
  </div>
</section>"""


def _how_to_read() -> str:
    slug = findings.OBSERVER_DIVERGENCE_SLUG
    return f"""<section class="editorial-section" id="how-to-read">
  <div class="editorial-heading"><h2>How to read this</h2></div>
  <p class="how-read"><strong>Weather Watch measures what its named observer
  saw.</strong> It does not claim that any observer sees “the network.” It
  counts aggregate ATProto events and keeps no people. It retains no raw
  events, publishes no account identifiers, keeps no social graph, reads no
  post text, detects no dispute, and retains no event-level comparison set.</p>
  <p class="how-read"><strong>Production observable. Consumption
  unobservable.</strong> The stream contains public writes, not reads,
  impressions, lurkers, private messages, reports, client-side mutes, actual
  audience, or attention. Activity is not engagement.</p>
  <p class="scope"><strong>It does not measure conflict, sentiment, users, or
  content.</strong> Disclosure-limited social periods are not claimed
  anonymous. <em>Global Beef Index</em> is a joke name for a composite that
  does not exist. Cortisol accounting for the ATProto firehose — event
  velocity, not affect.</p>
  <div class="text-links">
    <a href="findings/{slug}/#method">Method</a>
    <a href="#coverage">Coverage</a>
    <a href="#definitions">Definitions</a>
    <a href="#source">Source</a>
  </div>
</section>"""


def _finding_share_meta(public_url: str | None, finding: dict) -> str:
    if not public_url:
        return ""
    base = public_url.rstrip("/")
    url = f"{base}/findings/{finding['slug']}/"
    title = f"{finding['headline']} {finding['result']['display_ratio']} — Weather Watch"
    description = finding["claim"] + " " + finding["implication"]
    return f"""
<meta name="description" content="{_esc(description)}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Weather Watch">
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="{_esc(description)}">
<meta property="og:url" content="{_esc(url)}">
<meta property="og:image" content="{_esc(base + '/og-card.png')}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_esc(title)}">
<meta name="twitter:description" content="{_esc(description)}">"""


def _finding_page(public_url: str | None) -> str:
    finding = findings.observer_divergence()
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Jetstream observers disagree · Weather Watch</title>
{_finding_share_meta(public_url, finding)}
<style>{STYLE}</style>
</head><body><main class="paper">
<nav class="paper-nav"><a href="../../">← Weather Watch</a></nav>
<header>
  <div class="section-eyebrow"><span>Weather Watch finding</span><time>Aug 2026</time></div>
  <h1 class="finding-title">{_esc(finding['headline'])}</h1>
  <p class="paper-lead">{_esc(finding['claim'])}</p>
</header>
{_finding_figure()}
<p class="finding-implication">{_esc(finding['implication'])}</p>

<section class="paper-section" id="result"><h2>Result</h2>
<p>Over the concurrent interval, <span class="mono">jetstream1.us-east</span>
delivered <strong>7,088</strong> post events and each of two independent
<span class="mono">jetstream2.us-east</span> sockets delivered
<strong>4,400</strong>. The cross-observer ratio was
<strong>1.611</strong>; the two same-observer sockets agreed at
<strong>1.000</strong>.</p>
</section>

<section class="paper-section" id="method"><h2>Design and method</h2>
<p>On 2026-08-08, the probe opened four concurrent, post-filtered public
Jetstream connections from one host for the same 120-second wall-clock
interval. Counts lived only in memory and the probe wrote aggregate results;
it had no event writer, database, resolver, or identity-bearing comparison
set.</p>
<div class="scroll"><table class="paper-table"><thead><tr><th>observer</th>
<th>connections</th><th>posts observed</th></tr></thead><tbody>
<tr><td>jetstream2.us-east</td><td class="num">2</td><td class="num">4,400 each</td></tr>
<tr><td>jetstream1.us-east</td><td class="num">1</td><td class="num">7,088</td></tr>
<tr><td>jetstream1.us-west</td><td class="num">1</td><td class="num">6,928</td></tr>
</tbody></table></div>
</section>

<section class="paper-section" id="limitations"><h2>Limitations</h2>
<p class="paper-caveat"><strong>This does not establish coverage,
completeness, set inclusion, or which observer was closer to network
truth.</strong> There was no authoritative denominator. The higher-volume
stream may have been a superset, the sets may have partially overlapped, or
one observer may have been temporarily degraded.</p>
<p>Proving set equality would require retaining per-event identity across
observers. Weather Watch refuses that collection, correctly, and this finding
does not weaken the boundary. Equal aggregate counts are not evidence of
identical event sets.</p>
</section>

<section class="paper-section" id="continuity"><h2>Related instrument result: continuity</h2>
<p>A deliberately interrupted survey retained strictly monotonic
<span class="mono">time_us</span> across a known gap: zero timestamps
decreased. Monotonic cursor time therefore did <strong>not</strong> imply
complete observation. Weather Watch records gaps separately.</p>
</section>

<section class="paper-section" id="cursor-resume"><h2>Related instrument result: cursor resume</h2>
<p>Across 6/6 trials, resuming with <span class="mono">cursor=T+1</span>
produced exact continuation at the tested boundary. That supports the local
resume mechanism; it does not refresh a historical observation or establish
relay completeness.</p>
</section>

<section class="paper-section" id="receipts"><h2>Receipts and reproduction</h2>
<p>The public artifacts are aggregate and identity-free:</p>
<div class="text-links">
  <a href="finding.json">Finding record (JSON)</a>
  <a href="receipts/instances2.json">Aggregate probe receipt (JSON)</a>
</div>
<p>Repository reproduction record:
<span class="mono">python3 spike/m0_probe.py control</span>. The surrounding
analysis is in <span class="mono">M0-VERIFICATION-RESULTS.md</span> and
<span class="mono">docs/JETSTREAM-OBSERVER-DIVERGENCE.md</span>. Running the
probe contacts public infrastructure and produces a new observation; the
published receipt is not silently refreshed by later report generation.</p>
</section>

<footer>Finding <span class="mono">{_esc(finding['finding_id'])}</span> ·
aggregate counts only · no raw events · no account identifiers.</footer>
</main></body></html>"""


# --- assembly --------------------------------------------------------------

def _build_html(conn, run_ids, runs, latest, series_map, totals_series,
                health_points, metric_totals, generated_at,
                public_url, social_projection=None, freshness=None,
                conditions=None, field_obs=None, field_clim=None) -> str:
    """Assemble a finding-led observatory with its receipts underneath.

    The page answers, in order: what did we learn, what did this named observer
    see now, how conditioned is that observation, and where are the receipts.
    All of the prior diagnostic material remains available below that reading.
    """
    first = min((point.bucket_start for point in health_points), default=None)
    last = max((point.bucket_start + point.bucket_width for point in health_points),
               default=None)
    freshness = freshness or _freshness(
        health_points, generated_at, latest.bucket_width)
    conditions = conditions or {}
    field_obs = field_obs or []
    field_clim = field_clim or {}

    obs_s = sum(p.observed_seconds for p in health_points if p.observed)
    nominal = (last - first) if (first is not None and last is not None) else 0
    coverage = (100 * obs_s / nominal) if nominal else None
    quality_counts: dict[str, int] = {}
    for point in health_points:
        quality_counts[point.quality] = quality_counts.get(point.quality, 0) + 1
    dominant = max(quality_counts.items(), key=lambda kv: kv[1], default=("—", 0))

    total_dep = derive.rolling_departures(totals_series)
    tlast = next((d for d in reversed(total_dep) if d.value is not None), None)
    c_hint = (f"all events {_fmt(tlast.value, 1)}/s against a "
              f"{_fmt(tlast.baseline_mean, 1)}/s baseline · {tlast.condition}"
              if tlast else "baseline too short to compare against")

    episodes = len(social_projection.episodes) if social_projection else 0
    e_hint = (f"{episodes} disclosure-qualified period"
              f"{'' if episodes == 1 else 's'}"
              if social_projection and social_projection.available
              else "no episodes projected")

    history = _hero.recent_states(field_obs, field_clim) if field_obs else []
    reading = _hero.render(conditions, field_obs, field_clim, history=history,
                           generated_at=generated_at,
                           heading="Current conditions") if conditions else ""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Weather Watch · aggregate ATProto telemetry</title>{_share_meta(public_url)}
<style>{STYLE}</style>
</head><body><div class="wrap">

<header class="mast observatory">
  <p class="brand-kicker">Weather Watch</p>
  <h1 class="brand-title">Aggregate ATProto telemetry.</h1>
  <p class="brand-line">Counts the weather, <strong>keeps no people.</strong></p>
  <p class="brand-boundary">Production observable. Consumption unobservable.</p>
</header>

{_latest_finding()}

{_network_now(series_map, health_points, latest, freshness, generated_at)}
<p class="note">Observed from <span class="mono">{_esc(latest.endpoint)}</span>
over {_esc(_clock(_iso(first * 1_000_000 if first is not None else None)))}
→ {_esc(_clock(_iso(last * 1_000_000 if last is not None else None)))}.
Counts describe what this endpoint delivered; they are not a claim about the
network's total activity, and no relay is authoritative or complete.</p>

{reading}

{_recent_findings()}
{_how_to_read()}

<div class="deck" id="receipts">
<h2 class="deck-title">The receipts</h2>
<p class="sub">Everything the reading above is built from, and everything
needed to disbelieve it: the primitive rates, the ratios, the health of the
observation itself, and the run history. Nothing here has been removed from
the page — it has been folded, so that the weather does not arrive behind the
paperwork.</p>
<p class="sub warn">This is measured, not hypothetical. A controlled probe
(2026-08-08) compared two same-region public Jetstream endpoints over one
interval and found their post volumes differing by
<strong>~1.61&times;</strong>, with a same-endpoint self-control of 1.000 — so
relays are demonstrably not interchangeable. Rates here are <strong>as
observed at {_esc(latest.endpoint)}</strong> and are not estimates of
total-network activity. That ratio is an <em>inter-observer comparison</em>,
not a coverage or completeness figure: neither observer has a canonical
denominator.</p>

{_receipt("A · Observation status",
          f"{_fmt(coverage, 1)}% of the interval observed · "
          f"{len(runs)} run{'' if len(runs) == 1 else 's'} · "
          f"{len(health_points)} windows",
          _freshness_panel(freshness, first, last)
          + _section_status(runs, health_points, latest, metric_totals),
          id_="observation-status")}
{_receipt("B · Activity weather",
          "16 primitive event rates, each with its own per-window series",
          _section_weather(series_map), open_=True, id_="source")}
{_receipt("C · Derived conditions", c_hint,
          _section_conditions(conn, run_ids, series_map, totals_series),
          id_="definitions")}
{_receipt("D · Observation health",
          f"{len(health_points)} windows · mostly {_esc(dominant[0])}",
          _section_health_strip(health_points), open_=True, id_="coverage")}
{_receipt(_social_section.TITLE, e_hint,
          _social_section.render(social_projection))
 if social_projection else ""}
{_receipt("F · Beef conditions", "the composite that does not exist",
          _section_beef())}
</div>

<footer>
Generated <span class="mono">{_esc(generated_at)}</span> · collector
v{_esc(COLLECTOR_VERSION)} · public artifacts contain no DIDs, handles, record
keys, CIDs, event-supplied AT URIs or text.
The bounded local edge custody stated above is not published. Monotonic stream
time is not evidence of complete observation.
</footer>
</div></body></html>"""


def _summary_json(runs, latest, series_map, totals_series, health_points,
                  generated_at, freshness=None, conditions=None) -> dict:
    first = min((p.bucket_start for p in health_points), default=None)
    last = max((p.bucket_start + p.bucket_width for p in health_points),
               default=None)
    span_s = (last - first) if (first is not None and last is not None) else 0
    observed_s = sum(p.observed_seconds for p in health_points if p.observed)
    freshness = freshness or _freshness(
        health_points, generated_at, latest.bucket_width)
    published_finding = findings.observer_divergence()
    return {
        # `summary.json` carried no schema at all until v2, which left a
        # consumer no way to notice that `windows` had become bounded.
        "schema": archive.SCHEMA_SUMMARY,
        "interval": {
            "first_bucket_start": first,
            "last_bucket_end": last,
            "span_seconds": span_s,
            "observed_seconds": observed_s,
            "coverage_ratio": (observed_s / span_s) if span_s else None,
        },
        "generated_at": generated_at,
        "freshness": freshness,
        # The headline reading, in the same shape the page renders. A machine
        # reader should not have to scrape prose to learn the state, and it
        # must get the refusals with it -- `universal_not_observed`,
        # `cannot_see` and the per-measurement `pairings` all ride along, so
        # the limit cannot be dropped by consuming the JSON instead of the
        # page.
        "conditions": conditions or None,
        # A stable discovery link, not a claim that this historical finding
        # is a current observation. Its own publication month and status are
        # carried explicitly; report regeneration never refreshes them.
        "latest_finding": {
            "finding_id": published_finding["finding_id"],
            "slug": published_finding["slug"],
            "status": published_finding["status"],
            "published_month": published_finding["published_month"],
            "headline": published_finding["headline"],
            "display_result": published_finding["result"]["display_ratio"],
            "path": f"findings/{published_finding['slug']}/",
        },
        "collector_version": COLLECTOR_VERSION,
        "claim": ("Aggregate activity observed from this Jetstream source "
                  "during the stated observation interval."),
        "measures": ("rates of aggregate ATProto events (posts, likes, "
                     "follows, blocks, deletes, account/identity events) and "
                     "the health of the observation itself"),
        "does_not_measure": [
            "conflict or disputes", "sentiment or affect", "individual users",
            "post content or text", "the social graph", "any identity",
            "reads, impressions, or lurkers", "private messages or reports",
            "client-side mutes", "actual audience, attention, or engagement",
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
        # Bounded. The full set is split by `archive.partition` before this
        # document is written; see `generate_report`. Same fields, same
        # precision, fewer of them.
        "windows": [],
        "history": {},
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


def _load_conditions(social_db, generated_at: str) -> tuple[dict, list, dict]:
    """Current conditions, from the sealed field observations.

    The report does not *compute* the field. Sealing one content-addressed
    observation per window over a fortnight of minute windows is seconds of
    work, and the publisher runs every five minutes; the archive exists so
    that the page can be a reader of it. `weatherwatch social field` is the
    writer, on its own timer.

    Every failure resolves to **station offline with the reason attached**,
    never to calm and never to an exception. A page that renders a reassuring
    state because its data source was missing is the single worst thing this
    instrument could do, so the fallbacks all say what went wrong instead:
    no store configured, no baseline sealed yet, or nothing filed against it.
    """
    if social_db is None:
        return (_conditions.offline(
            "This report was generated without a field observation store, so "
            "no conditions can be read.").as_dict(), [], {})
    try:
        conn = _social_store.connect(social_db)
    except sqlite3.Error:
        return (_conditions.offline(
            "The field observation store could not be opened.").as_dict(),
            [], {})
    try:
        _field_obs.init(conn)
        clim = _field_obs.load_climatology(conn)
        if not clim:
            return (_conditions.offline(
                "No baseline has been sealed yet, so there is nothing to "
                "compare a reading against.").as_dict(), [], {})
        docs, _total = _field_obs.load_observations(
            conn, climatology_id=clim.get("climatology_id"))
        if not docs:
            return (_conditions.offline(
                "A baseline exists, but no observation has been filed against "
                "it.").as_dict(), [], clim)
    except sqlite3.Error:
        return (_conditions.offline(
            "The field observation store could not be read.").as_dict(),
            [], {})
    finally:
        conn.close()

    # `now` is what lets the instrument distinguish "the network went quiet"
    # from "the station stopped reporting" — identical readings, opposite
    # meanings. Without it a dead collector reads as weather.
    cond = _conditions.assess(docs, clim, now=generated_at).as_dict()
    cond["criteria_table"] = _conditions.criteria_table()
    return cond, docs, clim


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
    """Render the observatory and finding pages into `out_dir`, atomically.

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
    # Same budget as the metric series: the report asks for the whole
    # observed interval on purpose and discloses what it got.
    health_points = query.observation_window_health(
        conn, run_ids, max_points=REPORT_MAX_WINDOWS)
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
    conditions, field_obs, field_clim = _load_conditions(
        social_db, generated_at)
    freshness = _freshness(health_points, generated_at, latest.bucket_width)
    html_doc = _build_html(conn, run_ids, runs, latest, series_map,
                           totals_series, health_points, metric_totals,
                           generated_at, public_url, social, freshness,
                           conditions, field_obs, field_clim)
    summary = _summary_json(runs, latest, series_map, totals_series,
                            health_points, generated_at, freshness,
                            conditions)

    # Split the published windows before anything is written. Every window
    # lands on exactly one side, and the archive is written first: a summary
    # that points at day files which do not exist would be worse than the
    # unbounded artifact it replaces.
    all_windows = [
        {"bucket_start": p.bucket_start, "quality": p.quality,
         "flags": sorted(p.flags), "events_seen": p.events_seen,
         "observed_duration_us": p.observed_duration_us}
        for p in health_points
    ]
    recent, days = archive.partition(all_windows)
    summary["windows"] = recent

    tmp = out_dir.parent / f".{out_dir.name}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    (tmp / "index.html").write_text(html_doc, encoding="utf-8")
    finding_stats = findings.write_artifacts(tmp)
    finding_dir = tmp / "findings" / findings.OBSERVER_DIVERGENCE_SLUG
    (finding_dir / "index.html").write_text(
        _finding_page(public_url), encoding="utf-8")
    index = archive.write_archive(tmp, days, generated_at=generated_at,
                                  source_endpoint=latest.endpoint)
    archive.write_index(tmp, index)
    summary["history"] = archive.history_block(index, recent)
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
        "conditions_state": (conditions or {}).get("state"),
        "summary_windows": len(recent),
        "archived_windows": index["window_count"],
        "archive_days": index["day_count"],
        "archive_problems": len(index["problems"]),
        "findings": finding_stats["count"],
        "latest_finding_id": finding_stats["latest_finding_id"],
    }
