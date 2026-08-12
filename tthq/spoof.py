"""Inflate an MP4's declared video sample count without touching the pictures.

Clip editors circulate this as the "120fps method": the file's sample table is
padded with dummy entries so a parser that derives a frame rate from
`stsz.sample_count / duration` sees several times the real rate, while `stts`
(the real timing) is left alone. Nothing is interpolated and no frame is added
to the picture stream — the extra entries all point at one 8-byte dummy sample
appended to `mdat`.

The premise is that TikTok's ingest transcoder allocates its delivery bitrate
partly from the declared frame rate. That is unverified: measured delivery gears
for ordinary uploads are 540p/30fps at ~1-2 Mbps regardless of the source, so
treat this as an experiment to A/B, not a fix.

The remux that precedes the patch also normalises the container the way a stock
phone export looks: metadata and `udta` stripped, `isom` brand, 90 kHz video
timescale, canonical handler names, `und` language.

Note the output is deliberately inconsistent: ffmpeg reports "wrong sample
count" and ignores the padding. Players that trust the sample table over `stts`
may misbehave.
"""

from __future__ import annotations

import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .probe import require_binary

# One dummy sample, referenced by every padding entry in the sample table.
FAKE_SAMPLE = bytes([0, 0, 0, 4, 0, 0, 0, 0])
FAKE_SAMPLE_SIZE = len(FAKE_SAMPLE)

# Boxes whose payload is a list of child boxes rather than data.
CONTAINERS = frozenset(
    {"moov", "trak", "mdia", "minf", "stbl", "edts", "dinf", "udta", "meta", "ilst"}
)

# Declared samples per real sample. 20/3 is the ratio the circulated tool uses:
# a 60fps clip ends up claiming ~400fps.
INFLATION_NUMERATOR = 20
INFLATION_DENOMINATOR = 3

# ISO 639-2/T "und", packed as three 5-bit values the way `mdhd` stores it.
UNDETERMINED_LANGUAGE = 0x55C4


class SpoofError(RuntimeError):
    pass


@dataclass
class Box:
    type: str
    start: int
    end: int
    header: int
    children: list[Box] | None = None

    @property
    def size(self) -> int:
        return self.end - self.start

    def child(self, box_type: str) -> Box | None:
        for child in self.children or ():
            if child.type == box_type:
                return child
        return None

    def path(self, *types: str) -> Box | None:
        box: Box | None = self
        for box_type in types:
            if box is None:
                return None
            box = box.child(box_type)
        return box


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from(">Q", data, offset)[0]


def _box(box_type: str, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type.encode("latin-1") + payload


def _size_at(data: bytes, offset: int, limit: int) -> int:
    if offset + 8 > limit:
        return 0
    size = _u32(data, offset)
    if size == 1:
        if offset + 16 > limit:
            return 0
        return _u64(data, offset + 8)
    if size == 0:
        return limit - offset
    return size


def parse_boxes(data: bytes, start: int, limit: int) -> list[Box]:
    boxes: list[Box] = []
    offset = start
    while offset + 8 <= limit:
        size = _size_at(data, offset, limit)
        if not size or offset + size > limit:
            break
        box_type = data[offset + 4 : offset + 8].decode("latin-1")
        header = 16 if _u32(data, offset) == 1 else 8
        box = Box(type=box_type, start=offset, end=offset + size, header=header)
        payload_start = offset + header + (4 if box_type == "meta" else 0)
        if box_type in CONTAINERS and payload_start < box.end:
            box.children = parse_boxes(data, payload_start, box.end)
        boxes.append(box)
        offset += size
    return boxes


def _raw(data: bytes, box: Box) -> bytes:
    return data[box.start : box.end]


def _payload(data: bytes, box: Box) -> bytes:
    return data[box.start + box.header : box.end]


def _is_video_trak(data: bytes, trak: Box) -> bool:
    hdlr = trak.path("mdia", "hdlr")
    return bool(hdlr and _payload(data, hdlr)[8:12] == b"vide")


def _sample_count(data: bytes, stsz: Box) -> int:
    return _u32(_payload(data, stsz), 8)


def _sample_sizes(data: bytes, stsz: Box) -> list[int]:
    payload = _payload(data, stsz)
    uniform_size = _u32(payload, 4)
    count = _u32(payload, 8)
    if uniform_size:
        return [uniform_size] * count
    sizes = list(struct.unpack_from(f">{count}I", payload, 12))
    if len(sizes) != count:
        raise SpoofError("stsz sample count does not match its table")
    return sizes


def _patch_stsz(data: bytes, stsz: Box, padding: int) -> bytes:
    if padding < 1:
        return _raw(data, stsz)
    sizes = _sample_sizes(data, stsz) + [FAKE_SAMPLE_SIZE] * padding
    payload = _payload(data, stsz)[:4] + struct.pack(">II", 0, len(sizes))
    payload += struct.pack(f">{len(sizes)}I", *sizes)
    return _box("stsz", payload)


def _patch_stsc(data: bytes, stsc: Box, chunk_count: int, padding: int) -> bytes:
    """Give every padding sample its own chunk, appended after the real ones."""
    if padding < 1:
        return _raw(data, stsc)
    payload = _payload(data, stsc)
    entry_count = _u32(payload, 4)
    entries = [struct.unpack_from(">III", payload, 8 + index * 12) for index in range(entry_count)]
    description = entries[-1][2] if entries else 1
    entries.append((chunk_count + 1, 1, description))
    rebuilt = payload[:4] + struct.pack(">I", len(entries))
    for entry in entries:
        rebuilt += struct.pack(">III", *entry)
    return _box("stsc", rebuilt)


def _patch_chunk_offsets(
    data: bytes, box: Box, shift: int, fake_offset: int, padding: int
) -> bytes:
    """Shift real chunk offsets by the moov size change, then point padding at the dummy."""
    wide = box.type == "co64"
    payload = _payload(data, box)
    count = _u32(payload, 4)
    item = ">Q" if wide else ">I"
    step = 8 if wide else 4
    offsets = [
        struct.unpack_from(item, payload, 8 + index * step)[0] + shift for index in range(count)
    ]
    offsets += [fake_offset] * padding
    rebuilt = payload[:4] + struct.pack(">I", len(offsets))
    for offset in offsets:
        rebuilt += struct.pack(item, offset)
    return _box(box.type, rebuilt)


def _patch_mdhd(data: bytes, mdhd: Box) -> bytes:
    payload = bytearray(_payload(data, mdhd))
    language_offset = 28 if payload[0] == 1 else 16
    if language_offset + 2 <= len(payload):
        struct.pack_into(">H", payload, language_offset, UNDETERMINED_LANGUAGE)
    return _box("mdhd", bytes(payload))


def _patch_hdlr(data: bytes, hdlr: Box) -> bytes:
    payload = _payload(data, hdlr)
    names = {b"vide": b"VideoHandler\0", b"soun": b"SoundHandler\0"}
    name = names.get(payload[8:12])
    if name is None:
        return _raw(data, hdlr)
    return _box("hdlr", payload[:24] + name)


def _remux(source: Path, destination: Path) -> None:
    """Normalise the container before patching, so only our edits stand out."""
    command = [
        require_binary("ffmpeg"),
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-brand",
        "isom",
        "-movflags",
        "+faststart",
        "-video_track_timescale",
        "90000",
        "-metadata:s:v:0",
        "handler_name=VideoHandler",
        "-metadata:s:a:0",
        "handler_name=SoundHandler",
        str(destination),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-10:])
        raise SpoofError(f"remux failed:\n{tail}")


