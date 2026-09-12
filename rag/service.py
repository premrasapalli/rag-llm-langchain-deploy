"""RAG service: tiny FastAPI exposing /answer that does retrieval + generation."""
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chain import rag_answer

app = FastAPI(title="RAG Service")


class AnswerRequest(BaseModel):
    query: str
    k: int = Field(default=12, ge=1, le=20)


class AnswerResponse(BaseModel):
    answer: str


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/answer", response_model=AnswerResponse)
async def answer(req: AnswerRequest):
    try:
        text = rag_answer(req.query, k=req.k)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AnswerResponse(answer=text)
