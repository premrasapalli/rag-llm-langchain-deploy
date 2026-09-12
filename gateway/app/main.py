from fastapi import FastAPI

from .routes import router

app = FastAPI(title="RAG + LLM Gateway", version="1.0.0")
app.include_router(router)
