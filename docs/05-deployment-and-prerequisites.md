# Deployment, Prerequisites & End-to-End — From 0 to Live

Every prerequisite check, infrastructure command, and deploy step — from a blank
machine to a live public URL.

---

## Phase 0 — Prerequisites (from a blank machine)

### 0.1 Install tools

```bash
brew install --cask google-cloud-sdk
brew install kubectl terraform docker
brew install gh   # GitHub CLI (optional, used for WIF setup)
```

### 0.2 Authenticate and set project

```bash
gcloud auth login
gcloud config set project rag-llm-langchain
gcloud auth application-default login    # for terraform
```

### 0.3 Link billing (this blocked us — fix early)

```bash
gcloud billing projects link rag-llm-langchain \
  --billing-account=01716C-ECBC7F-34FFF7
gcloud billing projects describe rag-llm-langchain   # billingEnabled: true
```

> If the GKE API suddenly returns "this API method requires billing to be
> enabled", the billing link fell off. Re-link the account AND verify the Cloud
> Billing API is enabled, then wait a few minutes for propagation.

### 0.4 Enable required APIs

```bash
gcloud services enable compute.googleapis.com \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  file.googleapis.com \
  storage-api.googleapis.com
```

### 0.5 Check quotas (GPU quota bit us — check before designing)

```bash
gcloud compute regions describe us-central1 \
  --format='table(quotas[].metric,quotas[].limit,quotas[].usage)'
# GPUS_ALL_REGIONS was 0 globally — go CPU-only for now
```

### 0.6 Get cluster credentials

```bash
gcloud container clusters get-credentials rag-llm-langchain-cluster \
  --region=us-central1 --project=rag-llm-langchain
kubectl config current-context    # should print the rag-llm-langchain cluster
```

### 0.7 Verify Docker works locally (arm64 builds will fail on amd64 nodes)

```bash
docker --version
uname -m   # arm64 = Mac; builds MUST pass --platform linux/amd64
```

Because the local machine is Apple Silicon, `docker build` emits arm64 by
default. Always build with `--platform linux/amd64` for the GKE amd64 nodes.

---

## Phase A — Terraform Infrastructure

### A1. Initialize and apply

```bash
terraform init
terraform plan          # ALWAYS preview first
terraform apply
```

### A2. Verify infrastructure

```bash
gcloud container clusters list
gcloud container node-pools list --cluster rag-llm-langchain-cluster --region us-central1
gcloud artifacts repositories list --location=us-central1
terraform output
```

### A3. GPU pool — disabled by default

```bash
cat terraform/terraform.tfvars | grep enable_gpu_pool
# enable_gpu_pool = false  (GPUS_ALL_REGIONS quota was 0)
```

---

## Phase B — Storage: Filestore CSI driver + storage classes

### B1. Enable Filestore CSI on the cluster

```bash
gcloud container clusters update rag-llm-langchain-cluster --region us-central1 \
  --update-addons=GcpFilestoreCsiDriver=ENABLED
```

### B2. Verify storage classes exist after kustomize apply

```bash
kubectl get storageclass
# premium-rwo          (default, SSD, RWO)
# nfs-filestore        (Filestore CSI, RWX — for rag-data)
```

---

## Phase C — Build and push images (local, amd64)

### C1. Build the rag image (SQL/CPU changes only affect `rag/`)

The `gateway` and `model-loader` images are stable (`1.0.0`). Bump and rebuild
only `rag` when its code changes; `EMBED_BATCH`/chunking lives there.

```bash
export REGION=us-central1; export PROJECT_ID=rag-llm-langchain
docker build --platform linux/amd64 \
  -t $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/rag:1.0.3 rag/
docker push $REGION-docker.pkg.dev/$PROJECT_ID/rag-llm-langchain/rag:1.0.3
```

(Optional, needs Cloud Build API + IAM on your account:
`gcloud builds submit --config=cloudbuild.yaml .`.)

