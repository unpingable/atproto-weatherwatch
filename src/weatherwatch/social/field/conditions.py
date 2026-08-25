"""The public layer: conditions a person can read without knowing the machinery.

A visitor should be able to answer *is this environment calm, active,
turbulent, or under unusual stress right now?* without meeting a z-score.

THE HONESTY PROBLEM, AND HOW IT IS SOLVED
-----------------------------------------
A single state label is a composite, and this estate has explicitly refused
composite severity indices -- the published page already says the Global Beef
Index is a joke name for a composite that does not exist. So the label here is
built the way a weather service builds one: **published criteria over named,
visible inputs.**

A severe thunderstorm warning is not a mood or a score. It is issued when wind
reaches a stated speed or hail a stated size, and the criteria are printed
where anyone can check them. `CRITERIA` below is that table, it is rendered on
the page, and every state carries the rule that produced it and the numbers
that satisfied it. A reader who distrusts the label can reconstruct it.

WHAT THE STATES DESCRIBE
------------------------
Observed interaction conditions. Not participants, not communities, not
intent. "Storm" means the measured interaction rate is far outside what this
hour of day normally looks like and has stayed there. It does not mean anyone
did anything, and no state name refers to a person.

DEGRADING HONESTLY
------------------
When the climatology cannot support the comparison -- too few day replicates,
or the window was not observed -- the state is `unavailable` and says why.
It never falls back to "calm", because "we cannot tell" and "nothing is
happening" are different facts and only one of them is reassuring.
"""

from __future__ import annotations

from dataclasses import dataclass

from .climatology import SUPPORTED, THIN, UNSUPPORTED

CALM = "calm"
ACTIVE = "active"
UNSETTLED = "unsettled"
TURBULENT = "turbulent"
STORM = "storm"
SEVERE = "severe_storm"
UNAVAILABLE = "unavailable"
OFFLINE = "station_offline"

#: Display names. No state names a person, a group, or a motive.
STATE_LABEL = {
    CALM: "Calm",
    ACTIVE: "Active",
    UNSETTLED: "Unsettled",
    TURBULENT: "Turbulent",
    STORM: "Storm",
    SEVERE: "Severe storm",
    UNAVAILABLE: "Conditions unavailable",
    OFFLINE: "Station offline",
}

#: Standard weather grammar, because people already know how to read it:
#: sun = normal, cloud = unsettled, rain = degraded, thunder = severe,
#: fog = cannot see. Deliberately coarse -- no mechanism-specific icons (no
#: "block tornado", no "quote storm") while the climatology is still thin.
#: No fire emoji: "everything is on fire" is funny until the instrument
#: sounds like a meme account.
STATE_ICON = {
    CALM: "\u2600\ufe0f",
    ACTIVE: "\u26c5",
    UNSETTLED: "\U0001f32c\ufe0f",
    TURBULENT: "\U0001f327\ufe0f",
    STORM: "\u26c8\ufe0f",
    SEVERE: "\U0001f32a\ufe0f",
    UNAVAILABLE: "\U0001f32b\ufe0f",
    OFFLINE: "\U0001f4e1",
}

#: One sentence a stranger can read cold. Shown with the icon, never instead
#: of it -- an emoji alone is a mood, not a reading.
STATE_SENTENCE = {
    CALM: "Conditions are within the normal range for this time of day.",
    ACTIVE: "Activity is above typical, but not unusually so.",
    UNSETTLED: "Short bursts or shifts are visible, without settling into a "
               "sustained pattern.",
    TURBULENT: "Elevated activity is persisting.",
    STORM: "A significant interaction event is in progress.",
    SEVERE: "Conditions are substantially outside the normal range.",
    UNAVAILABLE: "The instrument cannot support a trustworthy reading right "
                 "now.",
    OFFLINE: "No current measurement is available from the instrument.",
}

#: Rendered with EVERY state, including Calm and the two null states. The
#: temptation to over-read is strongest when the reading is dramatic, but a
#: reader shown "Calm" will just as happily conclude "so the network is
#: healthy", which is equally unmeasured.
UNIVERSAL_NOT_OBSERVED = (
    "user intent, emotional state, correctness, coordination, culpability, "
    "or geographic origin"
)

STATE_ORDER = (OFFLINE, UNAVAILABLE, CALM, ACTIVE, UNSETTLED, TURBULENT,
               STORM, SEVERE)

