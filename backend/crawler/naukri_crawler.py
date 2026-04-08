from __future__ import annotations

from urllib.parse import quote_plus

from backend.auth.session_manager import human_delay
from backend.crawler.base_crawler import BaseCrawler
from backend.logger import log_event
from backend.models import JobListing, JobPortal, WorkMode

WORK_MODE_SLUG = {
    WorkMode.REMOTE: "wfhType-1",
    WorkMode.HYBRID: "wfhType-2",
}


class NaukriCrawler(BaseCrawler):
    portal = JobPortal.NAUKRI

    def _build_url(self, page_num: int = 0) -> str:
        query_slug = self.search_query.replace(" ", "-").lower()
        base = f"https://www.naukri.com/{query_slug}-jobs"

        params: list[str] = ["jobType=fulltime"]

        wfh_parts = [
            WORK_MODE_SLUG[m] for m in self.work_modes if m in WORK_MODE_SLUG
        ]
        if wfh_parts:
            params.append("&".join(wfh_parts))

        if self.location:
            loc_slug = self.location.replace(" ", "-").lower()
            base = f"https://www.naukri.com/{query_slug}-jobs-in-{loc_slug}"

        if page_num > 0:
            params.append(f"pageNo={page_num + 1}")

        return base + "?" + "&".join(params)

    async def search_jobs(self) -> list[JobListing]:
        assert self._page is not None
        jobs: list[JobListing] = []

        for page_num in range(self.max_pages):
            url = self._build_url(page_num)
            await self._emit(f"Searching page {page_num + 1}: {url}")
            await self._page.goto(url, wait_until="domcontentloaded")
            await human_delay(2, 4)

            cards = await self._page.query_selector_all(
                ".srp-jobtuple-wrapper, article.jobTuple, "
                "[data-job-id], .cust-job-tuple"
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
            "a.title, .jobTupleHeader a, a[class*='title']"
        )
        if not title_el:
            return None

        title = (await title_el.inner_text()).strip()
        href = await title_el.get_attribute("href") or ""

        company_el = await card.query_selector(
            "a.comp-name, .companyInfo a, [class*='companyName']"
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown"

        location_el = await card.query_selector(
            ".loc, .locWdth, [class*='location'], .ellipsis .loc"
        )
        location = (await location_el.inner_text()).strip() if location_el else ""

        work_mode = WorkMode.ONSITE
        combined = (title + " " + location).lower()
        if "remote" in combined or "work from home" in combined:
            work_mode = WorkMode.REMOTE
        elif "hybrid" in combined:
            work_mode = WorkMode.HYBRID

        return JobListing(
            title=title,
            company=company,
            location=location,
            url=href,
            portal=JobPortal.NAUKRI,
            work_mode=work_mode,
            job_type="Full-time",
        )