### C2. Verify images in Artifact Registry

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain
# gateway:1.0.0, rag:1.0.3, model-loader:1.0.0
```

### C3. Grant the node SA pull rights

```bash
gcloud projects add-iam-policy-binding rag-llm-langchain \
  --member="serviceAccount:rag-llm-langchain-gke@rag-llm-langchain.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

---

## Phase D — Deploy workloads (Kustomize / ArgoCD)

### D1. Apply base + prod overlay

```bash
kubectl apply -k k8s/overlays/prod
```

> Base alone carries short image names (`rag-llm-langchain/gateway`); the prod overlay
> rewrites them to the full registry path. Skipping the overlay = pods pull
> `docker.io/rag-llm-langchain/...` and fail.

This cluster is managed by ArgoCD instead — install it and register the app
(see README §4), or patch a sync to roll out a pushed change:

```bash
kubectl patch app rag-llm-langchain -n argocd --type merge \
  -p '{"operation":{"sync":{"revision":"main","prune":true}}}'
```

### D2. Wait for everything to become healthy

```bash
kubectl -n rag-llm-langchain rollout status deploy/gateway deploy/rag-service \
  deploy/serving-llm deploy/serving-embedding
kubectl -n rag-llm-langchain get pods
```

Expected:
```
gateway-xxx            2/2   Running
rag-service-xxx        1/1   Running
serving-llm-xxx        1/1   Running
serving-embedding-xxx  1/1   Running
```

---

## Phase E — Reach the API (port-forward / LB)

The `gateway` is exposed as a ClusterIP + GCE Ingress (HTTP, for testing). For
quick local access and the smoke tests below, use a port-forward:

```bash
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80 & sleep 3
curl -s http://localhost:8080/healthz    # {"status":"ok"}
curl -s http://localhost:8080/models     # qwen2.5:0.5b
kill %1
```

To get a public IP instead (not configured in this deploy), create a
LoadBalancer service:

```bash
kubectl -n rag-llm-langchain create service loadbalancer gateway-lb --tcp=80:8080 \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rag-llm-langchain get svc gateway-lb
# EXTERNAL-IP: 34.63.204.167  <- copy this IP
```

---

## Phase F — Seed RAG knowledge base

### F1. Copy the internal-data doc into the rag-data PVC

The live deploy seeds the file with a scratch pod on the shared Filestore PVC:

```bash
kubectl -n rag-llm-langchain run seed-docs \
  --image=busybox:1.36 --restart=Never --command -- sh -c "sleep 600"
kubectl cp local-data/10-internal-data-dump.md \
  rag-llm-langchain/seed-docs:/data/docs/10-internal-data-dump.md
kubectl delete pod seed-docs -n rag-llm-langchain
```

(For GCS seeding, set `DOCS_GCS_URI` on the CronJob and grant the node SA
`roles/storage.objectViewer` on the bucket.)

### F2. Run manual ingest

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n rag-llm-langchain
kubectl wait --for=condition=complete job/rag-ingest-manual -n rag-llm-langchain --timeout=300s
kubectl logs -n rag-llm-langchain job/rag-ingest-manual --tail=5
# Ingested ... -> N chunks
```

### F3. Verify vector store persisted

```bash
R=$(kubectl get pod -n rag-llm-langchain -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n rag-llm-langchain "$R" -- ls /data/chroma   # chroma.sqlite3 must exist
kubectl exec -n rag-llm-langchain "$R" -- python -c "from config import get_store; print(get_store()._collection.count())"
# > 0
```

### F4. Test RAG

```bash
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80 & sleep 3
curl -s -X POST http://localhost:8080/rag -H 'Content-Type: application/json' \
  -d '{"query":"What is RAG?"}' | python3 -m json.tool
# Grounded answer citing your docs
kill %1
```

---

## Teardown (in order)

```bash
kubectl delete -k k8s/base            # remove workloads first
terraform destroy                      # then infrastructure
# Note: PVCs persist until explicitly deleted
```
