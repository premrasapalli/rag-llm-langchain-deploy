import json
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import config

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GKE GenAI Playground</title>
<style>
:root { color-scheme: light dark; }
body { font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
       margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.5rem; }
textarea { width: 100%; min-height: 80px; font: inherit; padding: .5rem;
           border: 1px solid #888; border-radius: 8px; box-sizing: border-box; }
button { font: inherit; padding: .5rem 1rem; margin-top: .5rem; cursor: pointer;
         border-radius: 8px; border: 1px solid #888; background: #2277dd; color: #fff; }
button.secondary { background: #666; }
#out { white-space: pre-wrap; background: rgba(128,128,128,.12); border-radius: 8px;
       padding: 1rem; margin-top: 1rem; min-height: 3rem; }
.tabs button { background: #888; }
.tabs button.active { background: #2277dd; }
</style>
</head>
<body>
<h1>GKE GenAI Playground</h1>
<div class="tabs">
  <button id="tab-chat" class="active" onclick="show('chat')">Chat</button>
  <button id="tab-rag" onclick="show('rag')">RAG (ask docs)</button>
</div>
<div id="pane-chat">
  <p>Talk to the LLM (<span id="modelname"></span>).</p>
  <textarea id="msg" placeholder="Type your message to the model..."></textarea>
  <button onclick="chat()">Send</button>
</div>
<div id="pane-rag" style="display:none">
  <p>Retrieve from the knowledge base and get a grounded answer.</p>
  <textarea id="q" placeholder="Ask a question about the platform docs..."></textarea>
  <button onclick="askRag()">Ask</button>
</div>
<div id="out">Waiting for input...</div>
<script>
fetch('/models').then(r=>r.json()).then(m=>{
  document.getElementById('modelname').textContent =
    (m.data && m.data[0] ? m.data[0].id : 'unknown');
}).catch(()=>{});
function show(which){
  document.getElementById('pane-chat').style.display = which==='chat' ? '' : 'none';
  document.getElementById('pane-rag').style.display = which==='rag' ? '' : 'none';
  document.getElementById('tab-chat').className = which==='chat' ? 'active' : '';
  document.getElementById('tab-rag').className = which==='rag' ? 'active' : '';
}
function post(url, body){
  const out = document.getElementById('out');
  out.textContent = 'Thinking...';
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify(body)})
    .then(r=>r.json()).then(j=>{ out.textContent = j.answer || JSON.stringify(j); })
    .catch(e=>{ out.textContent = 'Error: ' + e; });
}
function chat(){ post('/chat', {messages:[{role:'user', content:document.getElementById('msg').value}]}); }
function askRag(){ post('/rag', {query: document.getElementById('q').value, k: 4}); }
document.getElementById('msg').addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();chat();} });
document.getElementById('q').addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();askRag();} });
</script>
</body>
</html>"""

router = APIRouter()


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    # Auth is opt-in: only enforced when config.API_KEY is set.
    if not config.API_KEY:
        return None
    # Constant-time-ish compare to avoid leaking the key length via timing.
    import hmac

    if not x_api_key or not hmac.compare_digest(x_api_key, config.API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return None


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    max_tokens: int = 256
    temperature: float = 0.2


class RagRequest(BaseModel):
    query: str
    k: int = 4


class Reply(BaseModel):
    answer: str


def _openai_payload(messages, max_tokens, temperature):
    return {
        "model": config.LLM_MODEL,
        "messages": [m.dict() for m in messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


@router.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.post("/chat", response_model=Reply, dependencies=[Depends(require_api_key)])
async def chat(req: ChatRequest):
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{config.LLM_URL}/chat/completions",
            json=_openai_payload(req.messages, req.max_tokens, req.temperature),
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return Reply(answer=r.json()["choices"][0]["message"]["content"])


@router.post("/rag", response_model=Reply, dependencies=[Depends(require_api_key)])
async def rag(req: RagRequest):
    # The rag-service exposes a /answer endpoint that does retrieval + generation.
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{config.RAG_URL}/answer", json={"query": req.query, "k": req.k}
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return Reply(answer=r.json()["answer"])


@router.get("/models", dependencies=[Depends(require_api_key)])
async def models():
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{config.LLM_URL}/models")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()
