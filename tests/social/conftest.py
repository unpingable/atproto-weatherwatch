"""Fixtures for the social sensors. Deterministic; no network, no clock reads."""

from __future__ import annotations

import pytest

from weatherwatch.social import store
from weatherwatch.social.edges import EdgeEvent, StatusEvent

from ..conftest import SYNTH_BASE, SYNTH_WIDTH, build_run  # noqa: F401

#: Microseconds at the synthetic epoch, so edge tests and weather tests sit on
#: the same clock.
BASE_US = SYNTH_BASE * 1_000_000


@pytest.fixture()
def edge_conn(tmp_path):
    c = store.connect(tmp_path / "social.sqlite")
    store.init_db(c)
    yield c
    c.close()


def steady_then_burst(
    baseline_count: int = 10,
    burst_count: int = 90,
    n_baseline: int = 20,
    n_burst: int = 4,
    n_tail: int = 6,
    metric: str = "block.create",
) -> list[dict]:
    """A flat-ish baseline, one clear excursion, then a return to baseline.

    The baseline jitters by +/-1 because a perfectly constant baseline has
    zero variance, and a z-score against zero variance is undefined rather
    than infinite — `derive.zscore` correctly returns None there.
    """
    windows: list[dict] = []
    for i in range(n_baseline):
        windows.append({"metrics": {metric: baseline_count + (i % 3) - 1}})
    for _ in range(n_burst):
        windows.append({"metrics": {metric: burst_count}})
    for i in range(n_tail):
        windows.append({"metrics": {metric: baseline_count + (i % 3) - 1}})
    return windows


def edge(
    actor: str, target: str, at_us: int, collection: str = "block",
    op: str = "create", rkey: str | None = None, kind: str = "did",
) -> EdgeEvent:
    return EdgeEvent(
        observed_us=at_us, actor_did=actor, collection=collection, op=op,
        subject_kind=kind, subject_ref=target,
        rkey=rkey or f"{actor[-4:]}{target[-4:]}{at_us}",
        rev="rev1", cid="bafy" + str(at_us), record_created_at="",
    )


def write_edges(conn, events: list[EdgeEvent], run_id: str = "run-test") -> None:
    w = store.EdgeWriter(conn, run_id, batch_rows=10_000)
    for e in events:
        w.add_edge(e)
    w.flush(BASE_US)


def write_status(conn, events: list[StatusEvent], run_id: str = "run-test") -> None:
    w = store.EdgeWriter(conn, run_id, batch_rows=10_000)
    for e in events:
        w.add_status(e)
    w.flush(BASE_US)
