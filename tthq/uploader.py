"""Cookie-authenticated uploader driving the TikTok Studio web uploader.

Why a browser instead of plain HTTP: TikTok's upload endpoints require request
signatures (X-Bogus / _signature / msToken) produced by obfuscated in-page
JavaScript. Cookies alone are not enough for a bare HTTP client, but they are
enough to restore a logged-in session inside a real browser, which then signs
its own requests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .cookies import storage_state

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

FILE_INPUT_SELECTOR = "input[type=file]"

CAPTION_SELECTORS = (
    "div[contenteditable='true'][role='combobox']",
    "div.public-DraftEditor-content",
    "div[contenteditable='true']",
)

POST_BUTTON_SELECTORS = (
    "button[data-e2e='post_video_button']",
    "button:has-text('Post')",
    "div[role='button']:has-text('Post')",
)


class UploadError(RuntimeError):
    def __init__(self, message: str, screenshots: list[Path] | None = None) -> None:
        super().__init__(message)
        self.screenshots = screenshots or []


@dataclass
class UploadResult:
    posted: bool
    caption: str
    video: Path
    elapsed_seconds: float
    screenshots: list[Path] = field(default_factory=list)
    note: str = ""


def _import_playwright():  # pragma: no cover - thin import shim
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: PERF203
        raise UploadError(
            "Playwright is not installed. Run 'pip install playwright' followed by "
            "'playwright install chromium'."
        ) from exc
    return sync_playwright


def _first_visible(page, selectors: tuple[str, ...], timeout_ms: int):
    """Return the first selector that resolves; the uploader's DOM shifts often."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception as exc:  # selector may be transiently detached
                last_error = exc
        page.wait_for_timeout(500)
    raise UploadError(
        f"None of these selectors appeared within {timeout_ms / 1000:.0f}s: "
        f"{list(selectors)}. TikTok Studio's layout has probably changed."
        + (f" Last error: {last_error}" if last_error else "")
    )


def _type_caption(page, caption_box, caption: str) -> None:
    """Type into TikTok's Draft.js caption editor.

    fill() does not work (the editor ignores direct value assignment), and typing a
    hashtag opens a suggestion popup that hijacks the next space or Enter. So each
    word is typed separately and the popup is dismissed after every hashtag.
    """
    caption_box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")

    words = caption.split(" ")
    for index, word in enumerate(words):
        if index:
            page.keyboard.type(" ")
        page.keyboard.type(word, delay=15)
        if word.startswith(("#", "@")) and len(word) > 1:
            # Let the suggestion list render, then dismiss it so the following
            # space is not swallowed as an "accept suggestion" keypress.
            page.wait_for_timeout(600)
            page.keyboard.press("Escape")


def upload(
    video: Path,
    cookies_file: Path,
    *,
    caption: str = "",
    headless: bool = True,
    dry_run: bool = False,
    timeout_seconds: int = 300,
    artifacts_dir: Path | None = None,
) -> UploadResult:
    if not video.is_file():
        raise UploadError(f"No such video: {video}")

    state = storage_state(cookies_file)
    artifacts = artifacts_dir or Path("artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    screenshots: list[Path] = []
    timeout_ms = timeout_seconds * 1000
    started = time.monotonic()

    sync_playwright = _import_playwright()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=state,
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")

            if "/login" in page.url:
                raise UploadError(
                    "TikTok redirected to the login page, so the exported cookies are "
                    "expired or incomplete. Re-export cookies.txt from a signed-in tab."
                )

            page.wait_for_selector(FILE_INPUT_SELECTOR, state="attached", timeout=timeout_ms)
            page.locator(FILE_INPUT_SELECTOR).first.set_input_files(str(video))

            caption_box = _first_visible(page, CAPTION_SELECTORS, timeout_ms)
            if caption:
                _type_caption(page, caption_box, caption)

            # Wait for the client-side upload to finish before enabling Post.
            post_button = _first_visible(page, POST_BUTTON_SELECTORS, timeout_ms)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if post_button.is_enabled():
                    break
                page.wait_for_timeout(1000)
            else:
                raise UploadError(
                    f"Post button never became enabled within {timeout_seconds}s; the "
                    f"video upload or processing did not complete."
                )

            shot = artifacts / f"before-post-{int(time.time())}.png"
            page.screenshot(path=str(shot), full_page=True)
            screenshots.append(shot)

            if dry_run:
                return UploadResult(
                    posted=False,
                    caption=caption,
                    video=video,
                    elapsed_seconds=time.monotonic() - started,
                    screenshots=screenshots,
                    note="dry run: video staged and caption filled, Post not clicked",
                )

            post_button.click()
            # The success state is a redirect away from the upload form or a toast.
            try:
                page.wait_for_url(lambda url: "upload" not in url, timeout=120_000)
            except Exception:
                page.wait_for_timeout(5000)

            shot = artifacts / f"after-post-{int(time.time())}.png"
            page.screenshot(path=str(shot), full_page=True)
            screenshots.append(shot)

            return UploadResult(
                posted=True,
                caption=caption,
                video=video,
                elapsed_seconds=time.monotonic() - started,
                screenshots=screenshots,
            )
        except UploadError as exc:
            failure = artifacts / f"failure-{int(time.time())}.png"
            try:
                page.screenshot(path=str(failure), full_page=True)
                screenshots.append(failure)
            except Exception:
                pass
            # Re-raise carrying the diagnostics, which the caller surfaces to the user.
            raise UploadError(str(exc), screenshots) from exc
        finally:
            context.close()
            browser.close()
