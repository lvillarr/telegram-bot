"""
NotebookLM configuration — paths driven by env vars for Railway compatibility
"""

import os
import base64
from pathlib import Path

# DATA_DIR: Railway volume at /data/notebooklm, locally ~/.notebooklm
_default_data = os.path.join(os.path.expanduser("~"), ".notebooklm")
DATA_DIR = Path(os.environ.get("NOTEBOOKLM_DATA_PATH", _default_data))
BROWSER_STATE_DIR = DATA_DIR / "browser_state"
BROWSER_PROFILE_DIR = BROWSER_STATE_DIR / "browser_profile"
STATE_FILE = BROWSER_STATE_DIR / "state.json"
AUTH_INFO_FILE = DATA_DIR / "auth_info.json"

# Create dirs if missing
BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# Decode state.json from env var if present (Railway — no volume needed)
_state_b64 = os.environ.get("NOTEBOOKLM_STATE_B64", "")
if _state_b64 and not STATE_FILE.exists():
    try:
        STATE_FILE.write_bytes(base64.b64decode(_state_b64))
    except Exception as _e:
        print(f"Warning: could not decode NOTEBOOKLM_STATE_B64: {_e}")

NOTEBOOK_URL = os.environ.get(
    "NOTEBOOKLM_NOTEBOOK_URL",
    "https://notebooklm.google.com/notebook/f68eff3c-f0e4-4dde-84bd-688bbd3e9037"
)

# Selectors
QUERY_INPUT_SELECTORS = [
    "textarea.query-box-input",
    'textarea[aria-label="Feld fur Anfragen"]',
    'textarea[aria-label="Input for queries"]',
]

RESPONSE_SELECTORS = [
    ".to-user-container .message-text-content",
    "[data-message-author='bot']",
    "[data-message-author='assistant']",
]

# Browser args — no-sandbox required in Railway containers
BROWSER_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--no-sandbox',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-gpu',
]

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

LOGIN_TIMEOUT_MINUTES = 10
QUERY_TIMEOUT_SECONDS = 120
PAGE_LOAD_TIMEOUT = 30000
