"""Retrieval helpers: bge cosine search over the persisted Chroma store.

We rank manually against the persisted embeddings instead of relying on
Chroma's internal index. Chroma's index has proven inconsistent with the
vectors returned by `collection.get()` (inflated, compressed distances), and
`query_texts` would be embedded by Chroma's bundled MiniLM function rather
than our bge model. At this project's scale (hundreds of vectors) loading and
sorting in-process is fast, deterministic, and always matches the stored data.
"""
import numpy as np

from langchain_core.documents import Document

from config import get_embeddings, get_store


def retrieve(query: str, k: int = 8):
    store = get_store()
    data = store._collection.get(
        include=["documents", "metadatas", "embeddings"]
    )
    ids = data["ids"]
    texts = data["documents"]
    metas = data["metadatas"]
    vectors = np.asarray(data["embeddings"])

    q = np.asarray(get_embeddings().embed_query(query))

    norms = np.linalg.norm(vectors, axis=1)
    sims = (vectors @ q) / (norms * np.linalg.norm(q))
    dists = 1.0 - sims

    order = np.argsort(dists)[:k]
    out = []
    for idx in order:
        out.append(
            (
                Document(
                    page_content=texts[idx],
                    metadata=metas[idx] or {"source": ids[idx]},
                ),
                float(dists[idx]),
            )
        )
    return out