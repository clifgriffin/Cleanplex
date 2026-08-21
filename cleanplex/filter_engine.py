"""Filter engine: checks playback position against stored segments and acts on them."""

from __future__ import annotations

import time

from .logger import get_logger
from . import database as db
from .plex_client import ActiveSession, PlexClient

logger = get_logger(__name__)

# Track recently handled sessions to avoid re-triggering: {session_key: end_ms}
_recently_skipped: dict[str, int] = {}
_seek_backoff_until: dict[str, float] = {}
# Sessions currently muted for a segment. Client details are kept alongside the
# restore level so volume can be restored even after the session has vanished from
# the sessions list — otherwise a viewer who stops mid-mute is left silent.
# {session_key: {"end_ms", "restore_to", "client_identifier", "client_address", "client_port"}}
_muted_sessions: dict[str, dict] = {}

# Severity ranks follow the MovieContentFilter vocabulary. A segment is filtered
# when the viewer's level for its category plus this rank exceeds 3 — the same
# threshold model VideoSkip uses, so imported severities behave as their authors
# intended.
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

# Actions we can carry out through the Plex playback API. blank/blur appear in
# imported files but need stream manipulation we do not have, so they are recorded
# and ignored rather than misapplied as skips.
_SUPPORTED_ACTIONS = {"skip", "mute"}
_logged_unsupported: set[str] = set()


async def reap(active_session_keys: set[str], client: PlexClient | None = None) -> None:
    """Drop tracking state for sessions that are no longer playing.

    Without this the dicts grow for the life of the process, and because Plex
    reuses sessionKey values a stale entry can suppress a skip for an unrelated
    session that happens to inherit the key. Any session that stopped while muted
    has its volume restored first, so the client is not left silent.
    """
    for key in [k for k in _muted_sessions if k not in active_session_keys]:
        muted = _muted_sessions[key]
        if client is not None:
            try:
                await client.set_volume(
                    muted["client_identifier"],
                    muted["restore_to"],
                    muted["client_address"],
                    muted["client_port"],
                )
            except Exception as exc:
                logger.warning("Failed to restore volume for ended session %s: %s", key, exc)

    for state in (_recently_skipped, _seek_backoff_until, _muted_sessions):
        for key in [k for k in state if k not in active_session_keys]:
            del state[key]


def _is_filtered(segment: dict, prefs: dict[str, dict]) -> bool:
    """Return True if this segment should be acted on for a viewer with these prefs."""
    if not prefs:
        # No stored preferences: filter everything, matching pre-category behaviour.
        return True
    pref = prefs.get(segment.get("category") or "other")
    if pref is None:
        return False
    rank = _SEVERITY_RANK.get(segment.get("severity") or "high", 3)
    return (pref["level"] + rank) > 3


def _resolve_action(segment: dict, prefs: dict[str, dict]) -> str:
    """Return the action to take, letting a user's per-category override win."""
    pref = prefs.get(segment.get("category") or "other") if prefs else None
    if pref and pref.get("action"):
        return pref["action"]
    return segment.get("action") or "skip"


