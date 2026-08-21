"""Unit tests for sidecar skip file discovery in the library watcher (issue #67)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import watcher
from cleanplex.plex_client import MediaItem

pytestmark = pytest.mark.usefixtures("setup_db")


def _item(file_path: str) -> MediaItem:
    return MediaItem(
        rating_key="rk-1",
        plex_guid="guid-side",
        title="Sidecar Movie",
        year=2020,
        thumb="",
        file_path=str(file_path),
        library_id="1",
        library_title="Movies",
        media_type="movie",
        duration_ms=7200000,
    )


async def _job() -> None:
    await db.upsert_scan_job(
        plex_guid="guid-side",
        title="Sidecar Movie",
        file_path="/media/x.mkv",
        rating_key="rk-1",
        library_id="1",
        library_title="Movies",
    )


async def test_sidecar_is_imported_and_reports_success(tmp_path):
    await _job()
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.edl").write_text("30.5 45.0 0\n", encoding="utf-8")

    assert await watcher._import_sidecar(_item(media)) is True

    stored = await db.get_segments_for_guid("guid-side")
    assert stored[0]["start_ms"] == 30500
    assert stored[0]["source"] == "edl"


async def test_sidecar_import_marks_the_job_complete(tmp_path):
    await _job()
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.skp").write_text("0:00:10 --> 0:00:20\nnudity 3\n", encoding="utf-8")

    await watcher._import_sidecar(_item(media))

    job = await db.get_scan_job_by_guid("guid-side")
    assert job["status"] == "completed"


async def test_missing_sidecar_falls_through_to_scanning(tmp_path):
    await _job()
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")

    assert await watcher._import_sidecar(_item(media)) is False
    assert await db.get_segments_for_guid("guid-side") == []


async def test_malformed_sidecar_does_not_block_scanning(tmp_path):
    """A bad file must leave the title queued for a normal scan, not crash the loop."""
    await _job()
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.skp").write_text("this is not a skip file", encoding="utf-8")

    assert await watcher._import_sidecar(_item(media)) is False

    job = await db.get_scan_job_by_guid("guid-side")
    assert job["status"] == "pending"
