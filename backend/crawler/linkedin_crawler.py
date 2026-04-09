from __future__ import annotations

import re
from urllib.parse import quote_plus

from backend.auth.session_manager import human_delay
from backend.crawler.base_crawler import BaseCrawler
from backend.logger import log_event
from backend.models import JobListing, JobPortal, WorkMode

WORK_MODE_FILTERS = {
    WorkMode.ONSITE: "1",
    WorkMode.REMOTE: "2",
    WorkMode.HYBRID: "3",
}


class LinkedInCrawler(BaseCrawler):
    portal = JobPortal.LINKEDIN

    def _build_url(self, page_num: int = 0) -> str:
        base = "https://www.linkedin.com/jobs/search/?"
        params = [
            f"keywords={quote_plus(self.search_query)}",
            "f_JT=F",  # Full-time only
            "f_AL=true",  # Easy Apply only
            "locale=en_US",
        ]
        if self.location:
            params.append(f"location={quote_plus(self.location)}")

        wt_codes = [WORK_MODE_FILTERS[m] for m in self.work_modes if m in WORK_MODE_FILTERS]
        if wt_codes:
            params.append(f"f_WT={'%2C'.join(wt_codes)}")

        if page_num > 0:
            params.append(f"start={page_num * 25}")

        return base + "&".join(params)

    async def search_jobs(self) -> list[JobListing]:
        assert self._page is not None
        jobs: list[JobListing] = []

        for page_num in range(self.max_pages):
            url = self._build_url(page_num)
            await self._emit(f"Searching page {page_num + 1}: {url}")
            await self._page.goto(url, wait_until="domcontentloaded")
            await human_delay(2, 4)

            await self._scroll_page()

            cards = await self._page.query_selector_all(
                ".job-card-container, .jobs-search-results__list-item, "
                "[data-job-id]"
            )
            await self._emit(f"Page {page_num + 1}: found {len(cards)} card(s)")

            for card in cards:
                try:
                    job = await self._parse_card(card)
                    if job:
                        jobs.append(job)
                except Exception as exc:
                    log_event(
                        "parse_error", portal=self.portal.value,
                        detail=str(exc),
                    )

            if not cards:
                break

        return jobs

    async def _scroll_page(self) -> None:
        """Scroll the results list to trigger lazy loading."""
        assert self._page is not None
        for _ in range(5):
            await self._page.evaluate("window.scrollBy(0, 600)")
            await human_delay(0.5, 1)

    async def _parse_card(self, card) -> JobListing | None:
        title_el = await card.query_selector(
            ".job-card-list__title, .job-card-container__link, "
            "a[data-control-name='job_card_title']"
        )
        if not title_el:
            return None

        title = (await title_el.inner_text()).strip()
        href = await title_el.get_attribute("href") or ""
        if href and not href.startswith("http"):
            href = "https://www.linkedin.com" + href
        if not href:
            job_id = await card.get_attribute("data-job-id") or ""
            if job_id:
                href = f"https://www.linkedin.com/jobs/view/{job_id}/"
        href = self._canonical_job_url(href)
        if not href:
            return None

        company_el = await card.query_selector(
            ".job-card-container__primary-description, "
            ".job-card-container__company-name, "
            ".artdeco-entity-lockup__subtitle"
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown"

        location_el = await card.query_selector(
            ".job-card-container__metadata-item, "
            ".artdeco-entity-lockup__caption"
        )
        location = (await location_el.inner_text()).strip() if location_el else ""

        work_mode = self._detect_work_mode(title + " " + location)

        return JobListing(
            title=title,
            company=company,
            location=location,
            url=href,
            portal=JobPortal.LINKEDIN,
            work_mode=work_mode,
            job_type="Full-time",
        )

    @staticmethod
    async def _is_easy_apply_card(card) -> bool:
        text = (await card.inner_text()).lower()
        return "easy apply" in text

    @staticmethod
    def _canonical_job_url(url: str) -> str:
        if not url:
            return ""
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/?locale=en_US"
        match = re.search(r"[?&]currentJobId=(\d+)", url)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/?locale=en_US"
        return url

    @staticmethod
    def _detect_work_mode(text: str) -> WorkMode:
        t = text.lower()
        if "remote" in t:
            return WorkMode.REMOTE
        if "hybrid" in t:
            return WorkMode.HYBRID
        return WorkMode.ONSITE
