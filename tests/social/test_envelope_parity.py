"""The vendored envelope must stay the same object driftwatch defines.

Vendoring is only honest if drift is detectable. These tests import
driftwatch's module directly from the sibling checkout when it is present and
compare structure and behaviour. When it is absent -- a standalone weatherwatch
checkout -- they skip, because the vendored copy is required to stand alone.
"""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path

import pytest

from weatherwatch.social import envelope as vendored

ORIGINAL = (Path(__file__).resolve().parents[3]
            / "driftwatch" / "src" / "labeler" / "detection.py")

#: The single documented delta. Adding to this list is how a second fork
#: starts, so it is deliberately a literal.
KNOWN_SUBJECT_TYPE_DELTA = {"episode"}


@pytest.fixture(scope="module")
def original():
    if not ORIGINAL.exists():
        pytest.skip(f"driftwatch not checked out at {ORIGINAL}")
    spec = importlib.util.spec_from_file_location("_dw_detection", ORIGINAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["SubjectRef", "EvidenceStub", "DetectionEnvelope"])
def test_dataclass_fields_match(original, name):
    def shape(cls):
        # Compare by type *name*: `subject: SubjectRef` resolves to a
        # different class object in each module, which is the vendoring
        # working, not drift.
        return {f.name: getattr(f.type, "__name__", str(f.type))
                for f in dataclasses.fields(cls)}
    a, b = shape(getattr(original, name)), shape(getattr(vendored, name))
    assert a == b, f"{name} drifted from driftwatch"
    assert list(a) == list(b), f"{name} field order drifted"


@pytest.mark.parametrize("const", [
    "ENVELOPE_SCHEMA_VERSION", "MAX_EXPLAIN_TOP_K", "MAX_EXPLAIN_STR_LEN",
    "VALID_SEVERITIES", "VALID_EVIDENCE_KINDS",
])
def test_constants_match(original, const):
    assert getattr(original, const) == getattr(vendored, const)


def test_subject_types_differ_by_exactly_the_documented_delta(original):
    a, b = set(original.VALID_SUBJECT_TYPES), set(vendored.VALID_SUBJECT_TYPES)
    assert a < b
    assert b - a == KNOWN_SUBJECT_TYPE_DELTA


def test_public_functions_match(original):
    def api(mod):
        return {n for n in dir(mod)
                if callable(getattr(mod, n)) and not n.startswith("_")
                and getattr(getattr(mod, n), "__module__", "") == mod.__name__}
    assert api(original) == api(vendored)


def test_hashing_is_bit_identical(original):
    obj = {"b": [3, 1, 2], "a": {"z": 1.0 / 3, "y": True}, "c": None}
    assert original.stable_json(obj) == vendored.stable_json(obj)
    assert original.receipt_hash(obj) == vendored.receipt_hash(obj)


def test_envelopes_seal_identically(original):
    kwargs = dict(
        detector_id="d", detector_version="v1",
        ts_start="2026-01-01T00:00:00+00:00",
        ts_end="2026-01-01T00:01:00+00:00", window="60s",
        detection_type="t", score=1.5, severity="low",
        explain={"k": "v"}, window_fingerprint="wf", config_hash="ch",
    )
    a = original.build_envelope(
        subject=original.SubjectRef("global", ""),
        evidence=(original.EvidenceStub("note", "n"),), **kwargs)
    b = vendored.build_envelope(
        subject=vendored.SubjectRef("global", ""),
        evidence=(vendored.EvidenceStub("note", "n"),), **kwargs)
    assert a.receipt_hash == b.receipt_hash
    assert a.det_id == b.det_id


def test_vendored_module_stands_alone():
    """No import of driftwatch anywhere in the package."""
    root = Path(vendored.__file__).resolve().parent
    for path in root.rglob("*.py"):
        text = path.read_text()
        assert "import driftwatch" not in text
        assert "from driftwatch" not in text
        assert "labeler.detection" not in text or path.name == "envelope.py"
