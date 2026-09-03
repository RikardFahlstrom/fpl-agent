import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional, List

from playwright.async_api import async_playwright

# Configure logging to see what's happening
logger = logging.getLogger("fpl_auth")


def _chromium_launch_options(playwright_executable: str) -> dict[str, str]:
    """Prefer Playwright's managed browser, then fall back to an installed Chrome."""
    override = os.environ.get("FPL_BROWSER_EXECUTABLE", "").strip()
    if override:
        executable = Path(override).expanduser()
        if not executable.is_file():
            raise RuntimeError(
                f"FPL_BROWSER_EXECUTABLE does not point to a browser: {executable}"
            )
        return {"executable_path": str(executable)}

    if Path(playwright_executable).is_file():
        return {}

    for command in (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ):
        executable = shutil.which(command)
        if executable:
            logger.info("Using system browser for FPL authentication: %s", executable)
            return {"executable_path": executable}

    raise RuntimeError(
        "No Chromium browser is available for FPL authentication. Run "
        "`uv run playwright install chromium` in fpl-agent, or set "
        "FPL_BROWSER_EXECUTABLE to an installed Chrome/Chromium binary."
    )


def _headless_browser_enabled() -> bool:
    configured = os.environ.get("FPL_BROWSER_HEADLESS", "").strip().lower()
    if configured:
        if configured in {"1", "true", "yes", "on"}:
            return True
        if configured in {"0", "false", "no", "off"}:
            return False
        raise RuntimeError("FPL_BROWSER_HEADLESS must be true or false when set.")
    return not bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def _visible_login_failure(page) -> str | None:
    known_failures = (
        (
            "Invalid username and/or password",
            "The Premier League rejected the email or password.",
        ),
        (
            "account has been locked",
            "The Premier League account is temporarily locked.",
        ),
        (
            "too many attempts",
            "The Premier League temporarily blocked further login attempts.",
        ),
    )
    for page_text, safe_message in known_failures:
        matches = page.get_by_text(page_text, exact=False)
        for index in range(await matches.count()):
            if await matches.nth(index).is_visible():
                return safe_message
    return None


