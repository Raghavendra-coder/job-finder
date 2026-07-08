from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from backend.config import (
    BROWSER_CONNECT_OVER_CDP,
    CHROME_CDP_URL,
    INDEED_EMAIL,
    INDEED_PASSWORD,
    LINKEDIN_EMAIL,
    LINKEDIN_PASSWORD,
    MAX_DELAY,
    MANUAL_LOGIN_TIMEOUT_SECONDS,
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
_managed_context_ids: set[int] = set()
_is_cdp_connection = False
_portal_sessions: dict[JobPortal, BrowserContext] = {}
_portal_login_pages: dict[JobPortal, Page] = {}
_authenticated_portals: set[JobPortal] = set()


async def get_browser():
    global _playwright_instance, _browser, _is_cdp_connection
    if _browser is None:
        _playwright_instance = await async_playwright().start()
        if BROWSER_CONNECT_OVER_CDP:
            logger.info("Connecting to existing Chrome over CDP: %s", CHROME_CDP_URL)
            _browser = await _playwright_instance.chromium.connect_over_cdp(CHROME_CDP_URL)
            _is_cdp_connection = True
        else:
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
            _is_cdp_connection = False
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
    if BROWSER_CONNECT_OVER_CDP and browser.contexts:
        # Reuse the existing Chrome profile context so saved sessions are available.
        context = browser.contexts[0]
        await context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        return context

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    await context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    _managed_context_ids.add(id(context))
    await load_cookies(context, portal)
    return context


def is_managed_context(context: BrowserContext) -> bool:
    return id(context) in _managed_context_ids


async def human_delay(min_s: float | None = None, max_s: float | None = None) -> None:
    """Sleep for a random human-like interval."""
    import random
    lo = min_s if min_s is not None else MIN_DELAY
    hi = max_s if max_s is not None else MAX_DELAY
    await asyncio.sleep(random.uniform(lo, hi))


def is_portal_authenticated(portal: JobPortal) -> bool:
    return portal in _authenticated_portals


def get_authenticated_portals() -> list[JobPortal]:
    return list(_authenticated_portals)


def _login_url_markers(portal: JobPortal) -> tuple[str, ...]:
    if portal == JobPortal.LINKEDIN:
        return ("login", "checkpoint", "authwall", "uas/login")
    if portal == JobPortal.INDEED:
        return ("auth", "login", "account/login")
    if portal == JobPortal.NAUKRI:
        return ("login", "nlogin")
    return ()


async def _is_logged_in(page: Page, portal: JobPortal) -> bool:
    """Detect whether the user is authenticated on a portal."""
    url = page.url.lower()

    if portal == JobPortal.LINKEDIN:
        if any(marker in url for marker in _login_url_markers(portal)):
            return False
        try:
            sign_in = page.locator(
                'a[href*="login"], button:has-text("Sign in"), '
                'a[data-tracking-control-name="guest_homepage-basic_sign-in"]'
            ).first
            if await sign_in.is_visible(timeout=1500):
                return False
        except Exception:
            pass
        try:
            me_nav = page.locator(
                ".global-nav__me, img.global-nav__me-photo, "
                '[data-control-name="nav.settings_and_privacy"], '
                'button[aria-label*="View profile"]'
            ).first
            if await me_nav.is_visible(timeout=2000):
                return True
        except Exception:
            pass
        return "feed" in url or "mynetwork" in url

    if portal == JobPortal.INDEED:
        if any(marker in url for marker in ("auth", "login")):
            return False
        try:
            account_nav = page.locator(
                'a[href*="myjobs"], a[href*="account"], '
                'button:has-text("Sign out"), a:has-text("Sign out")'
            ).first
            if await account_nav.is_visible(timeout=2000):
                return True
        except Exception:
            pass
        return "myjobs" in url or "account/home" in url

    if portal == JobPortal.NAUKRI:
        if "login" in url or "nlogin" in url:
            return False
        try:
            profile_nav = page.locator(
                'a[href*="mnjuser"], .nI-gNb-drawer__bars, #userNameId'
            ).first
            if await profile_nav.is_visible(timeout=2000):
                return True
        except Exception:
            pass
        return "homepage" in url or "mnjuser" in url

    return False


async def login_linkedin(page: Page) -> bool:
    email, password = PORTAL_CREDENTIALS[JobPortal.LINKEDIN]

    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    await human_delay(1, 2)
    if await _is_logged_in(page, JobPortal.LINKEDIN):
        logger.info("LinkedIn session already active")
        return True

    await page.goto(PORTAL_URLS[JobPortal.LINKEDIN], wait_until="domcontentloaded")
    await human_delay(1, 2)

    if not email or not password:
        logger.warning("LinkedIn credentials not set — pausing for manual login")
        return await _wait_for_manual_login(page, JobPortal.LINKEDIN)
    await human_delay(1, 2)

    username_input = page.locator("#username").first
    password_input = page.locator("#password").first
    if not await username_input.is_visible():
        # Login form isn't visible; likely already authenticated or page layout differs.
        if await _is_logged_in(page, JobPortal.LINKEDIN):
            logger.info("LinkedIn already logged in after redirect")
            return True
        logger.warning("LinkedIn login form not visible — waiting for manual login")
        return await _wait_for_manual_login(page, JobPortal.LINKEDIN)

    await username_input.fill(email)
    await human_delay(0.5, 1)
    await password_input.fill(password)
    await human_delay(0.5, 1)
    await page.click('button[type="submit"]')
    await human_delay(3, 5)

    if await _is_logged_in(page, JobPortal.LINKEDIN):
        return True

    logger.warning("LinkedIn auto-login may require verification — pausing for manual input")
    return await _wait_for_manual_login(page, JobPortal.LINKEDIN)


async def login_indeed(page: Page) -> bool:
    email, password = PORTAL_CREDENTIALS[JobPortal.INDEED]

    await page.goto("https://www.indeed.com/", wait_until="domcontentloaded")
    await human_delay(1, 2)
    if await _is_logged_in(page, JobPortal.INDEED):
        logger.info("Indeed session already active")
        return True

    await page.goto(PORTAL_URLS[JobPortal.INDEED], wait_until="domcontentloaded")
    await human_delay(1, 2)

    if not email or not password:
        logger.warning("Indeed credentials not set — pausing for manual login")
        return await _wait_for_manual_login(page, JobPortal.INDEED)
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

    await page.goto("https://www.naukri.com/", wait_until="domcontentloaded")
    await human_delay(1, 2)
    if await _is_logged_in(page, JobPortal.NAUKRI):
        logger.info("Naukri session already active")
        return True

    await page.goto(PORTAL_URLS[JobPortal.NAUKRI], wait_until="domcontentloaded")
    await human_delay(1, 2)

    if not email or not password:
        logger.warning("Naukri credentials not set — pausing for manual login")
        return await _wait_for_manual_login(page, JobPortal.NAUKRI)
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


async def _wait_for_manual_login(
    page: Page,
    portal: JobPortal,
    timeout: int | None = None,
) -> bool:
    """
    Wait up to `timeout` seconds for the user to complete manual login.
    The browser window stays open — the user logs in manually.
    """
    login_url = PORTAL_URLS.get(portal)
    if login_url and not any(marker in page.url.lower() for marker in _login_url_markers(portal)):
        await page.goto(login_url, wait_until="domcontentloaded")
        await human_delay(1, 2)

    wait_timeout = MANUAL_LOGIN_TIMEOUT_SECONDS if timeout is None else timeout
    if wait_timeout <= 0:
        logger.info(
            "Waiting indefinitely for manual login on %s — "
            "please log in via the browser window",
            portal.value,
        )
        while True:
            await asyncio.sleep(3)
            if await _is_logged_in(page, portal):
                logger.info("Manual login detected for %s", portal.value)
                return True

    logger.info(
        "Waiting up to %ds for manual login on %s — please log in via the browser window",
        wait_timeout, portal.value,
    )
    for _ in range(wait_timeout // 3):
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
        _authenticated_portals.add(portal)
    return success


async def login_portals(
    portals: list[JobPortal],
    on_status: Callable[[str], None] | None = None,
) -> dict[JobPortal, bool]:
    """Open browser windows and wait for the user to log in to each portal."""
    emit = on_status or (lambda _: None)
    results: dict[JobPortal, bool] = {}

    for portal in portals:
        if is_portal_authenticated(portal) and portal in _portal_sessions:
            emit(f"Already logged in to {portal.value}")
            results[portal] = True
            continue

        emit(
            f"Opening {portal.value} — sign in via the browser window "
            f"(search will not start until all portals are logged in)"
        )
        ctx = await create_context(portal)
        _portal_sessions[portal] = ctx
        page = await ctx.new_page()
        _portal_login_pages[portal] = page

        success = await ensure_logged_in(page, portal)
        if success:
            await save_cookies(ctx, portal)
            emit(f"Logged in to {portal.value}")
        else:
            emit(f"Login failed for {portal.value}")
        results[portal] = success

    return results


async def get_portal_context(portal: JobPortal) -> BrowserContext:
    if portal in _portal_sessions:
        return _portal_sessions[portal]
    ctx = await create_context(portal)
    _portal_sessions[portal] = ctx
    return ctx


async def release_portal_sessions() -> None:
    global _portal_sessions, _portal_login_pages, _authenticated_portals

    for page in list(_portal_login_pages.values()):
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
    _portal_login_pages.clear()

    for portal, ctx in list(_portal_sessions.items()):
        try:
            if is_managed_context(ctx):
                await save_cookies(ctx, portal)
                await ctx.close()
        except Exception:
            pass
    _portal_sessions.clear()
    _authenticated_portals.clear()
    await close_browser()


async def close_browser() -> None:
    global _browser, _playwright_instance, _managed_context_ids, _is_cdp_connection
    if _browser:
        # In CDP mode this detaches Playwright from the browser connection.
        await _browser.close()
        _browser = None
    _managed_context_ids = set()
    _is_cdp_connection = False
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None
