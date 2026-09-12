"""Shared RAG configuration and vector store factory.

Uses an OpenAI-compatible embedding endpoint (works with vLLM or Ollama).
"""
import os

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

COLLECTION = os.environ.get("RAG_COLLECTION", "knowledge_base")
PERSIST_DIR = os.environ.get("RAG_PERSIST_DIR", "/data/chroma")

# OpenAI-compatible embedding endpoint (vLLM or Ollama)
EMBEDDING_BASE_URL = os.environ.get(
    "EMBEDDING_BASE_URL", "http://serving-embedding:8001/v1"
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_KEY = os.environ.get("EMBEDDING_API_KEY", "EMPTY")
# bge models are prompt-dependent: for search, the query should carry this
# instruction (passages are stored without it).
QUERY_PREFIX = os.environ.get(
    "EMBEDDING_QUERY_PREFIX",
    "Represent this sentence for searching relevant passages: ",
)


class BGEEmbeddings(OpenAIEmbeddings):
    """OpenAIEmbeddings that prefixes the bge search instruction to queries."""

    def embed_query(self, text: str) -> list[float]:
        return super().embed_query(QUERY_PREFIX + text)


def get_embeddings() -> OpenAIEmbeddings:
    return BGEEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=EMBEDDING_KEY,
        base_url=EMBEDDING_BASE_URL,
    )


def get_store(collection: str = COLLECTION) -> Chroma:
    return Chroma(
        collection_name=collection,
        embedding_function=get_embeddings(),
        persist_directory=PERSIST_DIR,
    )
