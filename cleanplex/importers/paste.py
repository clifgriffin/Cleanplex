"""Parser for loose pasted timestamp lists.

Sources that publish timestamps as prose — forum posts, supporter posts, notes in
a spreadsheet — have no machine format. This accepts what people actually paste::

    00:12:30 - 00:13:05 nudity
    1:02:11 to 1:02:20  violence (fight)
    12:30-13:05

The category, when present, is matched loosely against the same vocabulary the
other parsers use; anything unrecognised becomes `other` rather than being lost.
"""

from __future__ import annotations

import re

from ._common import CATEGORIES, ParseError, to_ms, validate_segment

SOURCE = "paste"

# Two timestamps separated by -, –, to, or --> with optional trailing description.
_LINE_RE = re.compile(
    r"^\s*([\d:.,]+)\s*(?:-+>|–|—|-|to\b)\s*([\d:.,]+)\s*(.*)$",
    re.IGNORECASE,
)

_KEYWORDS = (
    (re.compile(r"nud|topless|naked", re.I), "nudity"),
    (re.compile(r"sex|kiss|intim", re.I), "sex"),
    (re.compile(r"viol|gore|blood|fight|kill", re.I), "violence"),
    (re.compile(r"lang|profan|curs|swear|f-word", re.I), "language"),
    (re.compile(r"drug|alcohol|smok|drink", re.I), "drugs"),
    (re.compile(r"scar|fright|intens|jump", re.I), "fear"),
    (re.compile(r"commercial|advert", re.I), "commercial"),
)


def _category_from(description: str) -> str:
    lowered = description.lower()
    for name in CATEGORIES:
        if name in lowered:
            return name
    for pattern, name in _KEYWORDS:
        if pattern.search(description):
            return name
    return "other"


def parse(text: str) -> list[dict]:
    """Parse pasted timestamp lines, ignoring anything that is not a time range."""
    segments: list[dict] = []

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue

        description = match.group(3).strip()
        category = _category_from(description)
        segments.append(validate_segment({
            "start_ms": to_ms(match.group(1)),
            "end_ms": to_ms(match.group(2)),
            "category": category,
            # Hand-entered lists rarely grade severity, and a viewer pasting a list
            # wants those moments gone, so treat them as high.
            "severity": "high",
            "action": "mute" if category == "language" else "skip",
            "labels": description,
        }))

    if not segments:
        raise ParseError(
            "No timestamp ranges found. Expected lines like "
            "'00:12:30 - 00:13:05 nudity'."
        )

    segments.sort(key=lambda s: s["start_ms"])
    return segments
