"""Integration tests for marker routes (issue #55)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


def _plex_client(**overrides) -> MagicMock:
    client = MagicMock()
    client.get_markers = AsyncMock(return_value=overrides.get("markers", []))
    client.update_marker = AsyncMock()
    client.create_marker = AsyncMock()
    for key, value in overrides.items():
        if key != "markers":
            setattr(client, key, value)
    return client


def _patch_client(client: MagicMock):
    return patch("cleanplex.web.routes.marker_routes.plex_mod.get_client", return_value=client)


async def _stored_marker(start_ms: int = 0, end_ms: int = 30000, plex_marker_id: int = 7) -> int:
    await db.upsert_plex_markers("guid-1", "rk-1", [{
        "plex_marker_id": plex_marker_id,
        "marker_type": "intro",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "final": False,
    }])
    return (await db.get_plex_markers_for_guid("guid-1"))[0]["id"]


# ── POST /api/markers/titles/{rating_key}/sync ────────────────────────────────

async def test_sync_returns_and_stores_upserted_markers(http_client):
    markers = [
        {"plex_marker_id": 1, "marker_type": "intro", "start_ms": 0, "end_ms": 30000, "final": False},
        {"plex_marker_id": 2, "marker_type": "credits", "start_ms": 600000, "end_ms": 660000, "final": True},
    ]

    with _patch_client(_plex_client(markers=markers)):
        resp = await http_client.post(
            "/api/markers/titles/rk-1/sync", json={"plex_guid": "guid-1"}
        )

    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert len(await db.get_plex_markers_for_guid("guid-1")) == 2


async def test_sync_with_no_markers_stores_nothing(http_client):
    with _patch_client(_plex_client(markers=[])):
        resp = await http_client.post(
            "/api/markers/titles/rk-1/sync", json={"plex_guid": "guid-1"}
        )

    assert resp.json()["count"] == 0
    assert await db.get_plex_markers_for_guid("guid-1") == []


async def test_sync_returns_503_when_plex_is_not_configured(http_client):
    with patch(
        "cleanplex.web.routes.marker_routes.plex_mod.get_client",
        side_effect=RuntimeError("not configured"),
    ):
        resp = await http_client.post(
            "/api/markers/titles/rk-1/sync", json={"plex_guid": "guid-1"}
        )

    assert resp.status_code == 503


async def test_sync_returns_502_when_plex_errors(http_client):
    client = _plex_client()
    client.get_markers = AsyncMock(side_effect=Exception("connection refused"))

    with _patch_client(client):
        resp = await http_client.post(
            "/api/markers/titles/rk-1/sync", json={"plex_guid": "guid-1"}
        )

    assert resp.status_code == 502


# ── GET /api/markers/titles/{rating_key} ──────────────────────────────────────

async def test_get_markers_for_title_returns_stored_rows(http_client):
    await _stored_marker()

    resp = await http_client.get("/api/markers/titles/rk-1")

    assert resp.status_code == 200
    assert len(resp.json()["markers"]) == 1


async def test_get_markers_for_unknown_title_returns_empty(http_client):
    resp = await http_client.get("/api/markers/titles/rk-missing")

    assert resp.status_code == 200
    assert resp.json()["markers"] == []


# ── PATCH /api/markers/{marker_id} ────────────────────────────────────────────

async def test_patch_writes_to_both_plex_and_the_database(http_client):
    marker_id = await _stored_marker()
    client = _plex_client()

    with _patch_client(client):
        resp = await http_client.patch(
            f"/api/markers/{marker_id}", json={"start_ms": 1000, "end_ms": 25000}
        )

    assert resp.status_code == 200
    client.update_marker.assert_awaited_once()
    stored = await db.get_plex_marker(marker_id)
    assert (stored["start_ms"], stored["end_ms"]) == (1000, 25000)


async def test_patch_returns_403_with_a_message_when_plex_rejects(http_client):
    marker_id = await _stored_marker()
    client = _plex_client()
    client.update_marker = AsyncMock(side_effect=PermissionError("Plex Pass required to edit markers"))

    with _patch_client(client):
        resp = await http_client.patch(
            f"/api/markers/{marker_id}", json={"start_ms": 1000, "end_ms": 25000}
        )

    assert resp.status_code == 403
    assert "Plex Pass" in resp.json()["detail"]


async def test_patch_does_not_persist_when_plex_rejects(http_client):
    """A rejected write must not leave the database claiming a change Plex never took."""
    marker_id = await _stored_marker(start_ms=0, end_ms=30000)
    client = _plex_client()
    client.update_marker = AsyncMock(side_effect=PermissionError("Plex Pass required"))

    with _patch_client(client):
        await http_client.patch(f"/api/markers/{marker_id}", json={"start_ms": 1000, "end_ms": 25000})

    stored = await db.get_plex_marker(marker_id)
    assert (stored["start_ms"], stored["end_ms"]) == (0, 30000)


async def test_patch_returns_502_on_other_plex_failures(http_client):
    marker_id = await _stored_marker()
    client = _plex_client()
    client.update_marker = AsyncMock(side_effect=RuntimeError("Plex marker update failed"))

    with _patch_client(client):
        resp = await http_client.patch(
            f"/api/markers/{marker_id}", json={"start_ms": 1000, "end_ms": 25000}
        )

    assert resp.status_code == 502


async def test_patch_unknown_marker_returns_404(http_client):
    resp = await http_client.patch("/api/markers/9999", json={"start_ms": 0, "end_ms": 1000})

    assert resp.status_code == 404


async def test_patch_rejects_an_inverted_range(http_client):
    marker_id = await _stored_marker()

    resp = await http_client.patch(
        f"/api/markers/{marker_id}", json={"start_ms": 30000, "end_ms": 1000}
    )

    assert resp.status_code == 422


# ── POST /api/markers/titles/{rating_key}/create ──────────────────────────────

async def test_create_stores_a_local_marker(http_client):
    with _patch_client(_plex_client()):
        resp = await http_client.post(
            "/api/markers/titles/rk-1/create",
            json={"plex_guid": "guid-1", "marker_type": "intro", "start_ms": 0, "end_ms": 30000},
        )

    assert resp.status_code == 200
    assert len(await db.get_plex_markers_for_guid("guid-1")) == 1


async def test_create_succeeds_and_reports_the_error_when_plex_rejects(http_client):
    """Locally managed markers still work without Plex Pass; the error is surfaced."""
    client = _plex_client()
    client.create_marker = AsyncMock(side_effect=Exception("Plex Pass required"))

    with _patch_client(client):
        resp = await http_client.post(
            "/api/markers/titles/rk-1/create",
            json={"plex_guid": "guid-1", "marker_type": "intro", "start_ms": 0, "end_ms": 30000},
        )

    assert resp.status_code == 200
    assert "Plex Pass" in resp.json()["plex_error"]
    assert len(await db.get_plex_markers_for_guid("guid-1")) == 1


async def test_create_rejects_an_unknown_marker_type(http_client):
    resp = await http_client.post(
        "/api/markers/titles/rk-1/create",
        json={"plex_guid": "guid-1", "marker_type": "advert", "start_ms": 0, "end_ms": 30000},
    )

    assert resp.status_code == 422


async def test_create_rejects_an_inverted_range(http_client):
    resp = await http_client.post(
        "/api/markers/titles/rk-1/create",
        json={"plex_guid": "guid-1", "marker_type": "intro", "start_ms": 30000, "end_ms": 0},
    )

    assert resp.status_code == 422


# ── DELETE /api/markers/{marker_id} ───────────────────────────────────────────

async def test_delete_removes_the_marker(http_client):
    marker_id = await _stored_marker()

    resp = await http_client.delete(f"/api/markers/{marker_id}")

    assert resp.status_code == 200
    assert await db.get_plex_marker(marker_id) is None


async def test_delete_unknown_marker_returns_404(http_client):
    resp = await http_client.delete("/api/markers/9999")

    assert resp.status_code == 404


# ── GET /api/markers/{marker_id}/stream ───────────────────────────────────────

async def test_stream_serves_the_source_file_with_range_support(http_client, tmp_path):
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"0123456789" * 10)
    await db.upsert_scan_job(
        plex_guid="guid-1", title="Movie", file_path=str(media),
        rating_key="rk-1", library_id="1", library_title="Movies",
    )
    marker_id = await _stored_marker()

    resp = await http_client.get(f"/api/markers/{marker_id}/stream", headers={"Range": "bytes=0-9"})

    # FileResponse answers range requests with 206 so browser <video> can seek.
    assert resp.status_code == 206
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == b"0123456789"


async def test_stream_without_a_range_header_returns_the_whole_file(http_client, tmp_path):
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"abc")
    await db.upsert_scan_job(
        plex_guid="guid-1", title="Movie", file_path=str(media),
        rating_key="rk-1", library_id="1", library_title="Movies",
    )
    marker_id = await _stored_marker()

    resp = await http_client.get(f"/api/markers/{marker_id}/stream")

    assert resp.status_code == 200
    assert resp.content == b"abc"


async def test_stream_unknown_marker_returns_404(http_client):
    resp = await http_client.get("/api/markers/9999/stream")

    assert resp.status_code == 404


async def test_stream_returns_404_when_the_title_was_never_scanned(http_client):
    marker_id = await _stored_marker()

    resp = await http_client.get(f"/api/markers/{marker_id}/stream")

    assert resp.status_code == 404
    assert "synced" in resp.json()["detail"]


async def test_stream_returns_404_when_the_file_is_gone_from_disk(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-1", title="Movie", file_path="/media/deleted.mp4",
        rating_key="rk-1", library_id="1", library_title="Movies",
    )
    marker_id = await _stored_marker()

    resp = await http_client.get(f"/api/markers/{marker_id}/stream")

    assert resp.status_code == 404
    assert "does not exist" in resp.json()["detail"]
