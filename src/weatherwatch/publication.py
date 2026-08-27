"""Weatherwatch's local candidate-publication gate.

This module reports whether a rendered directory is structurally complete and
passes the same identity-shaped-value tripwire used by ``deploy/publish.sh``.
Passing is only local eligibility: it is not evidence that publication
occurred and confers no publication authority.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import findings


SCHEMA = "weatherwatch.publication_gate.v1"
_FINDING_ROOT = f"findings/{findings.OBSERVER_DIVERGENCE_SLUG}"
REQUIRED_ARTIFACTS = (
    "index.html",
    "summary.json",
    "social.json",
    "findings/index.json",
    f"{_FINDING_ROOT}/index.html",
    f"{_FINDING_ROOT}/finding.json",
    f"{_FINDING_ROOT}/receipts/instances2.json",
)

# Keep these byte patterns aligned with the former shell gate.  Labels are
# returned instead of matching bytes so a refusal never repeats an identity.
IDENTITY_PATTERNS = (
    ("did", re.compile(rb"did:(?:plc|web|key):", re.IGNORECASE)),
    ("at_uri", re.compile(rb"at://", re.IGNORECASE)),
    ("cid", re.compile(rb"bafy[a-z0-9]{10,}", re.IGNORECASE)),
    ("bluesky_handle", re.compile(
        rb"[a-z0-9-]+\.bsky\.(?:social|app)", re.IGNORECASE)),
    ("actor_token", re.compile(rb"\ba:[0-9a-f]{12}\b", re.IGNORECASE)),
)


def evaluate_candidate(report_dir: str | Path) -> dict:
    """Evaluate one local rendered candidate without changing it."""
    root = Path(report_dir)
    result = {
        "schema": SCHEMA,
        "report_dir": str(root),
        "disposition": "NOT_EVALUATED",
        "publication_authority": False,
        "published": False,
        "required_artifacts": list(REQUIRED_ARTIFACTS),
        "missing_artifacts": [],
        "files_scanned": 0,
        "refusals": [],
    }
    if not root.exists():
        result["reason"] = "candidate directory absent"
        return result
    if not root.is_dir():
        result["disposition"] = "ERROR"
        result["reason"] = "candidate path is not a directory"
        return result

    missing = [name for name in REQUIRED_ARTIFACTS
               if not (root / name).is_file()]
    result["missing_artifacts"] = missing
    if missing:
        result["disposition"] = "REFUSED"
        result["refusals"].append({
            "kind": "candidate_incomplete", "files": missing,
        })
        return result

    try:
        entries = sorted(root.rglob("*"))
        for path in entries:
            if path.is_symlink():
                result["refusals"].append({
                    "kind": "unsafe_symlink",
                    "file": path.relative_to(root).as_posix(),
                })
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()
            result["files_scanned"] += 1
            for kind, pattern in IDENTITY_PATTERNS:
                if pattern.search(data):
                    result["refusals"].append({
                        "kind": "identity_shaped_value",
                        "shape": kind,
                        "file": path.relative_to(root).as_posix(),
                    })
    except OSError as exc:
        result["disposition"] = "ERROR"
        result["reason"] = f"candidate could not be read: {type(exc).__name__}"
        return result

    if result["refusals"]:
        result["disposition"] = "REFUSED"
        result["reason"] = "privacy gate refused the candidate"
    else:
        result["disposition"] = "PASSED"
        result["reason"] = "candidate is structurally complete and privacy-clean"
    return result
