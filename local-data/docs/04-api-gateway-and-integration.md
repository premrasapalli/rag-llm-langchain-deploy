# API Gateway & Service Integration — From 0 to Live

Every step below is a real command to deploy, test, trace, and debug the API
gateway and the full service-to-service integration.

---

# The API Gateway — From 0 to Live

## Deploy the gateway

```bash
kubectl apply -k k8s/overlays/prod
kubectl -n genai rollout status deploy/gateway
```

## Verify it is alive

```bash
kubectl -n genai get deploy gateway
kubectl -n genai logs deploy/gateway --tail=10

# Port-forward and health-check
kubectl port-forward -n genai svc/gateway 8080:80 >/dev/null 2>&1 &
PF=$!; sleep 3
curl -s http://localhost:8080/healthz    # {"status":"ok"}
curl -s http://localhost:8080/models     # qwen2.5:0.5b
kill $PF
```

## Test every endpoint

```bash
kubectl port-forward -n genai svc/gateway 8080:80 >/dev/null 2>&1 &
PF=$!; sleep 3

# GET /healthz
curl -s http://localhost:8080/healthz

# GET /models
curl -s http://localhost:8080/models

# POST /chat
curl -s -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What is a token?"}]}'

# POST /rag
curl -s -X POST http://localhost:8080/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"What endpoints does the gateway expose?"}'

kill $PF
```

## Gateway environment configuration

```bash
kubectl -n genai get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -m json.tool
```

Key vars:

| Var | Value | Why |
|-----|-------|-----|
| `LLM_URL` | `http://serving-llm:8000/v1` | Where the LLM lives |
| `LLM_MODEL` | `qwen2.5:0.5b` | Must match the served model |
| `RAG_URL` | `http://rag-service:8080` | Where the RAG service lives |
| `API_KEY` | (optional) | Shared secret for `X-API-Key` auth |

---

# How All Services Integrate — Hop-by-Hop Traces

## Service inventory

| Service | DNS name | Port | Talks to |
|---------|----------|------|----------|
| gateway | `gateway` | 80 | serving-llm, rag-service |
| serving-llm | `serving-llm` | 8000 | — (receives calls) |
| serving-embedding | `serving-embedding` | 8001 | — (receives calls) |
| rag-service | `rag-service` | 8080 | serving-embedding, Chroma, serving-llm |

```bash
# See all services and their IPs
kubectl -n genai get svc
```

## How they discover each other

Kubernetes gives each service a stable DNS name equal to its service name:

```bash
# Test DNS resolution from inside the cluster
kubectl -n genai exec deploy/gateway -- nslookup serving-llm
kubectl -n genai exec deploy/gateway -- nslookup rag-service
kubectl -n genai exec deploy/gateway -- nslookup serving-embedding
```

---

## Trace: /chat (plain conversation)

```
Client ──► gateway (/chat)
              │ POST {messages:[...]}  with model = LLM_MODEL
              ▼
          serving-llm (/v1/chat/completions, OpenAI-compatible)
              │
              ▼
          gateway returns {answer} ──► client
```

**Verify live:**

```bash
kubectl port-forward -n genai svc/gateway 8080:80 >/dev/null 2>&1 &
PF=$!; sleep 3

# Watch gateway logs in another terminal — you will see the LLM call
kubectl -n genai logs deploy/gateway -f &
LOG=$!

curl -s -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello"}]}'

sleep 2; kill $LOG $PF
```

---

## Trace: /rag (grounded answer)

```
Client ──► gateway (/rag)
              │ POST {query}
              ▼
          rag-service (/answer)
              │ 1) embed query via serving-embedding (bge-small-en-v1.5)
              │ 2) similarity-search Chroma for top-k chunks
              │ 3) build grounded prompt from chunks
              │ 4) call serving-llm to generate from that context
              ▼
          gateway returns {answer} ──► client
```

| Step | Why each hop exists |
|------|---------------------|
| query -> embedding | query must live in the same vector space as the docs |
| embed -> Chroma | vector index is the docs' semantic memory |
| chunks -> LLM prompt | grounded prompt constrains the model to facts |
| LLM -> answer | model turns retrieved facts into natural language |

**Verify live:**

```bash
kubectl port-forward -n genai svc/gateway 8080:80 >/dev/null 2>&1 &
PF=$!; sleep 3

# Watch rag-service logs — you will see embed -> retrieve -> generate
kubectl -n genai logs deploy/rag-service -f &
LOG=$!

curl -s -X POST http://localhost:8080/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"What is RAG?"}' | python3 -m json.tool

sleep 2; kill $LOG $PF
```

---

## Trace: ingestion (offline path)

```
GCS bucket ──► seed-docs (gsutil rsync) ──► /data/docs (rag-data PVC)
                                              │
rag-ingest (python -m ingest):                ▼
    chunk .md/.txt ──► embed via TEI ──► upsert into /data/chroma
```

```bash
# Trigger a manual ingest and watch the flow
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl -n genai logs -f job/rag-ingest-manual
```

---

## Keeping integrations consistent (the 3 rules)

```bash
# Rule 1: embedding model — same at ingest AND query
kubectl -n genai get cronjob rag-ingest -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].env}' | grep EMBEDDING_MODEL
kubectl -n genai get deploy rag-service -o jsonpath='{.spec.template.spec.containers[0].env}' | grep EMBEDDING_MODEL

# Rule 2: LLM model name — gateway and rag-service match the served model
kubectl -n genai get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].env}' | grep LLM_MODEL
kubectl -n genai get deploy rag-service -o jsonpath='{.spec.template.spec.containers[0].env}' | grep LLM_MODEL

# Rule 3: service URLs — point at the right Kubernetes service names
kubectl -n genai get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].env}' | grep -E 'LLM_URL|RAG_URL'
```

---

## Failure signatures (what to check)

| Symptom | Check |
|---------|-------|
| `/rag` answers generically with no docs | `kubectl exec deploy/rag-service -- python -c "from config import get_store; print(get_store()._collection.count())"` — count must be > 0 |
| `404 model not found` from `/chat` | `LLM_MODEL` env var does not match served model — check both |
| `ImagePullBackOff` on every pod | You applied `k8s/base` not `k8s/overlays/prod` — reapply the overlay |
| `Connection refused` from gateway -> LLM | `serving-llm` pod is not ready — `kubectl -n genai get pods` |
