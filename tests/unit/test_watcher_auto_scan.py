"""Tests for automatic scanning during Plex library discovery."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db
from cleanplex import watcher
from cleanplex.config import Config
from cleanplex.plex_client import LibrarySection, MediaItem


pytestmark = pytest.mark.usefixtures("setup_db")


async def _discover(item: MediaItem, *, auto_scan: bool) -> AsyncMock:
    client = MagicMock()
    client.get_library_sections = AsyncMock(
        return_value=[LibrarySection("1", "Movies", "movie")]
    )
    client.get_library_items = AsyncMock(return_value=[item])
    get_config = AsyncMock(
        return_value=Config(
            plex_url="http://plex:32400",
            plex_token="token",
            auto_scan_new_titles=auto_scan,
        )
    )

    with (
        patch.object(watcher, "enqueue", new=AsyncMock()) as enqueue,
        patch.object(
            watcher.asyncio,
            "sleep",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await watcher.library_watcher_loop(get_config, lambda: client)

    return enqueue


def _item(file_path: str) -> MediaItem:
    return MediaItem(
        rating_key="1",
        plex_guid="guid-new",
        title="New Movie",
        year=2026,
        thumb="",
        file_path=file_path,
        library_id="1",
        library_title="Movies",
        media_type="movie",
    )


async def test_discovery_leaves_new_title_pending_when_automatic_scanning_is_disabled(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"")
    await db.set_setting("scan_ratings", '["R"]')

    enqueue = await _discover(_item(str(media)), auto_scan=False)

    job = await db.get_scan_job_by_guid("guid-new")
    assert job["status"] == "pending"
    enqueue.assert_not_awaited()


async def test_discovery_enqueues_new_title_when_automatic_scanning_is_enabled(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"")

    enqueue = await _discover(_item(str(media)), auto_scan=True)

    assert await db.get_scan_job_by_guid("guid-new") is not None
    enqueue.assert_awaited_once_with("guid-new")


async def test_discovery_keeps_rating_filtered_title_visible_but_not_queued(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"")
    await db.set_setting("scan_ratings", '["R"]')

    enqueue = await _discover(_item(str(media)), auto_scan=True)

    assert await db.get_scan_job_by_guid("guid-new") is not None
    enqueue.assert_not_awaited()


async def test_discovery_imports_sidecar_when_automatic_scanning_is_disabled(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"")
    (tmp_path / "movie.edl").write_text("30.5 45.0 0\n", encoding="utf-8")
    await db.set_setting("scan_ratings", '["R"]')

    enqueue = await _discover(_item(str(media)), auto_scan=False)

    segments = await db.get_segments_for_guid("guid-new")
    assert segments[0]["start_ms"] == 30500
    assert segments[0]["source"] == "edl"
    assert (await db.get_scan_job_by_guid("guid-new"))["status"] == "done"
    enqueue.assert_not_awaited()