#: A reading older than this many windows means the station stopped, not that
#: the weather went quiet. Distinguishing the two is the whole reason
#: `station_offline` exists as its own state.
STALE_AFTER_WINDOWS = 15

#: Multiple of the hour-typical level at which a storm is called severe.
SEVERE_RATIO = 3.0
#: Consecutive elevated windows required for "storm" and for "severe".
STORM_PERSISTENCE = 2
SEVERE_PERSISTENCE = 3

#: The published warning criteria, in evaluation order. First match wins.
#: Rendered on the page verbatim -- if this table and the code disagree, the
#: table is the bug.
CRITERIA = (
    (OFFLINE, "The instrument produced no reading, or its most recent "
              f"complete reading is more than {STALE_AFTER_WINDOWS} windows "
              "old."),
    (UNAVAILABLE, "The window was not observed, or the baseline for this hour "
                  "is too thin to compare against."),
    (SEVERE, "Interaction activity is above the 95th percentile for this hour "
             f"of day, at least {SEVERE_RATIO:g}x its typical level, and has "
             f"stayed elevated for {SEVERE_PERSISTENCE} or more windows."),
    (STORM, "Interaction activity is above the 95th percentile for this hour "
            f"of day and has stayed elevated for {STORM_PERSISTENCE} or more "
            "windows."),
    (TURBULENT, "Activity or its variability is elevated for this hour of day "
                f"and has persisted across {STORM_PERSISTENCE} or more "
                "windows."),
    (UNSETTLED, "Activity or its variability is elevated for this hour of "
                "day, but has not persisted."),
    (ACTIVE, "Interaction activity is above the 75th percentile for this hour "
             "of day."),
    (CALM, "Every measured quantity is within its usual range for this hour "
           "of day."),
)

def criteria_table() -> list[tuple[str, str, str]]:
    """`CRITERIA` as (icon, label, text), in evaluation order.

    One builder, so the page a visitor sees and the page the tests inspect
    cannot drift into different shapes. The criteria are published because
    the state word is a composite over named inputs, and a composite whose
    rule is unpublished is a verdict.
    """
    return [(STATE_ICON[state], STATE_LABEL[state], text)
            for state, text in CRITERIA]


#: Things a reader may expect to be here and which the instrument cannot see.
#: Rendered beside the conditions so the gap is part of the reading.
CANNOT_SEE = (
    "How many people are involved, or how many are new. The counters "
    "aggregate events, not accounts, so participant turnover is unavailable "
    "by construction rather than unimplemented.",
    "What is being said. Record bodies are discarded before storage.",
    "Where anyone is. The protocol exposes no location and none is inferred.",
    "Who did anything. No actor or target is retained at any point.",
)


#: For each measurable property: what was observed, and the inference a reader
#: is most likely to make from it that the measurement does not license.
#:
#: Pairing them is the point. "Interaction velocity elevated" on its own
#: invites "people are angry", and a reader supplies that themselves unless
#: the instrument says otherwise in the same breath. The right-hand column is
#: not a disclaimer; it is the other half of the reading.
NOT_OBSERVED = {
    "interaction_velocity":
        "That anyone is upset, or that the activity is hostile. A like and a "
        "furious quote-post are one event each here.",
    "emission_velocity":
        "That more people are present. One repo may emit many records and the "
        "counters cannot tell them apart.",
    "interaction_pressure":
        "That the content is controversial. Reactions per post rises for a "
        "popular post and for a quiet emission rate alike.",
    "reply_share":
        "That an argument is happening. Replies include agreement, jokes, and "
        "threads an author writes to themselves.",
    "persistence":
        "That this was coordinated, planned or organised. Duration is "
        "duration.",
    "turbulence":
        "That a community is unstable. This is a property of an observed rate "
        "series at one endpoint.",
    "boundary_share":
        "That conflict increased. Blocks are also routine hygiene, "
        "list-driven, and frequently unrelated to any dispute.",
    "withdrawal_share":
        "That anyone regretted or retracted anything. Deletion is also "
        "editing, cleanup, migration and automation.",
    "graph_velocity":
        "That the social graph changed shape. These are graph *events*; no "
        "edge is retained and no endpoint is known.",
}


#: Used when a quantity has no specific entry above. Never omit the column.
GENERIC_NOT_OBSERVED = (
    "Anything about the people involved. This is a count of events, and the "
    "counters never held an account.")


