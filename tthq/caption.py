"""Compose a TikTok caption from a title and a hashtag list."""

from __future__ import annotations

import re

# TikTok's caption limit has moved over time; 2200 is the current documented cap.
MAX_CAPTION_CHARS = 2200
_INVALID_TAG_CHARS = re.compile(r"[^0-9A-Za-z_\u00c0-\uffff]")


class CaptionError(ValueError):
    pass


def normalize_hashtag(raw: str) -> str | None:
    """'#My Clip!' -> 'MyClip'. Returns None when nothing usable remains."""
    tag = _INVALID_TAG_CHARS.sub("", raw.strip().lstrip("#"))
    return tag or None


def parse_hashtags(raw: str | list[str]) -> list[str]:
    """Accept 'a, #b c' or ['a', '#b'] and return a deduplicated ordered list."""
    if isinstance(raw, str):
        candidates = re.split(r"[,\s]+", raw)
    else:
        candidates = [part for item in raw for part in re.split(r"[,\s]+", item)]

    tags: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tag = normalize_hashtag(candidate)
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
    return tags


def build_caption(title: str = "", hashtags: str | list[str] | None = None) -> str:
    tags = parse_hashtags(hashtags or [])
    parts = [title.strip()] if title.strip() else []
    parts += [f"#{tag}" for tag in tags]
    caption = " ".join(parts)

    if len(caption) > MAX_CAPTION_CHARS:
        raise CaptionError(
            f"Caption is {len(caption)} characters, over TikTok's {MAX_CAPTION_CHARS} "
            f"limit. Shorten the title or drop hashtags."
        )
    return caption
