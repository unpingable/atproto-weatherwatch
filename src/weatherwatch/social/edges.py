"""Edge extraction — the identity-bearing parallel to `classify()`.

`classify()` is documented as the identity boundary of the weather lane: its
output alphabet is a finite metric set, so a DID cannot appear in it. That
guarantee is exactly why the weather lane cannot answer the questions this
package asks. Concentration, overlap and synchronisation are statements about
*who acted on whom*; a counter has thrown that away before it is stored.

So this module reads the same message and keeps the edge. It is a different
retention posture, and it is deliberately a different sink writing a different
database file. Nothing here changes what the weather lane stores.

WHAT IS KEPT, AND ONLY THIS
---------------------------
observation time, actor DID, subject reference, operation, the record's own
`createdAt`, the repo revision, and the record CID. That is the minimum set
that supports velocity, concentration, overlap and synchronisation.

WHAT IS NEVER KEPT
------------------
Post text, handles, display names, descriptions, avatars, profile records of
any kind, or any per-actor aggregate. `store.py` enforces the first four
structurally (there is no column to put them in) and `test_boundaries.py`
enforces the last one (there is no table to put it in).

CLOCK
-----
`observed_us` is Jetstream `time_us`, the relay-observed clock — the same
clock the weather lane windows on. `record_created_at` is producer-controlled
and M0 observed year-2999 values in the wild; it is retained verbatim as a
*claim by the emitting repo* and is never used to order or window anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..classify import (
    ACCOUNT_STATUS_OTHER,
    ACCOUNT_STATUSES,
    COLLECTION_ALIASES,
    OPERATIONS,
)

#: Collections whose records name a *target*. Derived from the weather lane's
#: alias table rather than restated, so a collection cannot be tracked here
#: and untracked there (or vice versa) without the mismatch being visible.
#:
#: Mapping is alias -> where the target lives in the record:
#:   "did"      record["subject"] is a bare DID string
#:   "uri"      record["subject"]["uri"] is an AT-URI
#:   "did+list" record["subject"] is a DID, record["list"] is the list AT-URI
EDGE_TARGET_SHAPES: dict[str, str] = {
    "block": "did",
    "follow": "did",
    "like": "uri",
    "repost": "uri",
    "listitem": "did+list",
}

#: Alias -> NSID, inverted from the weather lane's table.
ALIAS_TO_NSID: dict[str, str] = {v: k for k, v in COLLECTION_ALIASES.items()}

#: Every collection this sink retains. Everything else is counted as skipped
#: and its payload discarded, exactly as the weather lane does with untracked
#: vocabulary.
TRACKED_ALIASES: frozenset[str] = frozenset(EDGE_TARGET_SHAPES)

SUBJECT_DID = "did"
SUBJECT_URI = "uri"
#: A delete commit carries no record, so the target is not in the message.
#: The event is still retained — a withdrawal is an event, and dropping it
#: would make unblocks and unlikes invisible, which is precisely the failure
#: mode the estate's own observability matrix warns about.
SUBJECT_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EdgeEvent:
    """One actor -> subject record operation, as observed."""

    observed_us: int
    actor_did: str
    collection: str          # alias, e.g. "block"
    op: str                  # create | update | delete
    subject_kind: str        # did | uri | unknown
    subject_ref: str         # DID, AT-URI, or "" when unknown
    rkey: str
    rev: str                 # repo revision at the commit
    cid: str                 # content address of the record; "" on delete
    record_created_at: str   # producer claim, verbatim; "" if absent


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """An account lifecycle transition (`kind: "account"`).

    ATProto distinguishes deactivated / deleted / suspended / takendown, so
    disappearance does not have to be inferred from absence. Note that a
    `deactivated` transition also occurs during PDS migration; this module
    records the transition and does not interpret it.
    """

    observed_us: int
    actor_did: str
    active: int              # 1 | 0 | -1 unknown
    status: str              # deactivated | deleted | ... | other | ""


@dataclass(frozen=True, slots=True)
class Skipped:
    """Why a message produced no edge. Counted, never stored per-event."""

    reason: str


SKIP_NOT_COMMIT = "not_commit"
SKIP_UNTRACKED = "untracked_collection"
SKIP_MALFORMED = "malformed"
SKIP_NO_TIME = "no_time_us"
SKIP_NO_SUBJECT = "no_subject"

SKIP_REASONS = (
    SKIP_NOT_COMMIT, SKIP_UNTRACKED, SKIP_MALFORMED,
    SKIP_NO_TIME, SKIP_NO_SUBJECT,
)


def _status_bucket(status: Any) -> str:
    if not isinstance(status, str) or not status:
        return ""
    return status if status in ACCOUNT_STATUSES else ACCOUNT_STATUS_OTHER


def _extract_subject(alias: str, record: Any) -> tuple[str, str] | None:
    """Return (subject_kind, subject_ref), or None if the record is malformed."""
    shape = EDGE_TARGET_SHAPES[alias]
    if not isinstance(record, Mapping):
        return None
    subject = record.get("subject")

    if shape in ("did", "did+list"):
        if isinstance(subject, str) and subject.startswith("did:"):
            return (SUBJECT_DID, subject)
        return None

    # shape == "uri": a strongRef, {"uri": ..., "cid": ...}
    if isinstance(subject, Mapping):
        uri = subject.get("uri")
        if isinstance(uri, str) and uri.startswith("at://"):
            return (SUBJECT_URI, uri)
    return None


def extract(msg: Any) -> EdgeEvent | StatusEvent | Skipped:
    """Pull an edge or a lifecycle transition out of one Jetstream message.

    Total function: every message yields an event or a counted skip reason.
    Never raises on hostile input — an observer that dies on a malformed
    message is an observer with a coverage hole it cannot report.
    """
    if not isinstance(msg, Mapping):
        return Skipped(SKIP_MALFORMED)

    time_us = msg.get("time_us")
    if not isinstance(time_us, int):
        return Skipped(SKIP_NO_TIME)

    did = msg.get("did")
    if not isinstance(did, str) or not did:
        return Skipped(SKIP_MALFORMED)

    kind = msg.get("kind")

    if kind == "account":
        account = msg.get("account")
        if not isinstance(account, Mapping):
            return Skipped(SKIP_MALFORMED)
        active = account.get("active")
        return StatusEvent(
            observed_us=time_us,
            actor_did=did,
            active=1 if active is True else (0 if active is False else -1),
            status=_status_bucket(account.get("status")),
        )

    if kind != "commit":
        return Skipped(SKIP_NOT_COMMIT)

    commit = msg.get("commit")
    if not isinstance(commit, Mapping):
        return Skipped(SKIP_MALFORMED)

    collection = commit.get("collection")
    alias = COLLECTION_ALIASES.get(collection) if isinstance(collection, str) else None
    if alias is None or alias not in TRACKED_ALIASES:
        return Skipped(SKIP_UNTRACKED)

    op = commit.get("operation")
    if op not in OPERATIONS:
        return Skipped(SKIP_MALFORMED)

    rkey = commit.get("rkey")
    if not isinstance(rkey, str) or not rkey:
        return Skipped(SKIP_MALFORMED)

    rev = commit.get("rev") if isinstance(commit.get("rev"), str) else ""
    cid = commit.get("cid") if isinstance(commit.get("cid"), str) else ""

    if op == "delete":
        # No record travels with a delete. The withdrawal is the event; the
        # target is recoverable only by joining against the create we may or
        # may not have observed. Recorded as unknown rather than guessed.
        return EdgeEvent(
            observed_us=time_us, actor_did=did, collection=alias, op=op,
            subject_kind=SUBJECT_UNKNOWN, subject_ref="", rkey=rkey,
            rev=rev, cid="", record_created_at="",
        )

    record = commit.get("record")
    found = _extract_subject(alias, record)
    if found is None:
        return Skipped(SKIP_NO_SUBJECT)
    subject_kind, subject_ref = found

    created = record.get("createdAt") if isinstance(record, Mapping) else None
    return EdgeEvent(
        observed_us=time_us, actor_did=did, collection=alias, op=op,
        subject_kind=subject_kind, subject_ref=subject_ref, rkey=rkey,
        rev=rev, cid=cid,
        record_created_at=created if isinstance(created, str) else "",
    )


def subject_actor_did(subject_kind: str, subject_ref: str) -> str:
    """The DID a subject reference points *at*, or "" if it names none.

    A like/repost target is an AT-URI whose authority is the target author's
    DID, so `at://did:plc:x/app.bsky.feed.post/3k` resolves to `did:plc:x`.
    This is string decomposition of an identifier already in hand, not a
    lookup: no network call, no resolution, no handle.
    """
    if subject_kind == SUBJECT_DID:
        return subject_ref
    if subject_kind == SUBJECT_URI and subject_ref.startswith("at://"):
        authority = subject_ref[len("at://"):].split("/", 1)[0]
        return authority if authority.startswith("did:") else ""
    return ""
