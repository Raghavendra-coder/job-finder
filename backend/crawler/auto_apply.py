from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import Page

from backend.ai.answer_generator import generate_answer
from backend.auth.session_manager import (
    create_context,
    human_delay,
    is_managed_context,
    save_cookies,
)
from backend.config import APPLICANT_EMAIL, APPLICANT_NAME, APPLICANT_PHONE
from backend.logger import log_event, logger
from backend.models import ApplicationLog, JobListing, JobPortal, ResumeData, WorkMode


class AutoApplyBot:
    """Handles the "Apply" flow for each supported portal."""

    def __init__(
        self,
        resume_data: ResumeData,
        resume_path: Path,
        job_description: str,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.resume_data = resume_data
        self.resume_path = resume_path
        self.job_description = job_description
        self.on_status = on_status or (lambda _: None)

    async def _emit(self, msg: str) -> None:
        self.on_status(msg)
        logger.info("[auto-apply] %s", msg)

    async def apply_to_job(self, job: JobListing) -> ApplicationLog:
        app_log = ApplicationLog(job=job)

        try:
            portal_handler = {
                JobPortal.LINKEDIN: self._apply_linkedin,
                JobPortal.INDEED: self._apply_indeed,
                JobPortal.NAUKRI: self._apply_naukri,
            }.get(job.portal)

            if portal_handler is None:
                app_log.status = "unsupported_portal"
                return app_log

            await self._emit(f"Applying to {job.title} @ {job.company} ({job.portal.value})")

            ctx = await create_context(job.portal)
            page = await ctx.new_page()

            try:
                success = await portal_handler(page, job, app_log)
                if success:
                    app_log.status = "applied"
                    job.applied = True
                    log_event(
                        "applied", portal=job.portal.value,
                        job_title=job.title, company=job.company,
                        url=job.url, status="success",
                    )
                else:
                    app_log.status = "failed"
                    log_event(
                        "apply_failed", portal=job.portal.value,
                        job_title=job.title, company=job.company,
                        url=job.url, status="failed",
                        detail=app_log.error or "unknown",
                    )
            finally:
                await page.close()
                if is_managed_context(ctx):
                    await save_cookies(ctx, job.portal)
                    await ctx.close()

        except Exception as exc:
            app_log.status = "error"
            app_log.error = str(exc)
            log_event(
                "apply_error", portal=job.portal.value,
                job_title=job.title, company=job.company,
                url=job.url, detail=str(exc),
            )

        return app_log

    # ── LinkedIn Easy Apply ─────────────────────────────────────────────

    async def _apply_linkedin(
        self, page: Page, job: JobListing, app_log: ApplicationLog,
    ) -> bool:
        if "linkedin.com/jobs/view" not in job.url:
            app_log.error = "not_linkedin_easy_apply_url"
            return False

        await page.goto(job.url, wait_until="domcontentloaded")
        await human_delay(2, 3)

        apply_btn = page.locator(
            "button.jobs-apply-button, "
            "button[aria-label*='Easy Apply'], "
            "button:has-text('Easy Apply')"
        ).first

        if not await apply_btn.is_visible():
            await self._emit("No Easy Apply button found — external application")
            app_log.error = "external_application"
            return False

        await apply_btn.click()
        await human_delay(1, 2)

        max_steps = 10
        for step in range(max_steps):
            await self._emit(f"  Step {step + 1} of application form")
            await self._fill_visible_fields(page, app_log)
            await human_delay(1, 2)

            submit = page.locator(
                "button[aria-label*='Submit application'], "
                "button:has-text('Submit application')"
            ).first
            if await submit.is_visible():
                await submit.click()
                await human_delay(2, 3)
                await self._emit("  Application submitted!")
                return True

            next_btn = page.locator(
                "button[aria-label='Continue to next step'], "
                "button:has-text('Next'), "
                "button:has-text('Review')"
            ).first
            if await next_btn.is_visible():
                await next_btn.click()
                await human_delay(1, 2)
            else:
                break

        app_log.error = "form_navigation_stuck"
        return False

    # ── Indeed Apply ────────────────────────────────────────────────────

    async def _apply_indeed(
        self, page: Page, job: JobListing, app_log: ApplicationLog,
    ) -> bool:
        await page.goto(job.url, wait_until="domcontentloaded")
        await human_delay(2, 3)

        apply_btn = page.locator(
            "#indeedApplyButton, "
            "button[id*='applyButton'], "
            "button:has-text('Apply now')"
        ).first

        if not await apply_btn.is_visible():
            app_log.error = "no_apply_button"
            return False

        await apply_btn.click()
        await human_delay(2, 3)

        max_steps = 10
        for step in range(max_steps):
            await self._emit(f"  Step {step + 1} of application form")
            await self._fill_visible_fields(page, app_log)
            await self._handle_resume_upload(page)
            await human_delay(1, 2)

            submit = page.locator(
                "button:has-text('Submit your application'), "
                "button:has-text('Submit'), "
                "button[type='submit']:has-text('Apply')"
            ).first
            if await submit.is_visible():
                await submit.click()
                await human_delay(2, 3)
                await self._emit("  Application submitted!")
                return True

            continue_btn = page.locator(
                "button:has-text('Continue'), "
                "button:has-text('Next')"
            ).first
            if await continue_btn.is_visible():
                await continue_btn.click()
                await human_delay(1, 2)
            else:
                break

        app_log.error = "form_navigation_stuck"
        return False

    # ── Naukri Apply ────────────────────────────────────────────────────

    async def _apply_naukri(
        self, page: Page, job: JobListing, app_log: ApplicationLog,
    ) -> bool:
        await page.goto(job.url, wait_until="domcontentloaded")
        await human_delay(2, 3)

        apply_btn = page.locator(
            "button#apply-button, "
            "button.apply-button, "
            "button:has-text('Apply'), "
            "[class*='apply-btn']"
        ).first

        if not await apply_btn.is_visible():
            app_log.error = "no_apply_button"
            return False

        await apply_btn.click()
        await human_delay(2, 3)

        await self._fill_visible_fields(page, app_log)
        await self._handle_resume_upload(page)
        await human_delay(1, 2)

        submit = page.locator(
            "button:has-text('Submit'), "
            "button:has-text('Apply'), "
            "button[type='submit']"
        ).first
        if await submit.is_visible():
            await submit.click()
            await human_delay(2, 3)
            await self._emit("  Application submitted!")
            return True

        app_log.error = "submit_button_not_found"
        return False

    # ── Shared form-filling helpers ─────────────────────────────────────

    async def _fill_visible_fields(self, page: Page, app_log: ApplicationLog) -> None:
        name = self.resume_data.name or APPLICANT_NAME
        email = self.resume_data.email or APPLICANT_EMAIL
        phone = self.resume_data.phone or APPLICANT_PHONE

        await self._safe_fill(page, 'input[name*="name" i], input[aria-label*="name" i]', name)
        await self._safe_fill(page, 'input[type="email"], input[name*="email" i]', email)
        await self._safe_fill(page, 'input[type="tel"], input[name*="phone" i]', phone)

        # Screening questions — text inputs
        question_inputs = await page.query_selector_all(
            'input[type="text"]:not([value]):not([name*="name" i]):not([name*="email" i]):not([name*="phone" i]), '
            "textarea:not([name*='cover'])"
        )
        for inp in question_inputs:
            label = await self._get_label(page, inp)
            if not label:
                continue
            existing = await inp.get_attribute("value") or ""
            if existing.strip():
                continue
            answer = await generate_answer(label, self.resume_data, self.job_description)
            if answer:
                await inp.fill(answer)
                app_log.answers[label] = answer
                await human_delay(0.3, 0.7)

        # Screening questions — select / radio
        await self._handle_select_fields(page, app_log)

    async def _handle_select_fields(self, page: Page, app_log: ApplicationLog) -> None:
        selects = await page.query_selector_all("select")
        for sel in selects:
            label = await self._get_label(page, sel)
            options = await sel.query_selector_all("option")
            option_texts = []
            for opt in options:
                val = await opt.get_attribute("value") or ""
                text = (await opt.inner_text()).strip()
                if val and text:
                    option_texts.append((val, text))

            if not option_texts or not label:
                continue

            choices = ", ".join(t for _, t in option_texts)
            question = f"{label} (choose one: {choices})"
            answer = await generate_answer(question, self.resume_data, self.job_description)

            best_val = option_texts[-1][0]
            for val, text in option_texts:
                if text.lower() in answer.lower() or answer.lower() in text.lower():
                    best_val = val
                    break

            await sel.select_option(value=best_val)
            app_log.answers[label] = answer
            await human_delay(0.3, 0.7)

    async def _handle_resume_upload(self, page: Page) -> None:
        file_input = page.locator('input[type="file"]').first
        try:
            if await file_input.is_visible():
                await file_input.set_input_files(str(self.resume_path))
                await self._emit("  Resume uploaded")
                await human_delay(1, 2)
        except Exception:
            pass

    @staticmethod
    async def _safe_fill(page: Page, selector: str, value: str) -> None:
        if not value:
            return
        try:
            el = page.locator(selector).first
            if await el.is_visible():
                existing = await el.get_attribute("value") or ""
                if not existing.strip():
                    await el.fill(value)
                    await human_delay(0.2, 0.5)
        except Exception:
            pass

    @staticmethod
    async def _get_label(page: Page, element) -> str:
        el_id = await element.get_attribute("id") or ""
        if el_id:
            label_el = await page.query_selector(f'label[for="{el_id}"]')
            if label_el:
                return (await label_el.inner_text()).strip()

        aria_label = await element.get_attribute("aria-label") or ""
        if aria_label:
            return aria_label.strip()

        placeholder = await element.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder.strip()

        name = await element.get_attribute("name") or ""
        return name.replace("_", " ").replace("-", " ").strip()