@dataclass(frozen=True)
class Pairing:
    """One measured property beside the inference it does not license."""

    quantity: str
    observed: str
    not_observed: str


@dataclass(frozen=True)
class Reason:
    """One plain-language sentence, with the number behind it."""

    plain: str
    quantity: str
    value: float | None = None
    reference: float | None = None
    ratio: float | None = None


@dataclass(frozen=True)
class Conditions:
    state: str
    label: str
    icon: str
    sentence: str
    headline: str
    plain: str
    criteria: str
    reasons: tuple
    confidence: str
    confidence_plain: str
    persistence_windows: int
    #: Measured properties beside the inferences they do not license.
    pairings: tuple = ()
    #: End of the most recent COMPLETE window these conditions describe.
    as_of: str = ""
    cannot_see: tuple = CANNOT_SEE

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "label": self.label,
            "icon": self.icon,
            "sentence": self.sentence,
            "headline": self.headline,
            "universal_not_observed": UNIVERSAL_NOT_OBSERVED,
            "plain": self.plain,
            "criteria": self.criteria,
            "reasons": [
                {"plain": r.plain, "quantity": r.quantity, "value": r.value,
                 "reference": r.reference, "ratio": r.ratio}
                for r in self.reasons
            ],
            "confidence": self.confidence,
            "confidence_plain": self.confidence_plain,
            "persistence_windows": self.persistence_windows,
            "pairings": [
                {"quantity": p.quantity, "observed": p.observed,
                 "not_observed": p.not_observed}
                for p in self.pairings
            ],
            "as_of": self.as_of,
            "cannot_see": list(self.cannot_see),
        }


def _cell(clim: dict, quantity: str, hour: int) -> dict:
    q = clim.get("quantities", {}).get(quantity, {})
    return next((c for c in q.get("diurnal", []) if c["hour"] == hour), {})


def _support(clim: dict, quantity: str) -> str:
    return clim.get("quantities", {}).get(quantity, {}).get(
        "support", UNSUPPORTED)


def _hour_of(iso: str) -> int | None:
    import datetime
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).hour
    except (ValueError, AttributeError):
        return None


def persistence(observations: list, clim: dict,
                quantity: str = "interaction_velocity") -> int:
    """Consecutive most-recent windows sitting above the hour-typical 75th.

    Persistence is what separates a spike from a storm: weather that arrives
    and leaves in one window is a gust. Counted backwards from the latest
    observation and stopping at the first window that is not elevated, not
    observed, or not comparable.
    """
    count = 0
    for o in reversed(observations):
        v = o.get("metrics", {}).get(quantity)
        hour = _hour_of(o.get("ts_start", ""))
        if v is None or hour is None:
            break
        cell = _cell(clim, quantity, hour)
        p75 = cell.get("p75")
        if p75 is None or v <= p75:
            break
        count += 1
    return count


def _fmt_ratio(x: float) -> str:
    return f"{x:.1f}x" if x < 10 else f"{x:.0f}x"


def _window_seconds(doc: dict) -> int:
    raw = str(doc.get("window", "60s")).rstrip("s")
    try:
        return max(int(float(raw)), 1)
    except ValueError:
        return 60


