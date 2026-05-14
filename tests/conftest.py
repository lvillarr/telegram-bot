"""
Mocks necesarios para importar bot.py sin efectos secundarios:
- anthropic/groq: evita conexion a APIs externas
- telegram/telegram.ext: evita app.run_polling() bloqueante
- uvicorn + threading.Thread: evita arranque de servidor web
- env vars minimas para que bot.py no falle en inicializacion
"""
import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch
import pytest


def _build_mocks():
    mocks = {
        "anthropic":         MagicMock(),
        "groq":              MagicMock(),
        "voyageai":          MagicMock(),
        "chromadb":          MagicMock(),
        "rag":               MagicMock(),
        "pdfplumber":        MagicMock(),
        "uvicorn":           MagicMock(),
        "telegram":          MagicMock(),
        "telegram.ext":      MagicMock(),
    }
    mocks["anthropic"].AsyncAnthropic.return_value = AsyncMock()
    return mocks


@pytest.fixture(scope="session")
def bot_module():
    """Importa bot.py una vez por sesion con todos los mocks activos."""
    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)

    env = {
        "TELEGRAM_TOKEN":      "1234567890:TEST_TOKEN_FOR_UNIT_TESTS",
        "ANTHROPIC_API_KEY":  "sk-test-key",
        "GROQ_API_KEY":       "gsk-test-key",
        "PORT":               "18080",
    }

    mocks = _build_mocks()
    with patch.dict(sys.modules, mocks), \
         patch("threading.Thread"), \
         patch.dict(os.environ, env):
        if "bot" in sys.modules:
            del sys.modules["bot"]
        import bot
        yield bot
