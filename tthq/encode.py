"""Quality-preserving re-encode for TikTok uploads.

The goal is not to make the smallest file, it is to hand TikTok's server-side
transcoder a source it degrades as little as possible: 9:16, yuv420p, H.264
High profile, generous and evenly distributed bitrate, frequent keyframes.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .probe import VideoInfo, probe, require_binary


class EncodeError(RuntimeError):
    pass


# Long edge -> (width, height). TikTok's canonical canvas is 1080x1920; taller
# renditions exist so users can A/B whether TikTok's own downscale looks better.
RESOLUTIONS: dict[int, tuple[int, int]] = {
    1080: (1080, 1920),
    1440: (1440, 2560),
    2160: (2160, 3840),
}

# Video bitrate in Mbps for a 1080x1920 / 30fps clip. Scaled by pixel count and
# frame rate at encode time.
QUALITY_BITRATE_MBPS: dict[str, float] = {
    "safe": 10.0,
    "high": 15.0,
    "max": 20.0,
}

FIT_MODES = ("pad", "crop", "stretch")


@dataclass(frozen=True)
class EncodeSettings:
    resolution: int = 1080
    quality: str = "max"
    fps: int | None = None
    fit: str = "pad"
    denoise: bool = False
    x264_preset: str = "slow"
    audio_bitrate_k: int = 320
    two_pass: bool = False

    def validate(self) -> None:
        if self.resolution not in RESOLUTIONS:
            raise EncodeError(
                f"Unsupported resolution {self.resolution}; choose from {sorted(RESOLUTIONS)}."
            )
        if self.quality not in QUALITY_BITRATE_MBPS:
            raise EncodeError(
                f"Unknown quality {self.quality!r}; choose from {sorted(QUALITY_BITRATE_MBPS)}."
            )
        if self.fit not in FIT_MODES:
            raise EncodeError(f"Unknown fit mode {self.fit!r}; choose from {list(FIT_MODES)}.")
        if self.fps is not None and not 1 <= self.fps <= 120:
            raise EncodeError(f"Unreasonable fps {self.fps}.")


def target_fps(info: VideoInfo, settings: EncodeSettings) -> int:
    if settings.fps is not None:
        return settings.fps
    source = info.fps or 30.0
    # Never invent frames, and never hand TikTok more than 60fps: above that the
    # bitrate budget per frame collapses.
    for candidate in (30, 50, 60):
        if abs(source - candidate) < 1.0:
            return candidate
    return 60 if source > 60 else max(24, int(round(source)))


def bitrate_kbps(settings: EncodeSettings, fps: int) -> int:
    width, height = RESOLUTIONS[settings.resolution]
    base_pixels = 1080 * 1920
    pixel_factor = (width * height) / base_pixels
    # Bitrate need grows sublinearly with frame rate, not linearly.
    fps_factor = (fps / 30.0) ** 0.6
    mbps = QUALITY_BITRATE_MBPS[settings.quality] * pixel_factor * fps_factor
    return int(round(mbps * 1000))


def build_filters(settings: EncodeSettings) -> str:
    width, height = RESOLUTIONS[settings.resolution]
    filters: list[str] = []

    if settings.denoise:
        # Light spatial/temporal denoise: grain and sensor noise are what make
        # gameplay clips fall apart after a second lossy pass.
        filters.append("hqdn3d=1.5:1.5:6:6")

    if settings.fit == "stretch":
        filters.append(f"scale={width}:{height}:flags=lanczos")
    elif settings.fit == "crop":
        filters.append(f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos")
        filters.append(f"crop={width}:{height}")
    else:
        filters.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos")
        filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")

    filters.append("format=yuv420p")
    return ",".join(filters)


def build_command(
    source: Path,
    destination: Path,
    info: VideoInfo,
    settings: EncodeSettings,
    *,
    pass_number: int | None = None,
    passlog: Path | None = None,
) -> list[str]:
    ffmpeg = require_binary("ffmpeg")
    fps = target_fps(info, settings)
    video_kbps = bitrate_kbps(settings, fps)
    keyint = fps * 2

    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
    ]
    if info.audio_codec is None:
        command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    command += [
        "-map",
        "0:v:0",
        "-map",
        "1:a:0" if info.audio_codec is None else "0:a:0",
        "-vf",
        build_filters(settings),
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-level",
        "4.2",
        "-preset",
        settings.x264_preset,
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{int(video_kbps * 1.1)}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-g",
        str(keyint),
        "-keyint_min",
        str(fps),
        "-sc_threshold",
        "0",
        "-x264-params",
        "ref=4:bframes=3:rc-lookahead=60:aq-mode=2",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-movflags",
        "+faststart",
    ]

    if pass_number == 1:
        command += [
            "-pass",
            "1",
            "-passlogfile",
            str(passlog),
            "-an",
            "-f",
            "mp4",
            os.devnull,
        ]
        return command

    if pass_number == 2:
        command += ["-pass", "2", "-passlogfile", str(passlog)]

    command += [
        "-c:a",
        "aac",
        "-b:a",
        f"{settings.audio_bitrate_k}k",
        "-ar",
        "48000",
        "-ac",
        "2",
    ]
    if info.audio_codec is None:
        # The synthetic silent track is infinite; stop when the video does.
        command.append("-shortest")
    command.append(str(destination))
    return command


def _run(command: list[str]) -> None:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise EncodeError(f"ffmpeg failed:\n{tail}")


def encode(source: Path, destination: Path, settings: EncodeSettings) -> Path:
    settings.validate()
    info = probe(source)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if settings.two_pass:
        passlog = destination.with_suffix("")
        _run(build_command(source, destination, info, settings, pass_number=1, passlog=passlog))
        _run(build_command(source, destination, info, settings, pass_number=2, passlog=passlog))
        for leftover in passlog.parent.glob(f"{passlog.name}*.log*"):
            leftover.unlink(missing_ok=True)
    else:
        _run(build_command(source, destination, info, settings))

    return destination


def describe(info: VideoInfo, settings: EncodeSettings) -> str:
    settings.validate()
    fps = target_fps(info, settings)
    width, height = RESOLUTIONS[settings.resolution]
    kbps = bitrate_kbps(settings, fps)
    return (
        f"{info.width}x{info.height}@{info.fps:.0f} {info.video_codec} -> "
        f"{width}x{height}@{fps} h264 high, {kbps / 1000:.1f} Mbps video, "
        f"{settings.audio_bitrate_k}k aac, fit={settings.fit}"
        f"{', denoise' if settings.denoise else ''}"
        f"{', 2-pass' if settings.two_pass else ''}"
    )
