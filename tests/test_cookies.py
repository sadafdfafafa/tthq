import pytest

from tthq.cookies import CookieError, parse_cookies_txt, storage_state, tiktok_cookies

VALID = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        ".tiktok.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc123",
        "#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t1893456000\tsid_tt\tdef456",
        ".example.com\tTRUE\t/\tFALSE\t0\tunrelated\tvalue",
    ]
)


def _write(tmp_path, text):
    path = tmp_path / "cookies.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_reads_all_cookies_including_httponly(tmp_path):
    cookies = parse_cookies_txt(_write(tmp_path, VALID))
    assert [c["name"] for c in cookies] == ["sessionid", "sid_tt", "unrelated"]


def test_parse_marks_secure_flag(tmp_path):
    cookies = parse_cookies_txt(_write(tmp_path, VALID))
    assert cookies[0]["secure"] is True
    assert cookies[2]["secure"] is False


def test_session_cookies_without_expiry_become_minus_one(tmp_path):
    cookies = parse_cookies_txt(_write(tmp_path, VALID))
    assert cookies[2]["expires"] == -1


def test_tiktok_cookies_filters_other_domains(tmp_path):
    cookies = tiktok_cookies(_write(tmp_path, VALID))
    assert {c["name"] for c in cookies} == {"sessionid", "sid_tt"}


def test_missing_sessionid_is_rejected(tmp_path):
    text = ".tiktok.com\tTRUE\t/\tTRUE\t1893456000\ttt_csrf_token\txyz"
    with pytest.raises(CookieError, match="sessionid"):
        tiktok_cookies(_write(tmp_path, text))


def test_no_tiktok_cookies_is_rejected(tmp_path):
    text = ".example.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc"
    with pytest.raises(CookieError, match="no tiktok.com cookies"):
        tiktok_cookies(_write(tmp_path, text))


def test_malformed_line_is_rejected(tmp_path):
    with pytest.raises(CookieError, match="expected 7"):
        parse_cookies_txt(_write(tmp_path, "just-one-field"))


def test_empty_file_is_rejected(tmp_path):
    with pytest.raises(CookieError, match="no cookies"):
        parse_cookies_txt(_write(tmp_path, "# only a comment\n"))


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(CookieError, match="No such cookies file"):
        parse_cookies_txt(tmp_path / "nope.txt")


def test_storage_state_shape(tmp_path):
    state = storage_state(_write(tmp_path, VALID))
    assert state["origins"] == []
    assert len(state["cookies"]) == 2
