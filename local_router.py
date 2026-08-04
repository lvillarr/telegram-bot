"""Router local-first: resuelve queries simples con Ollama antes de pegarle a Claude.

Inspirado en OpenJarvis (github.com/open-jarvis/OpenJarvis) - modelos locales
manejan la mayoria de queries de un solo turno, cloud solo cuando hace falta.

Desactivado por defecto (LOCAL_ROUTER_ENABLED=0). Pensado para dev/testing en
local; Railway no tiene Ollama corriendo, asi que en prod queda siempre en
fallback a Claude salvo que se prenda explicitamente.
"""
import os
import re

import requests

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
LOCAL_ROUTER_ENABLED = os.environ.get("LOCAL_ROUTER_ENABLED", "0") == "1"
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "12"))

_MAX_LOCAL_CHARS = 140

LOCAL_SYSTEM_PROMPT = (
    "Sos el asistente digital de la Subgerencia de Mejora Continua de Arauco "
    "(empresa forestal-industrial chilena). Respondé breve y en español. "
    "No inventes datos operacionales, KPIs ni capacidades que no tengas. "
    "Si la consulta pide un dato concreto, un archivo o un analisis de "
    "negocio, decí que no podés resolverlo en este modo y sugerí reformular."
)

# Temas de dominio (forestal / Arauco MC) o de tarea compleja: nunca ruteo local.
_DOMAIN_KEYWORDS = (
    "kpi", "sap", "sgl", "planex", "opticort", "cosecha", "transporte",
    "telemetria", "telemetría", "diagnostico", "diagnóstico", "rediseno",
    "rediseño", "proceso", "genera", "generar", "reporte", "analiza",
    "analizar", "compara", "comparar", "excel", "pdf", "pptx", "gantt",
    "email", "correo",
)


def should_route_local(user_msg: str, history: list | None) -> bool:
    """Heuristica: mensajes cortos, primer turno, sin senal de dominio/tarea compleja."""
    if not LOCAL_ROUTER_ENABLED:
        return False
    if history:
        return False
    msg = user_msg.strip()
    if not msg or len(msg) > _MAX_LOCAL_CHARS:
        return False
    if re.search(r"\d", msg):
        return False
    low = msg.lower()
    if any(kw in low for kw in _DOMAIN_KEYWORDS):
        return False
    if "opus" in low or "sonnet" in low:
        return False
    return True


def ollama_response(system: str, user_msg: str, history: list | None = None) -> str | None:
    """Llama a Ollama local. Devuelve None ante cualquier falla (fallback a Claude)."""
    messages = [{"role": "system", "content": system}]
    for turn in (history or []):
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_msg})

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("message", {}).get("content", "").strip()
        return text or None
    except (requests.RequestException, ValueError):
        return None
