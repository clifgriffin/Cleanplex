"""Unit tests for segment classification columns and per-user category prefs."""

from __future__ import annotations

import aiosqlite
import pytest

from cleanplex import database as db


# ── Segment classification schema (issue #61) ──────────────────────────────────

async def test_scanner_segments_default_to_nudity_skip(setup_db):
    seg_id = await db.insert_segment("guid-1", "Movie", 1000, 2000, confidence=0.9)
    seg = await db.get_segment_by_id(seg_id)

    assert seg["category"] == "nudity"
    assert seg["severity"] == "high"
    assert seg["action"] == "skip"
    assert seg["channel"] == "both"
    assert seg["source"] == "scanner"


async def test_insert_segment_accepts_explicit_classification(setup_db):
    seg_id = await db.insert_segment(
        "guid-1", "Movie", 1000, 2000,
        category="language", severity="low", action="mute", channel="audio", source="skp",
    )
    seg = await db.get_segment_by_id(seg_id)

    assert (seg["category"], seg["severity"], seg["action"]) == ("language", "low", "mute")
    assert seg["channel"] == "audio"
    assert seg["source"] == "skp"


async def test_migration_is_idempotent_and_preserves_rows(setup_db):
    seg_id = await db.insert_segment("guid-1", "Movie", 1000, 2000)

    await db.init_db()
    await db.init_db()

    seg = await db.get_segment_by_id(seg_id)
    assert seg is not None
    assert seg["category"] == "nudity"


async def test_migration_backfills_rows_written_before_the_columns_existed(setup_db):
    """A row inserted without classification must still read back sensibly."""
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms) VALUES(?,?,?,?)",
            ("legacy-guid", "Old Movie", 5000, 9000),
        )
        await conn.commit()
        legacy_id = cursor.lastrowid

    seg = await db.get_segment_by_id(legacy_id)
    assert seg["category"] == "nudity"
    assert seg["source"] == "scanner"
    assert seg["action"] == "skip"


async def test_bulk_insert_writes_all_segments(setup_db):
    segments = [
        {"start_ms": 1000, "end_ms": 2000, "category": "language", "severity": "low", "action": "mute"},
        {"start_ms": 5000, "end_ms": 6000, "category": "violence", "severity": "high", "action": "skip"},
    ]
    count = await db.insert_segments_bulk("guid-bulk", "Movie", segments, source="skp")

    stored = await db.get_segments_for_guid("guid-bulk")
    assert count == 2
    assert {s["category"] for s in stored} == {"language", "violence"}
    assert all(s["source"] == "skp" for s in stored)


async def test_bulk_insert_of_empty_list_is_a_noop(setup_db):
    assert await db.insert_segments_bulk("guid-empty", "Movie", [], source="skp") == 0
    assert await db.get_segments_for_guid("guid-empty") == []


# ── Per-user category preferences (issue #62) ──────────────────────────────────

async def test_category_prefs_are_empty_for_unknown_user(setup_db):
    assert await db.get_user_category_prefs("nobody") == {}


async def test_upsert_and_read_category_pref(setup_db):
    await db.upsert_user_category_pref("alice", "nudity", 3)
    prefs = await db.get_user_category_prefs("alice")

    assert prefs["nudity"]["level"] == 3
    assert prefs["nudity"]["action"] == ""


async def test_upsert_category_pref_updates_in_place(setup_db):
    await db.upsert_user_category_pref("alice", "nudity", 1)
    await db.upsert_user_category_pref("alice", "nudity", 3, action="mute")

    prefs = await db.get_user_category_prefs("alice")
    assert len(prefs) == 1
    assert prefs["nudity"] == {"level": 3, "action": "mute"}


async def test_category_prefs_are_isolated_per_user(setup_db):
    await db.upsert_user_category_pref("alice", "nudity", 3)
    await db.upsert_user_category_pref("bob", "violence", 2)

    assert set(await db.get_user_category_prefs("alice")) == {"nudity"}
    assert set(await db.get_user_category_prefs("bob")) == {"violence"}


async def test_delete_category_prefs_reverts_user_to_defaults(setup_db):
    await db.upsert_user_category_pref("alice", "nudity", 3)
    await db.upsert_user_category_pref("alice", "language", 2)

    removed = await db.delete_user_category_prefs("alice")

    assert removed == 2
    assert await db.get_user_category_prefs("alice") == {}


# ── Buffer settings migration (issue #58) ──────────────────────────────────────

async def test_buffers_default_to_three_seconds(setup_db):
    assert await db.get_setting("pre_buffer_ms") == "3000"
    assert await db.get_setting("post_buffer_ms") == "3000"


async def test_buffers_seed_from_a_tuned_skip_buffer(tmp_path):
    """An install that tuned skip_buffer_ms keeps that value across the split."""
    db.set_db_path(tmp_path / "legacy.db")
    async with aiosqlite.connect(str(db.get_db_path())) as conn:
        await conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("INSERT INTO settings(key, value) VALUES('skip_buffer_ms', '8000')")
        await conn.commit()

    await db.init_db()

    assert await db.get_setting("pre_buffer_ms") == "8000"
    assert await db.get_setting("post_buffer_ms") == "8000"
