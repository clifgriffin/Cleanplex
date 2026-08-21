"""Unit tests for plex_markers storage (issue #53)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


def _marker(plex_marker_id: int, start_ms: int, end_ms: int, marker_type: str = "intro", final: bool = False) -> dict:
    return {
        "plex_marker_id": plex_marker_id,
        "marker_type": marker_type,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "final": final,
    }


# ── Schema ─────────────────────────────────────────────────────────────────────

async def test_table_exists_after_init():
    async with db.get_connection() as conn:
        row = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='plex_markers'"
        )).fetchone()

    assert row is not None


async def test_unique_constraint_is_on_rating_key_and_marker_id():
    """Episodes of one show share a GUID, so the constraint must key on rating_key."""
    async with db.get_connection() as conn:
        row = await (await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plex_markers'"
        )).fetchone()

    assert "UNIQUE(rating_key, plex_marker_id)" in row["sql"]


# ── upsert_plex_markers ────────────────────────────────────────────────────────

async def test_upsert_inserts_markers():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000)])

    stored = await db.get_plex_markers_for_guid("guid-1")
    assert len(stored) == 1
    assert stored[0]["plex_marker_id"] == 10
    assert stored[0]["marker_type"] == "intro"


async def test_upsert_replaces_previous_state_for_the_same_rating_key():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000)])
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 5000, 35000)])

    stored = await db.get_plex_markers_for_guid("guid-1")
    assert len(stored) == 1
    assert stored[0]["start_ms"] == 5000


async def test_upsert_drops_markers_no_longer_present_in_plex():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000), _marker(11, 60000, 90000)])
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000)])

    assert len(await db.get_plex_markers_for_guid("guid-1")) == 1


async def test_upsert_of_empty_list_clears_the_title():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000)])

    await db.upsert_plex_markers("guid-1", "rk-1", [])

    assert await db.get_plex_markers_for_guid("guid-1") == []


async def test_episodes_sharing_a_guid_do_not_collide():
    """The whole point of the rating_key constraint: same show GUID, different episodes."""
    await db.upsert_plex_markers("show-guid", "rk-ep1", [_marker(1, 0, 30000)])
    await db.upsert_plex_markers("show-guid", "rk-ep2", [_marker(1, 0, 30000)])

    assert len(await db.get_plex_markers_for_guid("show-guid")) == 2


async def test_final_flag_is_persisted():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000, marker_type="credits", final=True)])

    assert (await db.get_plex_markers_for_guid("guid-1"))[0]["final"] == 1


# ── get_plex_markers_for_guid ──────────────────────────────────────────────────

async def test_markers_are_ordered_by_start():
    await db.upsert_plex_markers("guid-1", "rk-1", [
        _marker(11, 600000, 660000, marker_type="credits"),
        _marker(10, 0, 30000),
    ])

    stored = await db.get_plex_markers_for_guid("guid-1")

    assert [m["start_ms"] for m in stored] == [0, 600000]


async def test_unknown_guid_returns_empty_list():
    assert await db.get_plex_markers_for_guid("nope") == []


# ── update_plex_marker_timestamps ──────────────────────────────────────────────

async def test_update_changes_only_the_timestamps():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000)])
    marker_id = (await db.get_plex_markers_for_guid("guid-1"))[0]["id"]

    assert await db.update_plex_marker_timestamps(marker_id, 1000, 25000) is True

    updated = await db.get_plex_marker(marker_id)
    assert (updated["start_ms"], updated["end_ms"]) == (1000, 25000)
    assert updated["marker_type"] == "intro"
    assert updated["plex_marker_id"] == 10


async def test_update_of_a_missing_marker_reports_failure():
    assert await db.update_plex_marker_timestamps(9999, 0, 1000) is False


# ── get_plex_marker / delete ───────────────────────────────────────────────────

async def test_get_marker_by_id_returns_none_when_missing():
    assert await db.get_plex_marker(9999) is None


async def test_delete_removes_one_marker():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000), _marker(11, 60000, 90000)])
    marker_id = (await db.get_plex_markers_for_guid("guid-1"))[0]["id"]

    assert await db.delete_plex_marker(marker_id) is True
    assert len(await db.get_plex_markers_for_guid("guid-1")) == 1


async def test_delete_of_a_missing_marker_reports_failure():
    assert await db.delete_plex_marker(9999) is False


# ── Counts ─────────────────────────────────────────────────────────────────────

async def test_counts_by_rating_keys_are_batched():
    await db.upsert_plex_markers("guid-1", "rk-1", [_marker(10, 0, 30000)])
    await db.upsert_plex_markers("guid-2", "rk-2", [_marker(20, 0, 30000), _marker(21, 60000, 90000)])

    counts = await db.get_plex_marker_counts_for_rating_keys(["rk-1", "rk-2", "rk-missing"])

    assert counts["rk-1"] == 1
    assert counts["rk-2"] == 2
    assert "rk-missing" not in counts


async def test_counts_for_no_rating_keys_returns_empty():
    assert await db.get_plex_marker_counts_for_rating_keys([]) == {}
