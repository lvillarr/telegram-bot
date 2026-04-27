# Architecture Map — Telegram Bot Arauco

## Handlers principales (bot.py)
- `handle_message` — texto → Claude (con RAG + historial)
- `handle_image` — foto → análisis o artefacto (batch con debounce)
- `handle_audio` — voz → Groq Whisper → Claude
- `handle_location` — GPS → nota o nueva nota
- `handle_document` — archivo → agente DA (Excel/PDF/Word)

## Callbacks
- `artifact_callback` — genera HTML/PDF/PPT/Excel/Gantt/email
- `notas_callback` — modo notas (agregar, GPS, PDF, Word, OneNote)
- `modelo_callback` — cambiar modelo por sesión
- `email_confirm_callback` — confirmar/editar/cancelar correo

## Artefactos
- `build_html()` / `build_pdf_images()` / `build_pptx()` / `build_pptx_imagenes()`
- `build_excel()` / `build_gantt_html()` / `send_email_outlook()`
- `build_notas_pdf()` / `build_notas_docx()`

## Sistema multiagente (proyecto_claude/)
- `orquestador/` → Subgerente MC
- `agentes/EO/` → Excelencia Operacional
- `agentes/TD/` → Transformación Digital  
- `agentes/IA/` → Inteligencia Artificial
- `agentes/DA/` → Análisis de Datos (reactivo)
