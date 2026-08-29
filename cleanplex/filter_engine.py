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
# Seeks awaiting confirmation that playback actually moved: {session_key: {...}}
_pending_verification: dict[str, dict] = {}
# Cues already acted on for a session. A late seek on Apple TV can land on the
# word's end after playback has already passed it; without this we skip again
# and rewind in a loop. {session_key: {cue_key: end_ms}}
_handled_cues: dict[str, dict[object, int]] = {}

# A client can land slightly short of the requested offset (keyframe snapping), so
# verification allows this much shortfall before calling the seek a failure.
_VERIFY_TOLERANCE_MS = 2000

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

# NudeNet clusters frames and can miss the edges of a scene, so those segments
# keep the configured pre/post buffers. Authored mute/language cues stay at the
# times in the skip file — a pad is what turned a 0.5s word into a rewind.
# Lookahead only decides when we send seekTo(end), not where we land.
_WORD_LOOKAHEAD_MS = 1000
# A 0.5s swear is a "word"; a 10s language mute is a span we can still jump mid-way.
_SHORT_CUE_MS = 2000
# Start asking the player this far before a cue. PMS viewOffset can be a full
# heartbeat behind; 20s gives the tvOS poll time to take over before the word.
APPROACH_HORIZON_MS = 20_000


def _is_word_cue(segment: dict) -> bool:
    """Return True for a short audio cue rather than a detected visual scene."""
    return (segment.get("action") or "skip") == "mute" or (segment.get("category") or "") == "language"


def segment_pads(segment: dict, pre_buffer_ms: int, post_buffer_ms: int) -> tuple[int, int]:
    """Return (pre, post) pads for this segment given the viewer's buffer settings."""
    if _is_word_cue(segment):
        return 0, 0
    return pre_buffer_ms, post_buffer_ms


def _lookahead_for(segment: dict, lookahead_ms: int) -> int:
    """Word cues must not inherit the scene lookahead or a skip jumps several seconds early."""
    if _is_word_cue(segment):
        return min(lookahead_ms, _WORD_LOOKAHEAD_MS)
    return lookahead_ms


def ms_until_approach(
    segment: dict,
    position_ms: int,
    pre_buffer_ms: int,
    post_buffer_ms: int,
) -> int | None:
    """Ms until this segment enters the tight-poll window, 0 if already in it.

    None means playback is already past the authored end.
    """
    pre, post = segment_pads(segment, pre_buffer_ms, post_buffer_ms)
    trigger_at = max(0, segment["start_ms"] - pre)
    if position_ms > segment["end_ms"] + post:
        return None
    tight_from = trigger_at - APPROACH_HORIZON_MS
    if position_ms < tight_from:
        return tight_from - position_ms
    return 0


def _is_short_word_cue(segment: dict) -> bool:
    start = segment.get("_original_start_ms", segment["start_ms"])
    end = segment.get("_original_end_ms", segment["end_ms"])
    return _is_word_cue(segment) and (end - start) <= _SHORT_CUE_MS


def _word_already_started(segment: dict, pos: int) -> bool:
    """True when a short word cue has begun — a seek to its end would rewind."""
    if not _is_short_word_cue(segment):
        return False
    return pos >= segment.get("_original_start_ms", segment["start_ms"])


def _cue_key(segment: dict) -> object:
    """Stable identity for a cue, even after start/end are widened in place."""
    if "_cue_key" in segment:
        return segment["_cue_key"]
    if segment.get("id") is not None:
        return segment["id"]
    return (segment.get("start_ms"), segment.get("end_ms"))


def _remember_cue(session_key: str, segment: dict) -> None:
    _handled_cues.setdefault(session_key, {})[_cue_key(segment)] = segment["end_ms"]


def _forget_rewound_cues(session_key: str, pos: int) -> None:
    """Drop handled marks the viewer has rewound past, even outside a cue window."""
    bucket = _handled_cues.get(session_key)
    if not bucket:
        return
    for key, end_ms in list(bucket.items()):
        if pos < end_ms - _VERIFY_TOLERANCE_MS:
            del bucket[key]


