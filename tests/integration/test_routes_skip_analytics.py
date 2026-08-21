"""Integration tests for skip history analytics routes (issue #70)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _seed() -> None:
    await db.record_skip_event(
        plex_guid="guid-a", title="Movie A", username="alice",
        client_identifier="c1", client_title="Apple TV",
        category="nudity", action="skip", latency_ms=120, success=True,
    )
    await db.record_skip_event(
        plex_guid="guid-a", title="Movie A", username="alice",
        client_identifier="c2", client_title="Roku",
        category="language", action="mute", latency_ms=900, success=False,
    )


async def test_by_category_returns_counts(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/by-category")

    assert resp.status_code == 200
    categories = {c["category"]: c["count"] for c in resp.json()["categories"]}
    assert categories == {"nudity": 1, "language": 1}


async def test_by_client_reports_failure_rate_and_latency(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/by-client")
    clients = resp.json()["clients"]

    assert resp.status_code == 200
    assert clients[0]["client_title"] == "Roku"
    assert clients[0]["failure_rate"] == 1.0
    assert clients[0]["avg_latency_ms"] == 900


async def test_top_titles_ranks_by_volume(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/top-titles")

    assert resp.json()["titles"][0]["title"] == "Movie A"
    assert resp.json()["titles"][0]["count"] == 2


async def test_history_returns_events_newest_first(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/history?limit=1")
    body = resp.json()

    assert resp.status_code == 200
    assert body["total"] == 2
    assert len(body["events"]) == 1


async def test_history_rejects_an_out_of_range_limit(http_client):
    resp = await http_client.get("/api/analytics/skips/history?limit=9999")

    assert resp.status_code == 422


async def test_empty_history_returns_empty_aggregates(http_client):
    resp = await http_client.get("/api/analytics/skips/by-category")

    assert resp.status_code == 200
    assert resp.json()["categories"] == []
