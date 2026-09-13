# RAG + LLM LangChain Deployment on GKE — Model Serving · Inference

An end-to-end RAG + LLM (LangChain) deployment stack for Google Cloud (GKE):

- **Model serving** — Ollama (OpenAI-compatible) on CPU for the LLM, Text
  Embeddings Inference (TEI) for embeddings. Models auto-download into a shared
  volume. A GPU/vLLM path is declared in the `gke` overlay but **disabled** until
  L4 GPU quota is available.
- **RAG pipeline** — Chroma vector DB, document ingestion (CronJob), retrieval
  and a grounded-answer chain.
- **API gateway** — FastAPI exposing `/chat`, `/rag`, `/models`, `/healthz`.
- **GitOps-ready K8s manifests** — Kustomize `base` + overlays deployed via ArgoCD.
- **Terraform IaC** — GKE cluster (CPU pool) and Artifact Registry.

> **Deployment target**: `project_id = rag-llm-langchain`, region `us-central1`.
> Images live in Artifact Registry (`us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain`),
> NOT `gcr.io`.


## Architecture

```
                        ┌──────────────┐
   Ingress ──►  Gateway  ──►  serving-llm (Ollama CPU, :8000 /v1)
   (/chat,/rag)          │      └── model-store PVC (Ollama models)
                        │  ──►  serving-embedding (TEI, :8001 /v1)
                        │  ──►  rag-service (:8080 /answer)
                        │         └── Chroma (rag-data PVC: Filestore RWX)
                        └── ──►  rag-ingest (CronJob, every 6h)
```

Query flow for RAG: user → `/rag` → retriever embeds query via TEI, top-k chunks
from Chroma → Ollama prompt with grounded context → answer.

## 1. Local bring-up (Docker Compose)

```bash
docker compose up --build -d
docker compose exec ingest python -m ingest --dir /data/docs   # index docs

curl http://localhost:8080/healthz
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What is RAG in one sentence?"}]}'
curl -X POST http://localhost:8080/rag \
  -H 'Content-Type: application/json' -d '{"query":"What does the gateway do?"}'
```

Auth is **off by default** (empty `API_KEY`). To test API-key auth locally:

```bash
API_KEY=devkey docker compose up -d gateway
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' -H 'X-API-Key: devkey' \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

## 2. Provision GCP infra (Terraform)

There is a two-step process: **bootstrap first** (state bucket + APIs), then the
main cluster/repo apply.

```bash
# (0) Once: create the remote-state bucket and enable APIs.
cd terraform/bootstrap
terraform init && terraform apply

# (1) Back in terraform/: provision the cluster (CPU + GPU pools), Artifact
# Registry repo, and a reserved static IP for the gateway ingress.
cd ../terraform   # or: cd terraform
cp terraform.tfvars.example terraform.tfvars   # defaults already set to rag-llm-langchain
gcloud auth application-default login
terraform init && terraform plan && terraform apply
```

`backend.tf` expects the GCS bucket `rag-llm-langchain-tfstate`
(created by bootstrap) for remote state. `terraform apply` creates
`rag-llm-langchain-cluster` (e2-standard-8 CPU pool + g2-standard-12 GPU pool with L4), the
`rag-llm-langchain` Artifact Registry, and reserves `rag-llm-langchain-gateway-ip` (a global static IP
for the ingress). For CPU-only bring-up, comment out the `gpu` node pool.

```bash
gcloud container clusters get-credentials rag-llm-langchain-cluster --region us-central1
```

## 3. Build & push images (Artifact Registry)

The `rag` image is versioned (`1.0.3`). Because the local machine is arm64,
builds must target `linux/amd64` for the GKE amd64 nodes:

```bash
export REGION=us-central1; export PROJECT_ID=rag-llm-langchain
gcloud auth configure-docker $REGION-docker.pkg.dev

