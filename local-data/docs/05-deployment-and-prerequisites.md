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
gcloud config set project aiml-project-idp
gcloud auth application-default login    # for terraform
```

### 0.3 Link billing (this blocked us — fix early)

```bash
gcloud billing projects link aiml-project-idp \
  --billing-account=01716C-ECBC7F-34FFF7
gcloud billing projects describe aiml-project-idp   # billingEnabled: true
```

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
gcloud container clusters get-credentials genai-cluster \
  --region=us-central1 --project=aiml-project-idp
kubectl config current-context    # should print the genai cluster
```

### 0.7 Verify Docker works locally (arm64 builds will fail on amd64 nodes)

```bash
docker --version
uname -m   # arm64 = Mac; builds must target linux/amd64 via Cloud Build
```

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
gcloud container node-pools list --cluster genai-cluster --region us-central1
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
gcloud container clusters update genai-cluster --region us-central1 \
  --update-addons=GcpFilestoreCsiDriver=ENABLED
```

### B2. Verify storage classes exist after kustomize apply

```bash
kubectl get storageclass
# premium-rwo          (default, SSD, RWO)
# nfs-filestore        (Filestore CSI, RWX — for rag-data)
```

---

## Phase C — Build and push images (Cloud Build, amd64)

### C1. Build all three images

```bash
gcloud builds submit --region=us-central1 --config=cloudbuild.yaml .
```

### C2. Verify images in Artifact Registry

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/aiml-project-idp/genai
# gateway:1.0.0, rag:1.0.0, model-loader:1.0.0
```

### C3. Grant the node SA pull rights

```bash
gcloud projects add-iam-policy-binding aiml-project-idp \
  --member="serviceAccount:genai-gke@aiml-project-idp.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

---

## Phase D — Deploy workloads (Kustomize)

### D1. Apply base + prod overlay

```bash
kubectl apply -k k8s/overlays/prod
```

> Base alone carries short image names (`genai/gateway`); the prod overlay
> rewrites them to the full registry path. Skipping the overlay = pods pull
> `docker.io/genai/...` and fail.

### D2. Wait for everything to become healthy

```bash
kubectl -n genai rollout status deploy/gateway deploy/rag-service \
  deploy/serving-llm deploy/serving-embedding
kubectl -n genai get pods
```

Expected:
```
gateway-xxx            2/2   Running
rag-service-xxx        1/1   Running
serving-llm-xxx        1/1   Running
serving-embedding-xxx  1/1   Running
```

---

## Phase E — Public URL

### E1. Create the LoadBalancer

```bash
kubectl -n genai create service loadbalancer gateway-lb --tcp=80:8080 \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n get svc gateway-lb
# EXTERNAL-IP: 34.63.204.167
```

### E2. Verify health from the public IP

```bash
curl -s http://34.63.204.167/healthz    # {"status":"ok"}
curl -s http://34.63.204.167/models     # qwen2.5:0.5b
```

---

## Phase F — Seed RAG knowledge base

### F1. Create GCS bucket and upload docs

```bash
gcloud storage buckets create gs://aiml-project-idp-rag-docs --location=us-central1
gcloud storage cp -r local-data/docs gs://aiml-project-idp-rag-docs/docs
gsutil iam ch \
  serviceAccount:genai-gke@aiml-project-idp.iam.gserviceaccount.com:objectViewer \
  gs://aiml-project-idp-rag-docs
```

### F2. Run manual ingest

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl wait --for=condition=complete job/rag-ingest-manual -n genai --timeout=300s
kubectl logs -n genai job/rag-ingest-manual --tail=5
# Ingested ... -> N chunks
```

### F3. Verify vector store persisted

```bash
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n genai "$R" -- ls /data/chroma   # chroma.sqlite3 must exist
kubectl exec -n genai "$R" -- python -c "from config import get_store; print(get_store()._collection.count())"
# > 0
```

### F4. Test RAG

```bash
curl -s -X POST http://34.63.204.167/rag -H 'Content-Type: application/json' \
  -d '{"query":"What is RAG?"}' | python3 -m json.tool
# Grounded answer citing your docs
```

---

## Teardown (in order)

```bash
kubectl delete -k k8s/base            # remove workloads first
terraform destroy                      # then infrastructure
# Note: PVCs persist until explicitly deleted
```
