"""M0 spike: structural scrubber. Deny-by-default.

Takes a live Jetstream envelope, returns a fixture that preserves ONLY the
structure a future classifier reads, with every identity-bearing or
user-authored value replaced by an obvious synthetic value or dropped.

Design rule: omission and replacement. Not anonymization. A key that is not
explicitly known to be structural is DROPPED, and the drop is counted so the
drop histogram can be reviewed.

The raw envelope is never written to disk by any caller of this module.
"""

from __future__ import annotations

# --- Synthetic value vocabulary -------------------------------------------
# Deliberately not did:plc: / did:web: — the privacy validator rejects those
# prefixes outright, so a synthetic value can never be mistaken for a real one.
# did:example: is the W3C-reserved example method. .invalid is RFC 2606.

SYNTH_DID = "did:example:synth0000000000000001"
SYNTH_DID_B = "did:example:synth0000000000000002"
SYNTH_HANDLE = "synth0001.invalid"
SYNTH_CID = "bafysynthetic000000000000000000000000000001"
SYNTH_RKEY = "synthrkey0000001"
SYNTH_REV = "synthrev0000001"
SYNTH_TIME = "2020-01-01T00:00:00.000Z"
SYNTH_HTTP = "https://example.invalid/synthetic"
SYNTH_AT_URI = f"at://{SYNTH_DID_B}/app.bsky.feed.post/{SYNTH_RKEY}"

# Self-label values are a small closed-ish vocabulary and are classifier
# relevant. Anything outside it is user-controllable text -> replaced.
KNOWN_SELF_LABELS = {
    "!no-unauthenticated", "porn", "sexual", "nudity", "graphic-media",
}

# NSID-valued keys: preserved verbatim. These are protocol vocabulary, not
# user content.
NSID_KEYS = {"$type", "collection"}

# Enum / numeric / structural scalars preserved verbatim.
PASSTHROUGH_KEYS = {
    "kind", "operation", "active", "status", "seq", "langs",
    "byteStart", "byteEnd", "width", "height", "size", "mimeType",
    "aspectRatio", "index",
}

# Containers recursed into.
CONTAINER_KEYS = {
    "commit", "record", "identity", "account", "reply", "root", "parent",
    "embed", "media", "external", "images", "image", "video", "thumb",
    "avatar", "banner", "facets", "features", "labels", "values", "ref",
    "captions", "file", "aspectRatio", "index", "pinnedPost",
}

# Lists of safe scalars (BCP-47 language tags) preserved verbatim. Handled
# before the container branch, which would otherwise drop them.
LIST_PASSTHROUGH_KEYS = {"langs"}

# User-authored text and personal fields: dropped outright, never replaced.
DROP_KEYS = {
    "text", "alt", "title", "description", "displayName", "tag", "nickname",
    "email", "bio", "name", "note", "reason", "comment",
}


class Scrubber:
    """Stateful so time_us can be synthesised as a monotonic sequence."""

    def __init__(self, start_time_us: int = 1_700_000_000_000_000):
        self._next_time_us = start_time_us
        self.dropped_keys: dict[str, int] = {}
        self.kept_keys: dict[str, int] = {}

    def _mint_time_us(self) -> int:
        v = self._next_time_us
        self._next_time_us += 1_000
        return v

    def _note_drop(self, key: str) -> None:
        self.dropped_keys[key] = self.dropped_keys.get(key, 0) + 1

    def _note_keep(self, key: str) -> None:
        self.kept_keys[key] = self.kept_keys.get(key, 0) + 1

    # -- value transforms ---------------------------------------------------

    def _scrub_uri(self, value):
        if not isinstance(value, str):
            return SYNTH_AT_URI
        if value.startswith("at://"):
            # Preserve the collection segment: a classifier may branch on it.
            parts = value[len("at://"):].split("/")
            if len(parts) >= 2:
                return f"at://{SYNTH_DID_B}/{parts[1]}/{SYNTH_RKEY}"
            return SYNTH_AT_URI
        if value.startswith("http://") or value.startswith("https://"):
            return SYNTH_HTTP
        return SYNTH_HTTP

    def _scrub_scalar(self, key: str, value):
        """Return (keep: bool, new_value)."""
        if key in NSID_KEYS or key in PASSTHROUGH_KEYS:
            return True, value
        if key == "did":
            return True, SYNTH_DID
        if key == "handle":
            return True, SYNTH_HANDLE
        if key == "cid":
            return True, SYNTH_CID
        if key == "$link":
            return True, SYNTH_CID
        if key == "rkey":
            return True, SYNTH_RKEY
        if key == "rev":
            return True, SYNTH_REV
        if key in ("createdAt", "time", "indexedAt", "updatedAt"):
            return True, SYNTH_TIME
        if key == "time_us":
            return True, self._mint_time_us()
        if key in ("uri", "list"):
            return True, self._scrub_uri(value)
        if key == "subject":
            # scalar subject is a bare DID (follow / block records)
            return True, SYNTH_DID_B
        if key == "val":
            return True, value if value in KNOWN_SELF_LABELS else "synthlabel"
        if key == "src":
            return True, SYNTH_DID
        return False, None

    # -- recursion ----------------------------------------------------------

    def scrub(self, node, key: str | None = None):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in DROP_KEYS:
                    self._note_drop(k)
                    continue
                if isinstance(v, list) and k in LIST_PASSTHROUGH_KEYS:
                    self._note_keep(k)
                    out[k] = [x for x in v if isinstance(x, (str, int, float))][:8]
                    continue
                if isinstance(v, (dict, list)):
                    if k in CONTAINER_KEYS or k in ("subject",):
                        self._note_keep(k)
                        out[k] = self.scrub(v, k)
                    else:
                        self._note_drop(k)
                    continue
                keep, newv = self._scrub_scalar(k, v)
                if keep:
                    self._note_keep(k)
                    out[k] = newv
                else:
                    self._note_drop(k)
            return out
        if isinstance(node, list):
            return [self.scrub(item, key) for item in node]
        return node

    def scrub_envelope(self, msg: dict) -> dict:
        """Scrub a full Jetstream envelope. Also records whether the commit
        carried a `record` key at all — structurally load-bearing for delete
        classification, and lost if `record` is simply absent from the output.
        """
        out = self.scrub(msg)
        commit = msg.get("commit")
        if isinstance(commit, dict) and isinstance(out.get("commit"), dict):
            out["commit"]["_record_key_present"] = "record" in commit
        return out
