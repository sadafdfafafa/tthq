import shutil
import struct
import subprocess

import pytest

from tthq.spoof import parse_boxes, spoof_sample_count

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to build a fixture clip"
)


def make_clip(path, fps=60, seconds=2):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x568:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )
    return path


def tables(path):
    data = path.read_bytes()
    found = {}

    def walk(boxes):
        for box in boxes:
            if box.children:
                walk(box.children)
            elif box.type in {"stsz", "stts"}:
                payload = data[box.start + box.header : box.end]
                found.setdefault(box.type, []).append(payload)

    walk(parse_boxes(data, 0, len(data)))
    return found


def test_spoof_inflates_the_declared_sample_count(tmp_path):
    source = make_clip(tmp_path / "in.mp4")
    destination = tmp_path / "out.mp4"

    declared = spoof_sample_count(source, destination)

    before = struct.unpack_from(">I", tables(source)["stsz"][0], 8)[0]
    after = struct.unpack_from(">I", tables(destination)["stsz"][0], 8)[0]
    assert before == 120
    assert after == declared == before * 20 // 3


def test_spoof_leaves_real_timing_and_pictures_untouched(tmp_path):
    source = make_clip(tmp_path / "in.mp4")
    destination = tmp_path / "out.mp4"

    spoof_sample_count(source, destination)

    # stts is what decoders trust: same count of real samples, same real timing
    # (the remux rescales it to a 90 kHz timescale, so compare seconds).
    stts = tables(destination)["stts"][0]
    entries = struct.unpack_from(">I", stts, 4)[0]
    count, duration = struct.unpack_from(">II", stts, 8)
    assert entries == 1
    assert count == 120
    assert duration == 90_000 // 60
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=r_frame_rate,nb_read_frames",
            "-of",
            "csv=p=0",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "60/1,120"


def test_spoof_does_not_leave_its_temporary_remux_behind(tmp_path):
    source = make_clip(tmp_path / "in.mp4")
    destination = tmp_path / "out.mp4"

    spoof_sample_count(source, destination)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.mp4", "out.mp4"]
