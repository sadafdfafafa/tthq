from __future__ import annotations

import json
import time

import pytest

from tthq import api


def test_chunk_plan_small_file_uploads_whole() -> None:
    assert api._chunk_plan(4 * 1024 * 1024) == (4 * 1024 * 1024, 1)


def test_chunk_plan_respects_chunk_limits() -> None:
    size = 100 * 1024 * 1024
    chunk, count = api._chunk_plan(size)
    assert api.MIN_CHUNK <= chunk <= api.MAX_CHUNK
    assert count == size // chunk
    # The trailing bytes ride along with the final chunk.
    assert count * chunk <= size


def test_privacy_levels_cover_visibility_choices() -> None:
    assert api.PRIVACY_LEVELS["private"] == "SELF_ONLY"
    assert api.PRIVACY_LEVELS["public"] == "PUBLIC_TO_EVERYONE"


def test_token_round_trip(tmp_path) -> None:
    token = api.Token.from_response(
        {
            "access_token": "act.x",
            "refresh_token": "rft.x",
            "open_id": "abc",
            "scope": "video.publish",
            "expires_in": 86400,
            "refresh_expires_in": 31536000,
        }
    )
    path = tmp_path / "token.json"
    token.save(path)
    assert json.loads(path.read_text())["access_token"] == "act.x"
    assert api.Token.load(path) == token
    assert not token.expired


def test_expired_token_is_detected() -> None:
    token = api.Token("a", "r", "o", "video.publish", time.time() - 1, time.time() + 100)
    assert token.expired


def test_load_token_without_file_explains_login(tmp_path) -> None:
    with pytest.raises(api.ApiError, match="api-login"):
        api.load_token(token_path=tmp_path / "missing.json")


def test_authorize_rejects_non_local_redirect_without_manual() -> None:
    with pytest.raises(api.ApiError, match="--manual"):
        api.authorize("key", "secret", redirect_uri="https://example.com/cb")


def test_parse_redirect_extracts_code_and_state() -> None:
    parsed = api.parse_redirect("https://example.com/cb?code=abc%2F1&state=xyz&scopes=a,b ")
    assert parsed["code"] == "abc/1"
    assert parsed["state"] == "xyz"


def test_authorize_url_carries_scopes_and_state() -> None:
    url = api.authorize_url("key", "https://example.com/cb", "st")
    assert "client_key=key" in url and "state=st" in url and "video.publish" in url
    assert "code_challenge" not in url


def test_authorize_url_adds_hex_pkce_challenge() -> None:
    verifier = "v" * 43
    url = api.authorize_url("key", "http://localhost:8420/cb", "st", verifier=verifier)
    assert f"code_challenge={api.code_challenge(verifier)}" in url
    assert "code_challenge_method=S256" in url
    # Hex, not base64url: 64 characters, all hex digits.
    digest = api.code_challenge(verifier)
    assert len(digest) == 64 and int(digest, 16) >= 0


def test_error_explanation_mentions_audit_workaround() -> None:
    message = api._explain(
        {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "nope"}
    )
    assert "private" in message and "--inbox" in message


def test_upload_rejects_unknown_visibility(tmp_path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    token = api.Token("a", "r", "o", "video.publish", time.time() + 999, time.time() + 999)
    with pytest.raises(api.ApiError, match="Unknown visibility"):
        api.upload(video, token, visibility="everyone")
