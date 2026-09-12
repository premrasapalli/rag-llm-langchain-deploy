# GKE GenAI Deployment — Introduction & Overview

This project is an end-to-end Generative AI (GenAI) platform that runs on Google
Kubernetes Engine (GKE). It lets you chat with a Large Language Model (LLM),
ask questions grounded in your own documents, and manage the whole thing with
version-controlled infrastructure and code.

Think of it as a "self-hosted AI assistant" — no calls to closed APIs, no keys
for an external provider. Everything runs inside your own cloud cluster using
open-source tools.

## The four big building blocks

1. **Model serving** — hosts the AI models that generate text. We use vLLM (or
   the lighter Ollama in CPU mode) to run the LLM, and a second service called
   "Text Embeddings Inference" (TEI) to turn text into numbers (embeddings).

2. **RAG (Retrieval Augmented Generation)** — lets the model answer questions
   using *your* documents instead of only its own training data. Documents are
   split into chunks, converted to embeddings, and stored in a vector database
   (Chroma). At question time, relevant chunks are retrieved and given to the
   LLM as context.

3. **API Gateway** — a FastAPI application that exposes simple HTTP endpoints
   (`/chat`, `/rag`, `/models`, `/healthz`) so a frontend or a script can talk
   to the AI without knowing about any of the internals.

4. **Infrastructure as Code** — everything (cluster, storage, node pools) is
   defined in Terraform and deployed with Kubernetes manifests, so the whole
   platform is reproducible, reviewable, and GitOps-ready.

## Quick reference — what is running

| Service               | Port | What it does                                    |
|-----------------------|------|-------------------------------------------------|
| gateway               | 80   | FastAPI entry point: `/chat`, `/rag`, `/models`, `/healthz` |
| serving-llm           | 8000 | LLM inference (Ollama CPU or vLLM GPU)          |
| serving-embedding     | 8001 | TEI embeddings (`BAAI/bge-small-en-v1.5`)       |
| rag-service           | 8080 | Retrieve from Chroma + generate grounded answer |
| rag-ingest            | —    | CronJob: chunk docs, embed, store in Chroma     |

## Verify the platform is live (from zero)

After completing `07-implementation-guide.md`, confirm everything works:

```bash
# Check all pods are running
kubectl -n genai get pods

# Health check via port-forward
kubectl port-forward -n genai svc/gateway 8080:80 >/dev/null & PF=$!
sleep 5
curl -s localhost:8080/healthz       # {"status":"ok"}
curl -s localhost:8080/models        # qwen2.5:0.5b

# Plain chat
curl -s -X POST localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello"}]}'

# RAG grounded question
curl -s -X POST localhost:8080/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"What endpoints does the API gateway expose?"}'
kill $PF
```

If the RAG answer quotes `/healthz`, `/models`, `/chat`, `/rag` — the whole
platform is live from 0.

## Who is this for?

Anyone who wants to learn how modern AI applications are actually deployed in
production: an LLM server, a retrieval pipeline, an API layer, and the cloud
plumbing that keeps them running. The rest of this knowledge base explains each
piece in depth.
