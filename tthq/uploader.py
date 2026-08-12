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

# TikTok interrupts the upload flow with modals ("Turn on automatic content
# checks?", promos, tips) whose overlay swallows clicks on the form beneath.
MODAL_OVERLAY_SELECTOR = ".TUXModal-overlay"
MODAL_DISMISS_SELECTORS = (
    ".common-modal-close",
    "[role=dialog] button:has-text('Cancel')",
    "[role=dialog] button:has-text('Not now')",
    "[role=dialog] button:has-text('Got it')",
    "[role=dialog] button:has-text('Skip')",
)
# Feature-announcement tooltips are not modals and have no overlay, but they do
# sit on top of the form.
TOOLTIP_DISMISS_SELECTOR = "button:has-text('Got it')"

# TikTok forces HD on for Web Studio uploads and renders the switch disabled, so
# this is read for confirmation rather than toggled.
HQ_SWITCH_SELECTOR = ".headline-wrapper:has-text('High-quality uploads') [role=switch]"

VISIBILITY_TRIGGER_SELECTOR = "[data-e2e='video_visibility_container'] [role=combobox]"
# Menu labels per visibility choice, in preference order: a private account is
# offered "Followers" where a public one is offered "Everyone".
VISIBILITY_LABELS: dict[str, tuple[str, ...]] = {
    "public": ("Everyone", "Followers"),
    "friends": ("Friends",),
    "private": ("Only you",),
}

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
    high_quality: bool | None = None
    visibility: str = ""


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


def _set_visibility(page, visibility: str, timeout_seconds: int) -> str:
    """Pick an option in TikTok's "Who can see this post" menu. Returns its label."""
    labels = VISIBILITY_LABELS[visibility]
    trigger = page.locator(VISIBILITY_TRIGGER_SELECTOR).first
    if not trigger.count():
        raise UploadError(
            "The 'Who can see this post' control was not found, so visibility could "
            "not be set. TikTok Studio's layout has probably changed."
        )

    _click_past_modals(page, trigger, timeout_seconds)
    options = page.locator("[role=option]")
    options.first.wait_for(state="visible", timeout=timeout_seconds * 1000)

    for label in labels:
        option = options.filter(has_text=label).first
        if _is_visible(option):
            option.click()
            page.wait_for_timeout(500)
            return label

    available = [options.nth(i).inner_text().splitlines()[0] for i in range(options.count())]
    raise UploadError(
        f"TikTok does not offer {visibility!r} for this account; it lists {available}. "
        f"A private account has no 'Everyone' option, for example."
    )


def _high_quality_enabled(page) -> bool | None:
    """Whether TikTok's "High-quality uploads" switch is on. None if not found."""
    switch = page.locator(HQ_SWITCH_SELECTOR).first
    try:
        return switch.is_checked() if switch.count() else None
    except Exception:
        return None


def _is_visible(locator) -> bool:
    try:
        return bool(locator.count()) and locator.is_visible()
    except Exception:  # the node can detach while React re-renders
        return False


def _dismiss_modals(page, attempts: int = 5) -> list[str]:
    """Close modals and tooltips covering the form. Returns what was dismissed."""
    dismissed: list[str] = []
    for _ in range(attempts):
        overlay = page.locator(MODAL_OVERLAY_SELECTOR).first
        tooltip = page.locator(TOOLTIP_DISMISS_SELECTOR).first

        if not _is_visible(overlay):
            if not _is_visible(tooltip):
                return dismissed
            try:
                tooltip.click(timeout=5000)
                dismissed.append("tooltip")
            except Exception:
                return dismissed
            page.wait_for_timeout(500)
            continue

        try:
            label = page.locator("[role=dialog]").first.inner_text().splitlines()[0]
        except Exception:
            label = "unknown modal"

        for selector in MODAL_DISMISS_SELECTORS:
            target = page.locator(selector).first
            if not _is_visible(target):
                continue
            try:
                target.click(timeout=5000)
                break
            except Exception:
                continue
        else:
            page.keyboard.press("Escape")

        page.wait_for_timeout(1000)
        dismissed.append(label)
    return dismissed


def _click_past_modals(page, locator, timeout_seconds: int) -> None:
    """Click an element, clearing modals that appear on top of it as we go.

    Dismissing once up front is not enough: the modals are triggered by upload
    progress, so a new one can appear between the dismissal and the click.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        _dismiss_modals(page)
        try:
            locator.click(timeout=5000)
            return
        except Exception as exc:
            last_error = exc
            page.wait_for_timeout(500)
    raise UploadError(
        f"Could not click the element within {timeout_seconds}s; a modal kept "
        f"intercepting the click. Last error: {last_error}"
    )


def _type_caption(page, caption_box, caption: str, timeout_seconds: int) -> None:
    """Type into TikTok's Draft.js caption editor.

    fill() does not work (the editor ignores direct value assignment), and typing a
    hashtag opens a suggestion popup that hijacks the next space or Enter. So each
    word is typed separately and the popup is dismissed after every hashtag.

    The editor is pre-filled with the file name, so it is cleared first.
    """
    _click_past_modals(page, caption_box, timeout_seconds)
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
    visibility: str | None = None,
    headless: bool = True,
    dry_run: bool = False,
    timeout_seconds: int = 300,
    artifacts_dir: Path | None = None,
) -> UploadResult:
    if not video.is_file():
        raise UploadError(f"No such video: {video}")
    if visibility is not None and visibility not in VISIBILITY_LABELS:
        raise UploadError(
            f"Unknown visibility {visibility!r}; choose from {list(VISIBILITY_LABELS)}."
        )

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

            _dismiss_modals(page)

            page.wait_for_selector(FILE_INPUT_SELECTOR, state="attached", timeout=timeout_ms)
            page.locator(FILE_INPUT_SELECTOR).first.set_input_files(str(video))

            caption_box = _first_visible(page, CAPTION_SELECTORS, timeout_ms)
            if caption:
                _type_caption(page, caption_box, caption, timeout_seconds)

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

            chosen_visibility = (
                _set_visibility(page, visibility, timeout_seconds) if visibility else ""
            )
            high_quality = _high_quality_enabled(page)

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
                    high_quality=high_quality,
                    visibility=chosen_visibility,
                )

            _click_past_modals(page, post_button, timeout_seconds)
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
                high_quality=high_quality,
                visibility=chosen_visibility,
            )
        except Exception as exc:
            failure = artifacts / f"failure-{int(time.time())}.png"
            try:
                page.screenshot(path=str(failure), full_page=True)
                screenshots.append(failure)
            except Exception:
                pass
            # Re-raise carrying the diagnostics, which the caller surfaces to the user.
            message = str(exc) if isinstance(exc, UploadError) else f"{type(exc).__name__}: {exc}"
            raise UploadError(message, screenshots) from exc
        finally:
            context.close()
            browser.close()
