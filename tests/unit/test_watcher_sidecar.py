"""Unit tests for sidecar imports from discovery and manual scans."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import scanner
from cleanplex import watcher
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")


async def _job(file_path: str) -> None:
    await db.upsert_scan_job(
        plex_guid="guid-side",
        title="Sidecar Movie",
        file_path=file_path,
        rating_key="",
        library_id="1",
        library_title="Movies",
    )


async def test_sidecar_is_imported_and_reports_success(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.edl").write_text("30.5 45.0 0\n", encoding="utf-8")
    await _job(str(media))

    assert await watcher.import_sidecar("guid-side", "Sidecar Movie", str(media)) is True

    stored = await db.get_segments_for_guid("guid-side")
    assert stored[0]["start_ms"] == 30500
    assert stored[0]["source"] == "edl"


async def test_manual_scan_replaces_segments_and_skips_ml(tmp_path, caplog):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.skp").write_text("0:00:10 --> 0:00:20\nnudity 3\n", encoding="utf-8")
    await _job(str(media))
    await db.insert_segment("guid-side", "Sidecar Movie", 1000, 2000)
    await db.set_force_scan("guid-side", True)
    caplog.set_level("INFO")

    with patch.object(scanner, "_classify_frame") as detector:
        await scanner.scan_video("guid-side", Config())

    detector.assert_not_called()
    stored = await db.get_segments_for_guid("guid-side")
    assert [(row["start_ms"], row["source"]) for row in stored] == [(10000, "skp")]
    job = await db.get_scan_job_by_guid("guid-side")
    assert job["status"] == "done"
    assert job["progress"] == 1.0
    assert "Found sidecar movie.skp" in caplog.text


async def test_missing_sidecar_falls_through_to_scanning(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    await _job(str(media))

    assert await watcher.import_sidecar("guid-side", "Sidecar Movie", str(media)) is False
    assert await db.get_segments_for_guid("guid-side") == []


async def test_malformed_sidecar_does_not_block_scanning(tmp_path):
    """A bad file must not remove stored segments before the ML scan starts."""
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.skp").write_text("this is not a skip file", encoding="utf-8")
    await _job(str(media))
    await db.insert_segment("guid-side", "Sidecar Movie", 1000, 2000)

    assert await watcher.import_sidecar("guid-side", "Sidecar Movie", str(media)) is False

    job = await db.get_scan_job_by_guid("guid-side")
    assert job["status"] == "pending"
    assert len(await db.get_segments_for_guid("guid-side")) == 1


async def test_malformed_sidecar_falls_back_to_ml_scan(tmp_path, caplog):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.skp").write_text("this is not a skip file", encoding="utf-8")
    await _job(str(media))
    await db.set_force_scan("guid-side", True)

    async def frames(*_args):
        yield 0, b"jpeg"

    with (
        patch.object(scanner, "get_duration_ms", new=AsyncMock(return_value=1000)),
        patch.object(scanner, "extract_frames_batch", new=frames),
        patch.object(scanner, "_classify_frame", return_value=(False, 0.0, [])) as detector,
        patch.object(scanner, "THUMBNAILS_DIR", tmp_path / "thumbs"),
    ):
        await scanner.scan_video("guid-side", Config())

    detector.assert_called_once()
    assert "using normal scan handling" in caplog.text
    assert (await db.get_scan_job_by_guid("guid-side"))["status"] == "done"
