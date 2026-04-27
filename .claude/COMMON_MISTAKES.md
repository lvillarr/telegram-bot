# Common Mistakes — Telegram Bot Arauco

## 1. Bytes literales con caracteres no-ASCII
`b"texto"` solo acepta ASCII. Acentos rompen Railway con SyntaxError.
Fix: `b"texto"` sin acentos, o usar `"texto".encode()`.

## 2. Doble teclado en batch de fotos
El debounce debe usar `is asyncio.current_task()` antes de hacer `.pop()`.
No usar `.get()` para limpiar — permite que múltiples tasks completen.

## 3. RAG llamando Voyage AI con colección vacía
Siempre guardar con `if rag.col and rag.col.count() > 0` antes de `build_context()`.

## 4. `resp.content` vacío del modelo
Antes de `resp.content[0].text`, siempre: `if not resp.content: raise ValueError(...)`.

## 5. `reset_handler` incompleto
Usar `context.user_data.clear()` — no `.pop()` individual. Evita estados corruptos (notas, email, imagen).
