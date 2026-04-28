"""
Browser utilities — uses launch + new_context(storage_state) instead of
launch_persistent_context, so it works with headless-shell on Railway.
"""

import os
import time
import random

from patchright.sync_api import Playwright, Browser, BrowserContext, Page
from config import STATE_FILE, BROWSER_ARGS, USER_AGENT


class BrowserFactory:

    @staticmethod
    def launch_for_query(playwright: Playwright) -> tuple[Browser, BrowserContext]:
        """Headless browser with auth state loaded from state.json."""
        storage = str(STATE_FILE) if STATE_FILE.exists() else None

        browser = playwright.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            storage_state=storage,
        )
        return browser, context

    @staticmethod
    def launch_for_auth(playwright: Playwright, headless: bool = False) -> tuple[Browser, BrowserContext]:
        """Visible browser for interactive Google login (local only)."""
        channel = os.environ.get("PLAYWRIGHT_CHANNEL", "chrome")
        browser = playwright.chromium.launch(
            channel=channel,
            headless=headless,
            args=BROWSER_ARGS,
        )
        context = browser.new_context(user_agent=USER_AGENT)
        return browser, context


class StealthUtils:

    @staticmethod
    def random_delay(min_ms: int = 100, max_ms: int = 500):
        time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

    @staticmethod
    def human_type(page: Page, selector: str, text: str):
        element = page.query_selector(selector)
        if not element:
            try:
                element = page.wait_for_selector(selector, timeout=2000)
            except Exception:
                pass
        if not element:
            return
        element.click()
        for char in text:
            element.type(char, delay=random.uniform(25, 75))
            if random.random() < 0.05:
                time.sleep(random.uniform(0.15, 0.4))