def assess(observations: list, clim: dict, now: str | None = None) -> Conditions:
    """Current conditions from stored observations and their climatology.

    `now` enables staleness detection. Without it the instrument can say what
    it last measured but not whether it is still measuring, and those are
    different questions: a station that stopped an hour ago and a network that
    went quiet an hour ago produce identical readings and mean opposite things.
    """
    if not observations or not clim:
        return _offline("The instrument has filed no observations.")

    # The most recent *complete* window, not literally the last one. The
    # window in flight is partial by definition, so keying on it would make
    # the page read "unavailable" almost permanently -- observed on the live
    # instrument. A weather station reports the last full reading and says
    # when it was taken.
    latest = next((o for o in reversed(observations)
                   if o.get("confidence", {}).get("eligible")), None)
    if latest is None:
        return _unavailable(
            "No window in range was observed cleanly enough to compare "
            "against its baseline.")
    stale_windows = len(observations) - 1 - observations.index(latest)

    # Staleness is measured against wall clock, not against position in the
    # list: an archive that simply stops has no later rows to count.
    if now:
        from ... import timeutil
        end = timeutil.to_epoch(latest.get("ts_end", ""))
        ref = timeutil.to_epoch(now)
        if end is not None and ref is not None:
            width = _window_seconds(latest)
            behind = (ref - end) / width
            if behind > STALE_AFTER_WINDOWS:
                return _offline(
                    f"The most recent complete reading is {behind:.0f} windows "
                    f"old. That is a fact about the instrument, not about the "
                    f"network.")
    conf = latest.get("confidence", {})
    hour = _hour_of(latest.get("ts_start", ""))
    iv = latest.get("metrics", {}).get("interaction_velocity")
    support = _support(clim, "interaction_velocity")

    if hour is None or iv is None:
        return _unavailable(
            "The most recent complete window carried no comparable reading.")
    if support == UNSUPPORTED:
        return _unavailable(
            "The instrument has not been watching long enough to say what is "
            "normal for this hour of day. Conditions are being recorded, but "
            "not yet compared.")

    cell = _cell(clim, "interaction_velocity", hour)
    p50, p75, p95 = cell.get("p50"), cell.get("p75"), cell.get("p95")
    if p50 is None or p75 is None or p95 is None:
        return _unavailable("No baseline exists for this hour of day yet.")

    ratio = iv / p50 if p50 else None
    runs = persistence(observations, clim)

    turb = latest.get("metrics", {}).get("turbulence")
    turb_cell = _cell(clim, "turbulence", hour)
    turb_p95 = turb_cell.get("p95")
    turb_high = (turb is not None and turb_p95 is not None
                 and _support(clim, "turbulence") != UNSUPPORTED
                 and turb > turb_p95)

    above_p95 = iv > p95
    above_p75 = iv > p75

    # Evaluated in CRITERIA order, first match wins. Persistence is what
    # separates Turbulent from Unsettled: the same reading that has held for
    # several windows is weather, and one that has not is a gust.
    sustained = runs >= STORM_PERSISTENCE
    elevated = above_p75 or turb_high
    if above_p95 and ratio and ratio >= SEVERE_RATIO and runs >= SEVERE_PERSISTENCE:
        state = SEVERE
    elif above_p95 and sustained:
        state = STORM
    elif elevated and sustained:
        state = TURBULENT
    elif turb_high or above_p95:
        state = UNSETTLED
    elif above_p75:
        state = ACTIVE
    else:
        state = CALM

    criteria = next(c for s, c in CRITERIA if s == state)
    reasons = _reasons(latest, clim, hour, iv, p50, p75, p95, ratio, runs,
                       turb, turb_p95, turb_high)
    pairings = _pairings(reasons)
    return Conditions(
        state=state,
        label=STATE_LABEL[state],
        icon=STATE_ICON[state],
        sentence=STATE_SENTENCE[state],
        headline=STATE_LABEL[state],
        plain=_plain(state, ratio, runs),
        criteria=criteria,
        reasons=tuple(reasons),
        confidence=support,
        confidence_plain=_confidence_plain(support, conf, stale_windows),
        persistence_windows=runs,
        pairings=pairings,
        as_of=latest.get("ts_end", ""),
    )


def offline(why: str) -> Conditions:
    """`station_offline` with a caller-supplied reason.

    The public wrapper exists because a caller that cannot even *reach* the
    observations knows something `assess` does not -- "no field store is
    configured for this report" and "the instrument filed no observations" are
    different facts, and a station that cannot say which is not much of a
    station. Behaviour is identical; only the sentence differs.
    """
    return _offline(why)


def _offline(why: str) -> Conditions:
    """No measurement at all. Distinct from "cannot support a reading"."""
    return Conditions(
        state=OFFLINE, label=STATE_LABEL[OFFLINE],
        icon=STATE_ICON[OFFLINE], sentence=STATE_SENTENCE[OFFLINE],
        headline=STATE_LABEL[OFFLINE],
        plain=f"{STATE_SENTENCE[OFFLINE]} {why}",
        criteria=next(c for st, c in CRITERIA if st == OFFLINE),
        reasons=(), confidence=UNSUPPORTED,
        confidence_plain=(
            "This says nothing about network conditions. It says the "
            "instrument is not reporting."),
        persistence_windows=0,
    )


def _unavailable(why: str) -> Conditions:
    return Conditions(
        state=UNAVAILABLE, label=STATE_LABEL[UNAVAILABLE],
        icon=STATE_ICON[UNAVAILABLE], sentence=STATE_SENTENCE[UNAVAILABLE],
        headline=STATE_LABEL[UNAVAILABLE],
        plain=why,
        criteria=next(c for s, c in CRITERIA if s == UNAVAILABLE),
        reasons=(),
        confidence=UNSUPPORTED,
        confidence_plain=(
            "This is not a reading of calm. It means the instrument cannot "
            "say, which is a different thing."),
        persistence_windows=0,
    )


