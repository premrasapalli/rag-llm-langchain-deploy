# RAG + LLM LangChain Deployment on GKE — Model Serving · Inference

An end-to-end RAG + LLM (LangChain) deployment stack for Google Cloud (GKE):

- **Model serving** — vLLM (OpenAI-compatible) for the LLM, Text Embeddings
  Inference (TEI) for embeddings. Models auto-download into a shared volume.
- **RAG pipeline** — Chroma vector DB, document ingestion (CronJob), retrieval
  and a grounded-answer chain.
- **API gateway** — FastAPI exposing `/chat`, `/rag`, `/models`, `/healthz`.
- **GitOps-ready K8s manifests** — Kustomize `base` + overlays deployable via ArgoCD.
- **Terraform IaC** — GKE cluster (CPU + GPU pools) and Artifact Registry.

> **Deployment target**: `project_id = rag-llm-langchain`, region `us-central1`.
> Images live in Artifact Registry (`us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain`),
> NOT `gcr.io`.


## Architecture

```
                        ┌──────────────┐
   Ingress ──►  Gateway  ──►  serving-llm (vLLM, :8000 /v1)
   (/chat,/rag)          │      └── model-store PVC (HF download initContainer)
                        │  ──►  serving-embedding (TEI, :8001 /v1)
                        │  ──►  rag-service (:8080 /answer)
                        │         └── Chroma (rag-data PVC)
                        └── ──►  rag-ingest (CronJob, every 6h)
```

Query flow for RAG: user → `/rag` → retriever embeds query via TEI, top-k chunks
from Chroma → vLLM prompt with grounded context → answer.

## 1. Local bring-up (Docker Compose)

```bash
docker compose up --build -d
docker compose exec ingest python -m ingest --dir /data/docs   # index local-data/docs

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

```bash
export REGION=us-central1; export PROJECT_ID=rag-llm-langchain
gcloud auth configure-docker $REGION-docker.pkg.dev

docker build -t $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/model-loader:1.0.0 serving/ -f serving/Dockerfile.model-loader
docker build -t $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/rag:1.0.0 rag/
docker build -t $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/gateway:1.0.0 gateway/

# vLLM and TEI are pulled from upstream (pinned versions, not latest):
docker pull vllm/vllm-openai:v0.9.0
docker pull ghcr.io/huggingface/text-embeddings-inference:1.5
```

## 4. Deploy to GKE (Kustomize / GitOps)

The base manifests reference **bare image names** (`rag-llm-langchain/<name>`). Overlays set
the real registry and GPU settings, so the registry is configured in one place.

```bash
# CPU-only bring-up (small 0.5B model, no GPU):
kubectl apply -k k8s/overlays/prod

# GPU serving (needs gpu-pool + NVIDIA driver DaemonSet):
kubectl apply -k k8s/overlays/gke
```

Or via ArgoCD (manifest in `argocd/rag-llm-langchain-app.yaml`):

```bash
argocd app create rag-llm-langchain --repo https://github.com/premrasapalli/rag-llm-langchain-deploy.git \
  --path k8s/overlays/prod --dest-server https://kubernetes.default.svc --dest-namespace rag-llm-langchain \
  --sync-policy automated --self-heal --prune