docker build --platform linux/amd64 -t $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/rag:1.0.3 rag/
docker push $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/rag:1.0.3
```

The `gateway` and `model-loader` images were built once from their directories
(`gateway/`, `serving/ -f serving/Dockerfile.model-loader`) and are pinned at
`1.0.0`. TEI is pulled from upstream (`ghcr.io/huggingface/text-embeddings-inference:1.5`).

> If your `gcloud` account lacks Cloud Build roles, build+push locally as above
> and skip `gcloud builds submit` (it also needs Cloud Build API + IAM).

## 4. Deploy to GKE (ArgoCD / Kustomize)

The base manifests reference **bare image names** (`rag-llm-langchain/<name>`). The
`prod` overlay rewrites them to the real Artifact Registry path.

Current live config is **CPU-only** (`k8s/overlays/prod`):

```bash
# CPU-only bring-up (Ollama, qwen2.5:0.5b, no GPU):
kubectl apply -k k8s/overlays/prod
```

Deploy via ArgoCD (this is how the cluster is actually managed):

```bash
# 1. Install ArgoCD once
helm repo add argo https://argoproj.github.io/argo-helm
helm upgrade --install argocd argo/argo-cd --namespace argocd --create-namespace --wait

# 2. Register the app (auto-sync + self-heal + prune)
kubectl apply -f argocd/rag-llm-langchain-app.yaml   # targets k8s/overlays/prod

# 3. Force a sync after a push to main
kubectl patch app rag-llm-langchain -n argocd --type merge \
  -p '{"operation":{"sync":{"revision":"main","prune":true}}}'
