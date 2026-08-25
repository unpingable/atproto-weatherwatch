"""M2 — the pure classifier. This is the identity boundary of the whole system.

    classify(msg) -> Classification(time_us, metrics) | None

Identity-bearing fields arrive inside the public event and may be *read* here.
They are never returned, never stored, never logged. The event dict does not
escape this call.

The strongest guarantee available is structural, not filtering: **the output
alphabet is finite and enumerable**. `metrics` is a tuple drawn from
`ALLOWED_METRICS`, a frozenset computed at import time from closed vocabulary
tables below. A DID cannot appear in the output because no DID is a member of
that set. `tests/test_classify_privacy.py` asserts the containment on every
fixture, so the guarantee is checked rather than asserted.

Everything in this module is derived from `M0-VERIFICATION-RESULTS.md`. Nothing
here encodes semantics M0 did not observe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# --- vocabulary, all M0-observed ------------------------------------------

OPERATIONS = ("create", "update", "delete")

#: NSID -> short metric alias. Exactly the `app.bsky.*` collections M0 saw at
#: usable volume in a 600s unfiltered survey. Anything else is unclassified,
#: counted, and its payload discarded.
COLLECTION_ALIASES: dict[str, str] = {
    "app.bsky.feed.post": "post",
    "app.bsky.feed.like": "like",
    "app.bsky.feed.repost": "repost",
    "app.bsky.graph.follow": "follow",
    "app.bsky.graph.block": "block",
    "app.bsky.graph.listitem": "listitem",
    "app.bsky.actor.profile": "profile",
    "app.bsky.feed.threadgate": "threadgate",
    "app.bsky.feed.postgate": "postgate",
    "app.bsky.actor.status": "actor_status",
}

#: Embed `$type` -> alias. M0 measured `$type` present on 8,695/8,695 post
#: embeds, so branching on it is safe. Open enum: unknown types fall to
#: `other` rather than being dropped or guessed at.
EMBED_ALIASES: dict[str, str] = {
    "app.bsky.embed.images": "images",
    "app.bsky.embed.external": "external",
    "app.bsky.embed.record": "record",
    "app.bsky.embed.recordWithMedia": "recordWithMedia",
    "app.bsky.embed.video": "video",
    "app.bsky.embed.gallery": "gallery",
}
EMBED_OTHER = "other"

#: A quote is exactly these two. `app.bsky.embed.gallery` — found by M0 and
#: absent from the design candidate — is media, NOT a quote. Enumerating the
#: quote set positively is what keeps a future embed type from silently
#: becoming a quote.
QUOTE_EMBED_TYPES = frozenset({
    "app.bsky.embed.record",
    "app.bsky.embed.recordWithMedia",
})

#: Operations that carry a record, and so can be shape-classified. M0: `cid`
#: and `record` are present iff the operation is not a delete — exact across
#: 197,926 commits.
SHAPED_OPERATIONS = ("create", "update")

#: M0 observed deleted/deactivated/takendown. `suspended` is in the protocol
#: vocabulary but was not observed; it is listed so it maps to its own bucket
#: rather than to `other` if it shows up. Open enum regardless.
ACCOUNT_STATUSES = ("deleted", "deactivated", "takendown", "suspended")
ACCOUNT_STATUS_OTHER = "other"

KNOWN_KINDS = frozenset({"commit", "identity", "account"})


def _build_allowed_metrics() -> frozenset[str]:
    m: set[str] = set()
    for alias in COLLECTION_ALIASES.values():
        for op in OPERATIONS:
            m.add(f"{alias}.{op}")
    for op in SHAPED_OPERATIONS:
        m.add(f"post.{op}.reply")
        m.add(f"post.{op}.quote")
        for embed in list(EMBED_ALIASES.values()) + [EMBED_OTHER]:
            m.add(f"post.{op}.embed.{embed}")
    m.add("identity.event")
    m.add("account.event")
    m.add("account.active.true")
    m.add("account.active.false")
    m.add("account.active.unknown")
    for status in list(ACCOUNT_STATUSES) + [ACCOUNT_STATUS_OTHER]:
        m.add(f"account.status.{status}")
    # Schema-drift canaries. Deliberately unparameterised: recording *which*
    # unknown NSID appeared would make `metric` unbounded-cardinality and
    # would put third-party lexicon names into the product data. The count is
    # the canary, and there is no breakdown anywhere -- not in the database,
    # not in the artifacts, and not in the collector's STATS line, which emits
    # scalar counts only. A canary that names the thing it saw would be the
    # schema-leak channel it exists to warn about.
    m.add("unclassified.kind")
    m.add("untracked.collection")
    m.add("malformed.collection")
    m.add("unclassified.operation")
    m.add("malformed.commit")
    return frozenset(m)


#: Every value `classify` can ever emit. Finite, 63 entries, no free text.
#: The count is asserted in `tests/test_classify_privacy.py`: the size of the
#: identity boundary is a published figure, and a published figure that drifts
#: is worse than no figure. Changing it is a deliberate act, not a side effect.
ALLOWED_METRICS: frozenset[str] = _build_allowed_metrics()


@dataclass(frozen=True, slots=True)
class Classification:
    """The only thing that crosses out of the classifier.

    Two integers-worth of information: when, and which counters to bump.
    """

    time_us: int
    metrics: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.time_us, int) or isinstance(self.time_us, bool):
            raise TypeError("time_us must be int")


def _embed_alias(embed: Any) -> str | None:
    if not isinstance(embed, Mapping):
        return None
    etype = embed.get("$type")
    if not isinstance(etype, str):
        # M0 saw 0/8,695 missing, but the path stays exercised by
        # fixtures/jetstream_synthetic.jsonl:synthetic|embed_missing_type.
        return EMBED_OTHER
    return EMBED_ALIASES.get(etype, EMBED_OTHER)


def _is_quote(embed: Any) -> bool:
    if not isinstance(embed, Mapping):
        return False
    return embed.get("$type") in QUOTE_EMBED_TYPES


def _post_shape_metrics(op: str, record: Any) -> list[str]:
    """Reply / quote / embed-kind metrics for a post create or update.

    Subsets, not a partition: a post that is both a reply and a quote emits
    both. `post.<op>` remains the total.
    """
    if not isinstance(record, Mapping):
        return []
    out: list[str] = []
    if isinstance(record.get("reply"), Mapping):
        out.append(f"post.{op}.reply")
    embed = record.get("embed")
    if embed is not None:
        alias = _embed_alias(embed)
        if alias is not None:
            out.append(f"post.{op}.embed.{alias}")
        if _is_quote(embed):
            out.append(f"post.{op}.quote")
    return out


def _classify_commit(commit: Any) -> list[str]:
    if not isinstance(commit, Mapping):
        return ["malformed.commit"]

    collection = commit.get("collection")
    operation = commit.get("operation")

    if not isinstance(operation, str) or operation not in OPERATIONS:
        return ["unclassified.operation"]
    if not isinstance(collection, str) or not collection:
        return ["malformed.collection"]

    alias = COLLECTION_ALIASES.get(collection)
    if alias is None:
        # Third-party lexicon. ~2% of commits in M0's window, 61 distinct
        # NSIDs. Counted; payload discarded; NSID not persisted.
        return ["untracked.collection"]

    metrics = [f"{alias}.{operation}"]

    # Deletes carry no record (M0: 0/5,830), so no shape is recoverable.
    # Nothing here infers one.
    if alias == "post" and operation in SHAPED_OPERATIONS:
        metrics.extend(_post_shape_metrics(operation, commit.get("record")))
    return metrics


def _classify_account(account: Any) -> list[str]:
    metrics = ["account.event"]
    if not isinstance(account, Mapping):
        return metrics
    active = account.get("active")
    if active is True:
        metrics.append("account.active.true")
    elif active is False:
        metrics.append("account.active.false")
    else:
        metrics.append("account.active.unknown")
    status = account.get("status")
    if isinstance(status, str) and status:
        bucket = status if status in ACCOUNT_STATUSES else ACCOUNT_STATUS_OTHER
        metrics.append(f"account.status.{bucket}")
    return metrics


def classify(msg: Any) -> Classification | None:
    """Classify one Jetstream envelope into aggregate counter keys.

    Returns ``None`` when the message cannot be assigned to a time window —
    i.e. ``time_us`` is absent, non-integer, or non-positive. The caller
    counts those separately (``rejected_no_time_us``); they are not silently
    folded into any bucket, because a count with no window is not an
    observation.
    """
    if not isinstance(msg, Mapping):
        return None

    time_us = msg.get("time_us")
    if isinstance(time_us, bool) or not isinstance(time_us, int) or time_us <= 0:
        return None

    kind = msg.get("kind")
    if kind == "commit":
        metrics = _classify_commit(msg.get("commit"))
    elif kind == "account":
        metrics = _classify_account(msg.get("account"))
    elif kind == "identity":
        # M0: identity events carried only did/seq/time on 145/145 — no
        # handle. There is nothing non-identity to record beyond the fact
        # that one happened, which is a usable handle-churn proxy carrying
        # no handle.
        metrics = ["identity.event"]
    else:
        metrics = ["unclassified.kind"]

    return Classification(time_us=time_us, metrics=tuple(metrics))
