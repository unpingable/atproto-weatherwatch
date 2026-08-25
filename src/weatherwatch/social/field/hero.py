"""The conditions block: one instrument reading, rendered the same everywhere.

This is the answer to *what is happening right now*, and it is deliberately a
single builder with two consumers -- the public field page (`viz.render_public`)
and the canonical weather report (`report.py`). Two renderers for one reading
is how the criteria table and the page drifted apart before `criteria_table()`
was introduced to stop it, and a state word that looks different depending on
which URL you reached it through is a different claim, not a different theme.

Nothing here decides anything. Every state, sentence, rule, pairing and refusal
arrives already assembled from `conditions.assess`; this module chooses type
size and ink. Where it derives at all -- `recent_states` -- it derives by
calling `assess` again on a prefix of the same observations, so the history
strip cannot disagree with the headline about what a state means.

VISUAL RULES THAT ARE NOT DECORATION
------------------------------------
* **The refusal travels with the reading.** The universal non-claim sits in the
  same block as the state word, above the fold, at readable size -- not in a
  tooltip, a footer, or an expandable panel. A cropped screenshot of the state
  should carry its principal non-claim with it.
* **State is never encoded by colour alone.** Icon, word, and position in the
  published ladder all carry it; hue is the fourth channel, not the first.
* **A gap in the history strip stays a gap.** No reading is carried forward
  under a fresher label.
* **No geography.** The radar's axes are hour of day and ratio-to-typical,
  both of which the data actually has. See the module docstring in `viz.py`
  for the globe that was considered and rejected.
"""

from __future__ import annotations

import html
import math

from ... import timeutil
from . import conditions as cond_mod

#: Wall-clock offsets shown in the history strip, newest first. Chosen in
#: human units rather than window counts so the strip reads the same whether
#: the instrument is running 60-second or hourly windows.
HISTORY_OFFSETS_S = (0, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600)

HISTORY_LABELS = ("now", "1h ago", "3h ago", "6h ago", "12h ago", "24h ago")

#: The published states, in the order the criteria are evaluated, minus the two
#: null states -- a ladder is a statement about measured conditions, and
#: "cannot tell" does not sit on it. Rendered as a legend with the current
#: state marked, which is the weather-warning idiom: a hurricane category
#: chart prints every category and marks the one in force.
#:
#: Deliberately NOT numbered. This estate has refused composite severity
#: indices, and a ladder that prints "4 of 6" is an index with extra steps.
#: The ladder shows position among *named* states whose criteria are published
#: immediately below it; it never reduces them to a scalar.
LADDER = (cond_mod.CALM, cond_mod.ACTIVE, cond_mod.UNSETTLED,
          cond_mod.TURBULENT, cond_mod.STORM, cond_mod.SEVERE)

#: The tokens the conditions block brings with it.
#:
#: A consuming page must already define `--bg --panel --ink --muted --rule
#: --accent --font-sans --font-mono`; everything the *reading* needs is
#: defined here so that a state word cannot be one colour on the report and
#: another on the field page. Colour here encodes measured magnitude and
#: nothing else -- it is not a moral scale, and "storm" is not "bad people".
TOKENS = """
:root{
  --rule-strong:#c3c8d4; --sunk:#f0f1f4; --grid:#e8eaf0; --cloud:#8aa0b8;
  --mark:#c0632c;
  /* Measured against the light panel: every state word clears 4.5:1, and
     because the ladder inverts one cell (panel ink on the state colour) the
     same figure has to hold both ways round. #b07d23 and #c0632c did not --
     3.62 and 4.15 -- so they are darkened here and only here; the dark theme
     already cleared 5.7:1 everywhere. */
  --s-calm:#3f7d57; --s-active:#3b6ea5; --s-unsettled:#7b6bb5;
  --s-turbulent:#996d1e; --s-storm:#b45d29; --s-severe:#b03a3a;
}
@media (prefers-color-scheme:dark){:root{
  --rule-strong:#3a4150; --sunk:#12151b; --grid:#20242e; --cloud:#5c708a;
  --mark:#e08a4a;
  --s-calm:#6fbf8a; --s-active:#7fb2e5; --s-unsettled:#b09ae8;
  --s-turbulent:#e0b45a; --s-storm:#e08a4a; --s-severe:#ef6a5a;
}}
"""

