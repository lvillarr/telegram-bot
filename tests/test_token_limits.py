"""
Tests de contrato para limites de tokens por tipo de artefacto.
Si alguien cambia _TOKENS_MAP en bot.py, estos tests fallan
y obligan a actualizar este archivo conscientemente.
"""
import pytest

EXPECTED_LIMITS = {
    "html":          8000,
    "pdf":           6000,
    "gantt":         4000,
    "excel":         3000,
    "pptx":          6000,
    "email":         2000,
    "notas_onenote": 12000,
}


def test_token_limits_match_expected(bot_module):
    assert bot_module._TOKENS_MAP == EXPECTED_LIMITS, (
        f"_TOKENS_MAP cambio en bot.py.\n"
        f"  Esperado: {EXPECTED_LIMITS}\n"
        f"  Actual:   {bot_module._TOKENS_MAP}\n"
        "Actualizar EXPECTED_LIMITS en test_token_limits.py si el cambio es intencional."
    )


def test_notas_onenote_es_el_mayor(bot_module):
    assert bot_module._TOKENS_MAP["notas_onenote"] == max(bot_module._TOKENS_MAP.values()), \
        "notas_onenote debe tener el limite mas alto (es el artefacto mas complejo)"