def _already_handled(session_key: str, segment: dict, pos: int) -> bool:
    """Return True if we already acted on this cue and the viewer has not rewound."""
    bucket = _handled_cues.get(session_key)
    if not bucket:
        return False
    return _cue_key(segment) in bucket


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

    for state in (
        _recently_skipped, _seek_backoff_until, _muted_sessions,
        _pending_verification, _handled_cues,
    ):
        for key in [k for k in state if k not in active_session_keys]:
            del state[key]


def _matches_language(segment: dict, audio_language: str) -> bool:
    """Return True if this segment applies to the track being played.

    An empty segment language means "any track", which is right for image-derived
    segments. A profanity mute authored against the English track would land on
    unrelated dialogue in a dub, so those only fire on their own language. When the
    session's language cannot be determined, everything applies — the previous
    behaviour, and safer than silently filtering nothing.
    """
    seg_language = (segment.get("language") or "").strip().lower()
    if not seg_language or not audio_language:
        return True
    # Plex reports both 2- and 3-letter codes depending on the source, so compare
    # on the shorter prefix rather than demanding an exact match.
    shortest = min(len(seg_language), len(audio_language))
    return seg_language[:shortest] == audio_language[:shortest]


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

    await verify_pending_seek(session, client)
    _forget_rewound_cues(session.session_key, pos)
    await _restore_expired_mute(session, client, pos)

    # Plex can land just short of the seek target. Suppress that landing, but
    # clear the guard when the viewer rewinds farther into the segment.
    skip_until = _recently_skipped.get(session.session_key)
    if skip_until is not None:
        if skip_until - _VERIFY_TOLERANCE_MS <= pos <= skip_until:
            return
        del _recently_skipped[session.session_key]

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

    prefs = {}
    for username in session.user_identities:
        prefs = await db.get_user_category_prefs(username)
        if prefs:
            break

    # Widen scene bounds so the action lands ahead of the flagged content.
    # Word cues keep their authored times.
    for seg in segments:
        if "_cue_key" not in seg:
            seg["_cue_key"] = _cue_key(seg)
        if "_original_start_ms" not in seg:
            seg["_original_start_ms"] = seg["start_ms"]
            seg["_original_end_ms"] = seg["end_ms"]
        pre, post = segment_pads(seg, pre_buffer_ms, post_buffer_ms)
        seg["start_ms"] = max(0, seg["start_ms"] - pre)
        seg["end_ms"] = seg["end_ms"] + post

    logger.info("Checking %d segment(s) for '%s' at pos=%dms (client=%s)", len(segments), session.full_title, pos, session.client_identifier)
    for seg in segments:
        # Trigger when approaching the segment (within lookahead before start) or already inside.
        # This compensates for polling latency so the action fires before/at the segment start.
        if not (seg["start_ms"] - _lookahead_for(seg, lookahead_ms) <= pos <= seg["end_ms"]):
            continue
        if not _matches_language(seg, session.audio_language):
            continue
        if not _is_filtered(seg, prefs):
            continue
        if _already_handled(session.session_key, seg, pos):
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
            await _apply_mute(session, client, seg, pos)
        else:
            await _apply_skip(session, client, seg, pos)
        return

    if session.session_key in _seek_backoff_until and time.time() > _seek_backoff_until[session.session_key]:
        del _seek_backoff_until[session.session_key]