class FPLAutomation:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.api_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_in: Optional[int] = None
        # Field names only, never values. Lets an operator confirm whether the
        # account service returns a refresh token without logging a secret.
        self.token_response_keys: list[str] = []
        self.failure_reason: str | None = None
        self.base_url = "https://fantasy.premierleague.com"

    async def login_and_get_token(self) -> Optional[str]:
        async with async_playwright() as p:
            launch_options = _chromium_launch_options(p.chromium.executable_path)
            headless = _headless_browser_enabled()
            browser_args = ["--no-sandbox"]
            if not headless:
                browser_args.extend(
                    [
                        "--start-minimized",
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                    ]
                )
            logger.info(
                "Launching the FPL authentication browser in %s mode.",
                "headless" if headless else "local graphical",
            )
            browser = await p.chromium.launch(
                headless=headless,
                args=browser_args,
                **launch_options,
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()

            # 1. Setup Token Listener
            async def handle_response(response):
                if "/as/token" in response.url and response.request.method == "POST":
                    try:
                        data = await response.json()
                        if not isinstance(data, dict):
                            return
                        self.token_response_keys = sorted(str(key) for key in data)
                        if "access_token" in data:
                            self.api_token = f"Bearer {data['access_token']}"
                            # Capture renewal material when the account service offers
                            # it, so unattended runs can avoid a browser login.
                            refresh = data.get("refresh_token")
                            self.refresh_token = refresh if isinstance(refresh, str) else None
                            expires = data.get("expires_in")
                            self.expires_in = expires if isinstance(expires, int) else None
                            logger.info(
                                "Captured API token. Token response fields: %s (refresh token %s).",
                                ", ".join(self.token_response_keys) or "none",
                                "present" if self.refresh_token else "absent",
                            )
                    except Exception:
                        pass

            page.on("response", handle_response)
            
            try:
                logger.info(f"Navigating to {self.base_url}")
                await page.goto(self.base_url)
                await page.wait_for_load_state("networkidle")
                
                # 2. Handle Cookie Banner (Robust)
                try:
                    cookie_btn = await page.wait_for_selector('#onetrust-accept-btn-handler', timeout=5000)
                    if cookie_btn:
                        await cookie_btn.click()
                        logger.info("Accepted Cookies")
                        await page.wait_for_timeout(1000)
                except Exception:
                    logger.info("No cookie banner found (or already accepted)")

                # 3. Find Login Button (Try Multiple Selectors)
                logger.info("Looking for Login button...")
                login_selectors = [
                    'button:has-text("Log in")',
                    'a:has-text("Log in")', 
                    'button:has-text("Sign in")',
                    'a:has-text("Sign in")',
                    '[data-cy="login"]',
                    '.login-button',
                    'button[class*="login"]',
                    'a[href*="login"]'
                ]
                
                login_clicked = False
                for selector in login_selectors:
                    try:
                        # Short timeout for checking each selector
                        btn = await page.wait_for_selector(selector, state="visible", timeout=2000)
                        if btn:
                            await btn.click()
                            logger.info(f"Clicked login using selector: {selector}")
                            login_clicked = True
                            break
                    except Exception:
                        continue
                
                if not login_clicked:
                    logger.error("Could not find any login button")
                    self.failure_reason = "The current FPL page did not expose its login button."
                    return None

                # CRITICAL FIX: Wait for navigation after clicking login
                # This matches your working code which waits for networkidle here
                logger.info("Waiting for login page navigation...")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    logger.warning("Network idle timeout - continuing anyway...")

                # 4. Fill Credentials (Try Multiple Input Selectors from your working code)
                logger.info("Looking for email input...")
                
                # Email
                email_input = None
                email_selectors = [
                    'input[name="username"]',
                    '#username',
                    'input[name="email"]',
                    'input[type="email"]',
                    'input[placeholder*="email" i]',
                    'input[id*="email" i]',
                    '#email',
                    '[data-cy="email"]'
                ]
                
                for sel in email_selectors:
                    try:
                        email_input = await page.wait_for_selector(sel, state="visible", timeout=3000)
                        if email_input:
                            logger.info(f"Found email input using: {sel}")
                            await email_input.fill(self.email)
                            break
                    except: continue

                if not email_input:
                    logger.error("Failed to find email field")
                    self.failure_reason = "The current Premier League login page did not expose its email field."
                    # Take screenshot for debugging
                    await page.screenshot(path="email_fail.png")
                    return None

                # Password
                pass_input = None
                pass_selectors = [
                    'input[name="password"]', 
                    'input[type="password"]', 
                    '#password', 
                    '[data-cy="password"]',
                    'input[placeholder*="password" i]'
                ]
                for sel in pass_selectors:
                    try:
                        pass_input = await page.wait_for_selector(sel, state="visible", timeout=3000)
                        if pass_input:
                            await pass_input.fill(self.password)
                            logger.info(f"Filled password using {sel}")
                            break
                    except: continue

                if not pass_input:
                    logger.error("Failed to find password field")
                    self.failure_reason = "The current Premier League login page did not expose its password field."
                    return None

                # 5. Submit (Try Multiple Buttons)
                submit_selectors = [
                    '#btnSignIn',
                    'button[data-skbuttonvalue="SIGNON"]',
                    'button:has-text("Sign in")',
                    'button:has-text("Log in")',
                    'input[type="submit"]',
                    '[data-cy="signin"]',
                    'button[class*="signin"]',
                    'button[class*="login"]',
                ]
                submit_clicked = False
                for sel in submit_selectors:
                    try:
                        btn = await page.wait_for_selector(sel, state="visible", timeout=3000)
                        if btn:
                            await btn.click()
                            logger.info(f"Clicked Submit using {sel}")
                            submit_clicked = True
                            break
                    except: continue

                if not submit_clicked:
                    self.failure_reason = "The current Premier League login page did not expose its Sign In action."
                    return None

                # 6. Wait for Token Capture
                logger.info("Waiting for token capture...")
                # Observe an explicit, sanitised rejection rather than masking it as a token timeout.
                for _ in range(30):
                    if self.api_token:
                        return self.api_token
                    failure = await _visible_login_failure(page)
                    if failure:
                        self.failure_reason = failure
                        logger.warning("FPL authentication was rejected by the account page.")
                        return None
                    await asyncio.sleep(0.5)
                
                logger.error("Login flow finished but no token captured.")
                self.failure_reason = (
                    "The Premier League login completed without returning an FPL session. "
                    "Its browser security check may have interrupted the redirect."
                )
                return None

            except Exception as e:
                logger.error(f"Auth Critical Error: {e}")
                self.failure_reason = "The local authentication browser could not complete the FPL login flow."
                return None
            finally:
                await browser.close()