STYLE = TOKENS + """
/* --- conditions block ---------------------------------------------------
   Shared verbatim by the public field page and the canonical report, so one
   reading cannot acquire two appearances. */
.wx-sub { font-family:var(--font-sans); font-size:11px; font-weight:650;
  text-transform:uppercase; letter-spacing:.1em; color:var(--muted);
  margin:26px 0 9px; }
.mono { font-family:var(--font-mono); font-size:.94em;
  font-variant-numeric:tabular-nums; }
/* The radar sizes to its container and scales by viewBox: no pixel width
   anywhere, or a grid child with min-width:auto refuses to shrink and the
   chart paints outside its card. */
.radar { width:100%; height:auto; }
table.crit td { font-family:var(--font-sans); font-size:12.5px; }
details.wx-crit { margin:14px 0 0; }
details.wx-crit > summary { cursor:pointer; color:var(--accent);
  font-family:var(--font-sans); font-size:13px; padding:6px 0; }
.wx { --wx-ink: var(--ink); }
.wx-head { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,340px);
  gap:26px; align-items:start; }
@media (max-width:820px){ .wx-head { grid-template-columns:1fr; gap:18px; } }
/* A state with no baseline has no dial to draw, and a reserved-but-empty
   340px column makes the panel look broken rather than honest. */
.wx-head-solo { grid-template-columns:1fr; }
.wx-state { display:flex; align-items:baseline; gap:14px; margin:0 0 10px;
  flex-wrap:wrap; }
.wx-ico { font-size:44px; line-height:1;
  font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif; }
.wx-label { font-family:var(--font-sans); font-size:40px; line-height:1.05;
  font-weight:650; letter-spacing:-0.02em; color:var(--wx-ink); margin:0; }
@media (max-width:560px){ .wx-label{font-size:31px} .wx-ico{font-size:34px} }
.wx-sentence { font-family:var(--font-sans); font-size:17px; line-height:1.45;
  margin:0 0 12px; color:var(--ink); }
.wx-plain { font-family:var(--font-sans); font-size:14.5px; line-height:1.6;
  margin:0 0 14px; color:var(--muted); max-width:54ch; }
/* The refusal. Same block, same fold, readable size -- see module docstring. */
.wx-nobs { font-family:var(--font-sans); font-size:13.5px; line-height:1.55;
  margin:0 0 12px; padding:10px 14px; border-left:3px solid var(--rule-strong);
  background:var(--sunk); border-radius:0 5px 5px 0; max-width:60ch;
  /* Explicit: the refusal must never pick up the state hue from the panel.
     It is not part of the reading's magnitude and must not read as alarm. */
  color:var(--ink); }
.wx-nobs b { font-family:var(--font-sans); letter-spacing:.07em;
  text-transform:uppercase; font-size:10.5px; color:var(--muted);
  display:block; margin-bottom:3px; font-weight:650; }
.wx-conf { font-family:var(--font-sans); font-size:12.5px; color:var(--muted);
  margin:0 0 4px; max-width:60ch; }
/* State colours. All eight states, including the two that had no rule and
   silently fell back to body ink. */
.st-calm{--wx-ink:var(--s-calm)} .st-active{--wx-ink:var(--s-active)}
.st-unsettled{--wx-ink:var(--s-unsettled)} .st-turbulent{--wx-ink:var(--s-turbulent)}
.st-storm{--wx-ink:var(--s-storm)} .st-severe_storm{--wx-ink:var(--s-severe)}
.st-unavailable{--wx-ink:var(--muted)} .st-station_offline{--wx-ink:var(--muted)}

/* --- ladder ------------------------------------------------------------ */
.wx-ladder { display:flex; flex-wrap:wrap; gap:0; margin:16px 0 0;
  border:1px solid var(--rule); border-radius:6px; overflow:hidden; }
.wx-ladder span { font-family:var(--font-sans); font-size:11px; padding:5px 10px;
  color:var(--muted); flex:1 1 auto; text-align:center; white-space:nowrap;
  border-right:1px solid var(--rule); letter-spacing:.02em; }
.wx-ladder span:last-child { border-right:0; }
.wx-ladder span.on { color:var(--panel); background:var(--wx-ink);
  font-weight:650; }
.wx-ladder-note { font-family:var(--font-sans); font-size:11px;
  color:var(--muted); margin:6px 0 0; }

/* --- history strip ----------------------------------------------------- */
.wx-hist { display:grid; gap:1px; background:var(--rule);
  grid-template-columns:repeat(auto-fit,minmax(min(112px,100%),1fr));
  border:1px solid var(--rule); border-radius:6px; overflow:hidden;
  margin:0; }
.wx-hist div { background:var(--panel); padding:9px 11px; min-width:0; }
.wx-hist .t { font-family:var(--font-sans); font-size:10.5px;
  text-transform:uppercase; letter-spacing:.07em; color:var(--muted);
  margin-bottom:3px; }
.wx-hist .s { font-family:var(--font-sans); font-size:13px; font-weight:600;
  display:flex; align-items:center; gap:6px; }
.wx-hist .s i { font-style:normal; font-size:15px;
  font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif; }
.wx-hist .none { color:var(--muted); font-weight:400; }

/* --- observed / not observed ------------------------------------------- */
.wx-pairs { display:grid; grid-template-columns:1fr 1fr; gap:1px;
  background:var(--rule); border:1px solid var(--rule); border-radius:6px;
  overflow:hidden; margin:2px 0 0; }
@media (max-width:720px){ .wx-pairs { grid-template-columns:1fr; } }
.wx-pairs > div { background:var(--panel); padding:11px 14px;
  font-family:var(--font-sans); font-size:13.5px; line-height:1.5; }
.wx-pairs .hd { font-size:10.5px; text-transform:uppercase; letter-spacing:.08em;
  color:var(--muted); font-weight:650; padding-bottom:6px; padding-top:8px; }
.wx-pairs .yes { box-shadow:inset 3px 0 0 var(--s-calm); }
.wx-pairs .no { box-shadow:inset 3px 0 0 var(--rule-strong); color:var(--muted); }
/* On one column the two headers must stay attached to their own cells. */
@media (max-width:720px){
  .wx-pairs .hd { padding-bottom:2px; }
}
.wx-rule { font-family:var(--font-sans); font-size:12px; color:var(--muted);
  margin:10px 0 0; }
.wx-cant { font-family:var(--font-sans); font-size:13px; margin:8px 0 0;
  padding-left:20px; color:var(--muted); }
.wx-cant li { margin:5px 0; }
.wx-radar-note { font-family:var(--font-sans); font-size:11.5px;
  color:var(--muted); text-align:center; margin:6px auto 0; max-width:30em;
  line-height:1.5; }
.wx-radar { width:100%; max-width:340px; margin:0 auto; }
"""


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _clock(iso: str) -> str:
    """`2026-08-25T19:52:00+00:00` -> `2026-08-25 19:52 UTC`.

    A raw ISO stamp is a machine's answer to "when". The machine-readable
    answer stays in `summary.json`; a person reading the page needs to see the
    clock without parsing an offset.
    """
    if not iso:
        return "—"
    text = str(iso)
    if len(text) >= 16 and text[10] in "Tt":
        return f"{text[:10]} {text[11:16]} UTC"
    return text


