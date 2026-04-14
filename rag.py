"""
RAG — Retrieval Augmented Generation
Indexa documentos con Voyage AI + ChromaDB y recupera contexto relevante.
"""
import os
import hashlib
import chromadb
import voyageai

CHROMA_PATH     = os.environ.get("CHROMA_PATH", "/data/chroma")
COLLECTION_NAME = "arauco_docs"
EMBED_MODEL     = "voyage-3"
TOP_K           = 4
MAX_DISTANCE    = 0.45   # umbral de similitud coseno (menor = más similar)

# ── Clientes ──────────────────────────────────────────────────────────────────
voyage  = voyageai.Client(api_key=os.environ.get("VOYAGE_API_KEY", ""))
chroma  = chromadb.PersistentClient(path=CHROMA_PATH)
col     = chroma.get_or_create_collection(
    name=COLLECTION_NAME,
    metadata={"hnsw:space": "cosine"}
)


# ── Chunking ──────────────────────────────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = 400, overlap: int = 60) -> list[str]:
    """Divide el texto en fragmentos con overlap para no perder contexto."""
    words  = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + chunk_size])
        if len(chunk.strip()) > 80:   # ignora fragmentos muy cortos
            chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


# ── Indexar ───────────────────────────────────────────────────────────────────
def index_document(text: str, filename: str) -> int:
    """Indexa un documento y retorna el número de fragmentos almacenados."""
    chunks = chunk_text(text)
    if not chunks:
        return 0

    # Voyage AI: generar embeddings de los fragmentos
    result     = voyage.embed(chunks, model=EMBED_MODEL, input_type="document")
    embeddings = result.embeddings

    # IDs únicos por fragmento (filename + posición + hash del contenido)
    ids = [
        f"{filename}__{i}__{hashlib.md5(c.encode()).hexdigest()[:8]}"
        for i, c in enumerate(chunks)
    ]

    col.upsert(
        ids        = ids,
        embeddings = embeddings,
        documents  = chunks,
        metadatas  = [{"filename": filename, "chunk": i} for i in range(len(chunks))]
    )
    return len(chunks)


# ── Consulta ──────────────────────────────────────────────────────────────────
def query(question: str, n: int = TOP_K) -> list[dict]:
    """Retorna los fragmentos más relevantes para la pregunta."""
    total = col.count()
    if total == 0:
        return []

    result    = voyage.embed([question], model=EMBED_MODEL, input_type="query")
    embedding = result.embeddings[0]

    results = col.query(
        query_embeddings = [embedding],
        n_results        = min(n, total),
        include          = ["documents", "metadatas", "distances"]
    )

    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    ):
        if dist <= MAX_DISTANCE:
            chunks.append({
                "text":     doc,
                "filename": meta["filename"],
                "score":    round(1 - dist, 2)   # similitud: 1.0 = idéntico
            })
    return chunks


# ── Utilidades ────────────────────────────────────────────────────────────────
def list_documents() -> list[str]:
    """Lista los nombres de archivo únicos indexados."""
    if col.count() == 0:
        return []
    results   = col.get(include=["metadatas"])
    filenames = sorted(set(m["filename"] for m in results["metadatas"]))
    return filenames


def delete_document(filename: str) -> int:
    """Elimina todos los fragmentos de un documento. Retorna cuántos borró."""
    results = col.get(where={"filename": filename})
    ids     = results["ids"]
    if ids:
        col.delete(ids=ids)
    return len(ids)


def build_context(question: str) -> str:
    """Construye el bloque de contexto RAG para inyectar en el SYSTEM_PROMPT."""
    chunks = query(question)
    if not chunks:
        return ""

    lines = [
        "\n\n---",
        "## Contexto desde base de conocimiento Arauco",
        "Los siguientes fragmentos provienen de documentos indexados. "
        "Úsalos si son relevantes y cita el nombre del documento como fuente.\n"
    ]
    for c in chunks:
        lines.append(f"*Documento: {c['filename']}* (relevancia: {c['score']})")
        lines.append(c["text"])
        lines.append("")
    lines.append("---")
    return "\n".join(lines)
