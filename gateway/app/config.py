import os

LLM_URL = os.environ.get("LLM_URL", "http://serving-llm:8000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "genai-model")
RAG_URL = os.environ.get("RAG_URL", "http://rag-service:8080")
EMBED_URL = os.environ.get("EMBED_URL", "http://serving-embedding:8001/v1")

# If API_KEY is set, /chat, /rag and /models require it via the X-API-Key header.
# If unset (default), auth is disabled for local testing. Wire it to a Secret in
# production (see k8s gateway.yaml) and terminate TLS at the ingress.
API_KEY = os.environ.get("API_KEY", "")