def age_phrase(iso: str, reference: str | None) -> str:
    """How old a reading was **at publication**, phrased so it stays true.

    "12 minutes ago" is a lie the moment the page is cached, and this is a
    static artifact that outlives its own generation. Anchoring the age to the
    publication instant instead of to the reader's clock keeps the sentence
    correct forever, and the freshness panel is where a reader learns whether
    the publication itself is recent.
    """
    then, now = timeutil.to_epoch(iso or ""), timeutil.to_epoch(reference or "")
    if then is None or now is None:
        return ""
    delta = now - then
    if delta < 0:
        return ""
    if delta < 90:
        span = f"{delta:.0f} seconds"
    elif delta < 5400:
        span = f"{delta / 60:.0f} minutes"
    elif delta < 172800:
        span = f"{delta / 3600:.1f} hours"
    else:
        span = f"{delta / 86400:.1f} days"
    return f" — {span} before this page was published"


# --- history ---------------------------------------------------------------

def _window_seconds(doc: dict) -> int:
    raw = str(doc.get("window", "60s")).rstrip("s")
    try:
        return max(int(float(raw)), 1)
    except ValueError:
        return 60


def recent_states(observations: list, clim: dict,
                  offsets_s: tuple = HISTORY_OFFSETS_S) -> list:
    """The state at each past offset, as the instrument would have called it.

    History, not prediction and not smoothing. Each entry re-runs the *same*
    `assess` over the observations that existed up to that moment, so the
    strip cannot disagree with the headline about what "Turbulent" means.

    `now` is deliberately not passed: staleness is a fact about the present,
    and an archived window is not "offline" merely because time has moved on.

    A reading is shown for an offset only when an eligible window ends within
    one window-width of it. Otherwise the entry reports **no observation** --
    carrying the last known reading forward under a fresher label is exactly
    the interpolation that "unavailable is not calm" exists to refuse.
    """
    if not observations or not clim:
        return []
    ends = [timeutil.to_epoch(o.get("ts_end", "")) for o in observations]
    newest = next((e for e in reversed(ends) if e is not None), None)
    if newest is None:
        return []
    width = _window_seconds(observations[-1])

    out = []
    for offset, label in zip(offsets_s, HISTORY_LABELS):
        target = newest - offset
        cut = -1
        for i, end in enumerate(ends):
            if end is not None and end <= target + 0.5:
                cut = i
            else:
                break
        if cut < 0:
            out.append({"label": label, "state": None, "as_of": None})
            continue
        cond = cond_mod.assess(observations[:cut + 1], clim)
        as_of = timeutil.to_epoch(cond.as_of) if cond.as_of else None
        # The reading must come from a window that actually sits at this
        # offset, not from the last one before a gap.
        if as_of is None or (target - as_of) > width:
            out.append({"label": label, "state": None, "as_of": None})
            continue
        out.append({"label": label, "state": cond.state, "icon": cond.icon,
                    "name": cond.label, "as_of": cond.as_of})
    return out


