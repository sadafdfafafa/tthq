"""Official TikTok Content Posting API backend.

This is the path Twitch's "share to TikTok" uses: the file is handed to TikTok's
ingestion servers directly instead of through the Studio web form. It needs a
TikTok developer app (client key/secret) and a one-time OAuth grant from the
posting account, and until that app passes TikTok's audit every post it makes is
forced to private visibility.

Only the standard library is used for HTTP: the payloads are small and simple,
and chunked PUT bodies are easier to control byte-exactly this way.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
DIRECT_POST_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
INBOX_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

SCOPES = "user.info.basic,video.publish,video.upload"
DEFAULT_REDIRECT_URI = "http://localhost:8420/api/tiktok/callback"
DEFAULT_TOKEN_PATH = Path.home() / ".cache" / "tthq" / "tiktok_token.json"

# TikTok's chunking rules: 5 MB minimum per chunk, 64 MB maximum, trailing bytes
# merged into the final chunk (which may reach 128 MB).
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
CHUNK_SIZE = 32 * 1024 * 1024

PRIVACY_LEVELS: dict[str, str] = {
    "public": "PUBLIC_TO_EVERYONE",
    "friends": "MUTUAL_FOLLOW_FRIENDS",
    "followers": "FOLLOWER_OF_CREATOR",
    "private": "SELF_ONLY",
}


class ApiError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str
    open_id: str
    scope: str
    expires_at: float
    refresh_expires_at: float

    @property
    def expired(self) -> bool:
        # Refresh a minute early rather than racing the expiry.
        return time.time() > self.expires_at - 60

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> Token:
        if not path.is_file():
            raise ApiError(f"No saved TikTok token at {path}. Run 'tthq api-login' first.")
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            return cls(**data)
        except TypeError as exc:
            raise ApiError(f"{path} is not a token file written by tthq: {exc}") from exc

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> Token:
        now = time.time()
        try:
            return cls(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                open_id=payload.get("open_id", ""),
                scope=payload.get("scope", ""),
                expires_at=now + float(payload["expires_in"]),
                refresh_expires_at=now + float(payload.get("refresh_expires_in", 0)),
            )
        except KeyError as exc:
            raise ApiError(f"TikTok's token response is missing {exc}: {payload}") from exc


def _post_json(url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ApiError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Could not reach {url}: {exc.reason}") from exc

    error = payload.get("error", {})
    if error.get("code") not in (None, "ok"):
        raise ApiError(_explain(error))
    return dict(payload.get("data") or {})


def _explain(error: dict[str, Any]) -> str:
    code = error.get("code", "")
    message = error.get("message", "")
    hints = {
        "unaudited_client_can_only_post_to_private_accounts": (
            "An unaudited developer app can only post to a private account. Either set "
            "the target account to private in TikTok's privacy settings, or use "
            "--inbox to send the video as a draft instead."
        ),
        "privacy_level_option_mismatch": (
            "TikTok did not offer that privacy level for this account; the options come "
            "from creator_info and vary with account type."
        ),
        "scope_not_authorized": (
            "The token does not carry the video.publish scope. Re-run 'tthq api-login' "
            "after adding that scope to the developer app."
        ),
        "access_token_invalid": "The saved token expired. Re-run 'tthq api-login'.",
        "spam_risk_too_many_posts": (
            "TikTok is rate limiting posts for this account; wait and retry."
        ),
    }
    hint = hints.get(code, "")
    return f"TikTok API error {code}: {message}{' -- ' + hint if hint else ''}"


def _token_request(body: dict[str, str]) -> Token:
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(
            f"Token request failed with HTTP {exc.code}: {exc.read().decode(errors='replace')}"
        ) from exc
    if payload.get("error"):
        raise ApiError(f"Token request failed: {payload}")
    return Token.from_response(payload)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches TikTok's OAuth redirect and hands the code back to authorize()."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in query.items()}
        body = b"<h2>tthq: authorization received. You can close this tab.</h2>"
        if "code" not in _CallbackHandler.result:
            body = b"<h2>tthq: no authorization code in the redirect.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def authorize_url(client_key: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_redirect(url: str) -> dict[str, str]:
    """Pull the query parameters out of a redirect URL pasted from the address bar."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url.strip()).query)
    return {key: value[0] for key, value in query.items()}


