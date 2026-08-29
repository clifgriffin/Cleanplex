"""Unit tests for persisted skip history and adaptive polling (issues #69, #71)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cleanplex import database as db
from cleanplex import watcher

pytestmark = pytest.mark.usefixtures("setup_db")


# ── Skip event storage (issue #69) ─────────────────────────────────────────────

async def test_recorded_event_round_trips():
    await db.record_skip_event(
        plex_guid="guid-1",
        title="Movie",
        username="alice",
        client_identifier="client-1",
        client_title="Apple TV",
        category="nudity",
        action="skip",
        position_ms=1000,
        target_ms=5000,
        latency_ms=42,
    )

    events = await db.get_skip_events()
    assert len(events) == 1
    assert events[0]["category"] == "nudity"
    assert events[0]["client_title"] == "Apple TV"
    assert events[0]["latency_ms"] == 42
    assert events[0]["success"] == 1


async def test_failed_event_is_stored_with_success_zero():
    await db.record_skip_event(plex_guid="guid-1", success=False)

    assert (await db.get_skip_events())[0]["success"] == 0


async def test_count_matches_stored_rows():
    for _ in range(3):
        await db.record_skip_event(plex_guid="guid-1")

    assert await db.count_skip_events() == 3


async def test_history_is_paginated():
    for i in range(5):
        await db.record_skip_event(plex_guid=f"guid-{i}")

    page = await db.get_skip_events(limit=2, offset=2)
    assert len(page) == 2


# ── Retention (issue #69) ──────────────────────────────────────────────────────

async def test_prune_removes_rows_past_the_window():
    await db.record_skip_event(plex_guid="fresh")
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO skip_events(plex_guid, created_at) VALUES('stale', datetime('now', '-200 days'))"
        )
        await conn.commit()

    removed = await db.prune_skip_events(90)

    assert removed == 1
    assert [e["plex_guid"] for e in await db.get_skip_events()] == ["fresh"]


async def test_prune_of_zero_days_keeps_everything():
    await db.record_skip_event(plex_guid="guid-1")

    assert await db.prune_skip_events(0) == 0
    assert await db.count_skip_events() == 1


# ── Aggregates (issue #70) ─────────────────────────────────────────────────────

async def test_counts_by_category_are_ordered_by_volume():
    for _ in range(3):
        await db.record_skip_event(plex_guid="g", category="nudity")
    await db.record_skip_event(plex_guid="g", category="language")

    counts = await db.get_skip_counts_by_category()

    assert counts[0] == {"category": "nudity", "count": 3}
    assert counts[1]["category"] == "language"


async def test_counts_by_client_expose_failure_rate_worst_first():
    await db.record_skip_event(plex_guid="g", client_identifier="ok", client_title="Web", success=True)
    await db.record_skip_event(plex_guid="g", client_identifier="bad", client_title="Roku", success=False)
    await db.record_skip_event(plex_guid="g", client_identifier="bad", client_title="Roku", success=True)

    clients = await db.get_skip_counts_by_client()

    assert clients[0]["client_title"] == "Roku"
    assert clients[0]["failure_rate"] == 0.5
    assert clients[1]["failure_rate"] == 0.0


async def test_most_skipped_titles_are_ranked():
    for _ in range(2):
        await db.record_skip_event(plex_guid="guid-a", title="A")
    await db.record_skip_event(plex_guid="guid-b", title="B")

    titles = await db.get_most_skipped_titles()

    assert titles[0]["title"] == "A"
    assert titles[0]["count"] == 2


async def test_aggregates_on_empty_history_return_empty():
    assert await db.get_skip_counts_by_category() == []
    assert await db.get_skip_counts_by_client() == []
    assert await db.get_most_skipped_titles() == []


# ── Adaptive polling (issue #71) ───────────────────────────────────────────────

@dataclass
class _Cfg:
    poll_interval: int = 5
    pre_buffer_ms: int = 3000


@dataclass
class _Sess:
    session_key: str = "s1"
    plex_guid: str = "guid-poll"
    rating_key: str = "rk-1"
    position_ms: int = 0


async def test_no_sessions_polls_at_the_full_interval():
    assert await watcher._next_poll_delay([], _Cfg()) == 5.0


async def test_distant_segment_polls_at_the_full_interval():
    await db.insert_segment("guid-poll", "Movie", 600000, 610000)

    assert await watcher._next_poll_delay([_Sess(position_ms=0)], _Cfg()) == 5.0


async def test_imminent_segment_tightens_the_poll():
    # Segment starts at 10s; with a 3s buffer the trigger point is 7s away, and the
    # session is at 5s — so under 2s remain.
    await db.insert_segment("guid-poll", "Movie", 10000, 12000)

    delay = await watcher._next_poll_delay([_Sess(position_ms=5000)], _Cfg())

    assert delay <= 2.0


async def test_language_cues_do_not_use_the_scene_pre_buffer_for_polling():
    """A word at 10s is not 'imminent' at 5s the way a padded nudity scene is."""
    await db.insert_segment(
        "guid-poll", "Movie", 10000, 10500, category="language", action="mute",
    )

    delay = await watcher._next_poll_delay([_Sess(position_ms=5000)], _Cfg())

    assert delay > 4.0


async def test_poll_delay_never_drops_below_the_floor():
    await db.insert_segment("guid-poll", "Movie", 10000, 12000)

    delay = await watcher._next_poll_delay([_Sess(position_ms=6990)], _Cfg())

    assert delay == watcher.MIN_POLL_INTERVAL_S


async def test_passed_segments_do_not_tighten_the_poll():
    await db.insert_segment("guid-poll", "Movie", 10000, 12000)

    assert await watcher._next_poll_delay([_Sess(position_ms=60000)], _Cfg()) == 5.0


async def test_the_nearest_session_across_several_wins():
    await db.insert_segment("guid-poll", "Movie", 600000, 610000)
    await db.insert_segment("guid-close", "Other", 10000, 12000)

    sessions = [_Sess(position_ms=0), _Sess(session_key="s2", plex_guid="guid-close", position_ms=5000)]

    assert await watcher._next_poll_delay(sessions, _Cfg()) <= 2.0