def spoof_sample_count(source: Path, destination: Path) -> int:
    """Rewrite `source` into `destination`; returns the declared sample count."""
    remuxed = destination.with_name(f"{destination.stem}.tthq-remux{destination.suffix}")
    try:
        _remux(source, remuxed)
        data = remuxed.read_bytes()
        top = parse_boxes(data, 0, len(data))
        moov = next((box for box in top if box.type == "moov"), None)
        mdat = next((box for box in top if box.type == "mdat"), None)
        if moov is None or mdat is None:
            raise SpoofError("no moov/mdat box: not a plain MP4")
        if mdat.header != 8:
            raise SpoofError("64-bit mdat is not supported")

        traks = [box for box in (moov.children or []) if box.type == "trak"]
        video = next((trak for trak in traks if _is_video_trak(data, trak)), None)
        if video is None:
            raise SpoofError("no video track")
        stbl = video.path("mdia", "minf", "stbl")
        stsz = stbl.child("stsz") if stbl else None
        stsc = stbl.child("stsc") if stbl else None
        chunks = (stbl.child("stco") or stbl.child("co64")) if stbl else None
        if stsz is None or stsc is None or chunks is None:
            raise SpoofError("video sample table is incomplete")

        real = _sample_count(data, stsz)
        declared = real * INFLATION_NUMERATOR // INFLATION_DENOMINATOR
        padding = max(0, declared - real)
        chunk_count = _u32(_payload(data, chunks), 4)

        def rebuild(box: Box, shift: int, fake_offset: int, in_video: bool) -> bytes | None:
            if box.type == "udta":
                return None
            if box.type == "mdhd":
                return _patch_mdhd(data, box)
            if box.type == "hdlr":
                return _patch_hdlr(data, box)
            if in_video and box.type == "stsz":
                return _patch_stsz(data, box, padding)
            if in_video and box.type == "stsc":
                return _patch_stsc(data, box, chunk_count, padding)
            if box.type in {"stco", "co64"}:
                return _patch_chunk_offsets(
                    data, box, shift, fake_offset, padding if in_video else 0
                )
            if box.children is not None:
                parts: list[bytes] = []
                if box.type == "meta":
                    parts.append(_payload(data, box)[:4])
                for child in box.children:
                    child_in_video = video is child if child.type == "trak" else in_video
                    rebuilt = rebuild(child, shift, fake_offset, child_in_video)
                    if rebuilt is not None:
                        parts.append(rebuilt)
                return _box(box.type, b"".join(parts))
            return _raw(data, box)

        # The rebuilt moov changes size, which moves mdat and therefore every
        # chunk offset inside moov. Iterate until the shift stops changing.
        shift = 0
        new_moov = b""
        for _ in range(4):
            new_moov = rebuild(moov, shift, mdat.end + shift, False) or b""
            settled = new_moov and len(new_moov) - moov.size == shift
            shift = len(new_moov) - moov.size
            if settled:
                break
        else:
            raise SpoofError("chunk offsets did not converge")

        media = data[mdat.start + 8 : mdat.end]
        if padding:
            new_mdat = (
                struct.pack(">I", 8 + len(media) + FAKE_SAMPLE_SIZE) + b"mdat" + media + FAKE_SAMPLE
            )
        else:
            new_mdat = _raw(data, mdat)

        parts = []
        for box in top:
            if box.type == "ftyp":
                parts.append(_raw(data, box))
                parts.append(_box("free", b""))
            elif box.type == "moov":
                parts.append(new_moov)
            elif box.type == "mdat":
                parts.append(new_mdat)
            elif box.type not in {"free", "wide"}:
                parts.append(_raw(data, box))

        destination.write_bytes(b"".join(parts))
        return declared
    finally:
        remuxed.unlink(missing_ok=True)
