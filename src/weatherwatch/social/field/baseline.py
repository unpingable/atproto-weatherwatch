"""The baseline report: *what does normal look like?*, written out in full.

Generated from the climatology rather than composed by hand, so it cannot
drift from the numbers it describes. Its job is not to be reassuring. A
baseline report that only says what the instrument knows is marketing; this
one is organised so that the sections about what it *cannot* conclude are as
prominent as the sections about what it can, and it names blind spots that
will never be filled rather than listing them as future work.

Written for the calibration surface, not the public page.
"""

from __future__ import annotations

from .observation import STRUCTURAL_ABSENCES

SUPPORT_GLOSS = {
    "supported": "enough independent days to place a reading against this hour",
    "thin": "usable for description; too thin to carry confident expectations",
    "unsupported": "cannot carry a comparison; excluded from states and "
                   "candidates",
}


def _f(v, digits=4, dash="—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) >= 100 else f"{v:.{digits}g}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def report(clim: dict, observations: list, candidates: dict,
           meta: dict | None = None) -> str:
    meta = meta or {}
    q = clim.get("quantities", {})
    days = clim.get("n_days", 0)
    weeks = clim.get("n_weeks", 0)

    by_support: dict = {}
    for name, spec in q.items():
        by_support.setdefault(spec.get("support", "unsupported"), []).append(name)

    sample_rows = "\n".join(
        f"| `{n}` | {_f(s['overall'].get('n'))} | "
        f"{_f(s['overall'].get('n_eff'))} | "
        f"{_f(s['overall'].get('n_eff_residual'))} | "
        f"{_f(s['overall'].get('lag1_r'), 2)} | "
        f"{_f(s['overall'].get('lag1_r_residual'), 2)} | {days} | "
        f"**{s.get('support')}** |"
        for n, s in sorted(q.items())
    )

    normal_rows = "\n".join(
        f"| `{n}` | {s.get('unit','')} | {_f(s['overall'].get('p05'))} | "
        f"{_f(s['overall'].get('p25'))} | {_f(s['overall'].get('p50'))} | "
        f"{_f(s['overall'].get('p75'))} | {_f(s['overall'].get('p95'))} |"
        for n, s in sorted(q.items())
    )

    iv = q.get("interaction_velocity", {})
    diurnal_rows = "\n".join(
        f"| {c['hour']:02d} | {_f(c.get('p25'))} | **{_f(c.get('p50'))}** | "
        f"{_f(c.get('p75'))} | {c.get('n_days', 0)} |"
        for c in iv.get("diurnal", [])
    ) or "| — | | | | |"

    medians = [c.get("p50") for c in iv.get("diurnal", [])
               if c.get("p50") is not None]
    swing = (max(medians) / min(medians)) if medians and min(medians) else None

    unsupported = sorted(by_support.get("unsupported", []))
    thin = sorted(by_support.get("thin", []))
    supported = sorted(by_support.get("supported", []))

    cannot_rows = "\n".join(
        f"| **{n}** | {s.get('non_claim','')} |" for n, s in sorted(q.items()))

    blind_rows = "\n".join(
        f"| **{k}** | {v} |" for k, v in sorted(STRUCTURAL_ABSENCES.items()))

    obs_rate = candidates.get("observed_rate")
    return f"""# What does normal look like?

Climatology `{clim.get('climatology_id', '—')}` ·
generated {meta.get('generated_at', '—')}

This is the baseline the public state model is placed against. It exists so
that "unusual" has a denominator before anything is called unusual, and so
that the cases where this instrument **cannot** reach a conclusion are as
legible as the cases where it can.

---

## 1. What was observed

| | |
|---|---|
| window width | {clim.get('window', '—')} |
| interval | {clim.get('ts_start', '—')} → {clim.get('ts_end', '—')} |
| windows in range | {_f(clim.get('n_windows'))} |
| observed | {_f(clim.get('n_observed'))} |
| eligible for the baseline | {_f(clim.get('n_eligible'))} |
| distinct days | {days} |
| distinct weeks | {weeks} |
| source | {clim.get('provenance', {}).get('source', '—')} |
| endpoint | `{clim.get('provenance', {}).get('endpoint', '—')}` |

Windows that were not observed are absent from the baseline entirely. They are
never filled, interpolated or read as zero: a hole in observation is a fact
about the instrument, and a climatology built over filled holes would describe
the filling.

---

## 2. How much independent sample is behind "normal"

Two different sample sizes matter here, and the raw window count is neither.

**Effective N.** Adjacent windows are strongly autocorrelated, so the nominal
count overstates the information. Each distribution reports
`n_eff = n(1 − r) / (1 + r)`.

**Effective N of the residual, which is the one that governs.** The raw series
is dominated by the daily cycle; conditioning happens *inside* an hour cell,
so the series whose independence actually matters is the deseasonalised
residual. The gap between the two columns below is the daily cycle, not
information.

| quantity | n | n_eff (raw) | n_eff (residual) | lag-1 r (raw) | lag-1 r (resid) | day replicates | support |
|---|---|---|---|---|---|---|---|
{sample_rows}

**Replicates, not windows.** To ask *is this unusual for this hour of day*,
the independent replicates are **days** — {days} of them here, however many
minutes each contains. Percentile tails need more replicates than quartiles
do, which is why the threshold for `supported` is 20 days rather than a week.

---

## 3. Seasonality

Handled by conditioning, not by removal: a reading is compared with the same
hour of day, so the daily cycle never has to be modelled or subtracted.

{"Observed diurnal swing: the busiest hour's median is "
 f"**{swing:.1f}×** the quietest hour's." if swing else
 "Diurnal swing could not be computed."}
That is why a flat all-hours average would be the wrong reference, and why a
reading at 03:00 and the same reading at 21:00 are not the same event.

**Hour of week is computed, reported, and not used.** The replicates for that
question are *weeks*, and this interval contains **{weeks}**. One hundred and
sixty-eight cells against {weeks} replicates each is not a baseline.

> {clim.get('hour_of_week_note', '—')}

### Interaction velocity by hour (UTC)

| hour | p25 | **median** | p75 | day replicates |
|---|---|---|---|---|
{diurnal_rows}

---

## 4. What normal looks like

| quantity | unit | p05 | p25 | p50 | p75 | p95 |
|---|---|---|---|---|---|---|
{normal_rows}

---

## 5. Conclusions this baseline cannot support

**{len(unsupported)} unsupported**{': `' + '`, `'.join(unsupported) + '`' if unsupported else ''}.
{SUPPORT_GLOSS['unsupported'].capitalize()}.

**{len(thin)} thin**{': `' + '`, `'.join(thin) + '`' if thin else ''}.
{SUPPORT_GLOSS['thin'].capitalize()}.

**{len(supported)} supported**{': `' + '`, `'.join(supported) + '`' if supported else ''}.

**The candidate rate is mostly arithmetic.** A 5th–95th rule marks 10% of
windows by construction. Observed here:
**{_f(obs_rate, 3) if obs_rate is not None else 'not measured'}**.
{candidates.get('note', '')}

**Every quantity carries its own refusal.** These are not caveats appended to
the report; they are attached to the measurements themselves and travel with
them into every observation record.

| quantity | does not measure |
|---|---|
{cannot_rows}

---

## 6. Blind spots — permanent, not pending

These are not gaps awaiting work. Each is absent because measuring it would
require retention this instrument refuses, and adding it later would not be a
feature but a change of posture.

| | |
|---|---|
{blind_rows}

**Participant turnover specifically.** It is the most natural thing to want
here — *are these new people?* — and it is exactly what cannot be answered.
The counters aggregate events, not accounts, so any newcomer ratio would
require retaining actor identity across windows. It is therefore reported as a
blind spot and **not approximated**. No proxy is offered, because a proxy for
an unmeasurable quantity is a guess wearing a number's clothes.

---

## 7. Replay

Every observation is content-addressed and the stored document is its
canonical form, so re-deriving the identifier from storage reproduces it
exactly. This climatology is `{clim.get('climatology_id', '—')}`; observations
are loaded scoped to it, because a reading is only meaningful against the
baseline it was scored with.

Rebuild with:

```
weatherwatch social field --last <range> --social-db <path> \\
    --output <public> --station-output <calibration>
```
"""
