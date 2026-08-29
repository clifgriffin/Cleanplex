"""Unit tests for persisted skip history and adaptive polling (issues #69, #71)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

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
    post_buffer_ms: int = 3000


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
    # Segment starts at 10s; with a 3s buffer the trigger is at 7s. Position 5s
    # is inside the approach window, so we drop to 50ms.
    await db.insert_segment("guid-poll", "Movie", 10000, 12000)

    delay = await watcher._next_poll_delay([_Sess(position_ms=5000)], _Cfg())

    assert delay == watcher.TIGHT_POLL_INTERVAL_S


async def test_language_cue_twenty_seconds_away_stays_at_five_seconds():
    await db.insert_segment(
        "guid-poll", "Movie", 30000, 30500, category="language", action="mute",
    )

    delay = await watcher._next_poll_delay([_Sess(position_ms=0)], _Cfg())

    assert delay == 5.0


async def test_language_cue_within_ten_seconds_uses_tight_poll():
    """A word 5s away must enter the 50ms player-poll window."""
    await db.insert_segment(
        "guid-poll", "Movie", 10000, 10500, category="language", action="mute",
    )

    delay = await watcher._next_poll_delay([_Sess(position_ms=5000)], _Cfg())

    assert delay == watcher.TIGHT_POLL_INTERVAL_S


async def test_two_seconds_before_the_horizon_sleeps_until_it():
    """Stay at 5s until 20s out; 2s before the horizon, sleep ~2s."""
    # Nudity at 40s, 3s pre-buffer → trigger 37s. Horizon is 17s. Position 15s
    # is 2s before the horizon.
    await db.insert_segment("guid-poll", "Movie", 40000, 42000)

    delay = await watcher._next_poll_delay([_Sess(position_ms=15000)], _Cfg())

    assert 1.9 <= delay <= 2.1


async def test_poll_delay_never_drops_below_the_floor():
    await db.insert_segment("guid-poll", "Movie", 10000, 12000)

    delay = await watcher._next_poll_delay([_Sess(position_ms=6990)], _Cfg())

    assert delay == watcher.TIGHT_POLL_INTERVAL_S


async def test_passed_segments_do_not_tighten_the_poll():
    await db.insert_segment("guid-poll", "Movie", 10000, 12000)

    assert await watcher._next_poll_delay([_Sess(position_ms=60000)], _Cfg()) == 5.0


async def test_the_nearest_session_across_several_wins():
    await db.insert_segment("guid-poll", "Movie", 600000, 610000)
    await db.insert_segment("guid-close", "Other", 10000, 12000)

    sessions = [_Sess(position_ms=0), _Sess(session_key="s2", plex_guid="guid-close", position_ms=5000)]

    assert await watcher._next_poll_delay(sessions, _Cfg()) == watcher.TIGHT_POLL_INTERVAL_S


async def test_just_skipped_cue_does_not_keep_tight_poll():
    await db.insert_segment(
        "guid-poll", "Movie", 10000, 10500, category="language", action="mute",
    )
    from cleanplex import filter_engine as fe

    fe._recently_skipped["s1"] = 10500
    try:
        delay = await watcher._next_poll_delay([_Sess(position_ms=10000)], _Cfg())
    finally:
        fe._recently_skipped.clear()

    assert delay == 5.0


async def test_session_in_tight_window_when_a_word_is_five_seconds_away():
    await db.insert_segment(
        "guid-poll", "Movie", 10000, 10500, category="language", action="mute",
    )

    assert await watcher._session_in_tight_window(_Sess(position_ms=5000), _Cfg()) is True
    assert await watcher._session_in_tight_window(
        _Sess(plex_guid="guid-none", position_ms=5000), _Cfg(),
    ) is False


async def test_pending_seek_keeps_tight_poll():
    from cleanplex import filter_engine as fe

    fe._pending_verification["s1"] = {
        "target_ms": 1, "category": "", "latency_ms": 0,
    }
    try:
        delay = await watcher._next_poll_delay([_Sess(position_ms=0)], _Cfg())
    finally:
        fe._pending_verification.clear()

    assert delay == watcher.TIGHT_POLL_INTERVAL_S


# ── Playhead interpolation ─────────────────────────────────────────────────────

@pytest.fixture
def reset_playhead():
    watcher._playhead.clear()
    watcher._live_from_player.clear()
    yield
    watcher._playhead.clear()
    watcher._live_from_player.clear()


def test_stale_pms_offset_does_not_rewind_the_playhead(reset_playhead):
    session = SimpleNamespace(session_key="s1", position_ms=10000)
    watcher._mark_playhead("s1", 15000, 100.0)

    watcher._merge_pms_playhead(session, 100.5)

    assert session.position_ms == 15500


def test_pms_drop_is_ignored_as_a_stale_heartbeat(reset_playhead):
    """A 10s-lower viewOffset is a stale PMS report, not a viewer rewind."""
    session = SimpleNamespace(session_key="s1", position_ms=5000)
    watcher._mark_playhead("s1", 15000, 100.0)

    watcher._merge_pms_playhead(session, 101.0)

    assert watcher._playhead["s1"]["pos_ms"] == 15000
    assert session.position_ms == 16000


def test_estimated_position_advances_from_the_last_sample(reset_playhead):
    session = SimpleNamespace(session_key="s1", position_ms=0)
    watcher._mark_playhead("s1", 20000, 50.0)

    assert watcher._estimated_position(session, 51.0) == 21000


def test_reap_drops_playheads_for_ended_sessions(reset_playhead):
    watcher._mark_playhead("gone", 1, 0.0)
    watcher._live_from_player.add("gone")
    watcher._live_from_player.add("alive")
    watcher._mark_playhead("alive", 2, 0.0)

    watcher._reap_playheads({"alive"})

    assert "gone" not in watcher._playhead
    assert watcher._live_from_player == {"alive"}
