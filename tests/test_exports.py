"""
Tests unitarios para build_notas_pdf y build_notas_docx.
Funciones puras: no requieren servidor ni APIs externas.
"""
import io
import pytest


NOTAS_MINIMAS = [
    {"n": 1, "fecha": "2026-05-13", "texto": "Nota de prueba uno."},
    {"n": 2, "fecha": "2026-05-13", "texto": "**Negrita** y *cursiva* en nota dos."},
]

NOTAS_CON_TITULO_LARGO = [
    {"n": 1, "fecha": "2026-01-01", "texto": "A" * 500},
]


class TestBuildNotasPdf:
    def test_retorna_bytesio(self, bot_module):
        buf = bot_module.build_notas_pdf(NOTAS_MINIMAS)
        assert isinstance(buf, io.BytesIO)

    def test_pdf_no_vacio(self, bot_module):
        buf = bot_module.build_notas_pdf(NOTAS_MINIMAS)
        assert buf.getbuffer().nbytes > 1000

    def test_pdf_header_magico(self, bot_module):
        buf = bot_module.build_notas_pdf(NOTAS_MINIMAS)
        buf.seek(0)
        assert buf.read(4) == b"%PDF"

    def test_titulo_personalizado(self, bot_module):
        buf = bot_module.build_notas_pdf(NOTAS_MINIMAS, titulo="Informe Arauco")
        assert isinstance(buf, io.BytesIO)
        assert buf.getbuffer().nbytes > 1000

    def test_notas_vacias_no_lanza(self, bot_module):
        buf = bot_module.build_notas_pdf([])
        assert isinstance(buf, io.BytesIO)

    def test_texto_largo(self, bot_module):
        buf = bot_module.build_notas_pdf(NOTAS_CON_TITULO_LARGO)
        assert buf.getbuffer().nbytes > 1000


class TestBuildNotasDocx:
    def test_retorna_bytesio(self, bot_module):
        buf = bot_module.build_notas_docx(NOTAS_MINIMAS)
        assert isinstance(buf, io.BytesIO)

    def test_docx_no_vacio(self, bot_module):
        buf = bot_module.build_notas_docx(NOTAS_MINIMAS)
        assert buf.getbuffer().nbytes > 1000

    def test_docx_header_zip(self, bot_module):
        buf = bot_module.build_notas_docx(NOTAS_MINIMAS)
        buf.seek(0)
        assert buf.read(2) == b"PK"  # .docx es ZIP

    def test_titulo_personalizado(self, bot_module):
        buf = bot_module.build_notas_docx(NOTAS_MINIMAS, titulo="Notas Mejora Continua")
        assert isinstance(buf, io.BytesIO)

    def test_notas_vacias_no_lanza(self, bot_module):
        buf = bot_module.build_notas_docx([])
        assert isinstance(buf, io.BytesIO)
