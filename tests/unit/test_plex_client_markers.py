"""Unit tests for PlexClient marker methods (issue #54).

plexapi is mocked at the asyncio.to_thread boundary rather than imported, per
CLAUDE.md §6.3.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cleanplex.plex_client import PlexClient


def _raw_marker(marker_id: int, marker_type: str, start: int, end: int, final: bool = False) -> MagicMock:
    m = MagicMock()
    m.id = marker_id
    m.type = marker_type
    m.start = start
    m.end = end
    m.final = final
    return m


def _server_with(markers: list) -> MagicMock:
    item = MagicMock()
    item.markers = markers
    srv = MagicMock()
    srv.fetchItem = MagicMock(return_value=item)
    return srv


def _patch_to_thread(srv: MagicMock):
    """Run the blocking callables inline, mimicking asyncio.to_thread."""
    async def fake_to_thread(fn, *args, **kwargs):
        if fn is PlexClient._get_server or getattr(fn, "__name__", "") == "_get_server":
            return srv
        return fn(*args, **kwargs)

    return patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread)


def _client(srv: MagicMock) -> PlexClient:
    c = PlexClient("http://plex:32400", "token")
    c._server = srv
    return c


# ── get_markers ────────────────────────────────────────────────────────────────

async def test_get_markers_returns_dicts_for_a_known_rating_key():
    srv = _server_with([_raw_marker(1, "intro", 0, 30000), _raw_marker(2, "credits", 600000, 660000, final=True)])
    client = _client(srv)

    with _patch_to_thread(srv):
        markers = await client.get_markers("123")

    assert len(markers) == 2
    assert markers[0] == {
        "plex_marker_id": 1,
        "marker_type": "intro",
        "start_ms": 0,
        "end_ms": 30000,
        "final": False,
    }
    assert markers[1]["final"] is True


async def test_get_markers_returns_empty_list_when_item_has_none():
    srv = _server_with([])
    client = _client(srv)

    with _patch_to_thread(srv):
        assert await client.get_markers("123") == []


async def test_get_markers_handles_a_missing_markers_attribute():
    item = MagicMock(spec=[])  # no .markers at all
    srv = MagicMock()
    srv.fetchItem = MagicMock(return_value=item)
    client = _client(srv)

    with _patch_to_thread(srv):
        assert await client.get_markers("123") == []


async def test_get_markers_raises_when_plex_is_unreachable():
    client = _client(MagicMock())

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("connection refused")):
        with pytest.raises(Exception, match="connection refused"):
            await client.get_markers("123")


async def test_get_markers_does_not_block_the_event_loop():
    """Every plexapi call must go through to_thread; none may run inline."""
    srv = _server_with([_raw_marker(1, "intro", 0, 30000)])
    client = _client(srv)

    with _patch_to_thread(srv) as to_thread:
        await client.get_markers("123")

    assert to_thread.call_count >= 2  # _get_server, fetchItem, markers access


# ── update_marker ──────────────────────────────────────────────────────────────

async def test_update_marker_edits_with_millisecond_offsets():
    target = _raw_marker(7, "intro", 0, 30000)
    srv = _server_with([target])
    client = _client(srv)

    with _patch_to_thread(srv):
        await client.update_marker("123", 7, 1000, 25000)

    target.edit.assert_called_once_with(startTimeOffset=1000, endTimeOffset=25000)


async def test_update_marker_raises_when_the_marker_is_absent():
    srv = _server_with([_raw_marker(7, "intro", 0, 30000)])
    client = _client(srv)

    with _patch_to_thread(srv):
        with pytest.raises(RuntimeError, match="not found"):
            await client.update_marker("123", 999, 0, 1000)


async def test_update_marker_surfaces_a_plex_pass_rejection_as_permission_error():
    target = _raw_marker(7, "intro", 0, 30000)
    target.edit.side_effect = Exception("401 Unauthorized")
    srv = _server_with([target])
    client = _client(srv)

    with _patch_to_thread(srv):
        with pytest.raises(PermissionError, match="Plex Pass"):
            await client.update_marker("123", 7, 0, 1000)


async def test_update_marker_wraps_other_failures_as_runtime_error():
    target = _raw_marker(7, "intro", 0, 30000)
    target.edit.side_effect = Exception("server exploded")
    srv = _server_with([target])
    client = _client(srv)

    with _patch_to_thread(srv):
        with pytest.raises(RuntimeError, match="marker update failed"):
            await client.update_marker("123", 7, 0, 1000)


async def test_update_marker_does_not_block_the_event_loop():
    target = _raw_marker(7, "intro", 0, 30000)
    srv = _server_with([target])
    client = _client(srv)

    with _patch_to_thread(srv) as to_thread:
        await client.update_marker("123", 7, 1000, 25000)

    # The edit itself must be dispatched through to_thread, not called inline.
    assert any(call.args and call.args[0] is target.edit for call in to_thread.call_args_list)
