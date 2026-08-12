"""Command line entrypoint."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import api
from .caption import build_caption
from .encode import (
    ORIENTATIONS,
    QUALITY_BITRATE_MBPS,
    RESOLUTIONS,
    EncodeSettings,
    describe,
    encode,
)
from .probe import probe, warnings_for
from .uploader import VISIBILITY_LABELS, UploadError, upload


def _encode_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--resolution",
        type=int,
        default=1080,
        choices=sorted(RESOLUTIONS),
        help="output short edge (default: 1080, i.e. 1080x1920 or 1920x1080)",
    )
    parser.add_argument(
        "--orientation",
        default="vertical",
        choices=ORIENTATIONS,
        help="vertical 9:16, or landscape 16:9 like a YouTube video (default: vertical)",
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
        orientation=args.orientation,
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
        "--visibility",
        choices=sorted(VISIBILITY_LABELS),
        default=None,
        help="who can see the post (default: leave TikTok's own default)",
    )
    upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="stage the post and fill the caption, but do not click Post",
    )
    upload_parser.add_argument("--headful", action="store_true", help="show the browser window")
    upload_parser.add_argument("--timeout", type=int, default=300, help="seconds (default: 300)")
    _encode_arguments(upload_parser)

    login_parser = subparsers.add_parser(
        "api-login", help="authorize a TikTok developer app for API posting"
    )
    login_parser.add_argument("--client-key", default=os.environ.get("TIKTOK_CLIENT_KEY", ""))
    login_parser.add_argument("--client-secret", default=os.environ.get("TIKTOK_CLIENT_SECRET", ""))
    login_parser.add_argument(
        "--redirect-uri",
        default=api.DEFAULT_REDIRECT_URI,
        help=f"must match the app's registered URI (default: {api.DEFAULT_REDIRECT_URI})",
    )
    login_parser.add_argument("--token-file", type=Path, default=api.DEFAULT_TOKEN_PATH)
    login_parser.add_argument(
        "--no-browser", action="store_true", help="only print the URL, do not open a browser"
    )
    login_parser.add_argument(
        "--web-app",
        action="store_true",
        help="the app is registered as Web, not Desktop, so skip PKCE",
    )
    login_parser.add_argument(
        "--manual",
        action="store_true",
        help="paste the redirected URL by hand, for apps whose redirect URI is not localhost",
    )

    api_parser = subparsers.add_parser(
        "api-upload", help="upload through TikTok's official Content Posting API"
    )
    api_parser.add_argument("video", type=Path)
    api_parser.add_argument("--title", default="", help="caption text")
    api_parser.add_argument("--hashtags", default="", help="comma or space separated hashtags")
    api_parser.add_argument("--skip-encode", action="store_true", help="upload the file as-is")
    api_parser.add_argument(
        "--visibility",
        choices=sorted(api.PRIVACY_LEVELS),
        default="private",
        help="privacy level (default: private; unaudited apps can only post private)",
    )
    api_parser.add_argument(
        "--inbox",
        action="store_true",
        help="send as a draft to the TikTok inbox instead of posting directly",
    )
    api_parser.add_argument("--token-file", type=Path, default=api.DEFAULT_TOKEN_PATH)
    api_parser.add_argument("--client-key", default=os.environ.get("TIKTOK_CLIENT_KEY", ""))
    api_parser.add_argument("--client-secret", default=os.environ.get("TIKTOK_CLIENT_SECRET", ""))
    api_parser.add_argument("--timeout", type=int, default=600, help="seconds (default: 600)")
    _encode_arguments(api_parser)

    info_parser = subparsers.add_parser(
        "api-info", help="show what the API allows for the authorized account"
    )
    info_parser.add_argument("--token-file", type=Path, default=api.DEFAULT_TOKEN_PATH)
    info_parser.add_argument("--client-key", default=os.environ.get("TIKTOK_CLIENT_KEY", ""))
    info_parser.add_argument("--client-secret", default=os.environ.get("TIKTOK_CLIENT_SECRET", ""))

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
        for note in warnings_for(info, settings.orientation):
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
            for note in warnings_for(info, settings.orientation):
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
                visibility=args.visibility,
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
        if result.visibility:
            print(f"  visibility: {result.visibility}")
        if result.high_quality is not None:
            state = "on" if result.high_quality else "off"
            print(f"  TikTok's high-quality uploads switch: {state}")
        for shot in result.screenshots:
            print(f"  screenshot: {shot}")
        return 0

    if args.command == "api-login":
        if not (args.client_key and args.client_secret):
            print(
                "Pass --client-key/--client-secret, or set TIKTOK_CLIENT_KEY and "
                "TIKTOK_CLIENT_SECRET. Both come from your app at "
                "https://developers.tiktok.com/apps",
                file=sys.stderr,
            )
            return 2
        try:
            token = api.authorize(
                args.client_key,
                args.client_secret,
                redirect_uri=args.redirect_uri,
                token_path=args.token_file,
                open_browser=not args.no_browser,
                manual=args.manual,
                desktop=not args.web_app,
            )
        except api.ApiError as exc:
            print(f"login failed: {exc}", file=sys.stderr)
            return 1
        print(f"authorized; token saved to {args.token_file}")
        print(f"  scopes: {token.scope}")
        return 0

    if args.command == "api-info":
        try:
            token = api.load_token(
                token_path=args.token_file,
                client_key=args.client_key,
                client_secret=args.client_secret,
            )
            creator = api.creator_info(token)
        except api.ApiError as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1
        print(f"account: {creator.get('creator_nickname')} (@{creator.get('creator_username')})")
        print(f"  privacy levels: {creator.get('privacy_level_options')}")
        print(f"  max duration: {creator.get('max_video_post_duration_sec')}s")
        for key in ("comment_disabled", "duet_disabled", "stitch_disabled"):
            print(f"  {key}: {creator.get(key)}")
        return 0

    if args.command == "api-upload":
        caption = build_caption(args.title, args.hashtags)
        source = args.video
        if not args.skip_encode:
            settings = _settings_from(args)
            info_probe = probe(args.video)
            for note in warnings_for(info_probe, settings.orientation):
                print(f"warning: {note}", file=sys.stderr)
            print(describe(info_probe, settings))
            source = encode(
                args.video, args.video.with_name(f"{args.video.stem}-tthq.mp4"), settings
            )
            print(f"encoded {source}")

        try:
            token = api.load_token(
                token_path=args.token_file,
                client_key=args.client_key,
                client_secret=args.client_secret,
            )
            api_result = api.upload(
                source,
                token,
                caption=caption,
                visibility=args.visibility,
                inbox=args.inbox,
                timeout_seconds=args.timeout,
            )
        except api.ApiError as exc:
            print(f"api upload failed: {exc}", file=sys.stderr)
            return 1

        print(f"{api_result.status} in {api_result.elapsed_seconds:.0f}s")
        print(f"  publish id: {api_result.publish_id}")
        if api_result.privacy_level:
            print(f"  privacy: {api_result.privacy_level}")
        if api_result.inbox:
            print("  sent to the TikTok inbox: open the app notification to finish the post")
        if api_result.video_id:
            print(f"  video id: {api_result.video_id}")
        return 0

    if args.command == "serve":
        from .server import serve

        print(f"tthq UI on http://{args.host}:{args.port}")
        serve(host=args.host, port=args.port, work_dir=args.work_dir, cookies_file=args.cookies)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
