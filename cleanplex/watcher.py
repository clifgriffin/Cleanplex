"""Session watcher and library poller."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime

from .logger import get_logger
from . import database as db
from . import filter_engine
from .scanner import enqueue, enqueue_pending, import_sidecar, scanner_loop

logger = get_logger(__name__)

# Ring buffer of recent skip events for the live dashboard. The durable history
# lives in the skip_events table; this is just the hot cache.
skip_events: deque[dict] = deque(maxlen=50)

# Inside the approach window we tick this often and read /player/timeline/poll
# on the client (tvOS included). Alias kept so existing tests can import one name.
TIGHT_POLL_INTERVAL_S = 0.05
MIN_POLL_INTERVAL_S = TIGHT_POLL_INTERVAL_S
# Last real sample per session: {session_key: {"pos_ms", "at"}}
_playhead: dict[str, dict] = {}
# Sessions whose clock came from /player/timeline/poll rather than PMS viewOffset.
_live_from_player: set[str] = set()


def _mark_playhead(session_key: str, pos_ms: int, now: float) -> None:
    _playhead[session_key] = {"pos_ms": pos_ms, "at": now}


def _estimated_position(session, now: float) -> int:
    """Advance the last real sample by wall time so a stale PMS offset cannot freeze."""
    rec = _playhead.get(session.session_key)
    if rec is None:
        return session.position_ms
    return rec["pos_ms"] + max(0, int((now - rec["at"]) * 1000))


def _merge_pms_playhead(session, now: float) -> None:
    """Keep a clock-running playhead. A stale lower viewOffset must not win.

    PMS viewOffset can lag 5–10s. A drop is almost always a stale heartbeat, not
    a rewind. The player timeline is what moves the clock backward.
    """
    pms = session.position_ms
    rec = _playhead.get(session.session_key)
    if rec is None:
        _mark_playhead(session.session_key, pms, now)
        return
    estimated = _estimated_position(session, now)
    if pms > estimated:
        _mark_playhead(session.session_key, pms, now)
        return
    session.position_ms = estimated


def _note_live_playhead(session, live_ms: int, now: float) -> None:
    if session.session_key not in _live_from_player:
        logger.info(
            "Player timeline for %s (%s) at %dms — using the client playhead",
            session.client_title,
            session.client_identifier,
            live_ms,
        )
        _live_from_player.add(session.session_key)
    _mark_playhead(session.session_key, live_ms, now)
    session.position_ms = live_ms


async def _refresh_playhead(session, client, now: float) -> None:
    """Prefer the player's timeline; fall back to PMS without letting it rewind."""
    live_ms = await client.get_player_position(
        session.client_identifier,
        session.client_address,
        session.client_port,
    )
    if live_ms is not None:
        _note_live_playhead(session, live_ms, now)
        return
    _merge_pms_playhead(session, now)


def _reap_playheads(active_session_keys: set[str]) -> None:
    for key in [k for k in _playhead if k not in active_session_keys]:
        del _playhead[key]
    _live_from_player.intersection_update(active_session_keys)


