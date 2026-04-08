from __future__ import annotations

from urllib.parse import quote_plus

from backend.auth.session_manager import human_delay
from backend.crawler.base_crawler import BaseCrawler
from backend.logger import log_event
from backend.models import JobListing, JobPortal, WorkMode

WORK_MODE_ATTR = {
    WorkMode.REMOTE: "remotejob=032b3046-06a3-4876-8dfd-474eb5e7ed11",
}


class IndeedCrawler(BaseCrawler):
    portal = JobPortal.INDEED

    def _build_url(self, page_num: int = 0) -> str:
        base = "https://www.indeed.com/jobs?"
        params = [
            f"q={quote_plus(self.search_query)}",
            "jt=fulltime",
            "sort=date",
        ]
        if self.location:
            params.append(f"l={quote_plus(self.location)}")

        if WorkMode.REMOTE in self.work_modes:
            params.append(WORK_MODE_ATTR[WorkMode.REMOTE])

        if page_num > 0:
            params.append(f"start={page_num * 10}")

        return base + "&".join(params)

    async def search_jobs(self) -> list[JobListing]:
        assert self._page is not None
        jobs: list[JobListing] = []

        for page_num in range(self.max_pages):
            url = self._build_url(page_num)
            await self._emit(f"Searching page {page_num + 1}: {url}")
            await self._page.goto(url, wait_until="domcontentloaded")
            await human_delay(2, 4)

            cards = await self._page.query_selector_all(
                ".job_seen_beacon, .jobsearch-ResultsList .result, "
                "[data-jk], .tapItem"
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

    async def _parse_card(self, card) -> JobListing | None:
        title_el = await card.query_selector(
            "h2.jobTitle a, a[data-jk], .jcs-JobTitle"
        )
        if not title_el:
            return None

        title_span = await title_el.query_selector("span")
        title = (await title_span.inner_text()).strip() if title_span else (await title_el.inner_text()).strip()

        href = await title_el.get_attribute("href") or ""
        jk = await card.get_attribute("data-jk") or ""
        if jk and not href:
            href = f"https://www.indeed.com/viewjob?jk={jk}"
        elif href and not href.startswith("http"):
            href = "https://www.indeed.com" + href

        company_el = await card.query_selector(
            "[data-testid='company-name'], .companyName, .company"
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown"

        location_el = await card.query_selector(
            "[data-testid='text-location'], .companyLocation, .location"
        )
        location = (await location_el.inner_text()).strip() if location_el else ""

        work_mode = WorkMode.ONSITE
        loc_lower = (title + " " + location).lower()
        if "remote" in loc_lower:
            work_mode = WorkMode.REMOTE
        elif "hybrid" in loc_lower:
            work_mode = WorkMode.HYBRID

        return JobListing(
            title=title,
            company=company,
            location=location,
            url=href,
            portal=JobPortal.INDEED,
            work_mode=work_mode,
            job_type="Full-time",
        )
