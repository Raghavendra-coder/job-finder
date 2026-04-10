from __future__ import annotations

import re
from datetime import date
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
        phone_number: str = "",
        country_code: str = "",
        current_ctc: Optional[float] = None,
        expected_ctc: Optional[float] = None,
        notice_days: Optional[float] = None,
        total_experience: Optional[float] = None,
        is_immediate_joiner: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.resume_data = resume_data
        self.resume_path = resume_path
        self.job_description = job_description
        self.phone_number = phone_number
        self.country_code = country_code
        self.current_ctc = current_ctc
        self.expected_ctc = expected_ctc
        self.notice_days = notice_days
        self.total_experience = total_experience
        self.is_immediate_joiner = is_immediate_joiner
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

        is_split_layout = await self._is_split_linkedin_layout(page)
        job_container = await self._get_linkedin_job_container(page, target_url)
        if is_split_layout and job_container is not page:
            job_container = await self._fresh_linkedin_detail_panel(page) or job_container
            await self._prime_linkedin_detail_panel(page, job_container)

        apply_btn = await self._find_linkedin_easy_apply_button(
            page,
            job_container,
            allow_page_fallback=not is_split_layout,
        )
        clicked = False
        if apply_btn is not None:
            clicked = await self._click_if_enabled(apply_btn)
        if not clicked and job_container is not page:
            clicked = await self._force_click_easy_apply_by_text(page, job_container)
        if not clicked and is_split_layout and job_container is not page:
            visible_buttons = await self._list_button_labels(job_container)
            if visible_buttons:
                await self._emit(
                    "  Right panel buttons before fallback: "
                    + ", ".join(visible_buttons[:8])
                )
            try:
                await page.screenshot(path="/tmp/linkedin-split-debug.png", full_page=True)
            except Exception:
                pass

        if not clicked:
            apply_btn = await self._find_linkedin_easy_apply_button(
                page,
                page,
                allow_page_fallback=True,
            )
            if apply_btn is not None:
                clicked = await self._click_if_enabled(apply_btn)
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
            await self._fill_visible_fields(modal, app_log, page=page)
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
        allow_page_fallback: bool = True,
    ):
        selectors = [
            "button.jobs-apply-button",
            "button.jobs-apply-button--top-card",
            "button[aria-label*='Easy Apply']",
            "button:has-text('Easy Apply')",
            "[data-control-name='jobdetails_topcard_inapply']",
        ]

        if container is not None and container is not page:
            for sel in selectors:
                candidate = container.locator(sel).first
                try:
                    await candidate.wait_for(state="visible", timeout=5000)
                    return candidate
                except Exception:
                    continue

        search_roots = []
        if container is not None:
            search_roots.append(container)
        if allow_page_fallback and page not in search_roots:
            search_roots.append(page)

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
            await self._fill_visible_fields(page, app_log, page=page)
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

        await self._fill_visible_fields(page, app_log, page=page)
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

    async def _fill_visible_fields(
        self,
        container,
        app_log: ApplicationLog,
        page: Optional[Page] = None,
    ) -> None:
        name = self.resume_data.name or APPLICANT_NAME
        email = self.resume_data.email or APPLICANT_EMAIL
        phone = self.phone_number or self.resume_data.phone or APPLICANT_PHONE
        country_code = self.country_code

        await self._safe_fill(container, 'input[name*="name" i], input[aria-label*="name" i]', name)
        await self._safe_fill(container, 'input[type="email"], input[name*="email" i]', email)
        await self._safe_fill(container, 'input[type="tel"], input[name*="phone" i]', phone)
        await self._safe_fill(
            container,
            'input[name*="country" i], input[name*="dial" i], input[aria-label*="country code" i], input[placeholder*="country code" i]',
            country_code,
        )

        question_inputs = container.locator(
            "input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([type='submit']):not([type='button']), "
            "textarea:not([name*='cover' i])"
        )
        count = await question_inputs.count()
        for idx in range(count):
            inp = question_inputs.nth(idx)
            if not await self._is_visible(inp, timeout=250):
                continue

            label = await self._get_label(container, inp)
            field_context = await self._get_field_context(container, inp, label)
            placeholder = await self._locator_attribute(inp, "placeholder")
            input_type = (await self._locator_attribute(inp, "type")).lower()
            input_mode = (await self._locator_attribute(inp, "inputmode")).lower()
            existing = await self._get_field_value(inp)
            if existing.strip():
                continue

            direct_value = self._contact_value_for_label(
                field_context or label,
                name,
                email,
                phone,
                country_code,
            )
            if direct_value:
                try:
                    await inp.fill(direct_value)
                    await human_delay(0.2, 0.4)
                except Exception:
                    pass
                continue

            field_label = field_context or label or placeholder or "Screening question"
            validation_text = await self._get_field_validation_text(inp)
            field_type = self._classify_field(
                field_label,
                placeholder=placeholder,
                input_type=input_type,
                input_mode=input_mode,
                validation_text=validation_text,
            )

            if field_type == "text" and not field_label:
                continue

            if self._is_numeric_field_type(field_type):
                value = self._rule_based_field_value(
                    field_type,
                    field_label,
                    validation_text=validation_text,
                )
            elif field_type == "text":
                value = await generate_answer(field_label, self.resume_data, self.job_description)
            else:
                value = self._rule_based_field_value(
                    field_type,
                    field_label,
                    validation_text=validation_text,
                )

            if not value:
                continue

            value = self._sanitize_field_value(
                value,
                field_type=field_type,
                label=field_label,
                validation_text=validation_text,
            )
            logger.info("Field: %s | Type: %s | Value: %s", field_label, field_type, value)

            try:
                if self._is_numeric_field_type(field_type):
                    await inp.fill("")
                await inp.fill(value)
                await human_delay(0.3, 0.7)
            except Exception:
                continue

            retry_error = await self._get_field_validation_text(inp)
            if retry_error:
                retry_type = self._classify_field(
                    field_label,
                    placeholder=placeholder,
                    input_type=input_type,
                    input_mode=input_mode,
                    validation_text=retry_error,
                )
                if self._is_numeric_field_type(retry_type):
                    retry_value = self._rule_based_field_value(
                        retry_type,
                        field_label,
                        validation_text=retry_error,
                    )
                    retry_value = self._sanitize_field_value(
                        retry_value or value,
                        field_type=retry_type,
                        label=field_label,
                        validation_text=retry_error,
                    ) or "1"
                    try:
                        await inp.fill("")
                        await inp.fill(retry_value)
                        await human_delay(0.3, 0.5)
                        value = retry_value
                        field_type = retry_type
                    except Exception:
                        pass

            app_log.answers[field_label] = value

        await self._handle_select_fields(container, app_log, page=page)

    async def _handle_select_fields(
        self,
        container,
        app_log: ApplicationLog,
        page: Optional[Page] = None,
    ) -> None:
        selects = container.locator("select")
        count = await selects.count()
        for idx in range(count):
            sel = selects.nth(idx)
            if not await self._is_visible(sel, timeout=250):
                continue

            label = await self._get_label(container, sel)
            label = await self._get_field_context(container, sel, label) or label
            option_texts = await self._native_select_options(sel)

            if not option_texts or not label:
                continue

            best_val, answer = await self._resolve_select_answer(label, option_texts)
            if not best_val:
                continue

            try:
                await sel.select_option(value=best_val)
                await human_delay(0.3, 0.7)
            except Exception:
                continue

            if not await self._native_select_matches(sel, best_val, answer):
                try:
                    await sel.select_option(label=answer)
                    await human_delay(0.2, 0.4)
                except Exception:
                    pass
            if not await self._native_select_matches(sel, best_val, answer):
                continue

            app_log.answers[label] = answer

        if page is not None:
            await self._handle_combobox_fields(container, page, app_log)

    async def _handle_combobox_fields(
        self,
        container,
        page: Page,
        app_log: ApplicationLog,
    ) -> None:
        combos = container.locator(
            "[role='combobox'][aria-expanded], [role='combobox'][aria-controls], "
            "button[aria-haspopup='listbox'], div[aria-haspopup='listbox']"
        )
        count = await combos.count()
        for idx in range(count):
            combo = combos.nth(idx)
            if not await self._is_visible(combo, timeout=250):
                continue
            if await self._element_tag_name(combo) == "select":
                continue

            label = await self._get_label(container, combo)
            label = await self._get_field_context(container, combo, label) or label
            if not label:
                continue

            option_records = await self._open_combobox_and_collect_options(combo, page)
            if not option_records:
                continue

            option_texts = [(str(opt_idx), text) for opt_idx, (_, text) in enumerate(option_records)]
            selected_idx, answer = await self._resolve_option_choice(label, option_texts)
            if not answer:
                await self._dismiss_combobox(page)
                continue

            try:
                option_locator = option_records[int(selected_idx)][0]
            except Exception:
                await self._dismiss_combobox(page)
                continue

            try:
                await option_locator.click(timeout=2500)
                await human_delay(0.2, 0.5)
            except Exception:
                await self._dismiss_combobox(page)
                continue

            if not await self._combobox_value_matches(combo, answer):
                await self._dismiss_combobox(page)
                option_records = await self._open_combobox_and_collect_options(combo, page)
                retry_locator = self._find_option_locator_by_text(option_records, answer)
                if retry_locator is None:
                    await self._dismiss_combobox(page)
                    continue
                try:
                    await retry_locator.click(timeout=2500)
                    await human_delay(0.2, 0.5)
                except Exception:
                    await self._dismiss_combobox(page)
                    continue

            if not await self._combobox_value_matches(combo, answer):
                await self._dismiss_combobox(page)
                continue

            app_log.answers[label] = answer

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

            chosen, answer = await self._resolve_radio_answer(label, options)
            if chosen is None or not answer:
                continue

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

    async def _native_select_options(self, select) -> list[tuple[str, str]]:
        options = select.locator("option")
        option_count = await options.count()
        option_texts: list[tuple[str, str]] = []
        for opt_idx in range(option_count):
            opt = options.nth(opt_idx)
            val = await opt.get_attribute("value") or ""
            disabled = await opt.get_attribute("disabled")
            text = (await opt.inner_text()).strip()
            if disabled is not None or not text or self._is_placeholder_option(text):
                continue
            option_texts.append((val or text, text))
        return option_texts

    async def _native_select_matches(self, select, expected_value: str, expected_text: str) -> bool:
        try:
            selected = await select.evaluate(
                """(el) => ({
                    value: el.value || '',
                    text: el.options[el.selectedIndex]?.text || ''
                })"""
            )
        except Exception:
            return False

        selected_value = self._normalize_text((selected or {}).get("value", ""))
        selected_text = self._normalize_text((selected or {}).get("text", ""))
        return (
            selected_value == self._normalize_text(expected_value)
            or selected_text == self._normalize_text(expected_text)
        )

    @staticmethod
    def _css_attr_selector(attr: str, value: str) -> str:
        escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
        return f'[{attr}="{escaped}"]'

    async def _open_combobox_and_collect_options(
        self,
        combo,
        page: Page,
    ) -> list[tuple[Locator, str]]:
        try:
            await combo.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await combo.click(timeout=2500)
        except Exception:
            return []

        controls_id = (
            await self._locator_attribute(combo, "aria-controls")
            or await self._locator_attribute(combo, "aria-owns")
        )
        option_selectors: list[str] = []
        if controls_id:
            controlled_selector = self._css_attr_selector("id", controls_id)
            option_selectors.append(
                ", ".join(
                    (
                        f"{controlled_selector} [role='option']",
                        f"{controlled_selector}[role='option']",
                        f"{controlled_selector} option",
                        f"{controlled_selector} li",
                    )
                )
            )

        option_selectors.append(
            ", ".join(
                (
                    "[role='listbox'] [role='option']",
                    "[role='listbox'] li",
                    "[role='option']",
                    "li[role='option']",
                )
            )
        )
        option_locator = page.locator(", ".join(option_selectors))
        for _ in range(6):
            option_count = await option_locator.count()
            option_records: list[tuple[Locator, str]] = []
            for opt_idx in range(min(option_count, 40)):
                option = option_locator.nth(opt_idx)
                if not await self._is_visible(option, timeout=120):
                    continue
                try:
                    text = " ".join((await option.inner_text()).strip().split())
                except Exception:
                    text = ""
                if not text or self._is_placeholder_option(text):
                    continue
                option_records.append((option, text))
            if option_records:
                return option_records
            await human_delay(0.1, 0.2)

        await self._dismiss_combobox(page)
        return []

    async def _dismiss_combobox(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _combobox_value_matches(self, combo, expected_text: str) -> bool:
        normalized_expected = self._normalize_text(expected_text)
        if not normalized_expected:
            return False

        value_candidates: list[str] = []
        for attr in ("value", "aria-label", "innerText", "textContent"):
            try:
                if attr in {"innerText", "textContent"}:
                    raw = await combo.evaluate(f"(el) => el.{attr} || ''")
                else:
                    raw = await combo.get_attribute(attr) or ""
            except Exception:
                raw = ""
            normalized = self._normalize_text(raw)
            if normalized:
                value_candidates.append(normalized)

        return any(
            normalized_expected == candidate or normalized_expected in candidate
            for candidate in value_candidates
        )

    def _find_option_locator_by_text(
        self,
        option_records: list[tuple[Locator, str]],
        expected_text: str,
    ) -> Optional[Locator]:
        normalized_expected = self._normalize_text(expected_text)
        for option, text in option_records:
            normalized = self._normalize_text(text)
            if normalized == normalized_expected or normalized_expected in normalized:
                return option
        return None

    @staticmethod
    async def _element_tag_name(locator) -> str:
        try:
            return (await locator.evaluate("(el) => el.tagName || ''")).strip().lower()
        except Exception:
            return ""

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

    async def _resolve_select_answer(
        self,
        label: str,
        option_texts: list[tuple[str, str]],
    ) -> tuple[str, str]:
        selected = await self._resolve_option_choice(label, option_texts)
        logger.info("Field: %s | Type: select | Value: %s", label, selected[1])
        return selected

    async def _resolve_option_choice(
        self,
        label: str,
        option_texts: list[tuple[str, str]],
    ) -> tuple[str, str]:
        rule_based = self._rule_based_option_choice(label, option_texts)
        if rule_based is not None:
            return rule_based

        answer = await self._generate_option_answer(label, option_texts)
        matched = self._match_option(answer, option_texts)
        if matched is not None:
            return matched

        return option_texts[0]

    async def _generate_option_answer(
        self,
        label: str,
        option_texts: list[tuple[str, str]],
    ) -> str:
        choices = ", ".join(text for _, text in option_texts)
        question = (
            f"{label}\n"
            f"Options: {choices}\n"
            "Respond with exactly one option from the list above. "
            "Do not add any explanation or extra words."
        )
        return await generate_answer(question, self.resume_data, self.job_description)

    @classmethod
    def _match_option(
        cls,
        answer: str,
        option_texts: list[tuple[str, str]],
    ) -> Optional[tuple[str, str]]:
        answer_norm = cls._normalize_text(answer)
        if not answer_norm:
            return None

        for value, text in option_texts:
            if cls._normalize_text(text) == answer_norm:
                return value, text

        for value, text in option_texts:
            option_norm = cls._normalize_text(text)
            if option_norm and (option_norm in answer_norm or answer_norm in option_norm):
                return value, text

        if cls._is_yes_no_options(option_texts):
            if answer_norm.startswith(("yes", "y", "true")):
                return cls._yes_like_option(option_texts)
            if answer_norm.startswith(("no", "n", "false")):
                return cls._no_like_option(option_texts)

        return None

    async def _resolve_radio_answer(
        self,
        label: str,
        options: list[tuple[Locator, str]],
    ) -> tuple[Optional[Locator], str]:
        option_texts = [(str(idx), text) for idx, (_, text) in enumerate(options)]
        selected_idx, selected_answer = await self._resolve_option_choice(label, option_texts)
        logger.info("Field: %s | Type: radio | Value: %s", label, selected_answer)
        return options[int(selected_idx)][0], selected_answer

    async def _get_field_validation_text(self, field) -> str:
        for ancestor_xpath in (
            "xpath=ancestor::*[contains(@class,'fb-form-element')][1]",
            "xpath=ancestor::*[contains(@class,'jobs-easy-apply-form-section__grouping')][1]",
            "xpath=ancestor::div[1]",
        ):
            container = field.locator(ancestor_xpath).first
            if not await self._is_visible(container, timeout=100):
                continue

            errors = container.locator(
                ".artdeco-inline-feedback__message, .artdeco-inline-feedback--error"
            )
            count = await errors.count()
            for idx in range(min(count, 3)):
                error = errors.nth(idx)
                if not await self._is_visible(error, timeout=100):
                    continue
                try:
                    text = (await error.inner_text()).strip()
                except Exception:
                    text = ""
                if text:
                    return text

        aria_invalid = await self._locator_attribute(field, "aria-invalid")
        return "invalid" if aria_invalid == "true" else ""

    @staticmethod
    def _is_numeric_field_type(field_type: str) -> bool:
        return field_type in {"salary", "experience", "notice_days", "numeric"}

    @classmethod
    def _classify_field(
        cls,
        label: str,
        placeholder: str = "",
        input_type: str = "",
        input_mode: str = "",
        validation_text: str = "",
    ) -> str:
        text = " ".join(
            part for part in (label, placeholder, validation_text) if part
        ).lower()
        input_type = (input_type or "").lower()
        input_mode = (input_mode or "").lower()

        if any(token in text for token in ("ctc", "salary", "compensation", "package", "lpa")):
            return "salary"
        if cls._is_immediate_joiner_label(text):
            return "immediate_joiner"
        if cls._is_notice_label(text):
            return "notice_days"
        if any(token in text for token in ("overall exp", "overall experience", "years of experience", "years of exp", "experience")):
            return "experience"
        if input_type in {"number", "range"} or input_mode in {"numeric", "decimal"}:
            return "numeric"
        if "decimal number" in text or "whole number" in text:
            return "numeric"
        if any(token in text for token in ("how many", "number", "days", "day", "months", "month")):
            return "numeric"
        if re.search(r"\bexp\b", text) or re.search(r"\byears?\b", text):
            return "numeric"
        return "text"

    def _rule_based_field_value(
        self,
        field_type: str,
        label: str,
        validation_text: str = "",
    ) -> str:
        label_lower = label.lower()

        if field_type == "salary":
            return self._format_salary(
                self._salary_value_for_label(label_lower),
                label,
                validation_text=validation_text,
            )

        if field_type == "experience":
            return self._format_number(
                self._experience_value_for_label(label_lower),
                label,
                validation_text=validation_text,
            )

        if field_type == "notice_days":
            return self._format_number(
                self._notice_value_for_label(label_lower),
                label,
                validation_text=validation_text,
            )

        if field_type == "immediate_joiner":
            return self._immediate_joiner_answer()

        if field_type == "numeric":
            if any(token in label_lower for token in ("salary", "ctc", "compensation", "package", "lpa")):
                return self._format_salary(
                    self._salary_value_for_label(label_lower),
                    label,
                    validation_text=validation_text,
                )
            if self._is_notice_label(label_lower):
                return self._format_number(
                    self._notice_value_for_label(label_lower),
                    label,
                    validation_text=validation_text,
                )
            if any(token in label_lower for token in ("experience", "exp", "year", "years")):
                return self._format_number(
                    self._experience_value_for_label(label_lower),
                    label,
                    validation_text=validation_text,
                )
            return self._format_number(1, label, validation_text=validation_text)

        return ""

    def _rule_based_option_choice(
        self,
        label: str,
        option_texts: list[tuple[str, str]],
    ) -> Optional[tuple[str, str]]:
        label_lower = label.lower()

        if self._is_immediate_joiner_label(label_lower):
            return self._boolean_option_choice(option_texts, self.is_immediate_joiner)

        if self._is_yes_no_options(option_texts):
            experience_choice = self._experience_yes_no_choice(label_lower, option_texts)
            if experience_choice is not None:
                return experience_choice

        if "language" in label_lower:
            for value, text in option_texts:
                if "english" in text.lower():
                    return value, text

        if any(
            token in label_lower
            for token in ("hybrid", "relocation", "relocate", "comfortable", "willing", "authorized", "legal", "work permit", "commute", "travel")
        ):
            yes_like = self._yes_like_option(option_texts)
            if yes_like is not None:
                return yes_like

        field_type = self._classify_field(label)
        if field_type in {"salary", "experience", "notice_days", "numeric"}:
            numeric_value = self._rule_based_field_value(field_type, label)
            numeric_match = self._match_numeric_option(option_texts, numeric_value)
            if numeric_match is not None:
                return numeric_match

        return None

    @staticmethod
    def _yes_like_option(option_texts: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
        yes_tokens = ("yes", "y", "comfortable", "willing", "sure", "ok", "okay")
        for value, text in option_texts:
            normalized = text.strip().lower()
            if normalized in yes_tokens or normalized.startswith("yes"):
                return value, text
        return None

    @staticmethod
    def _no_like_option(option_texts: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
        no_tokens = ("no", "n", "not now", "nope")
        for value, text in option_texts:
            normalized = text.strip().lower()
            if normalized in no_tokens or normalized.startswith("no"):
                return value, text
        return None

    def _boolean_option_choice(
        self,
        option_texts: list[tuple[str, str]],
        desired: bool,
    ) -> Optional[tuple[str, str]]:
        if desired:
            return self._yes_like_option(option_texts)
        return self._no_like_option(option_texts)

    def _experience_yes_no_choice(
        self,
        label_lower: str,
        option_texts: list[tuple[str, str]],
    ) -> Optional[tuple[str, str]]:
        if not any(
            token in label_lower
            for token in (
                "experience",
                "worked with",
                "work with",
                "hands-on",
                "hands on",
                "knowledge of",
                "familiar with",
                "proficient",
                "expertise",
                "using",
                "designing",
                "building",
                "deploying",
                "integrated",
                "developing",
                "apis",
                "api",
            )
        ):
            return None

        resume_match = self._resume_supports_question(label_lower)
        if resume_match is True:
            return self._yes_like_option(option_texts)
        if resume_match is False:
            return self._no_like_option(option_texts)
        return None

    @classmethod
    def _is_yes_no_options(cls, option_texts: list[tuple[str, str]]) -> bool:
        normalized = {
            cls._normalize_text(text)
            for _, text in option_texts
            if cls._normalize_text(text) and not cls._is_placeholder_option(text)
        }
        return normalized == {"yes", "no"}

    def _resume_supports_question(self, label_lower: str) -> Optional[bool]:
        resume_text = self._resume_search_text()
        if not resume_text:
            return None

        for skill in self.resume_data.skills:
            skill_norm = self._normalize_text(skill)
            if skill_norm and skill_norm in label_lower:
                return True

        keywords = self._question_keywords(label_lower)
        if not keywords:
            return None

        matches = [keyword for keyword in keywords if keyword in resume_text]
        if matches:
            return True

        technical_keywords = [keyword for keyword in keywords if keyword not in {"experience", "worked", "work", "using"}]
        if technical_keywords:
            return False
        return None

    def _resume_search_text(self) -> str:
        parts = [
            self.resume_data.summary,
            self.resume_data.raw_text,
            " ".join(self.resume_data.skills),
        ]
        for exp in self.resume_data.experience:
            parts.extend([exp.title, exp.company, exp.description])
        return self._normalize_text(" ".join(part for part in parts if part))

    @classmethod
    def _question_keywords(cls, text: str) -> list[str]:
        tokens = [
            token
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9+#./-]{1,}", text.lower())
            if token not in {
                "do", "you", "have", "with", "for", "the", "and", "or", "in", "of",
                "to", "using", "use", "worked", "work", "experience", "designing",
                "building", "deploying", "operating", "professional", "services",
                "years", "year", "would", "your", "this", "that", "from",
            }
        ]
        seen: list[str] = []
        for token in tokens:
            if token not in seen:
                seen.append(token)
        return seen

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip()).lower()

    @staticmethod
    def _is_placeholder_option(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", (text or "").strip()).lower()
        return normalized in {"select", "select an option", "choose", "choose one", "please select"}

    @staticmethod
    def _match_numeric_option(
        option_texts: list[tuple[str, str]],
        target_value: str,
    ) -> Optional[tuple[str, str]]:
        target_number = AutoApplyBot._number_from_text(target_value)
        if target_number is None:
            return None

        best_match: Optional[tuple[str, str]] = None
        best_diff = float("inf")
        for value, text in option_texts:
            numbers = [float(match) for match in re.findall(r"\d+(?:\.\d+)?", text)]
            if not numbers:
                continue
            if len(numbers) >= 2:
                low, high = min(numbers[0], numbers[1]), max(numbers[0], numbers[1])
                if low <= target_number <= high:
                    return value, text
            closest = min(numbers, key=lambda num: abs(num - target_number))
            diff = abs(closest - target_number)
            if diff < best_diff:
                best_diff = diff
                best_match = (value, text)

        return best_match

    def _salary_value_for_label(self, label_lower: str) -> float:
        if any(token in label_lower for token in ("expected", "expecting", "desired")):
            return self.expected_ctc or self.current_ctc or 1.0
        if any(token in label_lower for token in ("current", "present", "existing", "fixed", "fix", "annual")):
            return self.current_ctc or self.expected_ctc or 1.0
        return self.expected_ctc or self.current_ctc or 1.0

    def _experience_value_for_label(self, label_lower: str) -> float:
        if (self.total_experience or 0) > 0:
            return float(self.total_experience or 0)

        years = self._estimate_total_experience_years()
        if years > 0:
            return years

        if any(skill.lower() in label_lower for skill in self.resume_data.skills):
            return max(years, 1.0)

        return max(years, 1.0)

    def _notice_value_for_label(self, label_lower: str) -> float:
        notice_days = self.notice_days if self.notice_days is not None else 30.0
        notice_days = notice_days if notice_days >= 0 else 0.0
        if any(token in label_lower for token in ("month", "months")):
            return max(round(notice_days / 30.0, 2), 0.0)
        return notice_days

    def _immediate_joiner_answer(self) -> str:
        return "Yes" if self.is_immediate_joiner else "No"

    def _estimate_total_experience_years(self) -> float:
        text = " ".join(
            part for part in (self.resume_data.summary, self.resume_data.raw_text) if part
        )
        matches = [
            float(match)
            for match in re.findall(r"(\d+(?:\.\d+)?)\+?\s+(?:years?|yrs?)", text, flags=re.I)
        ]
        if matches:
            return max(matches)

        current_year = date.today().year
        ranges: list[float] = []
        starts: list[int] = []
        for exp in self.resume_data.experience:
            duration = exp.duration or ""
            years = [int(year) for year in re.findall(r"(?:19|20)\d{2}", duration)]
            if years:
                start = years[0]
                end = years[1] if len(years) > 1 else current_year
                if re.search(r"present|current", duration, flags=re.I):
                    end = current_year
                starts.append(start)
                ranges.append(max(float(end - start), 0.0))

        if starts:
            ranges.append(max(float(current_year - min(starts)), 0.0))

        if ranges:
            return max(ranges)

        return float(len(self.resume_data.experience))

    def _sanitize_field_value(
        self,
        value: str,
        field_type: str,
        label: str,
        validation_text: str = "",
    ) -> str:
        if field_type == "text":
            return " ".join(str(value).strip().split())
        if field_type == "immediate_joiner":
            return self._immediate_joiner_answer()

        numeric = self._ensure_numeric_value(str(value))
        if numeric is None:
            numeric = 1.0
        if numeric <= 0 and "larger than 0.0" in validation_text.lower():
            numeric = 1.0
        return self._format_number(numeric, label, validation_text=validation_text)

    def _format_salary(self, value: float, label: str, validation_text: str = "") -> str:
        label_lower = label.lower()
        if any(token in label_lower for token in ("lpa", "lakh", "lakhs")) and value > 1000:
            value = value / 100000.0
        return self._format_number(value, label, validation_text=validation_text)

    @staticmethod
    def _format_number(value: float, label: str, validation_text: str = "") -> str:
        allow_decimal = (
            "decimal" in validation_text.lower()
            or any(token in label.lower() for token in ("lpa", "salary", "ctc", "compensation"))
        )
        if allow_decimal:
            formatted = f"{float(value):.2f}".rstrip("0").rstrip(".")
            return formatted or "0"
        rounded = int(round(float(value)))
        return str(max(rounded, 0))

    @staticmethod
    def _number_from_text(text: str) -> Optional[float]:
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @classmethod
    def _ensure_numeric_value(cls, text: str) -> Optional[float]:
        return cls._number_from_text(text)

    @staticmethod
    def _is_immediate_joiner_label(text: str) -> bool:
        normalized = text.lower()
        phrases = (
            "immediate join",
            "immediate joine",
            "join immediately",
            "start immediately",
            "available to join immediately",
            "are you available to join immediately",
            "can you start immediately",
            "can you join immediately",
            "immediate availability",
            "immediate joiner",
        )
        return any(phrase in normalized for phrase in phrases)

    @classmethod
    def _is_notice_label(cls, text: str) -> bool:
        normalized = text.lower()
        if cls._is_immediate_joiner_label(normalized):
            return False
        if any(token in normalized for token in ("notice period", "serving notice", "notice")):
            return True
        join_prompts = (
            "how soon can you join",
            "when can you join",
            "joining in",
            "join in",
            "earliest you can join",
        )
        time_tokens = ("day", "days", "month", "months", "period", "within", "timeline")
        return any(prompt in normalized for prompt in join_prompts) and any(
            token in normalized for token in time_tokens
        )

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

        detail_panel = await self._fresh_linkedin_detail_panel(page)
        clicked_card = False
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
                    await self._click_linkedin_card(page, card, expected_job_id=target_id)
                    clicked_card = True
                    break

                link = card.locator("a[href*='/jobs/view/'], a[href*='currentJobId=']").first
                href = await self._locator_attribute(link, "href")
                if target_id and target_id in href:
                    await self._click_linkedin_card(page, card, expected_job_id=target_id)
                    clicked_card = True
                    break

        if clicked_card or detail_panel is None:
            detail_panel = await self._fresh_linkedin_detail_panel(page)

        if detail_panel is None:
            cards = page.locator(".jobs-search-results__list-item, .job-card-container, [data-job-id]")
            if await cards.count() > 0:
                fallback_id = await self._locator_attribute(cards.first, "data-job-id")
                await self._click_linkedin_card(page, cards.first, expected_job_id=fallback_id)
                detail_panel = await self._fresh_linkedin_detail_panel(page)

        if detail_panel is not None:
            return detail_panel

        detail_panel = page.locator(
            ".jobs-search__job-details, .jobs-details, .jobs-details__main-content"
        ).first
        if await self._is_visible(detail_panel, timeout=2000):
            return detail_panel
        return page

    async def _fresh_linkedin_detail_panel(self, page: Page) -> Optional[Locator]:
        detail_panel = page.locator(
            ".jobs-search__job-details, .jobs-details, .jobs-details__main-content"
        ).first
        if await self._is_visible(detail_panel, timeout=2000):
            return detail_panel
        return None

    async def _prime_linkedin_detail_panel(self, page: Page, panel) -> None:
        try:
            await panel.scroll_into_view_if_needed()
        except Exception:
            pass
        for _ in range(2):
            try:
                await panel.evaluate("(el) => el.scrollBy(0, 900)")
            except Exception:
                break
            await human_delay(0.2, 0.4)
        try:
            await page.mouse.wheel(0, 1000)
        except Exception:
            pass
        await human_delay(0.6, 1.0)

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

    async def _click_linkedin_card(self, page: Page, card, expected_job_id: str = "") -> None:
        previous_title = await self._current_linkedin_detail_title(page)
        if not expected_job_id:
            expected_job_id = await self._locator_attribute(card, "data-job-id")
        if not expected_job_id:
            href = await self._locator_attribute(
                card.locator("a[href*='/jobs/view/'], a[href*='currentJobId=']").first,
                "href",
            )
            expected_job_id = self._extract_linkedin_job_id(href)
        click_target = card.locator("a, button").first
        if await self._click_if_enabled(click_target):
            await self._wait_for_linkedin_job_change(page, previous_title, expected_job_id)
            return
        try:
            await card.click(timeout=2500)
        except Exception:
            return
        await self._wait_for_linkedin_job_change(page, previous_title, expected_job_id)

    @staticmethod
    def _extract_linkedin_job_id(url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"[?&]currentJobId=(\d+)", url)
        if match:
            return match.group(1)
        return ""

    async def _get_field_context(self, container, element, label: str = "") -> str:
        texts: list[str] = []
        for candidate in (label, await self._locator_attribute(element, "aria-label")):
            normalized = " ".join(candidate.strip().split())
            if normalized and normalized not in texts:
                texts.append(normalized)

        group_locators = [
            element.locator("xpath=ancestor::*[contains(@class,'fb-form-element')][1]").first,
            element.locator("xpath=ancestor::*[contains(@class,'jobs-easy-apply-form-section__grouping')][1]").first,
            element.locator("xpath=ancestor::label[1]").first,
        ]
        for group in group_locators:
            if not await self._is_visible(group, timeout=120):
                continue
            prompt_nodes = group.locator(
                "label, legend, span, p, div[class*='label'], div[class*='question'], "
                "div[class*='title'], div[class*='prompt']"
            )
            count = await prompt_nodes.count()
            for idx in range(min(count, 8)):
                node = prompt_nodes.nth(idx)
                if not await self._is_visible(node, timeout=80):
                    continue
                try:
                    text = " ".join((await node.inner_text()).strip().split())
                except Exception:
                    text = ""
                if not text:
                    continue
                lowered = text.lower()
                if "enter a decimal number" in lowered or "required" == lowered:
                    continue
                if text not in texts:
                    texts.append(text)

        return " ".join(texts).strip()

    async def _current_linkedin_detail_title(self, page: Page) -> str:
        detail_panel = page.locator(
            ".jobs-search__job-details, .jobs-details, .jobs-details__main-content"
        ).first
        title = detail_panel.locator(
            "h1, .job-details-jobs-unified-top-card__job-title, .jobs-unified-top-card__job-title"
        ).first
        if await self._is_visible(title, timeout=500):
            try:
                return " ".join((await title.inner_text()).strip().split())
            except Exception:
                return ""
        return ""

    async def _wait_for_linkedin_job_change(
        self,
        page: Page,
        previous_title: str,
        expected_job_id: str = "",
    ) -> None:
        try:
            await page.wait_for_selector(
                ".jobs-search__job-details, .jobs-details, .jobs-details__main-content",
                timeout=5000,
            )
        except Exception:
            pass

        try:
            await page.wait_for_function(
                """
                ({ oldTitle, expectedId }) => {
                    const root = document.querySelector(
                        '.jobs-search__job-details, .jobs-details, .jobs-details__main-content'
                    );
                    if (!root) return false;
                    const titleEl = root.querySelector(
                        'h1, .job-details-jobs-unified-top-card__job-title, .jobs-unified-top-card__job-title'
                    );
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
        await self._wait_for_linkedin_transition(page)

    @staticmethod
    def _contact_value_for_label(
        label: str,
        name: str,
        email: str,
        phone: str,
        country_code: str,
    ) -> str:
        normalized = label.lower()
        if any(token in normalized for token in ("email", "e-mail", "mail")):
            return email
        if any(token in normalized for token in ("country code", "dial code", "calling code")):
            return country_code
        if any(token in normalized for token in ("phone", "mobile", "contact number")):
            return phone
        if "name" in normalized:
            return name
        return ""
