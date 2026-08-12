import pytest

from tthq.caption import MAX_CAPTION_CHARS, CaptionError, build_caption, parse_hashtags


def test_parse_hashtags_accepts_mixed_separators_and_strips_hashes():
    assert parse_hashtags("valorant, #gaming clutch") == ["valorant", "gaming", "clutch"]


def test_parse_hashtags_deduplicates_case_insensitively():
    assert parse_hashtags("Gaming gaming GAMING") == ["Gaming"]


def test_parse_hashtags_drops_invalid_characters():
    assert parse_hashtags("#my-clip!, ok_1") == ["myclip", "ok_1"]


def test_parse_hashtags_accepts_list_input():
    assert parse_hashtags(["a b", "#c"]) == ["a", "b", "c"]


def test_build_caption_joins_title_and_tags():
    assert build_caption("1v5 clutch", "valorant, gaming") == "1v5 clutch #valorant #gaming"


def test_build_caption_handles_empty_inputs():
    assert build_caption("", "") == ""
    assert build_caption("just a title") == "just a title"
    assert build_caption("", "solo") == "#solo"


def test_build_caption_rejects_overlong_caption():
    with pytest.raises(CaptionError):
        build_caption("x" * (MAX_CAPTION_CHARS + 1))