```

`:latest` tag on images was removed — model-loader/rag/gateway are versioned
(`1.0.0`) and upstream vLLM/TEI are pinned, so builds are reproducible. The
GitHub Actions workflow also tags each push with `1.0.<epoch>-<sha>`.

Verify:

```bash
kubectl get pods -n rag-llm-langchain -w          # wait: serving-llm initContainer downloads model
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80
curl http://localhost:8080/chat -X POST -d '{"messages":[{"role":"user","content":"hi"}]}' -H 'Content-Type: application/json'
```

The first model download can take a few minutes depending on model size.

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

## 5. GPU serving (optional)

The base stays CPU-only (Qwen 2.5 0.5B). To serve a real model on the GPU pool:

1. Apply `k8s/overlays/gke` (or `k8s/overlays/gpu`). This overlay:
   - adds `nvidia.com/gpu: "1"` and a `cloud.google.com/gke-nodepool: gpu-pool`
     nodeSelector to `serving-llm`,
   - overrides `HF_MODEL` to `Qwen/Qwen2.5-7B-Instruct` (edit the patch to change
     the model) and bumps the model-store PVC to 100Gi,
   - deploys the NVIDIA GPU driver DaemonSet vendored from the official GKE
     manifest (`k8s/overlays/gpu/nvidia-driver/`) in namespace `kube-system`.
   (On GKE ≥ 1.32.2 the driver is also auto-installed by default — the
   DaemonSet is a safe fallback either way.)
2. For **gated models** (Llama-3, Gemma, …), set the HF token:
   ```bash
   kubectl -n rag-llm-langchain create secret generic hf-secret --from-literal=hf-token=$HF_TOKEN \
     --dry-run=client -o yaml | kubectl apply -f -
   ```
   The model-loader initContainer reads `HF_TOKEN` from `hf-secret`
   (`optional: true`, so open models work with no secret present).
3. Edit `k8s/overlays/gpu/patch-serving-llm.yaml` if your chosen model needs
   more memory/VRAM than a single L4 (24 GB).

The `model-store` PVC is `pd-ssd` (ReadWriteOnce — a single pod owns the model;
use Filestore only if you must share one model across replicas).

## 6. RAG content & ops

- **Seeding docs**: the ingest CronJob reads `/data/docs` from the `rag-data`
  PVC. Either copy docs there manually, or set `DOCS_GCS_URI`
  (e.g. `gs://my-bucket/docs`) on the seed initContainer, which `gsutil rsync`s
  from GCS before indexing. The workload identity service account needs
  `roles/storage.objectViewer` on the bucket.
- **Persistence**: `rag-data` PVC must use a ReadWriteMany-capable class
  (`nfs-filestore`, NetApp, etc.) because the rag-service *and* ingest pod both
  mount it. GCE `pd-ssd`/`standard-rwo` are RWO-only. Sized at 20Gi by default;
  grow with your corpus.
- **Embedding model**: `BAAI/bge-small-en-v1.5` is served by TEI and is kept
  consistent between ingestion and query time via `EMBEDDING_MODEL`.

## 7. CI/CD & monitoring

- **GitHub Actions** (`.github/workflows/build-push.yml`) builds and pushes
  model-loader/rag/gateway to Artifact Registry using Workload Identity
  Federation (no static keys). Required secrets:
  - `PROJECT_ID` = `rag-llm-langchain`
  - `WIF_PROVIDER` = your workload identity provider resource name
  - `WIF_SERVICE_ACCOUNT` = the SA with `roles/artifactregistry.writer`
- **ArgoCD** app manifest: `argocd/rag-llm-langchain-app.yaml` (targets `k8s/overlays/gke`).
- **Monitoring** (`monitoring/main.tf`): alerting policies for rag-llm-langchain workload
  readiness (gateway/vLLM/rag-service down), GPU-quota usage > 80%, and
  gpu-pool node not-ready. Set `notify_emails` and `terraform apply` in
  `monitoring/`.

## Configuration quick reference

| Component | Env | Default |
|-----------|-----|---------|
| Gateway  | `LLM_URL`, `LLM_MODEL`, `RAG_URL`, `API_KEY` | vLLM :8000/v1, `rag-llm-langchain-model`, rag-service :8080, auth off |
| RAG      | `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `LLM_BASE_URL`, `LLM_MODEL`, `RAG_PERSIST_DIR` | TEI :8001/v1, `BAAI/bge-small-en-v1.5`, vLLM :8000/v1, `rag-llm-langchain-model`, /data/chroma |
| vLLM     | `HF_MODEL` (initContainer), optional `HF_TOKEN` | `Qwen/Qwen2.5-0.5B-Instruct` |