def authorize(
    client_key: str,
    client_secret: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    token_path: Path = DEFAULT_TOKEN_PATH,
    open_browser: bool = True,
    timeout_seconds: int = 300,
    manual: bool = False,
) -> Token:
    """Run the OAuth code flow, catching the redirect locally.

    TikTok only accepts https redirect URIs for some app configurations, in which
    case nothing can listen on them locally; `manual=True` instead asks for the
    redirected URL to be pasted back from the browser's address bar.
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    if not manual and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ApiError(
            f"redirect_uri {redirect_uri!r} is not local, so tthq cannot catch the code. "
            "Use --manual and paste the redirected URL instead."
        )

    state = secrets.token_urlsafe(16)
    url = authorize_url(client_key, redirect_uri, state)
    print("Open this URL and approve the app:\n" + url)
    if open_browser:
        webbrowser.open(url)

    if manual:
        print(
            "\nAfter approving, the browser lands on your redirect URI (the page itself "
            "may fail to load -- that is fine)."
        )
        pasted = input("Paste the full URL from the address bar here: ")
        result = parse_redirect(pasted)
    else:
        _CallbackHandler.result = {}
        server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), _CallbackHandler)
        server.timeout = timeout_seconds
        server.handle_request()
        server.server_close()
        result = _CallbackHandler.result

    if not result:
        raise ApiError(f"No OAuth redirect arrived within {timeout_seconds}s.")
    if result.get("state") != state:
        raise ApiError("OAuth state mismatch; the redirect did not come from this login attempt.")
    if "code" not in result:
        raise ApiError(f"TikTok refused the authorization: {result}")

    token = _token_request(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": urllib.parse.unquote(result["code"]),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )
    token.save(token_path)
    return token


def refresh(
    token: Token, client_key: str, client_secret: str, *, token_path: Path = DEFAULT_TOKEN_PATH
) -> Token:
    fresh = _token_request(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
    )
    fresh.save(token_path)
    return fresh


def load_token(
    *,
    token_path: Path = DEFAULT_TOKEN_PATH,
    client_key: str = "",
    client_secret: str = "",
) -> Token:
    token = Token.load(token_path)
    if token.expired:
        if not (client_key and client_secret):
            raise ApiError(
                "The saved access token expired and no client key/secret was given to refresh it."
            )
        token = refresh(token, client_key, client_secret, token_path=token_path)
    return token


def creator_info(token: Token) -> dict[str, Any]:
    """Privacy options, nickname and per-post limits TikTok allows for this account."""
    return _post_json(CREATOR_INFO_URL, token.access_token, {})


def _chunk_plan(size: int) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) satisfying TikTok's chunk rules."""
    if size <= MIN_CHUNK:
        return size, 1
    chunk = min(CHUNK_SIZE, MAX_CHUNK)
    count = size // chunk
    if count == 0:
        return size, 1
    return chunk, count


def _put_chunks(upload_url: str, video: Path, chunk: int, count: int) -> None:
    size = video.stat().st_size
    with video.open("rb") as handle:
        for index in range(count):
            first = index * chunk
            # The last chunk absorbs the trailing bytes.
            last = size - 1 if index == count - 1 else first + chunk - 1
            handle.seek(first)
            body = handle.read(last - first + 1)
            request = urllib.request.Request(
                upload_url,
                data=body,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                },
                method="PUT",
            )
            try:
                with urllib.request.urlopen(request, timeout=600) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                raise ApiError(
                    f"Chunk {index + 1}/{count} rejected with HTTP {exc.code}: "
                    f"{exc.read().decode(errors='replace')}"
                ) from exc
            expected = 201 if index == count - 1 else 206
            if status != expected:
                raise ApiError(
                    f"Chunk {index + 1}/{count} returned HTTP {status}, expected {expected}."
                )


def wait_for_publish(
    token: Token, publish_id: str, *, timeout_seconds: int = 600
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _post_json(STATUS_URL, token.access_token, {"publish_id": publish_id})
        status = last.get("status", "")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return last
        if status == "FAILED":
            raise ApiError(f"TikTok failed to publish: {last}")
        time.sleep(5)
    raise ApiError(f"Publish did not finish within {timeout_seconds}s; last status: {last}")


@dataclass
class ApiUploadResult:
    publish_id: str
    status: str
    privacy_level: str
    inbox: bool
    elapsed_seconds: float
    video_id: str = ""


def upload(
    video: Path,
    token: Token,
    *,
    caption: str = "",
    visibility: str = "private",
    inbox: bool = False,
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    wait: bool = True,
    timeout_seconds: int = 600,
) -> ApiUploadResult:
    """Send a video through the Content Posting API.

    `inbox=True` uses the upload endpoint, which drops the video into the user's
    TikTok inbox as a draft they finish in the app; otherwise the video is posted
    directly, which requires the video.publish scope.
    """
    if not video.is_file():
        raise ApiError(f"{video} does not exist.")
    if visibility not in PRIVACY_LEVELS:
        raise ApiError(f"Unknown visibility {visibility!r}; choose from {list(PRIVACY_LEVELS)}.")

    size = video.stat().st_size
    chunk, count = _chunk_plan(size)
    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk,
        "total_chunk_count": count,
    }

    started = time.monotonic()
    privacy_level = PRIVACY_LEVELS[visibility]
    if inbox:
        data = _post_json(INBOX_URL, token.access_token, {"source_info": source_info})
    else:
        options = creator_info(token).get("privacy_level_options") or []
        if options and privacy_level not in options:
            raise ApiError(
                f"TikTok does not offer {privacy_level} for this account; it allows {options}."
            )
        data = _post_json(
            DIRECT_POST_URL,
            token.access_token,
            {
                "post_info": {
                    "title": caption,
                    "privacy_level": privacy_level,
                    "disable_comment": disable_comment,
                    "disable_duet": disable_duet,
                    "disable_stitch": disable_stitch,
                },
                "source_info": source_info,
            },
        )

    publish_id = data.get("publish_id", "")
    upload_url = data.get("upload_url", "")
    if not (publish_id and upload_url):
        raise ApiError(f"TikTok did not return an upload target: {data}")

    _put_chunks(upload_url, video, chunk, count)

    status = "UPLOADED"
    video_id = ""
    if wait:
        final = wait_for_publish(token, publish_id, timeout_seconds=timeout_seconds)
        status = str(final.get("status", ""))
        ids = final.get("publicaly_available_post_id") or final.get("publicly_available_post_id")
        if isinstance(ids, list) and ids:
            video_id = str(ids[0])

    return ApiUploadResult(
        publish_id=publish_id,
        status=status,
        privacy_level="" if inbox else privacy_level,
        inbox=inbox,
        elapsed_seconds=time.monotonic() - started,
        video_id=video_id,
    )
