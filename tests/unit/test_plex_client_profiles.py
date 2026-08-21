"""Unit tests for learned per-client player transports (issue #60)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from cleanplex.plex_client import PlexClient


class _CountingTransport(httpx.MockTransport):
    """MockTransport that records every request and answers only one exact URL."""

    def __init__(self, accept_port: int, accept_variant_marker: str):
        self.requests: list[httpx.Request] = []
        self._accept_port = accept_port
        self._marker = accept_variant_marker
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        port_ok = request.url.port == self._accept_port
        # The marker distinguishes the token-in-query variants from the
        # token-in-header ones, and the plain header set from the full one.
        if self._marker == "query":
            variant_ok = "X-Plex-Token" in request.url.params
        else:
            variant_ok = "X-Plex-Token" in request.headers
        if port_ok and variant_ok:
            return httpx.Response(200, content=b"ok")
        return httpx.Response(404, content=b"no")


def _client(transport: httpx.MockTransport) -> PlexClient:
    c = PlexClient("http://plex:32400", "token")
    c._http = httpx.AsyncClient(transport=transport)
    return c


async def test_second_seek_reuses_learned_transport():
    """The scramble runs once; after that the client goes straight to what worked."""
    transport = _CountingTransport(accept_port=3005, accept_variant_marker="header")
    c = _client(transport)

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        assert await c.seek("client-id", 30000, client_address="10.0.0.5", client_port=32500) is True
        first_pass = len(transport.requests)
        assert first_pass > 1, "expected a search on the first command"

        transport.requests.clear()
        assert await c.seek("client-id", 60000, client_address="10.0.0.5", client_port=32500) is True

    assert len(transport.requests) == 1


async def test_learned_profile_records_port_and_variant():
    transport = _CountingTransport(accept_port=3005, accept_variant_marker="header")
    c = _client(transport)

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        await c.seek("client-id", 30000, client_address="10.0.0.5", client_port=32500)

    profile = c._client_profiles["client-id"]
    assert profile["transport"] == "direct"
    assert profile["port"] == 3005


async def test_proxy_success_is_remembered_and_skips_direct_attempts():
    transport = _CountingTransport(accept_port=32500, accept_variant_marker="query")
    c = _client(transport)

    with patch("cleanplex.plex_client.asyncio.to_thread", new=AsyncMock(return_value=MagicMock())):
        assert await c.seek("client-id", 30000, client_address="10.0.0.5") is True

    assert c._client_profiles["client-id"] == {"transport": "proxy"}
    assert transport.requests == []


async def test_stale_profile_is_relearned_when_it_stops_working():
    transport = _CountingTransport(accept_port=3005, accept_variant_marker="header")
    c = _client(transport)
    # Pretend we previously learned a port that no longer answers.
    c._client_profiles["client-id"] = {"transport": "direct", "port": 32500, "variant": 0}

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        assert await c.seek("client-id", 30000, client_address="10.0.0.5", client_port=32500) is True

    assert c._client_profiles["client-id"]["port"] == 3005


async def test_set_volume_uses_the_same_learned_transport():
    transport = _CountingTransport(accept_port=3005, accept_variant_marker="header")
    c = _client(transport)
    c._client_profiles["client-id"] = {"transport": "direct", "port": 3005, "variant": 1}

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        assert await c.set_volume("client-id", 0, client_address="10.0.0.5") is True

    assert len(transport.requests) == 1
    assert "setParameters" in str(transport.requests[0].url)
    assert transport.requests[0].url.params["volume"] == "0"


async def test_set_volume_clamps_out_of_range_levels():
    transport = _CountingTransport(accept_port=3005, accept_variant_marker="header")
    c = _client(transport)
    c._client_profiles["client-id"] = {"transport": "direct", "port": 3005, "variant": 1}

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        await c.set_volume("client-id", 250, client_address="10.0.0.5")

    assert transport.requests[0].url.params["volume"] == "100"


async def test_profiles_round_trip_through_settings(setup_db):
    from cleanplex import database as db

    c = PlexClient("http://plex:32400", "token")
    await c._remember_profile("client-id", {"transport": "direct", "port": 3005, "variant": 2})

    restored = PlexClient("http://plex:32400", "token")
    await restored.load_client_profiles()

    assert restored._client_profiles["client-id"] == {
        "transport": "direct",
        "port": 3005,
        "variant": 2,
    }
    assert await db.get_setting("client_seek_profiles") != "{}"
