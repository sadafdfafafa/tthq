"""In-process job registry so the web UI can stream encode/upload progress."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .caption import build_caption
from .encode import EncodeSettings, describe, encode
from .probe import probe, warnings_for
from .uploader import UploadError, upload

JobStatus = Literal["queued", "encoding", "uploading", "done", "failed"]
_SENTINEL = object()


@dataclass
class Job:
    id: str
    video: Path
    settings: EncodeSettings
    title: str = ""
    hashtags: str = ""
    dry_run: bool = False
    visibility: str | None = None
    cookies_file: Path | None = None
    skip_encode: bool = False
    status: JobStatus = "queued"
    events: queue.Queue = field(default_factory=queue.Queue)
    output: Path | None = None
    error: str = ""
    screenshots: list[Path] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def emit(self, kind: str, message: str, **extra: Any) -> None:
        self.events.put({"kind": kind, "message": message, "status": self.status, **extra})

    def finish(self) -> None:
        self.events.put(_SENTINEL)

    def stream(self, timeout: float = 0.5):
        """Yield event dicts until the job finishes."""
        while True:
            try:
                item = self.events.get(timeout=timeout)
            except queue.Empty:
                if self.status in ("done", "failed"):
                    return
                yield {"kind": "heartbeat", "message": "", "status": self.status}
                continue
            if item is _SENTINEL:
                return
            yield item


class JobRegistry:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def submit(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def new_job(self, **kwargs: Any) -> Job:
        return Job(id=uuid.uuid4().hex[:12], **kwargs)

    def _run(self, job: Job) -> None:
        try:
            caption = build_caption(job.title, job.hashtags)

            info = probe(job.video)
            for warning in warnings_for(info):
                job.emit("warning", warning)

            source = job.video
            if job.skip_encode:
                job.emit("info", "Skipping re-encode, uploading the file as provided.")
            else:
                job.status = "encoding"
                job.emit("stage", describe(info, job.settings))
                destination = self.work_dir / f"{job.id}-optimized.mp4"
                source = encode(job.video, destination, job.settings)
                job.output = source
                encoded = probe(source)
                job.emit(
                    "encoded",
                    f"Encoded to {encoded.width}x{encoded.height}@{encoded.fps:.0f}, "
                    f"{source.stat().st_size / 1_048_576:.1f} MiB",
                    output=str(source),
                )

            if job.cookies_file is None:
                job.status = "done"
                job.emit("done", "Encode complete. No cookies file given, so nothing was uploaded.")
                return

            job.status = "uploading"
            job.emit("stage", f"Uploading to TikTok with caption: {caption!r}")
            result = upload(
                source,
                job.cookies_file,
                caption=caption,
                visibility=job.visibility,
                dry_run=job.dry_run,
                artifacts_dir=self.work_dir / "artifacts",
            )
            job.screenshots = result.screenshots
            if result.visibility:
                job.emit("info", f"Visibility set to {result.visibility!r}")
            if result.high_quality is not None:
                job.emit(
                    "info",
                    "TikTok's high-quality uploads switch is "
                    + ("on" if result.high_quality else "off"),
                )
            job.status = "done"
            job.emit(
                "done",
                result.note or f"Posted in {result.elapsed_seconds:.0f}s.",
                posted=result.posted,
                screenshots=[str(p) for p in result.screenshots],
            )
        except UploadError as exc:
            job.status = "failed"
            job.error = str(exc)
            job.screenshots = exc.screenshots
            job.emit("error", str(exc), screenshots=[str(p) for p in exc.screenshots])
        except Exception as exc:  # surfaced to the UI rather than lost in a thread
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.emit("error", job.error)
        finally:
            job.finish()