async def session_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Poll Plex sessions and fire skips, tightening near a cue."""
    sessions = []
    last_full_fetch = 0.0
    while True:
        config = await get_config_fn()

        if not config.is_configured():
            await asyncio.sleep(10)
            continue

        try:
            client = get_client_fn()
            now = time.monotonic()
            # Full session list from PMS stays on the configured interval (5s).
            # Tight ticks keep a running playhead so we do not sit on a stale offset.
            if not sessions or (now - last_full_fetch) >= config.poll_interval:
                sessions = await client.get_active_sessions()
                last_full_fetch = now
                # Drop tracking state for sessions that ended, restoring volume for
                # any that stopped mid-mute. Plex reuses session keys, so stale
                # entries would otherwise suppress skips for unrelated playback.
                active = {s.session_key for s in sessions}
                await filter_engine.reap(active, client)
                _reap_playheads(active)
                for session in sessions:
                    await _refresh_playhead(session, client, now)

            for session in sessions:
                user_filter = None
                for username in session.user_identities:
                    user_filter = await db.get_user_filter(username)
                    if user_filter is not None:
                        break
                # Default: filter enabled if no explicit record
                if user_filter is not None and not user_filter["enabled"]:
                    continue

                session.position_ms = _estimated_position(session, time.monotonic())
                if await _session_in_tight_window(session, config):
                    await _refresh_playhead(session, client, time.monotonic())

                await filter_engine.process(
                    session,
                    client,
                    config.pre_buffer_ms,
                    config.post_buffer_ms,
                    config.poll_interval * 1000,
                )

                # Only a seek we actually sent belongs on the live dashboard.
                pending = filter_engine._pending_verification.get(session.session_key)
                if pending and pending["target_ms"] > session.position_ms:
                    skip_events.appendleft({
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "user": session.user,
                        "title": session.full_title,
                        "position_ms": session.position_ms,
                        "client": session.client_title,
                    })

        except Exception as exc:
            logger.warning("Session watcher error: %s", exc)
            sessions = []

        await asyncio.sleep(await _next_poll_delay(sessions, config))


def _post_buffer_ms(config) -> int:
    return getattr(config, "post_buffer_ms", 3000)


async def _segments_for_session(session) -> list[dict]:
    segments = await db.get_segments_for_guid(session.plex_guid)
    if not segments and session.rating_key:
        segments = await db.get_segments_by_rating_key(session.rating_key)
    return segments


async def _ms_until_tight(session, config) -> int | None:
    """Smallest ms until the tight-poll window, 0 if already in it, None if idle."""
    if session.session_key in filter_engine._pending_verification:
        return 0
    try:
        segments = await _segments_for_session(session)
    except Exception as exc:
        logger.debug("Could not load segments for poll delay %s: %s", session.session_key, exc)
        return None
    skip_until = filter_engine._recently_skipped.get(session.session_key)
    best: int | None = None
    post_ms = _post_buffer_ms(config)
    for seg in segments:
        _, post = filter_engine.segment_pads(seg, config.pre_buffer_ms, post_ms)
        if skip_until is not None and seg["end_ms"] + post <= skip_until:
            continue
        remaining = filter_engine.ms_until_approach(
            seg, session.position_ms, config.pre_buffer_ms, post_ms,
        )
        if remaining is None:
            continue
        best = remaining if best is None else min(best, remaining)
    return best


async def _session_in_tight_window(session, config) -> bool:
    remaining = await _ms_until_tight(session, config)
    return remaining == 0


async def _next_poll_delay(sessions, config) -> float:
    """Return how long to sleep before the next session poll.

    Stay at the configured interval (5s) until a cue is within the approach
    horizon, then drop to 50ms and read the player until that cue is handled.
    """
    delay = float(config.poll_interval)
    if not sessions:
        return delay

    for session in sessions:
        remaining = await _ms_until_tight(session, config)
        if remaining is None:
            continue
        if remaining == 0:
            delay = min(delay, TIGHT_POLL_INTERVAL_S)
        else:
            delay = min(delay, max(TIGHT_POLL_INTERVAL_S, remaining / 1000.0))

    return delay


async def library_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Periodically check for new Plex library items and enqueue unscanned ones."""
    first_run = True
    while True:
        if not first_run:
            await asyncio.sleep(60)
        first_run = False

        config = await get_config_fn()
        if not config.is_configured():
            continue

        try:
            client = get_client_fn()
            sections = await client.get_library_sections()
            excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
            scan_ratings = set(json.loads(await db.get_setting("scan_ratings", "[]")))

            for section in sections:
                if section.section_id in excluded:
                    continue
                items = await client.get_library_items(section.section_id)
                for item in items:
                    if not item.file_path:
                        continue
                    existing = await db.get_scan_job_by_guid(item.plex_guid)
                    if existing is None:
                        await db.upsert_scan_job(
                            plex_guid=item.plex_guid,
                            title=item.title,
                            file_path=item.file_path,
                            rating_key=item.rating_key,
                            library_id=item.library_id,
                            library_title=item.library_title,
                            content_rating=item.content_rating,
                            media_type=item.media_type,
                            year=item.year,
                            show_guid=item.show_guid,
                            part_files=json.dumps(item.part_files),
                            duration_ms=item.duration_ms,
                        )
                        # A skip file sitting beside the media is authoritative and
                        # free: importing it skips frame extraction and inference
                        # entirely, so only enqueue a scan when there isn't one.
                        if await import_sidecar(
                            item.plex_guid,
                            item.title,
                            item.file_path,
                        ):
                            continue
                        if not getattr(config, "auto_scan_new_titles", True):
                            logger.info(
                                "New item left pending because automatic scanning is disabled: %s",
                                item.title,
                            )
                            continue
                        if scan_ratings and (item.content_rating or "") not in scan_ratings:
                            continue
                        await enqueue(item.plex_guid)
                        logger.info("New item queued for scan: %s", item.title)
                    elif not existing.get("part_files") and len(item.part_files) > 1:
                        # Backfill part_files for existing jobs that predate multi-part support.
                        await db.update_part_files(item.plex_guid, json.dumps(item.part_files))
                        logger.info("Updated part_files for existing job: %s (%d parts)", item.title, len(item.part_files))

        except Exception as exc:
            logger.warning("Library watcher error: %s", exc)
