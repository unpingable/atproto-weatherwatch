"""Small, versioned registry of Weather Watch's published findings.

This is intentionally not a general publishing framework.  It is the stable,
machine-readable source for the paper-like finding pages emitted by
``weatherwatch report``.  A finding records an aggregate result and its
limits; it never turns a candidate report into publication authority and it
never retains the event identities that would be needed for set comparison.
"""

from __future__ import annotations

import json
from pathlib import Path


SCHEMA_INDEX = "weatherwatch.findings.index/v1"
SCHEMA_FINDING = "weatherwatch.finding/v1"
SCHEMA_RECEIPT = "weatherwatch.finding.aggregate-receipt/v1"

OBSERVER_DIVERGENCE_SLUG = "observer-divergence-2026-08"
OBSERVER_DIVERGENCE_ID = "weatherwatch.finding.observer-divergence.2026-08"


def observer_divergence() -> dict:
    """The published M0 observer-divergence finding, as aggregate facts."""
    return {
        "schema": SCHEMA_FINDING,
        "finding_id": OBSERVER_DIVERGENCE_ID,
        "slug": OBSERVER_DIVERGENCE_SLUG,
        "status": "published",
        "published_month": "2026-08",
        "headline": "Jetstream observers disagree.",
        "claim": (
            "Concurrent same-region public Jetstream observers reported "
            "substantially different post volumes over the same interval."
        ),
        "result": {
            "headline_ratio": 1.611,
            "display_ratio": "1.61×",
            "same_observer_control_ratio": 1.0,
            "display_control_ratio": "1.000×",
            "comparison": "jetstream1.us-east / jetstream2.us-east",
        },
        "design": {
            "duration_seconds": 120.0,
            "concurrent": True,
            "same_region": "us-east",
            "metric": "app.bsky.feed.post events delivered",
            "observers": [
                {"endpoint": "jetstream1.us-east", "sockets": 1,
                 "post_events": 7088, "relative_to_high": 1.0},
                {"endpoint": "jetstream2.us-east", "sockets": 2,
                 "post_events_per_socket": [4400, 4400],
                 "relative_to_high": 4400 / 7088},
            ],
        },
        "implication": (
            "An observed firehose volume may depend on observer choice; "
            "a study must disclose and condition on its observation source."
        ),
        "limitations": [
            "This is an inter-observer comparison, not a coverage estimate.",
            "There is no canonical denominator in this experiment.",
            "The result does not establish which observer was more complete.",
            "Equal aggregate counts do not prove equal event sets.",
            "Weather Watch retained no raw events or event identities, so it "
            "cannot perform set inclusion or set equality analysis.",
        ],
        "repository_receipts": [
            "M0-VERIFICATION-RESULTS.md",
            "docs/JETSTREAM-OBSERVER-DIVERGENCE.md",
            "measurements/instances2.json",
            "spike/m0_probe.py",
        ],
        "public_receipts": [
            "finding.json",
            "receipts/instances2.json",
        ],
    }


def aggregate_receipt() -> dict:
    """Identity-free aggregate receipt underlying the finding."""
    return {
        "schema": SCHEMA_RECEIPT,
        "finding_id": OBSERVER_DIVERGENCE_ID,
        "note": (
            "Aggregate post counts over one concurrent 120-second window. "
            "Equal counts are consistent with, but do not prove, identical "
            "event sets; this project retained no per-event identity."
        ),
        "seconds": 120.0,
        "cross_instance_ratio_j1east_over_j2east": 1.611,
        "cross_instance_ratio_j1west_over_j2east": 1.575,
        "same_instance_ratio_A_over_B": 1.0,
        "legs": {
            "jetstream1.us-east": {
                "post_events": 7088, "events_per_second": 59.7,
                "stream_span_us": 119_967_024, "total_events": 7169,
            },
            "jetstream1.us-west": {
                "post_events": 6928, "events_per_second": 58.3,
                "stream_span_us": 120_003_158, "total_events": 6996,
            },
            "jetstream2.us-east#A": {
                "post_events": 4400, "events_per_second": 37.3,
                "stream_span_us": 119_971_796, "total_events": 4476,
            },
            "jetstream2.us-east#B": {
                "post_events": 4400, "events_per_second": 37.3,
                "stream_span_us": 119_971_796, "total_events": 4476,
            },
        },
    }


def index_document() -> dict:
    finding = observer_divergence()
    return {
        "schema": SCHEMA_INDEX,
        "latest_finding_id": finding["finding_id"],
        "findings": [{
            "finding_id": finding["finding_id"],
            "slug": finding["slug"],
            "status": finding["status"],
            "published_month": finding["published_month"],
            "headline": finding["headline"],
            "display_result": finding["result"]["display_ratio"],
            "path": f"{finding['slug']}/",
        }],
    }


def write_artifacts(root: Path) -> dict:
    """Write the stable machine artifacts; HTML is owned by ``report``."""
    finding = observer_divergence()
    finding_dir = root / "findings" / finding["slug"]
    receipt_dir = finding_dir / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (root / "findings" / "index.json").write_text(
        json.dumps(index_document(), indent=2) + "\n", encoding="utf-8")
    (finding_dir / "finding.json").write_text(
        json.dumps(finding, indent=2) + "\n", encoding="utf-8")
    (receipt_dir / "instances2.json").write_text(
        json.dumps(aggregate_receipt(), indent=2) + "\n", encoding="utf-8")
    return {"count": 1, "latest_finding_id": finding["finding_id"]}
