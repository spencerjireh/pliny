from datetime import UTC, datetime

from pliny.snapshot.base import SnapshotResult


class PlaywrightSnapshotter:
    """Plays back JS-heavy pages via headless Chromium and captures
    rendered HTML + screenshot + page title.

    Browser launches per call (acceptable at v1's snapshot cadence and the
    slow pool's default 2-worker concurrency). A future optimization is to
    keep one persistent context per worker.
    """

    def __init__(self, *, user_agent: str = "pliny/0.1 (+https://example.invalid/pliny)") -> None:
        self._user_agent = user_agent

    async def capture_html(self, url: str, *, timeout_s: float = 30.0) -> SnapshotResult:
        from playwright.async_api import async_playwright

        timeout_ms = int(timeout_s * 1000)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=self._user_agent)
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                rendered = await page.content()
                screenshot = await page.screenshot(full_page=True, type="png")
                title = await page.title()
                final_url = page.url
            finally:
                await browser.close()
        return SnapshotResult(
            rendered_html=rendered.encode("utf-8"),
            screenshot_png=screenshot,
            final_url=final_url,
            page_title=title or None,
            fetched_at=datetime.now(tz=UTC),
        )
