"""Parsers for externally produced skip files.

Each parser turns a source format into the same normalized segment dict shape:

    {"start_ms": int, "end_ms": int, "category": str, "severity": str,
     "action": str, "channel": str, "labels": str}

Importing a file is dramatically cheaper than scanning: a title with a sidecar
costs no frame extraction and no inference at all.
"""

from __future__ import annotations

from pathlib import Path

from . import edl, mcf, paste, skp

# Extension to parser module. Used by sidecar discovery and by the upload endpoint
# when the caller does not name a format explicitly.
PARSERS = {
    ".skp": skp,
    ".edl": edl,
    ".mcf": mcf,
    ".txt": paste,
}

# Formats we look for beside a media file, most specific first.
SIDECAR_EXTENSIONS = (".skp", ".mcf", ".edl")


class ImportError_(ValueError):
    """Raised when a file cannot be parsed. Message is shown to the user."""


def parse(text: str, fmt: str) -> list[dict]:
    """Parse `text` using the named format ('skp', 'edl', 'mcf' or 'paste')."""
    module = {"skp": skp, "edl": edl, "mcf": mcf, "paste": paste}.get(fmt)
    if module is None:
        raise ImportError_(f"Unsupported skip file format: {fmt}")
    return module.parse(text)


def parse_file(path: str | Path) -> tuple[list[dict], str]:
    """Parse a file by extension, returning (segments, source_name)."""
    path = Path(path)
    module = PARSERS.get(path.suffix.lower())
    if module is None:
        raise ImportError_(f"Unsupported skip file type: {path.suffix or path.name}")
    # Skip files are user-authored text of unknown encoding; replace rather than
    # fail, so one bad byte does not lose an otherwise valid file.
    text = path.read_text(encoding="utf-8", errors="replace")
    return module.parse(text), module.SOURCE


def find_sidecar(media_path: str | Path) -> Path | None:
    """Return a skip file sitting beside the media file, if one exists."""
    media = Path(media_path)
    for ext in SIDECAR_EXTENSIONS:
        candidate = media.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None