async def verify_pending_seek(session: ActiveSession, client: PlexClient) -> None:
    """Confirm the previous seek actually moved playback, and re-probe if not.

    Some clients acknowledge a seek command and never move. Without this the
    segment is recorded as skipped, `_recently_skipped` suppresses a retry, and
    the viewer sees exactly the content the skip existed to remove.
    """
    pending = _pending_verification.pop(session.session_key, None)
    if pending is None:
        return

    if session.position_ms >= pending["target_ms"] - _VERIFY_TOLERANCE_MS:
        return

    from_ms = pending.get("from_ms", pending["target_ms"])
    # A live playhead that jumped backward is a rewind, not a failed seek.
    if session.position_ms < from_ms - _VERIFY_TOLERANCE_MS:
        return

    logger.warning(
        "Seek on client %s reported success but position is still %dms (expected >= %dms)",
        session.client_identifier, session.position_ms, pending["target_ms"],
    )
    # The learned transport accepted the command without acting on it, so stop
    # trusting it and let the next attempt rediscover a working one.
    await client._forget_profile(session.client_identifier)
    _recently_skipped.pop(session.session_key, None)

    await db.record_skip_event(
        plex_guid=session.plex_guid,
        title=session.full_title,
        username=session.user,
        client_identifier=session.client_identifier,
        client_title=session.client_title,
        category=pending["category"],
        action="skip",
        segment_id=pending.get("segment_id"),
        position_ms=session.position_ms,
        target_ms=pending["target_ms"],
        success=False,
        latency_ms=pending["latency_ms"],
    )


async def _apply_skip(session: ActiveSession, client: PlexClient, seg: dict, pos: int) -> None:
    """Seek past the segment and record the outcome."""
    target = seg["end_ms"]
    # A 0.4s remaining jump applies late on Apple TV and yanks backward. Only
    # skip a short word while still approaching; land on the authored end.
    if target <= pos or _word_already_started(seg, pos):
        logger.info(
            "Already at or past skip target %dms (pos=%dms) — not seeking backward",
            target, pos,
        )
        _remember_cue(session.session_key, seg)
        return

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
    started = time.monotonic()
    success = await client.seek(
        session.client_identifier,
        target,
        session.client_address,
        session.client_port,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    if success:
        # Track until the widened segment end to prevent re-triggering
        _recently_skipped[session.session_key] = seg["end_ms"]
        _remember_cue(session.session_key, seg)
        _seek_backoff_until.pop(session.session_key, None)
        # Checked on the next tick: a 2xx response is not proof playback moved.
        _pending_verification[session.session_key] = {
            "target_ms": target,
            "from_ms": pos,
            "category": seg.get("category", ""),
            "segment_id": seg.get("id"),
            "latency_ms": latency_ms,
        }
    else:
        _seek_backoff_until[session.session_key] = time.time() + 20

    await db.record_skip_event(
        plex_guid=session.plex_guid,
        title=session.full_title,
        username=session.user,
        client_identifier=session.client_identifier,
        client_title=session.client_title,
        category=seg.get("category", ""),
        action="skip",
        segment_id=seg.get("id"),
        position_ms=pos,
        target_ms=target,
        success=success,
        latency_ms=latency_ms,
    )


async def _apply_mute(session: ActiveSession, client: PlexClient, seg: dict, pos: int) -> None:
    """Mute for the duration of the segment, remembering the level to restore.

    Mute is a software-volume change. Clients that never report a volume (Apple TV
    is the usual case) accept setParameters?volume=0 with a 2xx and then ignore it,
    so the word still plays. Skip in that case — and when the volume command itself
    fails — rather than leaving the audio intact or blacking out all filtering.
    """
    if session.session_key in _muted_sessions:
        return
    if session.volume is None:
        logger.info(
            "Client %s (%s) does not report volume — skipping instead of muting",
            session.client_identifier,
            session.client_title,
        )
        await _apply_skip(session, client, seg, pos)
        return

    restore_to = session.volume if session.volume > 0 else 100
    logger.info(
        "Muting [%s] for user '%s' until %dms (category=%s)",
        session.full_title,
        session.user,
        seg["end_ms"],
        seg.get("category", "language"),
    )
    started = time.monotonic()
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
        muted_ok = True
    else:
        muted_ok = False

    await db.record_skip_event(
        plex_guid=session.plex_guid,
        title=session.full_title,
        username=session.user,
        client_identifier=session.client_identifier,
        client_title=session.client_title,
        category=seg.get("category", ""),
        action="mute",
        segment_id=seg.get("id"),
        position_ms=session.position_ms,
        target_ms=seg["end_ms"],
        success=muted_ok,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    if not muted_ok:
        logger.info(
            "Mute failed on client %s (%s) — skipping instead",
            session.client_identifier,
            session.client_title,
        )
        await _apply_skip(session, client, seg, pos)


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
