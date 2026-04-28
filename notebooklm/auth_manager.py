"""
AuthManager — manages Google auth state for NotebookLM
"""

import json
import re
import time
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright
from config import STATE_FILE, AUTH_INFO_FILE, BROWSER_STATE_DIR, LOGIN_TIMEOUT_MINUTES
from browser_utils import BrowserFactory


class AuthManager:

    def is_authenticated(self) -> bool:
        return STATE_FILE.exists() and STATE_FILE.stat().st_size > 100

    def get_auth_info(self) -> dict:
        info = {"authenticated": self.is_authenticated()}
        if AUTH_INFO_FILE.exists():
            try:
                with open(AUTH_INFO_FILE) as f:
                    info.update(json.load(f))
            except Exception:
                pass
        if STATE_FILE.exists():
            age_h = (time.time() - STATE_FILE.stat().st_mtime) / 3600
            info["state_age_hours"] = round(age_h, 1)
        return info

    def setup_auth(self, headless: bool = False, timeout_minutes: int = LOGIN_TIMEOUT_MINUTES) -> bool:
        playwright = None
        browser = None
        try:
            playwright = sync_playwright().start()
            browser, context = BrowserFactory.launch_for_auth(playwright, headless=headless)

            page = context.new_page()
            page.goto("https://notebooklm.google.com/", wait_until="domcontentloaded")

            # Already logged in?
            try:
                page.wait_for_url(re.compile(r"^https://notebooklm\.google\.com/"), timeout=5000)
                if "accounts.google" not in page.url:
                    self._save_state(context)
                    return True
            except Exception:
                pass

            print("\nPlease log in to your Google account in the browser window...")
            timeout_ms = int(timeout_minutes * 60 * 1000)
            page.wait_for_url(re.compile(r"^https://notebooklm\.google\.com/"), timeout=timeout_ms)

            self._save_state(context)
            self._save_auth_info()
            return True

        except Exception as e:
            print(f"Auth setup failed: {e}")
            return False
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def _save_state(self, context):
        BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = context.storage_state()
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)

    def _save_auth_info(self):
        info = {"authenticated_at_iso": datetime.now(timezone.utc).isoformat()}
        with open(AUTH_INFO_FILE, 'w') as f:
            json.dump(info, f)