def _history_strip(entries: list) -> str:
    if not entries:
        return ""
    cells = []
    for e in entries:
        if e["state"] is None:
            cells.append(
                f'<div><div class="t">{_esc(e["label"])}</div>'
                f'<div class="s none">no observation</div></div>')
            continue
        cells.append(
            f'<div><div class="t">{_esc(e["label"])}</div>'
            f'<div class="s st-{_esc(e["state"])}" style="color:var(--wx-ink)">'
            f'<i role="img" aria-hidden="true">{_esc(e["icon"])}</i>'
            f'{_esc(e["name"])}</div></div>')
    return (f'<div class="wx-hist">{"".join(cells)}</div>')


# --- ladder ----------------------------------------------------------------

def _ladder(state: str) -> str:
    """Every published measurable state, with the one in force marked.

    Doubles as a legend and as the non-colour encoding of severity: a reader
    who cannot separate the hues still sees which cell is filled and reads the
    word inside it.
    """
    cells = []
    for s in LADDER:
        on = " on" if s == state else ""
        # Built outside the f-string. An escaped quote inside an f-string
        # expression is a SyntaxError before Python 3.12, and the serving host
        # runs 3.10 -- see `spike/check_py310_fstrings.py`, which exists
        # because this exact line went red in CI.
        current = ' aria-current="true"' if s == state else ""
        cells.append(f'<span class="{("st-" + s + on).strip()}"{current}>'
                     f'{_esc(cond_mod.STATE_LABEL[s])}</span>')
    note = ("The state in force, among the published states. Two further "
            "states — conditions unavailable and station offline — are not on "
            "this ladder: neither is a reading about the network.")
    if state not in LADDER:
        note = ("No state on this ladder is in force: the instrument is "
                "reporting " + cond_mod.STATE_LABEL.get(state, state).lower()
                + ", which is a fact about the instrument rather than about "
                  "the network.")
    return (f'<div class="wx-ladder" role="group" aria-label="published '
            f'condition states">{"".join(cells)}</div>'
            f'<p class="wx-ladder-note">{_esc(note)}</p>')


