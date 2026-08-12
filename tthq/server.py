"""Local web UI for encoding and uploading clips.

Binds to 127.0.0.1 by default: the cookies it reads are a full TikTok session,
so the server must not be exposed to the network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import api
from .caption import CaptionError, build_caption
from .encode import ORIENTATIONS, QUALITY_BITRATE_MBPS, RESOLUTIONS, EncodeSettings
from .jobs import JobRegistry
from .probe import ProbeError, probe, warnings_for
from .uploader import VISIBILITY_LABELS

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_WORK_DIR = Path.home() / ".cache" / "tthq"


def create_app(
    work_dir: Path | None = None,
    cookies_file: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="tthq", docs_url=None, redoc_url=None)
    registry = JobRegistry(work_dir or DEFAULT_WORK_DIR)
    app.state.registry = registry
    app.state.cookies_file = cookies_file

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/app.js")
    def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return {
            "resolutions": sorted(RESOLUTIONS),
            "qualities": sorted(QUALITY_BITRATE_MBPS, key=lambda tier: QUALITY_BITRATE_MBPS[tier]),
            "orientations": list(ORIENTATIONS),
            "visibilities": list(VISIBILITY_LABELS),
            "backends": ["browser", "api"],
            "api_token_present": api.DEFAULT_TOKEN_PATH.is_file(),
            "cookies_file": str(app.state.cookies_file) if app.state.cookies_file else None,
            "cookies_present": bool(
                app.state.cookies_file and Path(app.state.cookies_file).is_file()
            ),
        }

    @app.post("/api/jobs")
    async def create_job(
        video: Annotated[UploadFile, Form()],
        title: Annotated[str, Form()] = "",
        hashtags: Annotated[str, Form()] = "",
        resolution: Annotated[int, Form()] = 1080,
        quality: Annotated[str, Form()] = "max",
        fps: Annotated[str, Form()] = "",
        fit: Annotated[str, Form()] = "pad",
        orientation: Annotated[str, Form()] = "vertical",
        denoise: Annotated[bool, Form()] = False,
        sharpen: Annotated[bool, Form()] = False,
        two_pass: Annotated[bool, Form()] = False,
        spoof_fps: Annotated[bool, Form()] = False,
        spoof_camera: Annotated[bool, Form()] = False,
        skip_encode: Annotated[bool, Form()] = False,
        upload_after_encode: Annotated[bool, Form()] = False,
        visibility: Annotated[str, Form()] = "",
        backend: Annotated[str, Form()] = "browser",
        dry_run: Annotated[bool, Form()] = True,
    ) -> dict[str, Any]:
        try:
            caption = build_caption(title, hashtags)
        except CaptionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        settings = EncodeSettings(
            resolution=resolution,
            quality=quality,
            fps=int(fps) if fps.strip() else None,
            fit=fit,
            orientation=orientation,
            denoise=denoise,
            sharpen=sharpen,
            two_pass=two_pass,
            spoof_fps=spoof_fps,
            spoof_camera=spoof_camera,
        )
        try:
            settings.validate()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        uploads = registry.work_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        stored = uploads / (Path(video.filename or "clip.mp4").name)
        with stored.open("wb") as handle:
            shutil.copyfileobj(video.file, handle)

        if visibility and visibility not in VISIBILITY_LABELS:
            raise HTTPException(status_code=422, detail=f"Unknown visibility {visibility!r}.")

        if backend not in ("browser", "api"):
            raise HTTPException(status_code=422, detail=f"Unknown backend {backend!r}.")

        cookies = Path(app.state.cookies_file) if app.state.cookies_file else None
        if upload_after_encode and backend == "browser" and cookies is None:
            raise HTTPException(
                status_code=422,
                detail="No cookie file configured. Restart with --cookies path/to/cookies.txt.",
            )
        if upload_after_encode and backend == "api" and not api.DEFAULT_TOKEN_PATH.is_file():
            raise HTTPException(
                status_code=422,
                detail="No API token yet. Run 'tthq api-login' first.",
            )

        job = registry.new_job(
            video=stored,
            settings=settings,
            title=title,
            hashtags=hashtags,
            dry_run=dry_run,
            visibility=visibility or None,
            backend=backend if upload_after_encode else "browser",
            cookies_file=cookies if upload_after_encode and backend == "browser" else None,
            skip_encode=skip_encode,
        )
        registry.submit(job)
        return {"id": job.id, "caption": caption}

    @app.get("/api/probe")
    def probe_endpoint(path: str) -> dict[str, Any]:
        try:
            info = probe(Path(path))
        except ProbeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "width": info.width,
            "height": info.height,
            "fps": round(info.fps, 2),
            "duration": round(info.duration, 2),
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
            "bit_rate": info.bit_rate,
            "warnings": warnings_for(info),
        }

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str) -> StreamingResponse:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No such job: {job_id}")

        def event_stream():
            for event in job.stream():
                yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'kind': 'closed', 'status': job.status})}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str) -> dict[str, Any]:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
        return {
            "id": job.id,
            "status": job.status,
            "error": job.error,
            "output": str(job.output) if job.output else None,
            "screenshots": [str(p) for p in job.screenshots],
        }

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str) -> FileResponse:
        job = registry.get(job_id)
        if job is None or job.output is None or not job.output.is_file():
            raise HTTPException(status_code=404, detail="No encoded output for this job yet.")
        return FileResponse(job.output, media_type="video/mp4", filename=job.output.name)

    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8420,
    work_dir: Path | None = None,
    cookies_file: Path | None = None,
) -> None:
    import uvicorn

    uvicorn.run(
        create_app(work_dir=work_dir, cookies_file=cookies_file),
        host=host,
        port=port,
        log_level="info",
    )