async def process(
    session: ActiveSession,
    client: PlexClient,
    pre_buffer_ms: int = 3000,
    post_buffer_ms: int = 3000,
    lookahead_ms: int = 5000,
) -> None:
    """Check the session's position against stored segments and act if needed."""
    if not session.is_controllable:
        logger.info("Session %s (%s) is not controllable – skipping", session.session_key, session.full_title)
        return

    pos = session.position_ms

    await _restore_expired_mute(session, client, pos)

    # Don't re-trigger if we already handled this stretch recently
    skip_until = _recently_skipped.get(session.session_key, 0)
    if pos < skip_until:
        return

    # Back off briefly if a previous seek command failed for this session/client.
    blocked_until = _seek_backoff_until.get(session.session_key, 0.0)
    if time.time() < blocked_until:
        return

    segments = await db.get_segments_for_guid(session.plex_guid)

    # Plex session GUIDs can differ from library-scan GUIDs (ordering of guids[] varies).
    # Fall back to rating_key lookup so we still find the right segments.
    if not segments and session.rating_key:
        segments = await db.get_segments_by_rating_key(session.rating_key)
        if segments:
            logger.info(
                "GUID mismatch for '%s': session_guid=%s, found %d segment(s) via rating_key=%s",
                session.full_title, session.plex_guid, len(segments), session.rating_key,
            )

    if not segments:
        logger.info("No segments found for '%s' (guid=%s, rating_key=%s)", session.full_title, session.plex_guid, session.rating_key)
        return

    prefs = await db.get_user_category_prefs(session.user)

    # Widen segment bounds so the action lands ahead of the flagged content and
    # does not re-trigger on its tail.
    for seg in segments:
        seg["start_ms"] = max(0, seg["start_ms"] - pre_buffer_ms)
        seg["end_ms"] = seg["end_ms"] + post_buffer_ms

    logger.info("Checking %d segment(s) for '%s' at pos=%dms (client=%s)", len(segments), session.full_title, pos, session.client_identifier)
    for seg in segments:
        # Trigger when approaching the segment (within lookahead_ms before start) or already inside.
        # This compensates for polling latency so the action fires before/at the segment start.
        if not (seg["start_ms"] - lookahead_ms <= pos <= seg["end_ms"]):
            continue
        if not _is_filtered(seg, prefs):
            continue

        action = _resolve_action(seg, prefs)
        if action not in _SUPPORTED_ACTIONS:
            if action not in _logged_unsupported:
                logger.info(
                    "Action '%s' is not supported through the Plex API — segments using it are ignored",
                    action,
                )
                _logged_unsupported.add(action)
            continue

        if action == "mute":
            await _apply_mute(session, client, seg)
        else:
            await _apply_skip(session, client, seg, pos)
        return

    # Clean up stale entries for sessions no longer in range
    if session.session_key in _recently_skipped and pos > _recently_skipped[session.session_key]:
        del _recently_skipped[session.session_key]
    if session.session_key in _seek_backoff_until and time.time() > _seek_backoff_until[session.session_key]:
        del _seek_backoff_until[session.session_key]


async def _apply_skip(session: ActiveSession, client: PlexClient, seg: dict, pos: int) -> None:
    """Seek past the segment and record the outcome."""
    target = seg["start_ms"]
    logger.info(
        "Skipping [%s] for user '%s': %dms → %dms (segment: %d–%d, category=%s, confidence=%.2f)",
        session.full_title,
        session.user,
        pos,
        target,
        seg["start_ms"],
        seg["end_ms"],
        seg.get("category", "nudity"),
        seg.get("confidence", 0.0),
    )
    success = await client.seek(
        session.client_identifier,
        target,
        session.client_address,
        session.client_port,
    )
    if success:
        # Track until the widened segment end to prevent re-triggering
        _recently_skipped[session.session_key] = seg["end_ms"]
        _seek_backoff_until.pop(session.session_key, None)
    else:
        _seek_backoff_until[session.session_key] = time.time() + 20


async def _apply_mute(session: ActiveSession, client: PlexClient, seg: dict) -> None:
    """Mute for the duration of the segment, remembering the level to restore."""
    if session.session_key in _muted_sessions:
        return
    restore_to = session.volume if session.volume is not None and session.volume > 0 else 100
    logger.info(
        "Muting [%s] for user '%s' until %dms (category=%s)",
        session.full_title,
        session.user,
        seg["end_ms"],
        seg.get("category", "language"),
    )
    if await client.set_volume(
        session.client_identifier,
        0,
        session.client_address,
        session.client_port,
    ):
        _muted_sessions[session.session_key] = {
            "end_ms": seg["end_ms"],
            "restore_to": restore_to,
            "client_identifier": session.client_identifier,
            "client_address": session.client_address,
            "client_port": session.client_port,
        }
    else:
        _seek_backoff_until[session.session_key] = time.time() + 20


async def _restore_expired_mute(session: ActiveSession, client: PlexClient, pos: int) -> None:
    """Restore volume once playback has passed the muted segment."""
    muted = _muted_sessions.get(session.session_key)
    if not muted or pos <= muted["end_ms"]:
        return
    await client.set_volume(
        session.client_identifier,
        muted["restore_to"],
        session.client_address,
        session.client_port,
    )
    del _muted_sessions[session.session_key]
