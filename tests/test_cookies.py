import pytest

from tthq.cookies import (
    CookieError,
    parse_cookies,
    parse_cookies_json,
    parse_cookies_txt,
    storage_state,
    tiktok_cookies,
)

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


JSON_EXPORT = """[
  {"name": "sessionid", "value": "abc123", "domain": ".tiktok.com", "path": "/",
   "secure": true, "httpOnly": true, "sameSite": "no_restriction",
   "expirationDate": 1893456000.123},
  {"name": "unrelated", "value": "v", "domain": ".example.com", "path": "/",
   "sameSite": "unspecified"}
]"""


def _write_json(tmp_path, text):
    path = tmp_path / "cookies.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_json_export_is_parsed(tmp_path):
    cookies = parse_cookies_json(_write_json(tmp_path, JSON_EXPORT))
    assert [c["name"] for c in cookies] == ["sessionid", "unrelated"]
    assert cookies[0]["expires"] == 1893456000
    assert cookies[0]["httpOnly"] is True


def test_json_same_site_values_are_mapped_to_playwright_spelling(tmp_path):
    cookies = parse_cookies_json(_write_json(tmp_path, JSON_EXPORT))
    assert cookies[0]["sameSite"] == "None"
    assert cookies[1]["sameSite"] == "Lax"


def test_json_wrapped_in_storage_state_shape_is_accepted(tmp_path):
    path = _write_json(tmp_path, '{"cookies": ' + JSON_EXPORT + ', "origins": []}')
    assert [c["name"] for c in parse_cookies_json(path)] == ["sessionid", "unrelated"]


def test_json_entry_missing_name_is_rejected(tmp_path):
    with pytest.raises(CookieError, match="missing 'name' or 'value'"):
        parse_cookies_json(_write_json(tmp_path, '[{"value": "v", "domain": ".tiktok.com"}]'))


def test_invalid_json_is_rejected(tmp_path):
    with pytest.raises(CookieError, match="not valid JSON"):
        parse_cookies_json(_write_json(tmp_path, "{not json"))


def test_json_object_without_cookie_list_is_rejected(tmp_path):
    with pytest.raises(CookieError, match="JSON array of cookie objects"):
        parse_cookies_json(_write_json(tmp_path, '{"foo": "bar"}'))


def test_parse_cookies_detects_format_by_content_not_suffix(tmp_path):
    txt_named_json = tmp_path / "cookies.json"
    txt_named_json.write_text(VALID, encoding="utf-8")
    assert [c["name"] for c in parse_cookies(txt_named_json)] == [
        "sessionid",
        "sid_tt",
        "unrelated",
    ]

    json_named_txt = tmp_path / "cookies.txt"
    json_named_txt.write_text(JSON_EXPORT, encoding="utf-8")
    assert [c["name"] for c in parse_cookies(json_named_txt)] == ["sessionid", "unrelated"]


def test_storage_state_accepts_json_export(tmp_path):
    state = storage_state(_write_json(tmp_path, JSON_EXPORT))
    assert [c["name"] for c in state["cookies"]] == ["sessionid"]
