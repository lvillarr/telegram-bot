# Quick Start — Telegram Bot Arauco

## Validar y desplegar
```bash
python3 -c "import ast; ast.parse(open('bot.py').read()); print('OK')"
git add bot.py && git commit -m "fix: ..." && git push origin main
```

## Estructura clave
- `bot.py` — bot principal (handlers, callbacks, artefactos)
- `rag.py` — ChromaDB + Voyage AI
- `prompts/` — system prompts por agente
- `proyecto_claude/` — submodule con CLAUDE.md del sistema multiagente

## Variables de entorno requeridas
`TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `VOYAGE_API_KEY`

## Deploy
Railway auto-deploy desde `main`. Ver logs en Railway dashboard.
