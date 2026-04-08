from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from backend.config import (
    INDEED_EMAIL,
    INDEED_PASSWORD,
    LINKEDIN_EMAIL,
    LINKEDIN_PASSWORD,
    MAX_DELAY,
    MIN_DELAY,
    NAUKRI_EMAIL,
    NAUKRI_PASSWORD,
    PROXY_URL,
    SESSIONS_DIR,
)
from backend.logger import logger
from backend.models import JobPortal

PORTAL_URLS = {
    JobPortal.LINKEDIN: "https://www.linkedin.com/login",
    JobPortal.INDEED: "https://secure.indeed.com/auth",
    JobPortal.NAUKRI: "https://login.naukri.com/nLogin/Login.php",
}

PORTAL_CREDENTIALS = {
    JobPortal.LINKEDIN: (LINKEDIN_EMAIL, LINKEDIN_PASSWORD),
    JobPortal.INDEED: (INDEED_EMAIL, INDEED_PASSWORD),
    JobPortal.NAUKRI: (NAUKRI_EMAIL, NAUKRI_PASSWORD),
}

_playwright_instance = None
_browser = None


async def get_browser():
    global _playwright_instance, _browser
    if _browser is None:
        _playwright_instance = await async_playwright().start()
        launch_args: dict = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }
        if PROXY_URL:
            launch_args["proxy"] = {"server": PROXY_URL}
        _browser = await _playwright_instance.chromium.launch(**launch_args)
    return _browser


def _cookies_path(portal: JobPortal) -> Path:
    return SESSIONS_DIR / f"{portal.value}_cookies.json"


async def save_cookies(context: BrowserContext, portal: JobPortal) -> None:
    cookies = await context.cookies()
    path = _cookies_path(portal)
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    logger.info("Saved %d cookies for %s", len(cookies), portal.value)


async def load_cookies(context: BrowserContext, portal: JobPortal) -> bool:
    path = _cookies_path(portal)
    if not path.exists():
        return False
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        await context.add_cookies(cookies)
        logger.info("Loaded %d cookies for %s", len(cookies), portal.value)
        return True
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load cookies for %s: %s", portal.value, exc)
        return False


async def create_context(portal: JobPortal) -> BrowserContext:
    browser = await get_browser()
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    await load_cookies(context, portal)
    return context


async def human_delay(min_s: float | None = None, max_s: float | None = None) -> None:
    """Sleep for a random human-like interval."""
    import random
    lo = min_s if min_s is not None else MIN_DELAY
    hi = max_s if max_s is not None else MAX_DELAY
    await asyncio.sleep(random.uniform(lo, hi))


async def _is_logged_in(page: Page, portal: JobPortal) -> bool:
    """Quick heuristic to detect if we're on a logged-in page."""
    url = page.url.lower()
    if portal == JobPortal.LINKEDIN:
        return "feed" in url or "mynetwork" in url or "jobs" in url
    if portal == JobPortal.INDEED:
        return "myjobs" in url or "account" in url
    if portal == JobPortal.NAUKRI:
        return "homepage" in url or "mnjuser" in url
    return False


async def login_linkedin(page: Page) -> bool:
    email, password = PORTAL_CREDENTIALS[JobPortal.LINKEDIN]
    if not email or not password:
        logger.warning("LinkedIn credentials not set — pausing for manual login")
        return await _wait_for_manual_login(page, JobPortal.LINKEDIN)

    await page.goto(PORTAL_URLS[JobPortal.LINKEDIN], wait_until="domcontentloaded")
    await human_delay(1, 2)

    await page.fill("#username", email)
    await human_delay(0.5, 1)
    await page.fill("#password", password)
    await human_delay(0.5, 1)
    await page.click('button[type="submit"]')
    await human_delay(3, 5)

    if await _is_logged_in(page, JobPortal.LINKEDIN):
        return True

    logger.warning("LinkedIn auto-login may require verification — pausing for manual input")
    return await _wait_for_manual_login(page, JobPortal.LINKEDIN)


async def login_indeed(page: Page) -> bool:
    email, password = PORTAL_CREDENTIALS[JobPortal.INDEED]
    if not email or not password:
        logger.warning("Indeed credentials not set — pausing for manual login")
        return await _wait_for_manual_login(page, JobPortal.INDEED)

    await page.goto(PORTAL_URLS[JobPortal.INDEED], wait_until="domcontentloaded")
    await human_delay(1, 2)

    email_input = page.locator('input[type="email"], input[name="__email"]').first
    await email_input.fill(email)
    await human_delay(0.5, 1)

    submit_btn = page.locator('button[type="submit"]').first
    await submit_btn.click()
    await human_delay(2, 3)

    pw_input = page.locator('input[type="password"]').first
    if await pw_input.is_visible():
        await pw_input.fill(password)
        await human_delay(0.5, 1)
        await page.locator('button[type="submit"]').first.click()
        await human_delay(3, 5)

    if await _is_logged_in(page, JobPortal.INDEED):
        return True

    return await _wait_for_manual_login(page, JobPortal.INDEED)


async def login_naukri(page: Page) -> bool:
    email, password = PORTAL_CREDENTIALS[JobPortal.NAUKRI]
    if not email or not password:
        logger.warning("Naukri credentials not set — pausing for manual login")
        return await _wait_for_manual_login(page, JobPortal.NAUKRI)

    await page.goto(PORTAL_URLS[JobPortal.NAUKRI], wait_until="domcontentloaded")
    await human_delay(1, 2)

    await page.fill('input[placeholder*="Email"], input[type="text"]', email)
    await human_delay(0.5, 1)
    await page.fill('input[type="password"]', password)
    await human_delay(0.5, 1)
    await page.click('button[type="submit"]')
    await human_delay(3, 5)

    if await _is_logged_in(page, JobPortal.NAUKRI):
        return True

    return await _wait_for_manual_login(page, JobPortal.NAUKRI)


async def _wait_for_manual_login(page: Page, portal: JobPortal, timeout: int = 120) -> bool:
    """
    Wait up to `timeout` seconds for the user to complete manual login.
    The browser window stays open — the user logs in manually.
    """
    logger.info(
        "Waiting up to %ds for manual login on %s — please log in via the browser window",
        timeout, portal.value,
    )
    for _ in range(timeout // 3):
        await asyncio.sleep(3)
        if await _is_logged_in(page, portal):
            logger.info("Manual login detected for %s", portal.value)
            return True
    logger.error("Manual login timed out for %s", portal.value)
    return False


LOGIN_HANDLERS = {
    JobPortal.LINKEDIN: login_linkedin,
    JobPortal.INDEED: login_indeed,
    JobPortal.NAUKRI: login_naukri,
}


async def ensure_logged_in(page: Page, portal: JobPortal) -> bool:
    if await _is_logged_in(page, portal):
        logger.info("Already logged in to %s", portal.value)
        return True

    handler = LOGIN_HANDLERS.get(portal)
    if handler is None:
        logger.error("No login handler for %s", portal.value)
        return False

    success = await handler(page)
    if success:
        context = page.context
        await save_cookies(context, portal)
    return success


async def close_browser() -> None:
    global _browser, _playwright_instance
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None
