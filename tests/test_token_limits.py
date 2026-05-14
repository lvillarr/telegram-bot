"""
Tests de contrato para limites de tokens por tipo de artefacto.
Si alguien cambia _tokens_map en bot.py, estos tests fallan
y obligan a actualizar este archivo conscientemente.
"""
import re
import os

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot.py")

EXPECTED_LIMITS = {
    "html":          8000,
    "pdf":           6000,
    "gantt":         4000,
    "excel":         3000,
    "pptx":          6000,
    "email":         2000,
    "notas_onenote": 12000,
}


def _parse_tokens_map():
    """Extrae _tokens_map de bot.py via regex — sin importar el modulo."""
    with open(BOT_PATH, encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'_tokens_map\s*=\s*(\{[^}]+\})', content)
    assert match, "_tokens_map no encontrado en bot.py"
    raw = match.group(1)
    pairs = re.findall(r'"(\w+)":\s*(\d+)', raw)
    return {k: int(v) for k, v in pairs}


def test_token_limits_match_expected():
    actual = _parse_tokens_map()
    assert actual == EXPECTED_LIMITS, (
        f"_tokens_map cambio en bot.py.\n"
        f"  Esperado: {EXPECTED_LIMITS}\n"
        f"  Actual:   {actual}\n"
        "Actualizar EXPECTED_LIMITS en test_token_limits.py si el cambio es intencional."
    )


def test_notas_onenote_es_el_mayor():
    actual = _parse_tokens_map()
    assert actual["notas_onenote"] == max(actual.values()), \
        "notas_onenote debe tener el limite mas alto (es el artefacto mas complejo)"
