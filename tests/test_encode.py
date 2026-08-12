from pathlib import Path

import pytest

from tthq.encode import (
    EncodeError,
    EncodeSettings,
    bitrate_kbps,
    build_command,
    build_filters,
    target_fps,
)
from tthq.probe import VideoInfo


def info(width=1920, height=1080, fps=60.0, audio="aac"):
    return VideoInfo(
        path=Path("clip.mp4"),
        width=width,
        height=height,
        fps=fps,
        duration=30.0,
        video_codec="h264",
        audio_codec=audio,
        bit_rate=20_000_000,
        pix_fmt="yuv420p",
    )


def test_target_fps_snaps_to_common_rates():
    assert target_fps(info(fps=59.94), EncodeSettings()) == 60
    assert target_fps(info(fps=29.97), EncodeSettings()) == 30


def test_target_fps_caps_high_frame_rate_sources():
    assert target_fps(info(fps=120.0), EncodeSettings()) == 60


def test_target_fps_respects_explicit_override():
    assert target_fps(info(fps=60.0), EncodeSettings(fps=30)) == 30


def test_bitrate_scales_with_resolution_and_frame_rate():
    base = bitrate_kbps(EncodeSettings(resolution=1080, quality="max"), 30)
    faster = bitrate_kbps(EncodeSettings(resolution=1080, quality="max"), 60)
    bigger = bitrate_kbps(EncodeSettings(resolution=2160, quality="max"), 30)
    assert base == 20_000
    assert base < faster < base * 2
    assert bigger > base * 3


def test_quality_tiers_are_ordered():
    tiers = [bitrate_kbps(EncodeSettings(quality=q), 30) for q in ("safe", "high", "max")]
    assert tiers == sorted(tiers)


def test_pad_filter_preserves_aspect_and_forces_yuv420p():
    filters = build_filters(EncodeSettings(fit="pad"))
    assert "force_original_aspect_ratio=decrease" in filters
    assert filters.startswith("scale=1080:1920")
    assert filters.endswith("format=yuv420p")


def test_crop_filter_fills_the_frame():
    filters = build_filters(EncodeSettings(fit="crop"))
    assert "force_original_aspect_ratio=increase" in filters
    assert "crop=1080:1920" in filters


def test_denoise_runs_before_scaling():
    filters = build_filters(EncodeSettings(denoise=True))
    assert filters.index("hqdn3d") < filters.index("scale=")


def test_command_uses_high_profile_and_faststart():
    command = build_command(
        Path("in.mp4"),
        Path("out.mp4"),
        info(),
        EncodeSettings(),
    )
    assert "-profile:v" in command and command[command.index("-profile:v") + 1] == "high"
    assert "+faststart" in command
    assert command[-1] == "out.mp4"


def test_command_adds_silent_audio_when_source_has_none():
    command = build_command(
        Path("in.mp4"),
        Path("out.mp4"),
        info(audio=None),
        EncodeSettings(),
    )
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in command
    assert "-shortest" in command


def test_keyframe_interval_is_two_seconds():
    command = build_command(
        Path("in.mp4"),
        Path("out.mp4"),
        info(fps=30.0),
        EncodeSettings(),
    )
    assert command[command.index("-g") + 1] == "60"


def test_invalid_settings_are_rejected():
    with pytest.raises(EncodeError):
        EncodeSettings(resolution=720).validate()
    with pytest.raises(EncodeError):
        EncodeSettings(quality="ultra").validate()
    with pytest.raises(EncodeError):
        EncodeSettings(fit="squish").validate()
    with pytest.raises(EncodeError):
        EncodeSettings(fps=0).validate()