# --- observed / not observed ----------------------------------------------

def pairs_table(cond: dict) -> str:
    """Two columns: what was measured, and what it does not license.

    Not a disclaimer block, and not a second panel further down the page. A
    reader given "interaction activity is 4.6x typical" supplies "people must
    be angry" themselves unless the instrument says otherwise in the same
    breath, so the limit sits in the same row as the measurement.
    """
    pairs = cond.get("pairings", [])
    if not pairs:
        return '<p class="wx-rule">No comparison was possible.</p>'
    rows = ['<div class="hd">Observed</div><div class="hd">Not observed</div>']
    for p in pairs:
        rows.append(f'<div class="yes">{_esc(p["observed"])}</div>'
                    f'<div class="no">{_esc(p["not_observed"])}</div>')
    return f'<div class="wx-pairs">{"".join(rows)}</div>'


# --- the radar -------------------------------------------------------------

#: Radius is LOGARITHMIC in the ratio, with "typical" at mid-radius.
#:
#: A linear scale capped at 5x squashes the entire interesting region --
#: roughly 0.8x to 1.3x -- into a disc a few pixels across, and the usual range
#: becomes an invisible dot. Log spacing gives quiet conditions somewhere to go
#: (inward) as well as busy ones (outward), which matters for an instrument
#: that reports lulls as readily as storms.
#:
#: The bounds are RECIPROCAL so that "typical at mid-radius" is true rather
#: than merely intended: 0.25 and 8.0 are log2 -2 and +3, which put typical at
#: 40% of the radius and left the busy 60% of the dial empty in ordinary
#: conditions. 1/8 and 8 are symmetric, so a halving and a doubling are the
#: same distance from the middle -- which is the only reading of a log dial a
#: person can do by eye.
RADAR_HI = 8.0
RADAR_LO = 1.0 / RADAR_HI

#: The trace covers one day, and is drawn at the resolution the dial can
#: actually resolve: **one point per hour**, each the median ratio of the
#: windows in that hour.
#:
#: Taking the last 24 *windows* instead was wrong on the instrument that is
#: actually deployed. The fixtures run hourly windows, where 24 readings is a
#: day; the live collector runs 60-second windows, where 24 readings is
#: twenty-four minutes — less than one angular cell — so the trace collapsed
#: onto a single spoke and the hour-of-day axis carried nothing. Drawing 1,440
#: minute readings onto 24 angular positions is the opposite error: a lie of
#: resolution, and 170 KB of it.
RADAR_TRACE_HOURS = 24


