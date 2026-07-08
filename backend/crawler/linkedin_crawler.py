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

_LI_SPLIT_LIST_SEL = (
    ".jobs-search-results-list, .jobs-search-results__list, "
    ".scaffold-layout__list, .scaffold-layout__list-detail, "
    ".jobs-search-two-pane__wrapper"
)

_LI_DETAIL_PANEL_SEL = (
    ".scaffold-layout__detail, "
    ".jobs-search__job-details, .jobs-details, .jobs-details__main-content, "
    ".jobs-search__job-details--wrapper, .jobs-details__main-content--single-pane"
)

_LI_TITLE_SEL = (
    "h1, h2.job-details-jobs-unified-top-card__job-title, "
    ".job-details-jobs-unified-top-card__job-title, "
    ".jobs-unified-top-card__job-title, "
    ".t-24.job-details-jobs-unified-top-card__job-title"
)


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
            try:
                url = self._build_url(page_num)
                await self._emit(f"Searching page {page_num + 1}: {url}")
                await self._page.goto(url, wait_until="domcontentloaded")
                await human_delay(2, 4)
            except Exception as exc:
                await self._emit(f"Page {page_num + 1} navigation failed: {exc}")
                break

            await self._scroll_page()

            raw_jobs = await self._extract_jobs_via_js()
            await self._emit(f"Page {page_num + 1}: extracted {len(raw_jobs)} job(s) via JS")

            parsed_count = 0
            for raw in raw_jobs:
                title = (raw.get("title") or "").strip()
                href = (raw.get("url") or "").strip()
                if not title or not href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.linkedin.com" + href
                href = self._canonical_job_url(href)
                if not href or href in seen_urls:
                    continue

                company = (raw.get("company") or "Unknown").strip()
                location = (raw.get("location") or "").strip()
                work_mode = self._detect_work_mode(title + " " + location)

                seen_urls.add(href)
                jobs.append(JobListing(
                    title=title,
                    company=company,
                    location=location,
                    url=href,
                    portal=JobPortal.LINKEDIN,
                    work_mode=work_mode,
                    job_type="Full-time",
                    description="",
                ))
                parsed_count += 1

            await self._emit(f"Page {page_num + 1}: parsed {parsed_count} unique job(s)")
            if parsed_count == 0 and len(raw_jobs) == 0:
                break

        return jobs

    async def _extract_jobs_via_js(self) -> list[dict]:
        assert self._page is not None
        return await self._page.evaluate("""
        () => {
            const jobs = [];
            const cards = document.querySelectorAll(
                '.job-card-container, .jobs-search-results__list-item, ' +
                '.scaffold-layout__list-item, [data-job-id]'
            );
            for (const card of cards) {
                let title = '';
                let url = '';
                let company = '';
                let location = '';

                const links = card.querySelectorAll('a[href*="/jobs/view/"]');
                for (const link of links) {
                    const text = (link.innerText || '').trim();
                    if (text && text.length > 2) {
                        title = title || text;
                        url = url || link.getAttribute('href') || '';
                    }
                }
                if (!title) {
                    const strong = card.querySelector('strong');
                    if (strong) title = (strong.innerText || '').trim();
                }
                if (!title) {
                    const titleEl = card.querySelector(
                        '.job-card-list__title, .artdeco-entity-lockup__title, ' +
                        '[class*="job-card"] a'
                    );
                    if (titleEl) title = (titleEl.innerText || '').trim();
                }
                if (!url) {
                    const anyLink = card.querySelector('a[href*="/jobs/view/"]');
                    if (anyLink) url = anyLink.getAttribute('href') || '';
                }
                if (!url) {
                    const jobId = card.getAttribute('data-job-id') ||
                        card.closest('[data-job-id]')?.getAttribute('data-job-id');
                    if (jobId) url = '/jobs/view/' + jobId + '/';
                }

                const companyEl = card.querySelector(
                    '.job-card-container__primary-description, ' +
                    '.job-card-container__company-name, ' +
                    '.artdeco-entity-lockup__subtitle, ' +
                    '[class*="company-name"], [class*="subtitle"]'
                );
                if (companyEl) company = (companyEl.innerText || '').trim();

                const locationEl = card.querySelector(
                    '.job-card-container__metadata-item, ' +
                    '.artdeco-entity-lockup__caption, ' +
                    '[class*="location"], [class*="metadata"]'
                );
                if (locationEl) location = (locationEl.innerText || '').trim();

                if (title && url) {
                    // Clean title: take first line, remove duplicates
                    const lines = title.split('\\n').map(l => l.trim()).filter(Boolean);
                    title = lines[0] || title;
                    jobs.push({ title, url, company, location });
                }
            }
            return jobs;
        }
        """)

    async def _scroll_page(self) -> None:
        """Scroll the results list to trigger lazy loading."""
        assert self._page is not None
        results_list = self._page.locator(_LI_SPLIT_LIST_SEL).first
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
            "a[data-control-name='job_card_title'], "
            "[class*='job-card'] a, a[href*='/jobs/view/'], "
            "strong, .artdeco-entity-lockup__title"
        ).first
        title = await self._locator_text(title_el)
        if not title and detail_panel is not None:
            title = await self._locator_text(
                detail_panel.locator(_LI_TITLE_SEL).first
            )
        if not title:
            all_links = card.locator("a")
            link_count = await all_links.count()
            for link_idx in range(min(link_count, 5)):
                link = all_links.nth(link_idx)
                link_text = await self._locator_text(link)
                link_href = await self._locator_attribute(link, "href")
                if link_text and len(link_text) > 3 and "/jobs/view/" in (link_href or ""):
                    title = link_text
                    title_el = link
                    break
        if not title:
            return None

        href = await self._locator_attribute(title_el, "href")
        if not href:
            card_link = card.locator("a[href*='/jobs/view/'], a[href*='currentJobId=']").first
            href = await self._locator_attribute(card_link, "href")
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
            if not job_id:
                job_id = await card.evaluate(
                    "(el) => el.closest('[data-job-id]')?.getAttribute('data-job-id') || ''"
                ) if await self._locator_text(card) else ""
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
            ".artdeco-entity-lockup__subtitle, "
            "[class*='company'], [class*='subtitle']"
        ).first)
        if not company and detail_panel is not None:
            company = await self._locator_text(
                detail_panel.locator(
                    ".job-details-jobs-unified-top-card__company-name, "
                    ".jobs-unified-top-card__company-name, "
                    ".topcard__org-name-link, .job-details-jobs-unified-top-card__company-name a"
                ).first
            )
        if not company:
            spans = card.locator("span, div")
            span_count = await spans.count()
            for span_idx in range(min(span_count, 10)):
                span = spans.nth(span_idx)
                text = await self._locator_text(span)
                if text and text != title and len(text) < 80 and "/" not in text:
                    company = text
                    break
        company = company or "Unknown"

        location = await self._locator_text(card.locator(
            ".job-card-container__metadata-item, "
            ".artdeco-entity-lockup__caption, "
            "[class*='location'], [class*='metadata']"
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

        work_mode = self._detect_work_mode(title + " " + (location or ""))

        return JobListing(
            title=title,
            company=company,
            location=location or "",
            url=href,
            portal=JobPortal.LINKEDIN,
            work_mode=work_mode,
            job_type="Full-time",
            description=description,
        )

    @staticmethod
    async def _is_easy_apply_card(card) -> bool:
        text = (await card.inner_text()).lower()
        return "apply" in text or "easy apply" in text

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
        return await self._page.locator(_LI_SPLIT_LIST_SEL).count() > 0

    async def _activate_split_layout_card(self, card):
        assert self._page is not None
        previous_title = await self._current_detail_title()
        expected_job_id = await self._locator_attribute(card, "data-job-id")
        if not expected_job_id:
            href = await self._locator_attribute(
                card.locator("a[href*='/jobs/view/'], a[href*='currentJobId=']").first,
                "href",
            )
            expected_job_id = self._extract_job_id(href)
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
                return None

        await self._wait_for_detail_panel_change(previous_title, expected_job_id)
        return await self._get_detail_panel()

    async def _wait_for_detail_panel(self) -> None:
        assert self._page is not None
        try:
            await self._page.wait_for_selector(
                _LI_DETAIL_PANEL_SEL, timeout=5000,
            )
        except Exception:
            pass
        await human_delay(0.6, 1.2)

    async def _wait_for_detail_panel_change(self, previous_title: str, expected_job_id: str = "") -> None:
        assert self._page is not None
        await self._wait_for_detail_panel()
        try:
            await self._page.wait_for_function(
                """
                ({ oldTitle, expectedId }) => {
                    const detailSel = '.scaffold-layout__detail, .jobs-search__job-details, .jobs-details, .jobs-details__main-content';
                    const titleSel = 'h1, h2.job-details-jobs-unified-top-card__job-title, .job-details-jobs-unified-top-card__job-title, .jobs-unified-top-card__job-title';
                    const root = document.querySelector(detailSel);
                    if (!root) return false;
                    const titleEl = root.querySelector(titleSel);
                    const linkEl = root.querySelector("a[href*='/jobs/view/'], a[href*='currentJobId=']");
                    const title = (titleEl?.innerText || '').trim();
                    const href = linkEl?.getAttribute('href') || '';
                    if (expectedId && href.includes(expectedId)) return true;
                    if (!oldTitle) return !!title;
                    return !!title && title !== oldTitle;
                }
                """,
                arg={"oldTitle": previous_title, "expectedId": expected_job_id},
                timeout=7000,
            )
        except Exception:
            pass
        await human_delay(0.3, 0.6)

    async def _get_detail_panel(self):
        assert self._page is not None
        panel = self._page.locator(_LI_DETAIL_PANEL_SEL).first
        try:
            if await panel.is_visible(timeout=1000):
                return panel
        except Exception:
            pass
        return None

    async def _current_detail_title(self) -> str:
        panel = await self._get_detail_panel()
        if panel is None:
            return ""
        title = panel.locator(_LI_TITLE_SEL).first
        return await self._locator_text(title)

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
    def _extract_job_id(url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"[?&]currentJobId=(\d+)", url)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _detect_work_mode(text: str) -> WorkMode:
        t = text.lower()
        if "remote" in t:
            return WorkMode.REMOTE
        if "hybrid" in t:
            return WorkMode.HYBRID
        return WorkMode.ONSITE
