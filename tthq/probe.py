"""ffprobe wrappers and source-quality heuristics."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    duration: float
    video_codec: str
    audio_codec: str | None
    bit_rate: int | None
    pix_fmt: str | None

    @property
    def is_vertical(self) -> bool:
        return self.height >= self.width

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def bitrate_per_megapixel(self) -> float | None:
        """Mbps per megapixel per 30fps -- a rough source-quality signal."""
        if not self.bit_rate or not self.width or not self.height or not self.fps:
            return None
        megapixels = (self.width * self.height) / 1_000_000
        fps_factor = self.fps / 30.0
        denom = megapixels * fps_factor
        if denom <= 0:
            return None
        return (self.bit_rate / 1_000_000) / denom


def require_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ProbeError(
            f"{name!r} not found on PATH. Install FFmpeg (e.g. 'apt install ffmpeg' "
            f"or 'brew install ffmpeg') and retry."
        )
    return found


def _parse_fraction(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            numerator, denominator = float(num), float(den)
        except ValueError:
            return 0.0
        return numerator / denominator if denominator else 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe(path: Path) -> VideoInfo:
    ffprobe = require_binary("ffprobe")
    if not path.is_file():
        raise ProbeError(f"No such file: {path}")

    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")

    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError(f"{path} contains no video stream")

    fmt = data.get("format", {})
    bit_rate = video.get("bit_rate") or fmt.get("bit_rate")

    return VideoInfo(
        path=path,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        duration=float(fmt.get("duration") or video.get("duration") or 0.0),
        video_codec=str(video.get("codec_name") or "unknown"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        bit_rate=int(bit_rate) if bit_rate else None,
        pix_fmt=video.get("pix_fmt"),
    )


def warnings_for(info: VideoInfo) -> list[str]:
    """Source problems that TikTok's transcoder will make worse."""
    notes: list[str] = []

    if not info.is_vertical:
        notes.append(
            f"Source is landscape ({info.width}x{info.height}). TikTok will pillarbox or "
            f"crop it; frame the clip vertically for the best use of the bitrate budget."
        )
    elif abs(info.aspect - 9 / 16) > 0.02:
        notes.append(
            f"Aspect {info.width}:{info.height} is not 9:16; padding or cropping will be applied."
        )

    if info.height < 1080 and info.width < 1080:
        notes.append(
            f"Source is only {info.width}x{info.height}. Upscaling cannot add detail -- "
            f"re-record at 1080p or higher if possible."
        )

    if info.fps > 60.5:
        notes.append(
            f"Source is {info.fps:.0f}fps. TikTok caps playback well below that; the extra "
            f"frames only dilute the bitrate. Consider --fps 60 or --fps 30."
        )

    density = info.bitrate_per_megapixel
    if density is not None and density < 4.0:
        notes.append(
            f"Source bitrate is low for its resolution (~{density:.1f} Mbps per megapixel at "
            f"30fps). Re-encoding cannot recover detail already lost -- capture at a higher "
            f"bitrate for future clips."
        )

    if info.pix_fmt and "420" not in info.pix_fmt:
        notes.append(
            f"Source pixel format is {info.pix_fmt}; it will be converted to yuv420p, which "
            f"is the only format TikTok reliably accepts."
        )

    if info.duration and info.duration > 600:
        notes.append(
            f"Clip is {info.duration / 60:.1f} minutes. Long uploads get a smaller bitrate "
            f"budget; splitting into shorter clips preserves more detail."
        )

    if info.audio_codec is None:
        notes.append("Source has no audio track; a silent AAC track will be added.")

    return notes
