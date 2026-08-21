"""Unit tests for the subtitle-driven profanity scanner (issue #68)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import subtitle_scanner as ss

SRT = """1
00:00:10,000 --> 00:00:12,000
Hello there, friend.

2
00:00:20,000 --> 00:00:21,500
What the hell was that, you bastard?

3
00:00:30,000 --> 00:00:31,000
A perfectly clean line.
"""


# ── Subtitle parsing ───────────────────────────────────────────────────────────

def test_parse_srt_returns_cues_with_times():
    cues = ss.parse_subtitles(SRT)

    assert len(cues) == 3
    assert cues[0]["start_ms"] == 10000
    assert cues[1]["end_ms"] == 21500
    assert "bastard" in cues[1]["text"]


def test_parse_webvtt_style_dots_are_accepted():
    cues = ss.parse_subtitles("00:00:05.250 --> 00:00:06.750\nA line.\n")

    assert cues[0]["start_ms"] == 5250
    assert cues[0]["end_ms"] == 6750


def test_parse_strips_markup_tags():
    cues = ss.parse_subtitles("00:00:01,000 --> 00:00:02,000\n<i>Italic</i> {\\an8}text\n")

    assert cues[0]["text"] == "Italic text"


def test_parse_joins_multi_line_cues():
    cues = ss.parse_subtitles("00:00:01,000 --> 00:00:02,000\nFirst line\nsecond line\n")

    assert cues[0]["text"] == "First line second line"


def test_parse_returns_empty_for_non_subtitle_text():
    assert ss.parse_subtitles("just some prose") == []


# ── Word matching ──────────────────────────────────────────────────────────────

def test_listed_word_produces_a_padded_mute_segment():
    pattern = ss.build_pattern(["bastard"])
    hits = ss.find_hits(ss.parse_subtitles(SRT), pattern)

    assert len(hits) == 1
    assert hits[0]["start_ms"] == 20000 - ss.PAD_MS
    assert hits[0]["end_ms"] == 21500 + ss.PAD_MS
    assert hits[0]["action"] == "mute"
    assert hits[0]["category"] == "language"
    assert hits[0]["channel"] == "audio"


def test_suffixed_forms_are_matched():
    pattern = ss.build_pattern(["shit"])
    cues = ss.parse_subtitles("00:00:01,000 --> 00:00:02,000\nHe was shitting himself.\n")

    assert len(ss.find_hits(cues, pattern)) == 1


def test_substring_inside_an_innocuous_word_is_not_matched():
    """'ass' must not fire on 'classic' — word boundaries, not substrings."""
    pattern = ss.build_pattern(["ass"])
    cues = ss.parse_subtitles("00:00:01,000 --> 00:00:02,000\nA classic assembly of glasses.\n")

    assert ss.find_hits(cues, pattern) == []


def test_matching_is_case_insensitive():
    pattern = ss.build_pattern(["damn"])
    cues = ss.parse_subtitles("00:00:01,000 --> 00:00:02,000\nDAMN it all.\n")

    assert len(ss.find_hits(cues, pattern)) == 1


def test_empty_wordlist_matches_nothing():
    pattern = ss.build_pattern([])
    cues = ss.parse_subtitles(SRT)

    assert ss.find_hits(cues, pattern) == []


def test_clean_subtitles_produce_no_segments():
    pattern = ss.build_pattern(["bastard"])
    cues = ss.parse_subtitles("00:00:01,000 --> 00:00:02,000\nA perfectly clean line.\n")

    assert ss.find_hits(cues, pattern) == []


# ── Merging ────────────────────────────────────────────────────────────────────

def test_nearby_hits_merge_into_one_segment():
    pattern = ss.build_pattern(["damn", "hell"])
    text = (
        "00:00:10,000 --> 00:00:10,500\nDamn.\n\n"
        "00:00:11,000 --> 00:00:11,500\nHell.\n"
    )
    hits = ss.find_hits(ss.parse_subtitles(text), pattern)

    assert len(hits) == 1
    assert hits[0]["start_ms"] == 10000 - ss.PAD_MS
    assert hits[0]["end_ms"] == 11500 + ss.PAD_MS
    assert set(hits[0]["labels"].split(",")) == {"damn", "hell"}


def test_distant_hits_stay_separate():
    pattern = ss.build_pattern(["damn"])
    text = (
        "00:00:10,000 --> 00:00:10,500\nDamn.\n\n"
        "00:01:00,000 --> 00:01:00,500\nDamn.\n"
    )

    assert len(ss.find_hits(ss.parse_subtitles(text), pattern)) == 2


# ── Wordlist configuration ─────────────────────────────────────────────────────

async def test_wordlist_defaults_when_unset(setup_db):
    assert await ss.load_wordlist() == ss.DEFAULT_WORDLIST


async def test_configured_wordlist_replaces_the_default(setup_db):
    await db.set_setting("profanity_wordlist", '["flibbertigibbet"]')

    assert await ss.load_wordlist() == ["flibbertigibbet"]


async def test_invalid_wordlist_json_falls_back_to_default(setup_db):
    await db.set_setting("profanity_wordlist", "not json")

    assert await ss.load_wordlist() == ss.DEFAULT_WORDLIST


# ── scan_title ─────────────────────────────────────────────────────────────────

async def test_scan_title_stores_segments_from_a_sidecar(setup_db, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")

    count = await ss.scan_title("guid-sub", "Sub Movie", str(media))

    assert count == 1
    stored = await db.get_segments_for_guid("guid-sub")
    assert stored[0]["action"] == "mute"
    assert stored[0]["source"] == "subtitles"


async def test_scan_title_without_subtitles_is_a_clean_no_op(setup_db, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")

    with patch.object(ss, "extract_embedded_subtitle", new=AsyncMock(return_value=None)):
        count = await ss.scan_title("guid-none", "No Subs", str(media))

    assert count == 0
    assert await db.get_segments_for_guid("guid-none") == []


async def test_scan_title_falls_back_to_the_embedded_track(setup_db, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_text("x", encoding="utf-8")

    with patch.object(ss, "extract_embedded_subtitle", new=AsyncMock(return_value=SRT)):
        count = await ss.scan_title("guid-embed", "Embedded", str(media))

    assert count == 1
