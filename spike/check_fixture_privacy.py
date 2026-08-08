"""Fixture privacy validator. Fails loud if a committed fixture carries real
identity or user-authored text.

This is a tripwire, not an anonymiser. The scrubber's job is omission and
replacement; this script's job is to prove the scrubber did it.

Run:  python3 spike/check_fixture_privacy.py
Exit: 0 clean, 1 violations found (prints every violation).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures"

# --- what a synthetic value is allowed to look like ------------------------

ALLOWED_DID_PREFIX = "did:example:"
ALLOWED_HOST_TLDS = {"invalid", "example", "test", "localhost"}

# Keys whose values are protocol vocabulary (NSIDs), not user content.
NSID_VALUED_KEYS = {"$type", "collection"}

# Metadata keys this spike adds to fixture lines; not part of the event.
META_KEYS = {"_shape", "_note", "_record_key_present"}

# Keys that must never survive scrubbing at all.
FORBIDDEN_KEYS = {
    "text", "alt", "title", "description", "displayName", "tag", "tags",
    "nickname", "email", "bio", "note", "comment", "name",
}

# Longest string a scrubbed fixture should contain. The synthetic CID is 42
# chars and synthetic at:// URIs run to ~80; anything beyond is suspicious.
MAX_STRING_LEN = 96

FORBIDDEN_SUBSTRINGS = ["did:plc:", "did:web:", "did:key:"]

DID_RE = re.compile(r"did:[a-zA-Z0-9]+:")
HOSTLIKE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$", re.I)
URL_RE = re.compile(r"^https?://([^/\s]+)", re.I)
AT_URI_RE = re.compile(r"^at://([^/\s]+)")
# Handles that look like real Bluesky accounts.
BSKY_HANDLE_RE = re.compile(r"\b[a-z0-9][a-z0-9.-]*\.(bsky\.social|bsky\.app|bsky\.team)\b", re.I)


class Violations:
    def __init__(self):
        self.items: list[str] = []

    def add(self, path: str, line_no: int, msg: str):
        self.items.append(f"{path}:{line_no}: {msg}")

    def __bool__(self):
        return bool(self.items)


def check_string(v: str, key: str | None, where: str, line_no: int, viol: Violations):
    for bad in FORBIDDEN_SUBSTRINGS:
        if bad in v:
            viol.add(where, line_no, f"forbidden DID method {bad!r} in value at key {key!r}")

    for m in DID_RE.finditer(v):
        if not v[m.start():].startswith(ALLOWED_DID_PREFIX):
            viol.add(where, line_no,
                     f"non-synthetic DID method {m.group(0)!r} at key {key!r} "
                     f"(only {ALLOWED_DID_PREFIX!r} allowed)")

    if BSKY_HANDLE_RE.search(v):
        viol.add(where, line_no, f"plausible Bluesky handle in value at key {key!r}")

    if key in NSID_VALUED_KEYS:
        return  # NSIDs are reverse-DNS protocol vocabulary, not hosts

    m = AT_URI_RE.match(v)
    if m:
        authority = m.group(1)
        if not authority.startswith(ALLOWED_DID_PREFIX):
            viol.add(where, line_no,
                     f"at:// URI with non-synthetic authority {authority!r} at key {key!r}")
        return

    m = URL_RE.match(v)
    if m:
        host = m.group(1).split(":")[0]
        if host.rsplit(".", 1)[-1].lower() not in ALLOWED_HOST_TLDS:
            viol.add(where, line_no, f"URL to non-reserved host {host!r} at key {key!r}")
        return

    if HOSTLIKE_RE.match(v) and "." in v:
        if v.rsplit(".", 1)[-1].lower() not in ALLOWED_HOST_TLDS:
            viol.add(where, line_no,
                     f"hostname-like value {v!r} at key {key!r} is not on a "
                     f"reserved TLD {sorted(ALLOWED_HOST_TLDS)}")

    if len(v) > MAX_STRING_LEN:
        viol.add(where, line_no,
                 f"string of {len(v)} chars at key {key!r} exceeds "
                 f"{MAX_STRING_LEN} — possible surviving free text")


def walk(node, key, where, line_no, viol: Violations):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in META_KEYS:
                continue
            if k in FORBIDDEN_KEYS:
                viol.add(where, line_no, f"forbidden key {k!r} present in fixture")
                continue
            walk(v, k, where, line_no, viol)
    elif isinstance(node, list):
        for item in node:
            walk(item, key, where, line_no, viol)
    elif isinstance(node, str):
        check_string(node, key, where, line_no, viol)


def check_file(path: Path, viol: Violations) -> int:
    checked = 0
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            viol.add(str(path), line_no, f"fixture line is not valid JSON: {e}")
            continue
        checked += 1
        # _shape/_note are spike metadata, but scan them for DIDs anyway —
        # a shape signature built from a live record could leak one.
        for meta in ("_shape", "_note"):
            if isinstance(obj.get(meta), str):
                for bad in FORBIDDEN_SUBSTRINGS:
                    if bad in obj[meta]:
                        viol.add(str(path), line_no, f"forbidden {bad!r} in {meta}")
        walk(obj, None, str(path), line_no, viol)
    return checked


# --- self-test: prove the validator actually rejects things ----------------

SELFTEST_BAD = [
    ('{"event": {"did": "did:plc:abc123def456"}}', "did:plc: prefix"),
    ('{"event": {"did": "did:web:example.com"}}', "did:web: prefix"),
    ('{"event": {"handle": "alice.bsky.social"}}', "bluesky handle"),
    ('{"event": {"handle": "someone.example.com"}}', "real-TLD hostname"),
    ('{"event": {"commit": {"record": {"text": "hello world"}}}}', "text key"),
    ('{"event": {"commit": {"record": {"alt": "a photo"}}}}', "alt key"),
    ('{"event": {"uri": "at://did:plc:xyz/app.bsky.feed.post/abc"}}', "real at:// authority"),
    ('{"event": {"uri": "https://realsite.com/page"}}', "non-reserved URL host"),
    ('{"event": {"x": "' + "z" * 200 + '"}}', "over-long string"),
]

SELFTEST_GOOD = [
    '{"event": {"did": "did:example:synth0000000000000001"}}',
    '{"event": {"handle": "synth0001.invalid"}}',
    '{"event": {"commit": {"collection": "app.bsky.feed.post", "operation": "create"}}}',
    '{"event": {"uri": "at://did:example:synth0000000000000002/app.bsky.feed.post/synthrkey0000001"}}',
    '{"event": {"record": {"$type": "app.bsky.richtext.facet#link"}}}',
    '{"event": {"record": {"langs": ["en", "ja"]}}}',
    '{"event": {"uri": "https://example.invalid/synthetic"}}',
]


def selftest() -> int:
    failures = []
    for line, why in SELFTEST_BAD:
        v = Violations()
        walk(json.loads(line), None, "<selftest>", 0, v)
        if not v:
            failures.append(f"validator FAILED to reject {why}: {line[:80]}")
    for line in SELFTEST_GOOD:
        v = Violations()
        walk(json.loads(line), None, "<selftest>", 0, v)
        if v:
            failures.append(f"validator wrongly rejected clean line: {line[:80]} -> {v.items}")
    if failures:
        print("SELFTEST FAIL:", file=sys.stderr)
        for f in failures:
            print("  " + f, file=sys.stderr)
        return 1
    print(f"selftest OK: {len(SELFTEST_BAD)} bad patterns rejected, "
          f"{len(SELFTEST_GOOD)} clean patterns accepted")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if selftest() != 0:
        return 1
    viol = Violations()
    files = sorted(FIXTURE_DIR.glob("*.jsonl"))
    if not files:
        print(f"FAIL: no fixture files found in {FIXTURE_DIR}", file=sys.stderr)
        return 1

    total = 0
    for f in files:
        n = check_file(f, viol)
        total += n
        print(f"checked {n:4d} fixture lines in {f.name}")

    raw = FIXTURE_DIR / "malformed_lines.txt"
    if raw.exists():
        for line_no, line in enumerate(raw.read_text().splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            check_string(line, None, str(raw), line_no, viol)
        print(f"checked malformed_lines.txt")

    if viol:
        print(f"\nFAIL: {len(viol.items)} privacy violation(s):", file=sys.stderr)
        for v in viol.items[:200]:
            print("  " + v, file=sys.stderr)
        if len(viol.items) > 200:
            print(f"  ... and {len(viol.items) - 200} more", file=sys.stderr)
        return 1

    print(f"\nOK: {total} fixture lines clean across {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
