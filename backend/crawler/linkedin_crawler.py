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
        seen_urls: set[str] = set()

        for page_num in range(self.max_pages):
            url = self._build_url(page_num)
            await self._emit(f"Searching page {page_num + 1}: {url}")
            await self._page.goto(url, wait_until="domcontentloaded")
            await human_delay(2, 4)

            await self._scroll_page()
            is_split_layout = await self._is_split_layout()

            cards = self._page.locator(
                ".job-card-container, .jobs-search-results__list-item, "
                "[data-job-id]"
            )
            card_count = await cards.count()
            await self._emit(f"Page {page_num + 1}: found {card_count} card(s)")

            for idx in range(card_count):
                card = cards.nth(idx)
                try:
                    detail_panel = None
                    if is_split_layout:
                        await self._activate_split_layout_card(card)
                        detail_panel = await self._get_detail_panel()

                    job = await self._parse_card(card, detail_panel)
                    if job and job.url not in seen_urls:
                        seen_urls.add(job.url)
                        jobs.append(job)
                except Exception as exc:
                    log_event(
                        "parse_error", portal=self.portal.value,
                        detail=str(exc),
                    )

            if card_count == 0:
                break

        return jobs

    async def _scroll_page(self) -> None:
        """Scroll the results list to trigger lazy loading."""
        assert self._page is not None
        results_list = self._page.locator(
            ".jobs-search-results-list, .jobs-search-results__list"
        ).first
        for _ in range(5):
            try:
                if await results_list.is_visible(timeout=500):
                    await results_list.evaluate("(el) => el.scrollBy(0, 800)")
                else:
                    await self._page.evaluate("window.scrollBy(0, 600)")
            except Exception:
                await self._page.evaluate("window.scrollBy(0, 600)")
            await human_delay(0.5, 1)

    async def _parse_card(self, card, detail_panel=None) -> JobListing | None:
        title_el = card.locator(
            ".job-card-list__title, .job-card-container__link, "
            "a[data-control-name='job_card_title']"
        ).first
        title = await self._locator_text(title_el)
        if not title and detail_panel is not None:
            title = await self._locator_text(
                detail_panel.locator(
                    "h1, .job-details-jobs-unified-top-card__job-title, "
                    ".jobs-unified-top-card__job-title"
                ).first
            )
        if not title:
            return None

        href = await self._locator_attribute(title_el, "href")
        if detail_panel is not None:
            panel_link = detail_panel.locator(
                "a[href*='/jobs/view/'], a[href*='currentJobId=']"
            ).first
            panel_href = await self._locator_attribute(panel_link, "href")
            href = panel_href or href
        if href and not href.startswith("http"):
            href = "https://www.linkedin.com" + href
        if not href:
            job_id = await self._locator_attribute(card, "data-job-id")
            if job_id:
                href = f"https://www.linkedin.com/jobs/view/{job_id}/"
        if not href and self._page is not None:
            href = self._page.url
        href = self._canonical_job_url(href)
        if not href:
            return None

        company = await self._locator_text(card.locator(
            ".job-card-container__primary-description, "
            ".job-card-container__company-name, "
            ".artdeco-entity-lockup__subtitle"
        ).first)
        if not company and detail_panel is not None:
            company = await self._locator_text(
                detail_panel.locator(
                    ".job-details-jobs-unified-top-card__company-name, "
                    ".jobs-unified-top-card__company-name, "
                    ".topcard__org-name-link, .job-details-jobs-unified-top-card__company-name a"
                ).first
            )
        company = company or "Unknown"

        location = await self._locator_text(card.locator(
            ".job-card-container__metadata-item, "
            ".artdeco-entity-lockup__caption"
        ).first)
        if not location and detail_panel is not None:
            location = await self._locator_text(
                detail_panel.locator(
                    ".job-details-jobs-unified-top-card__primary-description-container, "
                    ".jobs-unified-top-card__bullet, "
                    ".jobs-unified-top-card__primary-description"
                ).first
            )

        description = ""
        if detail_panel is not None:
            description = await self._locator_text(
                detail_panel.locator(
                    ".jobs-description__container, .jobs-box__html-content, "
                    ".jobs-description-content__text"
                ).first
            )

        work_mode = self._detect_work_mode(title + " " + location)

        return JobListing(
            title=title,
            company=company,
            location=location,
            url=href,
            portal=JobPortal.LINKEDIN,
            work_mode=work_mode,
            job_type="Full-time",
            description=description,
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

    async def _is_split_layout(self) -> bool:
        assert self._page is not None
        return await self._page.locator(
            ".jobs-search-results-list, .jobs-search-results__list"
        ).count() > 0

    async def _activate_split_layout_card(self, card) -> None:
        assert self._page is not None
        try:
            await card.scroll_into_view_if_needed()
        except Exception:
            pass

        target = card.locator("a, button").first
        try:
            await target.click(timeout=2500)
        except Exception:
            try:
                await card.click(timeout=2500)
            except Exception:
                return

        await self._wait_for_detail_panel()

    async def _wait_for_detail_panel(self) -> None:
        assert self._page is not None
        try:
            await self._page.wait_for_selector(
                ".jobs-search__job-details, .jobs-details, .jobs-details__main-content",
                timeout=5000,
            )
        except Exception:
            pass
        await human_delay(0.6, 1.2)

    async def _get_detail_panel(self):
        assert self._page is not None
        panel = self._page.locator(
            ".jobs-search__job-details, .jobs-details, .jobs-details__main-content"
        ).first
        try:
            if await panel.is_visible(timeout=1000):
                return panel
        except Exception:
            pass
        return None

    @staticmethod
    async def _locator_text(locator) -> str:
        try:
            text = (await locator.inner_text()).strip()
        except Exception:
            return ""
        return " ".join(text.split())

    @staticmethod
    async def _locator_attribute(locator, name: str) -> str:
        try:
            return (await locator.get_attribute(name)) or ""
        except Exception:
            return ""

    @staticmethod
    def _detect_work_mode(text: str) -> WorkMode:
        t = text.lower()
        if "remote" in t:
            return WorkMode.REMOTE
        if "hybrid" in t:
            return WorkMode.HYBRID
        return WorkMode.ONSITE
