from __future__ import annotations

import abc
from typing import Callable, Optional

from playwright.async_api import BrowserContext, Page

from backend.auth.session_manager import (
    ensure_logged_in,
    get_portal_context,
    human_delay,
    is_portal_authenticated,
    save_cookies,
)
from backend.logger import log_event, logger
from backend.models import JobListing, JobPortal, WorkMode


class BaseCrawler(abc.ABC):
    """Abstract base for all job-portal crawlers."""

    portal: JobPortal

    def __init__(
        self,
        search_query: str,
        work_modes: list[WorkMode],
        location: str = "",
        max_pages: int = 3,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.search_query = search_query
        self.work_modes = work_modes
        self.location = location
        self.max_pages = max_pages
        self.on_status = on_status or (lambda _: None)

        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def _emit(self, msg: str) -> None:
        self.on_status(msg)
        logger.info("[%s] %s", self.portal.value, msg)

    async def setup(self) -> bool:
        self._context = await get_portal_context(self.portal)

        if is_portal_authenticated(self.portal):
            self._page = await self._context.new_page()
            await self._emit("Using existing login session")
            return True

        self._page = await self._context.new_page()
        await self._emit("Logging in…")
        logged_in = await ensure_logged_in(self._page, self.portal)
        if not logged_in:
            await self._emit("Login failed — skipping portal")
            log_event("login_failed", portal=self.portal.value)
            return False

        await save_cookies(self._context, self.portal)
        await self._emit("Login successful")
        log_event("login_success", portal=self.portal.value)
        return True

    @abc.abstractmethod
    async def search_jobs(self) -> list[JobListing]:
        """Scrape job listings from the portal."""

    async def teardown(self) -> None:
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        self._context = None

    async def run(self) -> list[JobListing]:
        try:
            if not await self.setup():
                return []
            jobs = await self.search_jobs()
            await self._emit(f"Found {len(jobs)} job(s)")
            return jobs
        except Exception as exc:
            await self._emit(f"Crawler error: {exc}")
            log_event("crawler_error", portal=self.portal.value, detail=str(exc))
            return []
        finally:
            await self.teardown()
