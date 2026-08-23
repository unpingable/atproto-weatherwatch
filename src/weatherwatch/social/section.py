"""Render the disclosure-limited public social section.

This module receives a public ``SocialProjection`` and has no database handle.
Exact episode envelopes remain local; this renderer can only see the reduced,
cardinality-gated view produced by ``projection.py``.
"""

from __future__ import annotations

import html

from .projection import SocialProjection

STYLE_ADDITION = """
:root{--mag-info:#5a7f9c;--mag-low:#4a7f5c;--mag-med:#c99a3a;
      --mag-high:#c0632c;--mag-critical:#b03a3a;}
@media (prefers-color-scheme:dark){:root{--mag-info:#7fa9cc;--mag-low:#6fbf8a;
      --mag-med:#e0b45a;--mag-high:#e08a4a;--mag-critical:#ef6a5a;}}
.mag-info{color:var(--mag-info)}.mag-low{color:var(--mag-low)}
.mag-med{color:var(--mag-med)}.mag-high{color:var(--mag-high)}
.mag-critical{color:var(--mag-critical)}
"""

BAND_ORDER = ("info", "low", "med", "high", "critical")
BAND_MEANING = {
    "info": "under 1.5x",
    "low": "1.5x - 2x",
    "med": "2x - 4x",
    "high": "4x - 11x",
    "critical": "11x and above",
}

MAX_TABLE_ROWS = 250


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _receipt_panel(proj: SocialProjection) -> str:
    receipt = proj.sink_receipt or {}
    known = bool(receipt)
    enabled = bool(receipt.get("enabled"))
    state = ("ON" if enabled else "OFF") if known else "not recorded"
    collections = ", ".join(receipt.get("collections") or []) or "—"
    return f"""<div class="panel">
<dl class="kv">
<dt>Aggregate episode sensor</dt><dd>derived from identity-free minute counters</dd>
<dt>Edge custody (identity-bearing)</dt><dd><strong>{_esc(state)}</strong>
{'' if known else ' — no run has recorded a configuration receipt yet'}</dd>
<dt>Collections retained</dt><dd>{_esc(collections)}</dd>
<dt>Retention horizon</dt><dd>{_esc(receipt.get('retention') or '—')}</dd>
<dt>Configured by</dt><dd>{_esc(receipt.get('config_source', '—'))} ·
config <code>{_esc(receipt.get('config_hash', '—'))}</code></dd>
<dt>Published detector allowlist</dt><dd>{_esc(', '.join(proj.source.get(
    'detector_allowlist') or []) or 'none')}</dd>
</dl>
<p class="note">The local edge store is used only to establish a lower bound
on distinct actors before an aggregate episode can be published. Actor values,
edge counts, unsupported episodes, and lifecycle findings stay local.</p>
</div>"""


def _summary_panel(proj: SocialProjection) -> str:
    summary = proj.summary
    bands = summary.get("by_band", {})
    categories = summary.get("by_category", {})
    band_cells = "".join(
        f'<div class="panel"><div class="metric-name">{_esc(band)}</div>'
        f'<div class="metric-val mag-{_esc(band)}">{bands.get(band, 0)}</div>'
        f'<div class="metric-unit">{_esc(BAND_MEANING[band])}</div></div>'
        for band in BAND_ORDER
    )
    category_rows = "".join(
        f'<tr><td>{_esc(name)}</td><td class="num">{count}</td></tr>'
        for name, count in sorted(categories.items(), key=lambda item: -item[1])
    ) or '<tr><td colspan="2">none</td></tr>'
    directions = summary.get("by_direction", {})
    return f"""<div class="grid g3">{band_cells}</div>
<div class="grid g2" style="margin-top:12px">
<div class="panel"><div class="scroll"><table>
<tr><th>event family</th><th>disclosed periods</th></tr>
{category_rows}</table></div></div>
<div class="panel"><dl class="kv">
<dt>disclosed periods</dt><dd>{summary.get('n_disclosed', 0)}</dd>
<dt>excess / deficit</dt>
<dd>{directions.get('excess', 0)} / {directions.get('deficit', 0)}</dd>
<dt>first / last coarse period</dt>
<dd>{_esc(summary.get('first_period') or '—')}<br>
{_esc(summary.get('last_period') or '—')}</dd>
</dl></div></div>"""


def _episode_rows(episodes) -> str:
    rows = []
    for episode in episodes:
        rows.append(
            "<tr>"
            f"<td>{_esc(episode.period_start)} → {_esc(episode.period_end)}</td>"
            f"<td>{_esc(episode.type)}</td>"
            f"<td>{_esc(episode.direction)}</td>"
            f'<td><span class="pill mag-{_esc(episode.band)}">'
            f"{_esc(episode.band)}</span></td>"
            f"<td>{_esc(episode.actor_support)} distinct actors observed "
            "locally</td></tr>"
        )
    return "".join(rows)


def render(proj: SocialProjection, floor: float | None = None) -> str:
    """Render the public view. ``floor`` remains for call compatibility only."""
    del floor
    policy = proj.source.get("disclosure_policy") or {}
    min_actors = policy.get("minimum_distinct_actors", "—")
    bucket_s = policy.get("time_bucket_seconds", "—")
    span = ""
    if proj.summary.get("first_period"):
        span = (
            f' Covering coarse UTC periods from <strong>'
            f'{_esc(proj.summary["first_period"])}</strong> to <strong>'
            f'{_esc(proj.summary["last_period"])}</strong>.'
        )
    head = f"""<h2>E · Social observations — disclosure-limited episodes</h2>
<p class="sub">These are coarse periods in which an aggregate event rate
departed from its own trailing baseline. No account identifier is published.
That alone is <strong>not anonymity</strong>: precise aggregate timing and
counts can be joined back to the public firehose. Publication therefore fails
closed unless the existing local edge store independently observed at least
{_esc(min_actors)} distinct actors in the episode; eligible times are rounded
outward to {_esc(bucket_s)}-second UTC periods and exact counts, rates,
statistics, temporal shape, and stable episode identifiers are omitted.{span}</p>
<p class="sub warn"><strong>Observation is not causation, and magnitude is not
a verdict.</strong> Bands are provisional effect-size cuts. The actor floor is
also provisional disclosure resistance, not a statistical threshold and not a
claim that the remaining rows are anonymous.</p>"""

    if not proj.available:
        return f"""{head}
{_receipt_panel(proj)}
<div class="panel"><p class="sub">No episodes projected — {_esc(proj.reason)}.
</p></div>"""

    shown = sorted(
        proj.episodes, key=lambda episode: (episode.period_start, episode.type)
    )[:MAX_TABLE_ROWS]
    capped = len(proj.episodes) - len(shown)
    cap_note = (f" {capped} qualified periods exceed the "
                f"{MAX_TABLE_ROWS}-row display cap.") if capped else ""
    disclosure = (
        f"Table shows {len(shown)} disclosure-qualified coarse periods. "
        "Suppressed episodes and repeated signatures are not enumerated, "
        "because publishing their counts would partly undo suppression."
        f"{cap_note}"
    )

    return f"""{head}
{_receipt_panel(proj)}
{_summary_panel(proj)}
<div class="panel"><div class="scroll wide"><table>
<tr><th>coarse UTC period</th><th>type</th><th>direction</th><th>band</th>
<th>eligibility floor</th></tr>
{_episode_rows(shown)}
</table></div>
<p class="note">{disclosure}</p>
<p class="note"><code>social.json</code> exposes the same reduced fields. The
exact local detection envelopes and edge evidence are not published.</p></div>"""
