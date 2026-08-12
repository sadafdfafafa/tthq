"""Netscape cookies.txt -> Playwright cookie dicts.

This is the format every browser cookie-export extension produces and the same
one yt-dlp consumes, so users can reuse an export they already have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REQUIRED_COOKIE = "sessionid"
TIKTOK_DOMAIN_HINT = "tiktok.com"


class CookieError(RuntimeError):
    pass


def _same_site(domain: str) -> str:
    return "Lax" if domain else "None"


def parse_cookies_txt(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise CookieError(f"No such cookies file: {path}")

    cookies: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        # '#HttpOnly_' is a real prefix used by curl/extensions, not a comment.
        if line.startswith("#"):
            if not line.startswith("#HttpOnly_"):
                continue
            line = line[len("#HttpOnly_") :]

        fields = line.split("\t")
        if len(fields) != 7:
            fields = line.split()
        if len(fields) != 7:
            raise CookieError(
                f"{path}:{lineno} has {len(fields)} fields, expected 7 tab-separated "
                f"Netscape cookie fields. Re-export with a cookies.txt extension."
            )

        domain, _include_subdomains, cookie_path, secure, expires, name, value = fields
        try:
            expires_value = int(float(expires))
        except ValueError:
            expires_value = 0

        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie_path or "/",
                "expires": expires_value if expires_value > 0 else -1,
                "httpOnly": False,
                "secure": secure.upper() == "TRUE",
                "sameSite": _same_site(domain),
            }
        )

    if not cookies:
        raise CookieError(f"{path} contained no cookies.")
    return cookies


def tiktok_cookies(path: Path) -> list[dict[str, Any]]:
    """Cookies for tiktok.com only, validated to look like a logged-in session."""
    cookies = [c for c in parse_cookies_txt(path) if TIKTOK_DOMAIN_HINT in str(c["domain"])]
    if not cookies:
        raise CookieError(
            f"{path} has no tiktok.com cookies. Export them while logged in to "
            f"https://www.tiktok.com."
        )

    names = {c["name"] for c in cookies}
    if REQUIRED_COOKIE not in names:
        raise CookieError(
            f"{path} is missing the {REQUIRED_COOKIE!r} cookie, so it is not a logged-in "
            f"session. Export cookies from a tab where you are signed in to TikTok, with "
            f"an extension that includes httpOnly cookies."
        )
    return cookies


def storage_state(path: Path) -> dict[str, Any]:
    return {"cookies": tiktok_cookies(path), "origins": []}