def radar(obs: list, clim: dict, size: int = 380) -> str:
    """Diurnal radar. Angle is hour of day; radius is ratio to that hour's
    typical level. Both axes are quantities the data actually has.

    The shaded annulus is the usual range for each hour (p25-p75 as a ratio to
    that hour's median), so it wobbles with the daily cycle rather than being a
    circle. The trace is the most recent readings, drawn oldest-faintest so
    that time has a direction on the page rather than being a closed loop the
    reader has to guess at. A reading past the outer ring is drawn ON it and
    ringed, because a clipped storm that looks like a smaller one is a lie of
    omission.

    No geography. Angle is the clock, not a compass.

    `size` defines the viewBox coordinate space only. It is never emitted as a
    pixel attribute -- a chart with an intrinsic width refuses to shrink inside
    a grid child and paints outside its card.
    """
    name = "interaction_velocity"
    q = (clim or {}).get("quantities", {}).get(name, {})
    cells = {c["hour"]: c for c in q.get("diurnal", [])}
    if not cells:
        return ""

    cx = cy = size / 2
    r_max = size / 2 - 31
    log_lo, log_hi = math.log2(RADAR_LO), math.log2(RADAR_HI)

    def radius(ratio: float) -> float:
        r = min(max(ratio, RADAR_LO), RADAR_HI)
        return (math.log2(r) - log_lo) / (log_hi - log_lo) * r_max

    def xy(hour: float, ratio: float):
        rr = radius(ratio)
        ang = (hour / 24.0) * 2 * math.pi - math.pi / 2
        return cx + rr * math.cos(ang), cy + rr * math.sin(ang)

    parts = [
        f'<svg class="radar" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="Diurnal radar. Angle is hour of day; distance from the '
        f'centre is interaction activity as a multiple of what that hour of '
        f'day typically looks like. The shaded band is the usual range. This '
        f'is not a map: no geography is measured or implied.">'
    ]

    # Rings. Labels ride a single diagonal instead of stacking up the vertical
    # axis, where they used to collide with each other and with the trace.
    LABEL_ANGLE = -math.pi / 2 + (2 * math.pi) * (1.6 / 24.0)
    ring_labels: list = []
    # Every octave gets a ring; only four get a label. Seven labels on a
    # 340px dial stack into an illegible column, and a reader only needs
    # enough anchors to know the spacing is logarithmic.
    for ratio, label in ((RADAR_LO, ""), (0.25, ""), (0.5, "0.5×"),
                         (1.0, "typical"), (2.0, "2×"), (4.0, ""),
                         (RADAR_HI, "8×+")):
        rr = radius(ratio)
        typical = ratio == 1.0
        edge = ratio == RADAR_HI
        stroke = ("var(--s-active)" if typical
                  else "var(--rule-strong)" if edge else "var(--grid)")
        dash = "" if typical or edge else ' stroke-dasharray="2 4"'
        width = ' stroke-width="1.4"' if typical else ""
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{rr:.1f}" fill="none" '
                     f'stroke="{stroke}"{dash}{width}/>')
        if not label:
            continue
        lx = cx + rr * math.cos(LABEL_ANGLE)
        ly = cy + rr * math.sin(LABEL_ANGLE) + 3
        # Held back and emitted last. Drawn in place, the trace paints over
        # the radial scale, and a log dial whose scale you cannot read is a
        # decoration.
        ring_labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11" '
            f'text-anchor="middle" fill="var(--muted)" '
            f'stroke="var(--panel)" stroke-width="3.5" paint-order="stroke" '
            f'>{_esc(label)}</text>')

    # Every hour gets a tick; every third hour gets a label. A 4-spoke dial
    # made it impossible to read where in the day a bulge actually sat.
    for h in range(24):
        inner = r_max * (0.965 if h % 3 else 0.93)
        ang = (h / 24.0) * 2 * math.pi - math.pi / 2
        x1, y1 = cx + inner * math.cos(ang), cy + inner * math.sin(ang)
        x2, y2 = cx + r_max * math.cos(ang), cy + r_max * math.sin(ang)
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
                     f'y2="{y2:.1f}" stroke="var(--grid)"/>')
        if h % 3 == 0:
            # Computed directly rather than through xy(): routing it through
            # the ratio clamp pinned every hour label onto the outer ring,
            # where it collided with the ring labels.
            lx = cx + (r_max + 17) * math.cos(ang)
            ly = cy + (r_max + 17) * math.sin(ang) + 4.0
            parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11.5" '
                         f'text-anchor="middle" fill="var(--muted)">'
                         f'{h:02d}</text>')

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
    parts.append(f'<polygon points="{pts}" fill="var(--cloud)" opacity="0.5" '
                 f'stroke="var(--cloud)" stroke-width="1.2" '
                 f'stroke-opacity="0.95"><title>Usual range for each hour of '
                 f'day (25th to 75th percentile)</title></polygon>')

    # Group the last day into clock hours, keeping calendar order so the
    # trace still runs oldest-to-newest around the dial.
    buckets: dict = {}
    newest = timeutil.to_epoch(obs[-1].get("ts_end", "")) if obs else None
    horizon = (newest - RADAR_TRACE_HOURS * 3600) if newest is not None else None
    for o in obs:
        start = timeutil.to_epoch(o.get("ts_start", ""))
        if start is None or (horizon is not None and start < horizon):
            continue
        v = o.get("metrics", {}).get(name)
        if v is None:
            continue
        h = int((start // 3600) % 24)
        med = (cells.get(h) or {}).get("p50")
        if not med:
            continue
        buckets.setdefault(int(start // 3600), (h, []))[1].append(v / med)

    trace, clamped = [], []
    for key in sorted(buckets):
        h, ratios = buckets[key]
        ratios.sort()
        # Median, not mean: one 20x minute inside an ordinary hour should move
        # the point, not define it. The headline reading is what reports the
        # spike; this is the day's shape.
        ratio = ratios[len(ratios) // 2]
        pt = xy(h, ratio)
        trace.append(pt)
        if ratio > RADAR_HI:
            clamped.append((pt[0], pt[1], ratio))

    # Drawn oldest-faintest, one segment at a time, so the reader can see
    # which way round the dial time is running.
    for i in range(1, len(trace)):
        x0, y0 = trace[i - 1]
        x1, y1 = trace[i]
        op = 0.22 + 0.78 * (i / max(len(trace) - 1, 1))
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" '
                     f'y2="{y1:.1f}" stroke="var(--accent)" stroke-width="2" '
                     f'stroke-linecap="round" opacity="{op:.2f}"/>')
    # The marker is the newest WINDOW, not the newest hour bucket. The trace
    # is the day's shape at hour resolution; the marker is the reading the
    # headline is about, and those are different numbers whenever something is
    # happening right now. Labelling an hourly median "most recent window"
    # would put the dial and the headline in silent disagreement.
    for o in reversed(obs):
        start = timeutil.to_epoch(o.get("ts_start", ""))
        v = o.get("metrics", {}).get(name)
        if start is None or v is None:
            continue
        med = (cells.get(int((start // 3600) % 24)) or {}).get("p50")
        if not med:
            continue
        ratio = v / med
        x, y = xy((start // 3600) % 24, ratio)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" '
                     f'fill="var(--mark)" stroke="var(--panel)" '
                     f'stroke-width="1.5"><title>most recent complete '
                     f'window — {ratio:.1f}x typical for this hour'
                     f'</title></circle>')
        if ratio > RADAR_HI:
            clamped.append((x, y, ratio))
        break
    for x, y, ratio in clamped:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="none" '
                     f'stroke="var(--mark)" stroke-width="1.2" '
                     f'stroke-dasharray="2 2"><title>{ratio:.1f}x typical — '
                     f'beyond the outer ring, drawn on it</title></circle>')
    parts.extend(ring_labels)
    parts.append("</svg>")
    return "".join(parts)


# --- assembly --------------------------------------------------------------

def render(cond: dict, obs: list, clim: dict, *, history: list | None = None,
           heading: str = "", generated_at: str = "") -> str:
    """The whole conditions block: state, why, and the limits.

    `history` is the output of `recent_states`; pass None to omit the strip
    (there is nothing to show before the instrument has a baseline).

    The order is fixed and is the argument of the design: state, then the
    refusal, then the measurements each beside the inference it does not
    license, then the rule that produced the label. A reader who stops after
    the first screen has still been told what is not being claimed.
    """
    state = cond.get("state", "unavailable")
    cant = "".join(f"<li>{_esc(c)}</li>" for c in cond.get("cannot_see", []))
    # `conditions.criteria_table()` is the only builder; the row shape is
    # (icon, label, text) everywhere, so this renderer has one case.
    crit = "".join(
        f'<tr><td style="white-space:nowrap"><span role="img" '
        f'aria-hidden="true">{_esc(icon)}</span> {_esc(label)}</td>'
        f'<td>{_esc(text)}</td></tr>'
        for icon, label, text in cond.get("criteria_table", [])
    )
    dial = radar(obs, clim)
    dial_block = (f'<div class="wx-radar">{dial}<p class="wx-radar-note">'
                  f'The last {RADAR_TRACE_HOURS} hours, one point per hour. '
                  f'Angle is time of day; distance from the centre is activity '
                  f'against what that hour usually looks like, on a log scale '
                  f'with typical at mid-radius. The shaded band is the usual '
                  f'range. <strong>Not a map</strong> — the protocol exposes '
                  f'no location and none is inferred.</p>'
                  f'</div>') if dial else ""

    head = f"<h2>{_esc(heading)}</h2>" if heading else ""
    # `_offline` builds `plain` as "<sentence> <why>" so the field stays
    # self-contained for machine readers, which means the page would print the
    # sentence twice. Drop the duplicate here rather than thinning the data.
    sentence = cond.get("sentence", "")
    plain = cond.get("plain", "")
    if sentence and plain.startswith(sentence):
        plain = plain[len(sentence):].strip()
    plain_block = f'<p class="wx-plain">{_esc(plain)}</p>' if plain else ""
    strip = _history_strip(history or [])
    strip_block = (
        f'<h3 class="wx-sub">Recent conditions</h3>{strip}'
        f'<p class="wx-ladder-note">History, not a forecast. Each entry is '
        f'the same rule applied to the readings that existed at that time; '
        f'an interval with no eligible window says so rather than carrying '
        f'the previous reading forward.</p>'
    ) if strip else ""

    return f"""{head}
<div class="panel wx st-{_esc(state)}">
<div class="wx-head{"" if dial else " wx-head-solo"}">
<div>
<p class="wx-state"><span class="wx-ico" role="img"
aria-hidden="true">{_esc(cond.get("icon", ""))}</span>
<span class="wx-label">{_esc(cond.get("headline", ""))}</span></p>
<p class="wx-sentence">{_esc(sentence)}</p>
{plain_block}
<p class="wx-nobs"><b>Not observed</b>{_esc(cond.get(
    "universal_not_observed", ""))}.</p>
<p class="wx-conf">{_esc(cond.get("confidence_plain", ""))}</p>
<p class="wx-conf">Conditions as of <span class="mono">{_esc(
    _clock(cond.get("as_of", "")))}</span>{_esc(age_phrase(
    cond.get("as_of", ""), generated_at))}.</p>
</div>
{dial_block}
</div>
{_ladder(state)}
</div>

{strip_block}

<h3 class="wx-sub">Why the instrument says that</h3>
{pairs_table(cond)}
<p class="wx-rule"><strong>Rule applied:</strong> {_esc(cond.get(
    "criteria", ""))}</p>
<p class="wx-rule"><strong>What this instrument cannot see</strong>, in case
you were about to assume otherwise:</p>
<ul class="wx-cant">{cant}</ul>

<details class="wx-crit"><summary>How conditions are decided</summary>
<div class="panel scroll"><table class="crit">
<thead><tr><th>state</th><th>issued when</th></tr></thead>
<tbody>{crit}</tbody></table></div>
<p class="wx-rule">These criteria are published so the label can be checked
rather than trusted. A state is a statement about measured interaction
conditions — never about a person, a group, or anyone's intent. If this table
and the code ever disagree, the table is the bug.</p>
</details>"""