def _plain(state: str, ratio: float | None, runs: int) -> str:
    r = _fmt_ratio(ratio) if ratio else "an unusual multiple of"
    if state == SEVERE:
        return (f"Interaction activity is running about {r} its usual level "
                f"for this hour and has stayed there for {runs} windows. "
                "A large interaction event is active.")
    if state == STORM:
        return (f"Interaction activity is well above what this hour of day "
                f"normally looks like — about {r} typical — and has persisted "
                f"across {runs} windows.")
    if state == TURBULENT:
        return ("Activity is swinging more than it usually does at this hour. "
                "The overall level may be ordinary; the variability is not.")
    if state == ACTIVE:
        return (f"Busier than usual for this hour — about {r} typical — but "
                "within the range this hour normally reaches.")
    return ("Everything measured is within its usual range for this hour of "
            "day.")


def _confidence_plain(support: str, conf: dict, stale: int = 0) -> str:
    days = conf.get("baseline_days", 0)
    if support == SUPPORTED:
        base = f"Compared against {days} previous days of this same hour."
    elif support == THIN:
        base = (f"Provisional: only {days} previous days of this hour to "
                "compare against, so the usual range is not yet well "
                "established.")
    else:
        base = "No usable baseline for this hour."
    if stale == 1:
        base += " The window now in progress is not yet complete."
    elif stale > 1:
        base += (f" The most recent complete reading is {stale} windows "
                 "behind the present.")
    return base


def _reasons(latest, clim, hour, iv, p50, p75, p95, ratio, runs,
             turb, turb_p95, turb_high) -> list:
    out = []
    if ratio:
        where = ("above the busiest 5% of this hour" if iv > p95
                 else "above the usual range for this hour" if iv > p75
                 else "within the usual range for this hour")
        out.append(Reason(
            plain=(f"Interaction activity is {_fmt_ratio(ratio)} its typical "
                   f"level for this hour of day — {where}."),
            quantity="interaction_velocity", value=round(iv, 3),
            reference=round(p50, 3), ratio=round(ratio, 3)))
    if runs:
        out.append(Reason(
            plain=(f"Conditions have stayed elevated for {runs} consecutive "
                   f"window{'s' if runs != 1 else ''}."),
            quantity="persistence", value=float(runs)))
    if turb is not None and turb_p95 is not None:
        out.append(Reason(
            plain=("Variability is above the 95th percentile for this hour."
                   if turb_high else
                   "Variability is within its usual range for this hour."),
            quantity="turbulence", value=round(turb, 4),
            reference=round(turb_p95, 4)))

    for name, phrase in (("interaction_pressure",
                          "reactions per new post"),
                         ("boundary_share",
                          "share of new follow-or-block records that were "
                          "blocks")):
        v = latest.get("metrics", {}).get(name)
        cell = _cell(clim, name, hour)
        med = cell.get("p50")
        if v is None or med is None or _support(clim, name) == UNSUPPORTED:
            continue
        if med > 0:
            rr = v / med
            if rr >= 1.25 or rr <= 0.8:
                direction = "above" if rr > 1 else "below"
                out.append(Reason(
                    plain=(f"The {phrase} is {_fmt_ratio(rr)} its typical "
                           f"level for this hour ({direction} usual)."),
                    quantity=name, value=round(v, 4),
                    reference=round(med, 4), ratio=round(rr, 3)))
    return out


def _pairings(reasons: list) -> tuple:
    """Pair every reason shown with the inference it does not license.

    Driven off the reasons actually rendered, so the two columns always have
    the same number of rows and a reader cannot be given a measurement whose
    limit went unstated.
    """
    out = []
    seen = set()
    for r in reasons:
        if r.quantity in seen:
            continue
        # Fall back rather than drop: a measurement shown without its limit
        # is the failure this pairing exists to prevent, so an unmapped
        # quantity gets the generic refusal instead of vanishing.
        limit = NOT_OBSERVED.get(r.quantity, GENERIC_NOT_OBSERVED)
        seen.add(r.quantity)
        out.append(Pairing(quantity=r.quantity, observed=r.plain,
                           not_observed=limit))
    return tuple(out)
