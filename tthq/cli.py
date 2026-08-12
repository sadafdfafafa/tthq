"""Command line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .caption import build_caption
from .encode import QUALITY_BITRATE_MBPS, RESOLUTIONS, EncodeSettings, describe, encode
from .probe import probe, warnings_for
from .uploader import UploadError, upload


def _encode_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--resolution",
        type=int,
        default=1080,
        choices=sorted(RESOLUTIONS),
        help="vertical output resolution (default: 1080, i.e. 1080x1920)",
    )
    parser.add_argument(
        "--quality",
        default="max",
        choices=sorted(QUALITY_BITRATE_MBPS, key=lambda key: QUALITY_BITRATE_MBPS[key]),
        help="target bitrate tier (default: max)",
    )
    parser.add_argument(
        "--fps", type=int, default=None, help="output frame rate (default: match source)"
    )
    parser.add_argument(
        "--fit",
        default="pad",
        choices=("pad", "crop", "stretch"),
        help="how to reach 9:16 (default: pad)",
    )
    parser.add_argument("--denoise", action="store_true", help="light denoise before encoding")
    parser.add_argument("--two-pass", action="store_true", help="two-pass x264 encode")


def _settings_from(args: argparse.Namespace) -> EncodeSettings:
    return EncodeSettings(
        resolution=args.resolution,
        quality=args.quality,
        fps=args.fps,
        fit=args.fit,
        denoise=args.denoise,
        two_pass=args.two_pass,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tthq",
        description="Encode clips for TikTok at the highest quality it will accept, "
        "and optionally upload them with an exported cookie file.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe_parser = subparsers.add_parser("probe", help="inspect a clip and report quality risks")
    probe_parser.add_argument("video", type=Path)

    encode_parser = subparsers.add_parser("encode", help="re-encode a clip for upload")
    encode_parser.add_argument("video", type=Path)
    encode_parser.add_argument("-o", "--output", type=Path, default=None)
    _encode_arguments(encode_parser)

    upload_parser = subparsers.add_parser("upload", help="encode (optional) and upload a clip")
    upload_parser.add_argument("video", type=Path)
    upload_parser.add_argument(
        "--cookies", type=Path, required=True, help="cookies.txt or cookies.json export"
    )
    upload_parser.add_argument("--title", default="", help="caption text")
    upload_parser.add_argument("--hashtags", default="", help="comma or space separated hashtags")
    upload_parser.add_argument("--skip-encode", action="store_true", help="upload the file as-is")
    upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="stage the post and fill the caption, but do not click Post",
    )
    upload_parser.add_argument("--headful", action="store_true", help="show the browser window")
    upload_parser.add_argument("--timeout", type=int, default=300, help="seconds (default: 300)")
    _encode_arguments(upload_parser)

    serve_parser = subparsers.add_parser("serve", help="run the local web UI")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8420)
    serve_parser.add_argument(
        "--cookies", type=Path, default=None, help="cookies.txt or cookies.json export"
    )
    serve_parser.add_argument("--work-dir", type=Path, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "probe":
        info = probe(args.video)
        print(
            f"{info.width}x{info.height} @ {info.fps:.2f}fps, {info.duration:.1f}s, "
            f"{info.video_codec}/{info.audio_codec or 'no audio'}, "
            f"{(info.bit_rate or 0) / 1_000_000:.1f} Mbps, {info.pix_fmt}"
        )
        notes = warnings_for(info)
        if not notes:
            print("No quality risks detected.")
        for note in notes:
            print(f"  warning: {note}")
        return 0

    if args.command == "encode":
        settings = _settings_from(args)
        output = args.output or args.video.with_name(f"{args.video.stem}-tthq.mp4")
        info = probe(args.video)
        for note in warnings_for(info):
            print(f"warning: {note}", file=sys.stderr)
        print(describe(info, settings))
        encoded_path = encode(args.video, output, settings)
        final = probe(encoded_path)
        print(
            f"wrote {encoded_path} ({encoded_path.stat().st_size / 1_048_576:.1f} MiB, "
            f"{(final.bit_rate or 0) / 1_000_000:.1f} Mbps)"
        )
        return 0

    if args.command == "upload":
        caption = build_caption(args.title, args.hashtags)
        source = args.video
        if not args.skip_encode:
            settings = _settings_from(args)
            info = probe(args.video)
            for note in warnings_for(info):
                print(f"warning: {note}", file=sys.stderr)
            print(describe(info, settings))
            source = encode(
                args.video, args.video.with_name(f"{args.video.stem}-tthq.mp4"), settings
            )
            print(f"encoded {source}")

        try:
            result = upload(
                source,
                args.cookies,
                caption=caption,
                headless=not args.headful,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout,
            )
        except UploadError as exc:
            print(f"upload failed: {exc}", file=sys.stderr)
            for shot in exc.screenshots:
                print(f"  screenshot: {shot}", file=sys.stderr)
            return 1

        print(result.note or f"posted in {result.elapsed_seconds:.0f}s")
        if result.high_quality is not None:
            state = "on" if result.high_quality else "off"
            print(f"  TikTok's high-quality uploads switch: {state}")
        for shot in result.screenshots:
            print(f"  screenshot: {shot}")
        return 0

    if args.command == "serve":
        from .server import serve

        print(f"tthq UI on http://{args.host}:{args.port}")
        serve(host=args.host, port=args.port, work_dir=args.work_dir, cookies_file=args.cookies)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
