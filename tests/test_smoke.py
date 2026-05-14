"""
Smoke tests para el servidor web (FastAPI) del bot Arauco MC.
Requieren servidor corriendo en BASE_URL (local o Railway).
Ejecutar: pytest tests/ -v
"""
import os
import pytest
import httpx

BASE_URL = os.environ.get("BOT_BASE_URL", "http://localhost:8080")


def test_health():
    r = httpx.get(f"{BASE_URL}/health", timeout=10)
    assert r.status_code == 200
    assert r.text.strip() == "ok"


def test_root_returns_html():
    r = httpx.get(f"{BASE_URL}/", timeout=10)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_history_returns_json_list():
    r = httpx.get(f"{BASE_URL}/api/history", timeout=10)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_css_served():
    r = httpx.get(f"{BASE_URL}/arauco.css", timeout=10)
    assert r.status_code == 200
