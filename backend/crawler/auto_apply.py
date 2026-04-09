from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import Frame, Locator, Page

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
        target_url = self._canonical_linkedin_job_url(job.url)
        if not target_url:
            app_log.error = "not_linkedin_easy_apply_url"
            return False

        page.set_default_timeout(5000)
        await page.goto(target_url, wait_until="domcontentloaded")
        await self._wait_for_linkedin_transition(page)

        if "linkedin.com/jobs/view" not in page.url.lower():
            # LinkedIn can occasionally redirect into list-pane URLs. Retry canonical.
            retry_url = self._canonical_linkedin_job_url(page.url) or target_url
            await page.goto(retry_url, wait_until="domcontentloaded")
            await self._wait_for_linkedin_transition(page)
            if "linkedin.com/jobs/view" not in page.url.lower() and not await self._is_split_linkedin_layout(page):
                app_log.error = "linkedin_redirected_from_job_page"
                return False

        job_container = await self._get_linkedin_job_container(page, target_url)
        apply_btn = await self._find_linkedin_easy_apply_button(page, job_container)
        clicked = False
        if apply_btn is not None:
            clicked = await self._click_if_enabled(apply_btn)
        if not clicked:
            clicked = await self._force_click_easy_apply_by_text(page, job_container)
        if not clicked:
            clicked = await self._force_click_easy_apply_by_text(page)
        if not clicked:
            await self._emit("No Easy Apply button found — external application")
            app_log.error = "external_application"
            return False

        await self._wait_for_linkedin_transition(page)
        modal = await self._get_linkedin_modal(page)
        if modal is None:
            await self._emit("LinkedIn Easy Apply modal not found after click")
            app_log.error = "easy_apply_modal_not_found"
            return False

        max_steps = 10
        for step in range(max_steps):
            modal = await self._get_linkedin_modal(page)
            if modal is None:
                app_log.error = "easy_apply_modal_lost"
                return False

            await self._emit(f"  Step {step + 1} of application form")
            await self._fill_visible_fields(modal, app_log)
            await self._handle_radio_fields(modal, app_log)
            await self._handle_checkbox_fields(modal)
            await self._handle_resume_upload(modal)
            await human_delay(1, 2)

            submit = await self._find_button(
                modal,
                ["Submit application", "Submit", "Send application"],
                page=page,
                search_frames=True,
            )
            if submit and await self._click_if_enabled(submit):
                await self._wait_for_linkedin_transition(page)
                await self._emit("  Application submitted!")
                return True

            review_btn = await self._find_button(
                modal,
                ["Review application", "Review"],
                page=page,
                search_frames=True,
            )
            if review_btn and await self._click_if_enabled(review_btn):
                await self._wait_for_linkedin_transition(page)
                continue

            next_btn = await self._find_button(
                modal,
                ["Continue to next step", "Continue", "Next"],
                page=page,
                search_frames=True,
            )
            if next_btn and await self._click_if_enabled(next_btn):
                await self._wait_for_linkedin_transition(page)
                continue

            if await self._click_primary_easy_apply_action(modal):
                await self._wait_for_linkedin_transition(page)
                continue

            visible_buttons = await self._list_button_labels(modal)
            if visible_buttons:
                await self._emit(
                    "  Could not determine next LinkedIn action. Buttons: "
                    + ", ".join(visible_buttons[:8])
                )
            break

        app_log.error = "form_navigation_stuck"
        return False

    async def _find_linkedin_easy_apply_button(
        self,
        page: Page,
        container=None,
    ):
        search_roots = []
        if container is not None:
            search_roots.append(container)
        search_roots.append(page)

        selectors = [
            "button.jobs-apply-button",
            "button.jobs-apply-button--top-card",
            "button[aria-label*='Easy Apply']",
            "button:has-text('Easy Apply')",
            "[data-control-name='jobdetails_topcard_inapply']",
        ]
        for root in search_roots:
            for sel in selectors:
                btn = root.locator(sel).first
                if await self._is_visible(btn, timeout=1200):
                    return btn

        for root in search_roots:
            try:
                role_btn = root.get_by_role("button", name=re.compile("easy apply", re.I)).first
                if await self._is_visible(role_btn, timeout=1200):
                    return role_btn
            except Exception:
                continue

        for root in search_roots:
            btn = await self._find_button(root, ["Easy Apply"], page=page)
            if btn is not None:
                return btn

        return None

    @staticmethod
    async def _force_click_easy_apply_by_text(page: Page, container=None) -> bool:
        script = """
        (root) => {
          const nodes = Array.from(root.querySelectorAll("button, a, [role='button']"));
          const target = nodes.find((el) => {
            const text = (el.innerText || el.textContent || "").toLowerCase().trim();
            if (!text.includes("easy apply")) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            const visible = rect.width > 0 && rect.height > 0 &&
              style.visibility !== "hidden" && style.display !== "none" &&
              !el.disabled;
            return visible;
          });
          if (!target) return false;
          target.scrollIntoView({ block: "center", inline: "center" });
          target.click();
          return true;
        }
        """
        if container is not None:
            try:
                return bool(await container.evaluate(script))
            except Exception:
                pass
        try:
            return bool(await page.evaluate(
                """
                () => {
                  const nodes = Array.from(
                    document.querySelectorAll("button, a, [role='button']")
                  );
                  const target = nodes.find((el) => {
                    const text = (el.innerText || el.textContent || "").toLowerCase().trim();
                    if (!text.includes("easy apply")) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0 &&
                      style.visibility !== "hidden" && style.display !== "none" &&
                      !el.disabled;
                    return visible;
                  });
                  if (!target) return false;
                  target.scrollIntoView({ block: "center", inline: "center" });
                  target.click();
                  return true;
                }
                """
            ))
        except Exception:
            return False

    @staticmethod
    def _canonical_linkedin_job_url(url: str) -> str:
        if "linkedin.com/jobs/view/" in url:
            return url
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/?locale=en_US"
        match = re.search(r"[?&]currentJobId=(\d+)", url)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/?locale=en_US"
        return ""

    @staticmethod
    async def _in_linkedin_apply_flow(page: Page) -> bool:
        modal = page.locator(
            ".artdeco-modal[role='dialog'], .jobs-easy-apply-modal, .jobs-easy-apply-content"
        ).first
        try:
            return await modal.is_visible(timeout=2500)
        except Exception:
            return False

    @staticmethod
    async def _click_primary_easy_apply_action(container) -> bool:
        """
        Language-agnostic fallback: click the primary button in Easy Apply modal footer.
        """
        footer_btn = container.locator(
            "footer button.artdeco-button--primary, "
            "button.artdeco-button--primary"
        ).last
        return await AutoApplyBot._click_if_enabled(footer_btn)

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

    async def _fill_visible_fields(self, container, app_log: ApplicationLog) -> None:
        name = self.resume_data.name or APPLICANT_NAME
        email = self.resume_data.email or APPLICANT_EMAIL
        phone = self.resume_data.phone or APPLICANT_PHONE

        await self._safe_fill(container, 'input[name*="name" i], input[aria-label*="name" i]', name)
        await self._safe_fill(container, 'input[type="email"], input[name*="email" i]', email)
        await self._safe_fill(container, 'input[type="tel"], input[name*="phone" i]', phone)

        question_inputs = container.locator(
            'input[type="text"], input:not([type]), textarea:not([name*="cover" i])'
        )
        count = await question_inputs.count()
        for idx in range(count):
            inp = question_inputs.nth(idx)
            if not await self._is_visible(inp, timeout=250):
                continue

            label = await self._get_label(container, inp)
            existing = await self._get_field_value(inp)
            if existing.strip():
                continue

            direct_value = self._contact_value_for_label(label, name, email, phone)
            if direct_value:
                try:
                    await inp.fill(direct_value)
                    await human_delay(0.2, 0.4)
                except Exception:
                    pass
                continue

            if not label:
                continue

            answer = await generate_answer(label, self.resume_data, self.job_description)
            if answer:
                try:
                    await inp.fill(answer)
                    app_log.answers[label] = answer
                    await human_delay(0.3, 0.7)
                except Exception:
                    continue

        await self._handle_select_fields(container, app_log)

    async def _handle_select_fields(self, container, app_log: ApplicationLog) -> None:
        selects = container.locator("select")
        count = await selects.count()
        for idx in range(count):
            sel = selects.nth(idx)
            if not await self._is_visible(sel, timeout=250):
                continue

            label = await self._get_label(container, sel)
            options = sel.locator("option")
            option_count = await options.count()
            option_texts = []
            for opt_idx in range(option_count):
                opt = options.nth(opt_idx)
                val = await opt.get_attribute("value") or ""
                text = (await opt.inner_text()).strip()
                if val and text:
                    option_texts.append((val, text))

            if not option_texts or not label:
                continue

            choices = ", ".join(text for _, text in option_texts)
            question = f"{label} (choose one: {choices})"
            answer = await generate_answer(question, self.resume_data, self.job_description)

            best_val = option_texts[-1][0]
            answer_lower = answer.lower()
            for val, text in option_texts:
                text_lower = text.lower()
                if text_lower in answer_lower or answer_lower in text_lower:
                    best_val = val
                    break

            try:
                await sel.select_option(value=best_val)
                app_log.answers[label] = answer
                await human_delay(0.3, 0.7)
            except Exception:
                continue

    async def _handle_radio_fields(self, container, app_log: ApplicationLog) -> None:
        fieldsets = container.locator("fieldset")
        fieldset_count = await fieldsets.count()
        for idx in range(fieldset_count):
            fs = fieldsets.nth(idx)
            radios = fs.locator("input[type='radio']")
            radio_count = await radios.count()
            if radio_count == 0:
                continue

            selected = False
            for radio_idx in range(radio_count):
                radio = radios.nth(radio_idx)
                if await radio.is_checked():
                    selected = True
                    break
            if selected:
                continue

            legend = fs.locator("legend").first
            label = ""
            if await self._is_visible(legend, timeout=150):
                try:
                    label = (await legend.inner_text()).strip()
                except Exception:
                    label = ""
            if not label:
                label = "Screening question"

            options: list[tuple[Locator, str]] = []
            for radio_idx in range(radio_count):
                radio = radios.nth(radio_idx)
                rid = await radio.get_attribute("id") or ""
                option_label = ""
                if rid:
                    label_el = container.locator(f"label[for='{rid}']").first
                    if await self._is_visible(label_el, timeout=150):
                        try:
                            option_label = (await label_el.inner_text()).strip()
                        except Exception:
                            option_label = ""
                if not option_label:
                    option_label = await radio.get_attribute("value") or ""
                if option_label:
                    options.append((radio, option_label))

            if not options:
                continue

            choices = ", ".join(text for _, text in options)
            question = f"{label} (choose one: {choices})"
            answer = await generate_answer(question, self.resume_data, self.job_description)

            answer_lower = answer.lower()
            chosen = options[-1][0]
            for radio, text in options:
                text_lower = text.lower()
                if text_lower in answer_lower or answer_lower in text_lower:
                    chosen = radio
                    break

            try:
                await chosen.check()
                app_log.answers[label] = answer
                await human_delay(0.2, 0.5)
            except Exception:
                continue

    @staticmethod
    async def _handle_checkbox_fields(container) -> None:
        checkboxes = container.locator("input[type='checkbox']")
        count = await checkboxes.count()
        for idx in range(count):
            box = checkboxes.nth(idx)
            try:
                if await box.is_checked():
                    continue
                req = await box.get_attribute("required")
                aria_req = await box.get_attribute("aria-required")
                if req is not None or aria_req == "true":
                    await box.check()
                    await human_delay(0.1, 0.3)
            except Exception:
                continue

    @staticmethod
    async def _click_if_enabled(locator) -> bool:
        try:
            if not await locator.is_visible(timeout=1200):
                return False
            if await locator.is_disabled():
                return False
            await locator.scroll_into_view_if_needed()
            await locator.click(timeout=2500)
            return True
        except Exception:
            return False

    async def _handle_resume_upload(self, container) -> None:
        file_input = container.locator('input[type="file"]').first
        try:
            if await file_input.is_visible():
                await file_input.set_input_files(str(self.resume_path))
                await self._emit("  Resume uploaded")
                await human_delay(1, 2)
        except Exception:
            pass

    @staticmethod
    async def _safe_fill(container, selector: str, value: str) -> None:
        if not value:
            return
        try:
            el = container.locator(selector).first
            if await el.is_visible():
                existing = await AutoApplyBot._get_field_value(el)
                if not existing.strip():
                    await el.fill(value)
                    await human_delay(0.2, 0.5)
        except Exception:
            pass

    @staticmethod
    async def _get_label(container, element) -> str:
        el_id = await element.get_attribute("id") or ""
        if el_id:
            label_el = container.locator(f'label[for="{el_id}"]').first
            if await AutoApplyBot._is_visible(label_el, timeout=150):
                try:
                    return (await label_el.inner_text()).strip()
                except Exception:
                    pass

        parent_label = element.locator("xpath=ancestor::label[1]").first
        if await AutoApplyBot._is_visible(parent_label, timeout=150):
            try:
                text = (await parent_label.inner_text()).strip()
                if text:
                    return text
            except Exception:
                pass

        aria_label = await element.get_attribute("aria-label") or ""
        if aria_label:
            return aria_label.strip()

        placeholder = await element.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder.strip()

        name = await element.get_attribute("name") or ""
        return name.replace("_", " ").replace("-", " ").strip()

    async def _wait_for_linkedin_transition(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await human_delay(0.8, 1.4)

    @staticmethod
    async def _is_split_linkedin_layout(page: Page) -> bool:
        return await page.locator(
            ".jobs-search-results-list, .jobs-search-results__list"
        ).count() > 0

    async def _get_linkedin_job_container(self, page: Page, target_url: str):
        if not await self._is_split_linkedin_layout(page):
            return page

        detail_panel = page.locator(
            ".jobs-search__job-details, .jobs-details, .jobs-details__main-content"
        ).first
        target_id = self._extract_linkedin_job_id(target_url)
        if target_id:
            cards = page.locator(
                ".jobs-search-results__list-item, .job-card-container, [data-job-id]"
            )
            card_count = await cards.count()
            for idx in range(card_count):
                card = cards.nth(idx)
                card_id = await card.get_attribute("data-job-id") or ""
                if target_id == card_id:
                    await self._click_linkedin_card(page, card)
                    break

                link = card.locator("a[href*='/jobs/view/'], a[href*='currentJobId=']").first
                href = await self._locator_attribute(link, "href")
                if target_id and target_id in href:
                    await self._click_linkedin_card(page, card)
                    break

        if await self._is_visible(detail_panel, timeout=2000):
            return detail_panel
        return page

    async def _get_linkedin_modal(self, page: Page) -> Optional[Locator]:
        candidates = [
            page.locator(".jobs-easy-apply-modal").last,
            page.locator(".artdeco-modal[role='dialog']").last,
            page.locator(".jobs-easy-apply-content").last,
        ]
        for modal in candidates:
            if await self._is_visible(modal, timeout=1000):
                return modal
        return None

    async def _find_button(
        self,
        container,
        names: list[str],
        page: Optional[Page] = None,
        search_frames: bool = False,
    ) -> Optional[Locator]:
        pattern = re.compile("|".join(re.escape(name) for name in names), re.I)
        try:
            role_btn = container.get_by_role("button", name=pattern).first
            if await self._is_visible(role_btn, timeout=250):
                return role_btn
        except Exception:
            pass

        controls = container.locator("button, [role='button'], a")
        count = await controls.count()
        for idx in range(min(count, 80)):
            control = controls.nth(idx)
            if not await self._is_visible(control, timeout=150):
                continue
            label = await self._button_label(control)
            if any(name.lower() in label.lower() for name in names):
                return control

        if search_frames and page is not None:
            return await self._find_button_in_frames(page, names)

        return None

    async def _find_button_in_frames(self, page: Page, names: list[str]) -> Optional[Locator]:
        for frame in page.frames:
            button = await self._find_button_in_frame(frame, names)
            if button is not None:
                return button
        return None

    async def _find_button_in_frame(self, frame: Frame, names: list[str]) -> Optional[Locator]:
        controls = frame.locator("button, [role='button'], a")
        count = await controls.count()
        for idx in range(min(count, 80)):
            control = controls.nth(idx)
            if not await self._is_visible(control, timeout=150):
                continue
            label = await self._button_label(control)
            if any(name.lower() in label.lower() for name in names):
                return control
        return None

    async def _list_button_labels(self, container) -> list[str]:
        controls = container.locator("button, [role='button'], a")
        count = await controls.count()
        labels: list[str] = []
        for idx in range(min(count, 20)):
            control = controls.nth(idx)
            if not await self._is_visible(control, timeout=100):
                continue
            label = await self._button_label(control)
            if label:
                labels.append(label)
        return labels

    @staticmethod
    async def _button_label(control) -> str:
        try:
            text = (await control.inner_text()).strip()
            if text:
                return text
        except Exception:
            pass
        for attr in ("aria-label", "title"):
            try:
                value = await control.get_attribute(attr) or ""
                if value.strip():
                    return value.strip()
            except Exception:
                continue
        return ""

    @staticmethod
    async def _is_visible(locator, timeout: int = 300) -> bool:
        try:
            return await locator.is_visible(timeout=timeout)
        except Exception:
            return False

    @staticmethod
    async def _get_field_value(locator) -> str:
        try:
            return await locator.input_value()
        except Exception:
            pass
        try:
            return (await locator.get_attribute("value")) or ""
        except Exception:
            return ""

    @staticmethod
    async def _locator_attribute(locator, name: str) -> str:
        try:
            return (await locator.get_attribute(name)) or ""
        except Exception:
            return ""

    async def _click_linkedin_card(self, page: Page, card) -> None:
        click_target = card.locator("a, button").first
        if await self._click_if_enabled(click_target):
            await self._wait_for_linkedin_transition(page)
            return
        try:
            await card.click(timeout=2500)
        except Exception:
            return
        await self._wait_for_linkedin_transition(page)

    @staticmethod
    def _extract_linkedin_job_id(url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"[?&]currentJobId=(\d+)", url)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _contact_value_for_label(label: str, name: str, email: str, phone: str) -> str:
        normalized = label.lower()
        if any(token in normalized for token in ("email", "e-mail", "mail")):
            return email
        if any(token in normalized for token in ("phone", "mobile", "contact number")):
            return phone
        if "name" in normalized:
            return name
        return ""
