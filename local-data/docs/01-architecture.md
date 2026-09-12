# Architecture — From ZERO to Live (Structure, Rationale & Service Inventory)

This file maps the whole platform from 0: the moving parts, how the data flows,
why each layer exists, and how to inspect every piece with real commands.

## The layers (and why each exists)

```
CLIENTS (browser / curl)                     WHY: users need a way in; they should
   │                                             not know about cluster internals
   ▼
API GATEWAY (FastAPI, :80)                   WHY: a single door that hides the
   │                                             messy internals behind simple,
   │                                             stable endpoints (/chat, /rag)
   ▼
RAG SERVICE (:8080)                          WHY: retrieval is a separate concern
   │                                             (own scaling, own logic), so it
   │                                             does not entangle the gateway
   ▼
EMBEDDING TEI (:8001)   ──►  LLM (Ollama/vLLM :8000)
                                              WHY: two different jobs (vectors
   ▼                                             vs. language) are optimized for
CHROMA vector DB (rag-data volume)                different hardware and libs
                                              WHY: docs need a searchable index;
   ▼                                             separate DB = persistent, scalable
STORAGE / CLUSTER (GKE nodes, ingress,        WHY: containers are ephemeral, so
   Artifact Registry, Terraform)                   durable data, images, and IaC
                                                    give stability, reuse, and code review
```

### 1. The gateway — why add it
Clients should never depend on individual backend URLs or the served model name.
If you swap vLLM for Ollama, the client keeps calling `/chat` and never notices.

### 2. The RAG service — why add it
Retrieval (embed + search Chroma + build a grounded prompt) is a full
algorithmic chain. Isolating it keeps the gateway tiny and scales independently.

### 3. The embedding server (TEI) — why add it
A dedicated server guarantees the **same model** (`BAAI/bge-small-en-v1.5`) is
used at ingest and query time — without this consistency, retrieval returns
nonsense.

### 4. The LLM server (Ollama/vLLM) — why add it
Running it in-cluster keeps prompts and sensitive documents internal. Both
backends expose an OpenAI-compatible API so callers never change.

### 5. Chroma vector database — why add it
Embeddings + a vector index give semantic search. Persisted on a shared
read-write-many volume so ingest writes and the service reads at the same time.

### 6. Persistent storage (PVCs) — why add it
Containers are throwaway. Model weights, vector DB, and source docs must survive
pod restarts.

### 7. Cluster + nodes — why add it
GKE handles deployment, scaling, self-healing, and rolling updates.

### 8. Ingress / LoadBalancer — why add it
A stable public address that health-checks backend instances and only routes to
healthy ones.

## Inspect the architecture from your terminal

### Verify the cluster exists and nodes are ready

```bash
gcloud container clusters describe genai-cluster --region=us-central1 \
  --format="value(currentNodeVersion, currentNodeCount)"
gcloud container node-pools list --cluster genai-cluster --region=us-central1
```

### Verify every deployment is running

```bash
kubectl -n genai get deploy
# NAME              READY   UP-TO-DATE   AVAILABLE
# gateway           2/2     2            2
# rag-service       1/1     1            1
# serving-llm       1/1     1            1
# serving-embedding 1/1     1            1
```

### Verify the storage classes exist

```bash
kubectl get sc
# NAME                 PROVISIONER                    RECLAIMPOLICY
# premium-rwo          pd.csi.storage.gke.io          Delete
# nfs-filestore        filestore.csi.storage.gke.io   Delete
```

### Verify the PVCs are bound

```bash
kubectl -n genai get pvc
# NAME           STATUS   VOLUME                                     CAPACITY
# model-store    Bound    pvc-xxxx                                  50Gi
# embed-store    Bound    pvc-yyyy                                  10Gi
# rag-data       Bound    pvc-zzzz                                  100Gi
```

### Verify services have DNS names

```bash
kubectl -n genai get svc
# NAME                 TYPE        CLUSTER-IP    PORT
# gateway              ClusterIP   10.x.x.x      80/TCP
# serving-llm          ClusterIP   10.x.x.x      8000/TCP
# serving-embedding    ClusterIP   10.x.x.x      8001/TCP
# rag-service          ClusterIP   10.x.x.x      8080/TCP
```