```

ArgoCD watches `main`, so every pushed manifest change rolls out automatically.
The app manifest targets `k8s/overlays/prod` (CPU). To move to GPU later, switch
that path to `k8s/overlays/gke` and provision the GPU pool first (see §5).

Tags are not `latest` — model-loader/rag/gateway are versioned and upstream
TEI is pinned, so builds are reproducible. The GitHub Actions workflow also tags
each push with `1.0.<epoch>-<sha>`.

Verify:

```bash
kubectl get pods -n rag-llm-langchain -w          # wait: serving-llm pulls the Ollama model
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80
curl http://localhost:8080/healthz
curl http://localhost:8080/chat -X POST -d '{"messages":[{"role":"user","content":"hi"}]}' -H 'Content-Type: application/json'
```

The first model download (qwen2.5:0.5b) takes a minute or two.

### Networking / TLS

The default ingress is **HTTP-only** (for testing) and terminates at the GCP L4
load balancer. Terraform already reserves a global static IP
(`rag-llm-langchain-gateway-ip`, output `gateway_static_ip`). For a real domain, edit
`k8s/base/ingress.yaml` to:

- Reference the reserved IP: `kubernetes.io/ingress.global-static-ip-name: rag-llm-langchain-gateway-ip`
- Add a ManagedCertificate CR and set
  `networking.gke.io/managed-certificates: rag-llm-langchain-gateway-cert`
- Force HTTPS with `kubernetes.io/ingress.allow-http: "false"`
- Point a DNS A record for your domain at the static IP.

### Gateway auth

The gateway accepts a `X-API-Key` header. Auth is **disabled by default** so the
stack works out of the box; enable it by setting the `api-key` value in the
`gateway-secret` Secret:

```bash
kubectl -n rag-llm-langchain create secret generic gateway-secret --from-literal=api-key=$(openssl rand -hex 24) --dry-run=client -o yaml | kubectl apply -f -
```

Do **not** expose the gateway to the public internet with auth disabled.

## 5. GPU serving (optional, currently DISABLED)

The live deployment is **CPU-only** (Ollama, Qwen 2.5 0.5B) because the global
GPU quota (`GPUS_ALL_REGIONS`) is 0 — no `gpu-pool` node pool exists. The GPU
path is fully declared for later:

1. Flip on the GPU pool (`enable_gpu_pool = true` in `terraform.tfvars`) and
   `terraform apply`, then add `roles/artifactregistry.reader` to the new node SA.
2. Point the ArgoCD app at `k8s/overlays/gke` (GPU overlay) and sync. That overlay:
   - adds `nvidia.com/gpu: "1"` and a `cloud.google.com/gke-nodepool: gpu-pool`
     nodeSelector to `serving-llm`,
   - deploys the NVIDIA GPU driver DaemonSet (`k8s/overlays/gpu/nvidia-driver/`)
     in namespace `kube-system` (GKE ≥ 1.32.2 also auto-installs it),
   - overrides `HF_MODEL` to `Qwen/Qwen2.5-7B-Instruct` on the model-loader
     initContainer (edit the patch to change the model).
3. For **gated models** (Llama-3, Gemma, …), set the HF token:
   ```bash
   kubectl -n rag-llm-langchain create secret generic hf-secret --from-literal=hf-token=$HF_TOKEN \
     --dry-run=client -o yaml | kubectl apply -f -
   ```
4. Edit `k8s/overlays/gpu/patch-serving-llm.yaml` if your chosen model needs
   more memory/VRAM than a single L4 (24 GB).

The `model-store` PVC is `premium-rwo` (ReadWriteOnce — a single pod owns the
model; use Filestore only if you must share one model across replicas).

## 6. RAG content & ops

- **Knowledge base**: the ingest CronJob indexes
  `local-data/10-internal-data-dump.md` (the synthetic internal-data dump) into
  the `rag-data` PVC, wiping the collection first (`ingest --wipe --paths`), so
  the RAG index always equals exactly that file (last run: **3,489 chunks** from
  `rag:1.0.3`). Because the `prod` overlay doesn't use GCS seeding, the file is
  copied into the PVC directly:
  ```bash
  kubectl -n rag-llm-langchain run seed-docs --image=busybox:1.36 --restart=Never \
    --command -- sh -c "sleep 600"
  kubectl cp local-data/10-internal-data-dump.md \
    rag-llm-langchain/seed-docs:/data/docs/10-internal-data-dump.md
  kubectl delete pod seed-docs -n rag-llm-langchain
  ```
  (The CronJob's `seed-docs` initContainer still supports seeding from GCS via
  `DOCS_GCS_URI` if you prefer `gsutil cp`; the node SA then needs
  `roles/storage.objectViewer` on the bucket.)
- **Chunking limits**: TEI (`BAAI/bge-small-en-v1.5`) accepts at most 32 items
  per request and 512 tokens per input. `rag/loader.py` uses `CHUNK_SIZE=300` /
  `CHUNK_OVERLAP=30`, and `rag/ingest.py` embeds in batches of `EMBED_BATCH=16`
  to stay under both limits. If you raise these, you will hit TEI 413 errors;
  bump `EMBED_BATCH` (and re-chunk) instead of increasing chunk size above ~500.
- **Persistence**: `rag-data` PVC uses `nfs-filestore` (Filestore CSI,
  `basic-hdd`, RWX, **100Gi**), required because the rag-service *and* the ingest
  pod both mount it. The StorageClass must set `instance-location: us-central1-a`
  (Immediate binding needs an explicit zone — provisioning fails without it).
- **Embedding model**: `BAAI/bge-small-en-v1.5` is served by TEI and is kept
  consistent between ingestion and query time via `EMBEDDING_MODEL`.
- **Realtime feeds**: the `feed-ingest` Deployment was **removed** — this is an
  internal-data knowledge base, so RSS/Atom ingestion isn't deployed. The code
  (`rag/feeds.py`) still exists if you want to re-enable it later.

## 7. CI/CD & monitoring

- **GitHub Actions** (`.github/workflows/build-push.yml`) builds and pushes
  model-loader/rag/gateway to Artifact Registry using Workload Identity
  Federation (no static keys). Required secrets:
  - `PROJECT_ID` = `rag-llm-langchain`
  - `WIF_PROVIDER` = your workload identity provider resource name
  - `WIF_SERVICE_ACCOUNT` = the SA with `roles/artifactregistry.writer`
- **ArgoCD** app manifest: `argocd/rag-llm-langchain-app.yaml` (targets `k8s/overlays/prod`,
  installed via the `argo/argo-cd` Helm chart in the `argocd` namespace).
- **Monitoring** (`monitoring/main.tf`): alerting policies for rag-llm-langchain workload
  readiness (gateway/vLLM/rag-service down), GPU-quota usage > 80%, and
  gpu-pool node not-ready. Set `notify_emails` and `terraform apply` in
  `monitoring/`.

## Configuration quick reference

| Component | Env | Default |
|-----------|-----|---------|
| Gateway  | `LLM_URL`, `LLM_MODEL`, `RAG_URL`, `API_KEY` | Ollama :8000/v1, `qwen2.5:0.5b`, rag-service :8080, auth off |
| RAG      | `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `LLM_BASE_URL`, `LLM_MODEL`, `RAG_PERSIST_DIR`, `EMBED_BATCH` | TEI :8001/v1, `BAAI/bge-small-en-v1.5`, Ollama :8000/v1, `qwen2.5:0.5b`, /data/chroma, 16 |
| Ollama    | `HF_MODEL` → `OLLAMA_MODELS` | Qwen2.5-0.5B (CPU) |






