"""
Browser utilities for NotebookLM — uses bundled Chromium (no system Chrome needed)
"""

import json
import os
import time
import random

from patchright.sync_api import Playwright, BrowserContext, Page
from config import BROWSER_PROFILE_DIR, STATE_FILE, BROWSER_ARGS, USER_AGENT


class BrowserFactory:

    @staticmethod
    def launch_persistent_context(
        playwright: Playwright,
        headless: bool = True,
        user_data_dir: str = str(BROWSER_PROFILE_DIR)
    ) -> BrowserContext:
        # Use channel="chrome" locally if available, else bundled chromium (Railway)
        channel = os.environ.get("PLAYWRIGHT_CHANNEL", None)

        kwargs = dict(
            user_data_dir=user_data_dir,
            headless=headless,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            user_agent=USER_AGENT,
            args=BROWSER_ARGS,
        )
        if channel:
            kwargs["channel"] = channel

        context = playwright.chromium.launch_persistent_context(**kwargs)
        BrowserFactory._inject_cookies(context)
        return context

    @staticmethod
    def _inject_cookies(context: BrowserContext):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    if state.get('cookies'):
                        context.add_cookies(state['cookies'])
            except Exception as e:
                print(f"Warning: could not load state.json: {e}")


class StealthUtils:

    @staticmethod
    def random_delay(min_ms: int = 100, max_ms: int = 500):
        time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

    @staticmethod
    def human_type(page: Page, selector: str, text: str, wpm_min: int = 320, wpm_max: int = 480):
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