### Verify the LoadBalancer has a public IP

```bash
kubectl -n genai get svc gateway-lb
# NAME         TYPE           CLUSTER-IP    EXTERNAL-IP
# gateway-lb   LoadBalancer   10.x.x.x      34.63.204.167
```

## The data flows

**Chat path (no documents):**
`client -> gateway -> LLM -> gateway -> client`

```bash
curl -s -X POST http://34.63.204.167/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

**RAG path (grounded in documents):**
`client -> gateway -> rag-service -> embedding(TEI) -> Chroma retrieval
         -> rag-service -> LLM -> gateway -> client`

```bash
curl -s -X POST http://34.63.204.167/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"What is RAG?"}'
```

**Ingestion path (offline):**
`docs (/data/docs or GCS) -> chunk + embed(TEI) -> store in Chroma (rag-data)`

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl logs -n genai job/rag-ingest-manual --tail=5
# Ingested ... -> N chunks
```

## Storage decisions

| Data | Where | Why |
|------|-------|-----|
| LLM weights | `model-store` (premium-rwo, SSD, RWO) | Fast, single writer |
| Embedding model | `embed-store` (premium-rwo) | TEI loads it locally |
| Chroma vectors + docs | `rag-data` (Filestore, RWX) | Shared by rag-service and ingest |

```bash
kubectl -n genai get pvc -o custom-columns=\
  "NAME:.metadata.name,SC:.spec.storageClassName,CAP:.spec.resources.requests.storage"
```

## Design rules

- **Single entry:** clients reach only the gateway.
- **OpenAI-compatible seams:** swapping backends changes config, never code.
- **Shared store:** one Chroma volume, one collection (`knowledge_base`), two
  writers (ingest) and readers (service).
- **Fail-safe storage classes:** `premium-rwo` where one pod reads/writes;
  `nfs-filestore` (RWX) where multiple pods need the same volume.

---

# Complete Service Inventory

## 1. gateway (Deployment, FastAPI, :80)
```bash
kubectl -n genai describe deploy gateway
kubectl -n genai logs deploy/gateway --tail=5
```

## 2. rag-service (Deployment, FastAPI, :8080)
```bash
kubectl -n genai describe deploy rag-service
kubectl -n genai logs deploy/rag-service --tail=5
```

## 3. serving-llm (Deployment — Ollama :8000 / vLLM :8000)
```bash
kubectl -n genai describe deploy serving-llm
kubectl -n genai logs deploy/serving-llm --tail=5
```

## 4. serving-embedding (Deployment — TEI, :8001)
```bash
kubectl -n genai describe deploy serving-embedding
```

## 5. rag-ingest (CronJob, every 6h)
```bash
kubectl -n genai get cronjob rag-ingest
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl -n genai logs job/rag-ingest-manual --tail=5
```

## 6. Chroma vector database (on `rag-data` PVC)
```bash
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n genai "$R" -- python -c \
  "from config import get_store; print('count:', get_store()._collection.count())"
```

## 7. model-store / embed-store (PVCs, premium-rwo)
```bash
kubectl -n genai get pvc model-store embed-store
```

## 8. rag-data (PVC — Filestore / NFS, RWX)
```bash
kubectl -n genai get pvc rag-data
```

## 9. gateway-lb (Service, type: LoadBalancer)
```bash
kubectl -n genai get svc gateway-lb
```

## 10. Artifact Registry repo `genai`
```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/aiml-project-idp/genai
```

## 11. model-loader (initContainer image)
```bash
kubectl -n genai get pod -l app=serving-llm -o jsonpath='{.items[0].spec.initContainers[*].name}'
```

## 12. GKE node pools (cpu-pool / gpu-pool)
```bash
gcloud container node-pools list --cluster genai-cluster --region=us-central1
```

## 13. Monitoring, secrets, and namespaces
```bash
kubectl -n genai get secrets
kubectl -n genai get pods --field-selector=status.phase!=Running
```
