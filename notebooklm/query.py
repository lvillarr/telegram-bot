"""
NotebookLM query — synchronous, run via loop.run_in_executor() from async handler
Uses launch + new_context(storage_state) to work with headless-shell on Railway
"""

import re
import time

from patchright.sync_api import sync_playwright
from config import NOTEBOOK_URL, QUERY_INPUT_SELECTORS, RESPONSE_SELECTORS, QUERY_TIMEOUT_SECONDS
from browser_utils import BrowserFactory, StealthUtils
from auth_manager import AuthManager


def ask(question: str, notebook_url: str = None) -> str:
    """
    Query NotebookLM. Returns answer text or raises RuntimeError.
    """
    url = notebook_url or NOTEBOOK_URL

    auth = AuthManager()
    if not auth.is_authenticated():
        raise RuntimeError("NotebookLM no autenticado. Contactar al administrador.")

    playwright = None
    browser = None

    try:
        playwright = sync_playwright().start()
        browser, context = BrowserFactory.launch_for_query(playwright)

        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        # If redirected to login, auth expired
        try:
            page.wait_for_url(re.compile(r"^https://notebooklm\.google\.com/"), timeout=15000)
        except Exception:
            pass

        if "accounts.google" in page.url:
            raise RuntimeError("Sesion de NotebookLM expirada. Requiere re-autenticacion.")

        # Find query input
        query_element = None
        for selector in QUERY_INPUT_SELECTORS:
            try:
                query_element = page.wait_for_selector(selector, timeout=10000, state="visible")
                if query_element:
                    break
            except Exception:
                continue

        if not query_element:
            raise RuntimeError("No se encontro el campo de consulta en NotebookLM.")

        StealthUtils.human_type(page, QUERY_INPUT_SELECTORS[0], question)
        page.keyboard.press("Enter")
        StealthUtils.random_delay(500, 1500)

        # Poll for stable response
        answer = None
        stable_count = 0
        last_text = None
        deadline = time.time() + QUERY_TIMEOUT_SECONDS

        while time.time() < deadline:
            try:
                thinking = page.query_selector('div.thinking-message')
                if thinking and thinking.is_visible():
                    time.sleep(1)
                    continue
            except Exception:
                pass

            for selector in RESPONSE_SELECTORS:
                try:
                    elements = page.query_selector_all(selector)
                    if elements:
                        text = elements[-1].inner_text().strip()
                        if text:
                            if text == last_text:
                                stable_count += 1
                                if stable_count >= 3:
                                    answer = text
                                    break
                            else:
                                stable_count = 0
                                last_text = text
                except Exception:
                    continue

            if answer:
                break
            time.sleep(1)

        if not answer:
            raise RuntimeError("NotebookLM no respondio en el tiempo limite (120s).")

        return answer

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
