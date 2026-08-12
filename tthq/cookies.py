"""Browser cookie exports -> Playwright cookie dicts.

Two formats are accepted, because that covers what cookie-export extensions
produce: Netscape `cookies.txt` (the format yt-dlp consumes) and the JSON array
emitted by Cookie-Editor / EditThisCookie.
"""

from __future__ import annotations

import json
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


def _normalize_same_site(raw: Any) -> str:
    """Map extension spellings ('no_restriction', 'unspecified', ...) to Playwright's."""
    value = str(raw or "").strip().lower()
    if value in ("no_restriction", "none"):
        return "None"
    if value == "strict":
        return "Strict"
    if value in ("lax", "unspecified", ""):
        return "Lax"
    return "Lax"


def parse_cookies_json(path: Path) -> list[dict[str, Any]]:
    """Parse a Cookie-Editor / EditThisCookie style JSON export."""
    if not path.is_file():
        raise CookieError(f"No such cookies file: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CookieError(f"{path} is not valid JSON: {exc}") from exc

    # Some exports wrap the array in {"cookies": [...]}, as Playwright's own
    # storage_state does.
    if isinstance(data, dict):
        data = data.get("cookies", data)
    if not isinstance(data, list):
        raise CookieError(
            f"{path} should contain a JSON array of cookie objects, got {type(data).__name__}."
        )

    cookies: list[dict[str, Any]] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise CookieError(f"{path}[{index}] is not a cookie object.")
        name, value = entry.get("name"), entry.get("value")
        if not name or value is None:
            raise CookieError(f"{path}[{index}] is missing 'name' or 'value'.")

        raw_expiry: object = entry.get("expirationDate", entry.get("expires", -1))
        if isinstance(raw_expiry, (int, float, str)):
            try:
                expires_value = int(float(raw_expiry))
            except ValueError:
                expires_value = -1
        else:
            expires_value = -1

        cookies.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": str(entry.get("domain") or ""),
                "path": str(entry.get("path") or "/"),
                "expires": expires_value if expires_value > 0 else -1,
                "httpOnly": bool(entry.get("httpOnly", False)),
                "secure": bool(entry.get("secure", True)),
                "sameSite": _normalize_same_site(entry.get("sameSite")),
            }
        )

    if not cookies:
        raise CookieError(f"{path} contained no cookies.")
    return cookies


def parse_cookies(path: Path) -> list[dict[str, Any]]:
    """Parse either supported export format, chosen by content rather than suffix."""
    if not path.is_file():
        raise CookieError(f"No such cookies file: {path}")
    head = path.read_text(encoding="utf-8", errors="replace").lstrip()[:1]
    return parse_cookies_json(path) if head in ("[", "{") else parse_cookies_txt(path)


def tiktok_cookies(path: Path) -> list[dict[str, Any]]:
    """Cookies for tiktok.com only, validated to look like a logged-in session."""
    cookies = [c for c in parse_cookies(path) if TIKTOK_DOMAIN_HINT in str(c["domain"])]
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
            f"an extension that includes httpOnly cookies. Found: {sorted(names)}"
        )
    return cookies


def storage_state(path: Path) -> dict[str, Any]:
    return {"cookies": tiktok_cookies(path), "origins": []}
