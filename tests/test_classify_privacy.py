"""The classifier privacy boundary.

Two independent arguments, both required:

1. **Structural** — the output alphabet is a finite frozenset. Nothing outside
   `ALLOWED_METRICS` can ever be emitted, so no identity-bearing string can be.
2. **Empirical** — run every fixture through and grep the outputs for the
   shapes we care about. This is the weaker check, but it catches a future
   change that widens the alphabet.

If argument 1 ever fails, the design has been broken, not just a test.
"""

from __future__ import annotations

import pathlib
import re

from weatherwatch.classify import ALLOWED_METRICS, Classification, classify

# Patterns for the things that must never reach a metric key. Note metric
# keys are themselves dotted (`post.create.embed.images`), so a generic
# "looks like a hostname" rule would match them; the handle check is anchored
# on real TLDs instead.
FORBIDDEN_PATTERNS = {
    "DID": re.compile(r"did:[a-z0-9]+:", re.I),
    "at-URI": re.compile(r"at://", re.I),
    "URL": re.compile(r"https?://", re.I),
    "handle-like": re.compile(
        r"\.(social|com|net|org|io|dev|xyz|app|me|co|bsky|blue|gay)\b", re.I),
    "CID-like": re.compile(r"\bbafy[a-z0-9]{10,}", re.I),
    "long-free-text": re.compile(r"\s"),  # metric keys never contain whitespace
}

#: The alphabet is not just finite, it is shaped: lowercase dotted segments,
#: optionally with a leading `!` for self-label style values. Anything that
#: could carry a payload fails this.
METRIC_KEY_SHAPE = re.compile(r"^[a-z][a-zA-Z0-9_]*(\.[a-zA-Z0-9_!-]+)*$")


def _all_outputs(fixtures):
    for f in fixtures:
        c = classify(f["event"])
        if c is not None:
            yield f, c


def test_output_alphabet_is_finite_and_enumerable():
    assert isinstance(ALLOWED_METRICS, frozenset)
    assert 0 < len(ALLOWED_METRICS) < 500
    for m in ALLOWED_METRICS:
        assert isinstance(m, str) and m
        assert METRIC_KEY_SHAPE.match(m), f"metric {m!r} is not a plain key"
        assert len(m) < 64, f"metric {m!r} is suspiciously long"
        for name, pat in FORBIDDEN_PATTERNS.items():
            assert not pat.search(m), f"metric {m!r} matches forbidden {name}"


def test_the_published_alphabet_size_is_the_actual_one():
    """The repository states this number in four public places — the README
    twice, `BOUNDARIES.md`, and `classify.py` itself — as the size of the
    identity boundary. It read `~90` against an actual 63 until 2026-08-24.

    The privacy guarantee never depended on the figure; a public claim about
    an inspectable structure being wrong by 40% still costs the thing the
    figure was there to buy. Pinning it makes a change deliberate.
    """
    assert len(ALLOWED_METRICS) == 63

    root = pathlib.Path(__file__).resolve().parent.parent
    claims = (root / "README.md", root / "src/weatherwatch/social/BOUNDARIES.md",
              root / "src/weatherwatch/classify.py")
    for path in claims:
        text = path.read_text(encoding="utf-8")
        assert "~90" not in text, f"{path.name} still claims ~90 metrics"
        assert "63" in text, f"{path.name} no longer states the alphabet size"


def test_every_fixture_output_is_within_the_allowed_alphabet(all_fixtures):
    checked = 0
    for _f, c in _all_outputs(all_fixtures):
        for m in c.metrics:
            assert m in ALLOWED_METRICS, f"metric {m!r} escaped the alphabet"
        checked += 1
    assert checked > 250, "fixture corpus unexpectedly small"


def test_no_identity_bearing_value_in_any_classifier_output(all_fixtures):
    for f, c in _all_outputs(all_fixtures):
        for m in c.metrics:
            for name, pat in FORBIDDEN_PATTERNS.items():
                assert not pat.search(m), (
                    f"{name} leaked into metric {m!r} from shape {f.get('_shape')}"
                )


def test_classification_carries_only_two_fields():
    c = classify({"kind": "identity", "time_us": 1_700_000_000_000_000,
                  "did": "did:example:synth0000000000000001"})
    assert isinstance(c, Classification)
    assert set(Classification.__slots__) == {"time_us", "metrics"}
    assert isinstance(c.time_us, int)
    assert all(isinstance(m, str) for m in c.metrics)


def test_identity_bearing_input_is_read_but_never_returned():
    """The DID is right there in the envelope; it must not come back out."""
    msg = {
        "kind": "commit",
        "time_us": 1_700_000_000_000_000,
        "did": "did:example:synth0000000000000001",
        "commit": {
            "collection": "app.bsky.feed.post",
            "operation": "create",
            "rkey": "synthrkey0000001",
            "cid": "bafysynthetic000000000000000000000000000001",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": "some user authored text",
                "createdAt": "2020-01-01T00:00:00.000Z",
            },
        },
    }
    c = classify(msg)
    blob = repr(c)
    for needle in ("did:", "synthrkey", "bafy", "some user authored text",
                   "2020-01-01"):
        assert needle not in blob, f"{needle!r} survived into {blob!r}"
