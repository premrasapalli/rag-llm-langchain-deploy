# Implementation Guide — From ZERO to Live (Step by Step, Real Commands)

This is the exact order of operations we used to bring this platform from
nothing to a live public URL. Every step includes the **real commands** and
**why you add it**, so you can replay the whole process from scratch or repair
any part of it.

> Legend: run on your laptop (`local`), in Google Cloud (`gcloud`), or interacts
> with the cluster (`kubectl`).

---

## Phase 0 — Prerequisites (from a blank machine)

### 0.1 Install the tools we used. Why?
Everything below is driven from the command line.

```bash
# gcloud (GCP CLI) + kubectl (Kubernetes CLI) + terraform (IaC)
brew install --cask google-cloud-sdk
brew install kubectl terraform

# gh (GitHub CLI) — optional, but we used it for repo settings
brew install gh
```

### 0.2 Authenticate gcloud and pick the project. Why?
Without this, gcloud/kubectl/terraform cannot talk to GCP.

```bash
gcloud auth login
gcloud config set project rag-llm-langchain
gcloud auth application-default login   # for terraform locally
```

### 0.3 Enable billing on the project. Why?
No billing = virtually every create call fails with confusing quota/denial
errors. This bit us twice.

```bash
gcloud billing projects link rag-llm-langchain \
  --billing-account=01716C-ECBC7F-34FFF7
gcloud billing projects describe rag-llm-langchain   # expect billingEnabled: true
```

If a previously-working cluster suddenly returns "requires billing to be
enabled", the project lost its billing link: re-run the link above, enable
`cloudbilling.googleapis.com`, and wait for propagation.

### 0.4 Enable the APIs Terraform will call. Why?
The service APIs must exist before resources can be created.

```bash
gcloud services enable compute.googleapis.com \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  file.googleapis.com \
  storage-api.googleapis.com
```

### 0.5 Take GKE credentials. Why?
`kubectl` needs a kubeconfig pointing at the cluster.

```bash
gcloud container clusters get-credentials rag-llm-langchain-cluster \
  --region=us-central1 --project=rag-llm-langchain
kubectl config current-context             # should print the rag-llm-langchain cluster
```

---

## Phase A — Infrastructure (Terraform)

### A1. Write the infrastructure as code. Why?
Everything (VPC, cluster, node pools, registry, static IP) is declared in
`terraform/` so it is reproducible and reviewable instead of console clicking.

Files:
- `providers.tf` — Google provider + variables (`region`, `gpu_zone`,
  `enable_gpu_pool`, ...)
- `main.tf` — VPC network, GKE cluster (`rag-llm-langchain-cluster`), node pools, artifact
  repository, static IP
- `backend.tf` — GCS bucket storing remote state
- `terraform.tfvars` — the actual variable values applied

### A2. Initialize, preview, and apply. Why?
`plan` shows the exact diff; never `apply` blind.

```bash
terraform init
terraform plan          # preview changes
terraform apply -auto-approve
```

Key outputs afterward:

```bash
terraform output              # cluster_endpoint, gateway_static_ip
```

### A3. CPU-only now, GPU pool declared but disabled. Why?
`enable_gpu_pool = false` in `terraform.tfvars` — the global GPU quota
`GPUS_ALL_REGIONS` is 0, so a GPU pool would repeatedly fail to provision.
Keeping its config in Terraform means flipping it on later is one change.

```bash
cat terraform/terraform.tfvars
# enable_gpu_pool = false
# gpu_zone = "us-central1-a"   # L4 GPUs only exist in us-central1-a/b/c
```

Verify the cluster came up:

```bash
gcloud container clusters list
gcloud container node-pools list --cluster rag-llm-langchain-cluster --region us-central1
```

---

## Phase B — Storage: enable Filestore CSI + create storage classes

### B1. Enable the Filestore CSI add-on on the cluster. Why?
`rag-data` needs a read-write-many (RWX) volume shared by the ingest job
(writer) and the rag service (reader). Filestore is the managed way to get RWX
on GKE.

```bash
gcloud container clusters update rag-llm-langchain-cluster --region=us-central1 \
  --update-addons=GcpFilestoreCsiDriver=ENABLED
```

> Note the key is `GcpFilestoreCsiDriver`, not "FilestoreCSI".

### B2. Deploy the storage classes. Why?
Two access profiles:
- `premium-rwo` (SSD, single-writer) → `model-store`, `embed-store`
- `nfs-filestore` (Filestore CSI, `basic-hdd`) → `rag-data` (RWX)

They are already in `k8s/base/storageclass-filestore.yaml` and applied with the
base manifests in Phase D.

> **Gotcha:** the `nfs-filestore` StorageClass must set
> `instance-location: us-central1-a`. With Immediate binding and no topology
> from a pod, provisioning fails with `no available topology found` unless the
> zone is explicit. StorageClass `parameters` are immutable, so changing them on
> an existing class requires delete + recreate (ArgoCD handles this).

---

## Phase C — Build & push the container images

### C1. Build locally with `--platform linux/amd64`. Why?
Local `docker build` on Apple Silicon produces arm64 images that amd64 GKE
nodes refuse with `exec format error`. Only `rag` is versioned and changes often
(it holds the chunker and `EMBED_BATCH` batching); `gateway`/`model-loader` are
stable at `1.0.0`.

```bash
export AR=us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain
docker build --platform linux/amd64 -t $AR/rag:1.0.3 rag/
docker push $AR/rag:1.0.3
```

(GPU/unkind images don't apply here — TEI comes from upstream and Ollama is
pulled by the deployment.)

Verify they landed:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain
```

(Cloud Build alternative: `gcloud builds submit --config=cloudbuild.yaml .` —
requires the Cloud Build API and IAM on your account.)

### C2. Grant the nodes pull rights. Why?
Cluster nodes pull images as `rag-llm-langchain-gke@rag-llm-langchain.iam.gserviceaccount.com`.
Without `roles/artifactregistry.reader`, every pod lands in `ImagePullBackOff`.

```bash
gcloud projects add-iam-policy-binding rag-llm-langchain \
  --member="serviceAccount:rag-llm-langchain-gke@rag-llm-langchain.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

Also grant it read access to the docs bucket (for seed-docs, Phase F):

```bash
gsutil iam ch \
  serviceAccount:rag-llm-langchain-gke@rag-llm-langchain.iam.gserviceaccount.com:objectViewer \
  gs://rag-llm-langchain-docs
```

---

## Phase D — Deploy the workloads (Kustomize / ArgoCD)

### D1. Apply the base manifest set. Why?
One declarative pass creates namespace `rag-llm-langchain`, secrets, services, deployments,
the ingest CronJob, PVCs, and the storage class.

```bash
kubectl apply -k k8s/base
```

### D2. Apply the prod overlay. Why?
Base carries short names like `rag-llm-langchain/gateway:1.0.0`. The `prod` overlay rewrites
them to the full registry path via the `images:` kustomize transformer. Applying
base alone makes pods pull `docker.io/rag-llm-langchain/gateway` and fail.

```bash
kubectl apply -k k8s/overlays/prod
```

### D3. OR deploy via ArgoCD (how this cluster is actually run). Why?
GitOps means a push to `main` is the deploy — no manual kubectl apply.

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm upgrade --install argocd argo/argo-cd --namespace argocd --create-namespace --wait
kubectl apply -f argocd/rag-llm-langchain-app.yaml   # targets k8s/overlays/prod

# Watch it sync, or force a sync after a push:
kubectl get app -n argocd
kubectl patch app rag-llm-langchain -n argocd --type merge \
  -p '{"operation":{"sync":{"revision":"main","prune":true}}}'
```

### D3. Watch everything become healthy. Why?
Proof the rollout converged.

```bash
kubectl -n rag-llm-langchain get pods -w
kubectl -n rag-llm-langchain rollout status deploy/gateway deploy/rag-service \
  deploy/serving-llm deploy/serving-embedding
```

Expected:

```
gateway-xxxxxxxxxx-ccccc            2/2     Running
rag-service-xxxxxxxxxx-ccccc        1/1     Running
serving-llm-xxxxxxxxxx-ccccc        1/1     Running
serving-embedding-xxxxxxxxxx-ccccc  1/1     Running
```

If model/embedding downloads take time, wait for `model-loader` init containers
to finish before the pods show `Running`.

---

## Phase E — Reach the API

### E1. Port-forward (quick local access). Why?
The gateway Service is ClusterIP; a port-forward proves the API works without
exposing it publicly.

```bash
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80 & sleep 3
curl -s http://localhost:8080/healthz        # {"status":"ok"}
curl -s http://localhost:8080/models         # qwen2.5:0.5b
kill %1
```

### E2. Optional public LoadBalancer. Why?
If you need a public IP instead of the GCE Ingress, create an LB service:

```bash
kubectl -n rag-llm-langchain create service loadbalancer gateway-lb --tcp=80:8080 \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rag-llm-langchain get svc gateway-lb
# NAME         TYPE           CLUSTER-IP    EXTERNAL-IP
# gateway-lb   LoadBalancer   10.0.x.x      34.63.204.167   <- copy this IP
```

---

## Phase F — Seed the RAG knowledge base

### F1. Seed source documents into the PVC. Why?
The ingest CronJob reads the single internal-data file from `/data/docs` on the
shared `rag-data` PVC. The live CPU deploy copies it there with `kubectl cp`
(GCS seeding via `DOCS_GCS_URI` is supported but not configured):

```bash
kubectl -n rag-llm-langchain run seed-docs \
  --image=busybox:1.36 --restart=Never --command -- sh -c "sleep 600"
kubectl cp local-data/10-internal-data-dump.md \
  rag-llm-langchain/seed-docs:/data/docs/10-internal-data-dump.md
kubectl delete pod seed-docs -n rag-llm-langchain
```

### F2. Run a manual ingest. Why?
So the index exists immediately — the 6-hourly CronJob is just the safety net.

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n rag-llm-langchain
kubectl wait --for=condition=complete job/rag-ingest-manual -n rag-llm-langchain --timeout=300s
kubectl logs -n rag-llm-langchain job/rag-ingest-manual --tail=5
# expect: Ingested ... -> N chunks  /  Done. Total chunks: N
```

### F3. Verify the vector store persisted. Why?
A silent failure mode (in-memory Chroma) logs "success" but writes nothing.

```bash
R=$(kubectl get pod -n rag-llm-langchain -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n rag-llm-langchain "$R" -- ls /data/chroma          # must show chroma.sqlite3
kubectl exec -n rag-llm-langchain "$R" -- python -c \
  "from config import get_store; print(get_store()._collection.count())"   # > 0
```

---

## Phase G — Automate with CI/CD (GitHub Actions + WIF)

### G1. Create the Workload Identity Federation plumbing. Why?
The pipeline must authenticate to GCP with **no stored keys**. GitHub
exchanges an OIDC token for short-lived Google credentials.

```bash
gcloud iam workload-identity-pools create github-pool --location=global

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --workload-identity-pool=github-pool --location=global \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-condition="assertion.repository_owner == 'premrasapalli'"

gcloud iam service-accounts create github-actions \
  --display-name="GitHub Actions SA"

gcloud iam service-accounts add-iam-policy-binding \
  github-actions@rag-llm-langchain.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/premrasapalli/rag-llm-langchain-deploy"

gcloud projects add-iam-policy-binding rag-llm-langchain \
  --member="serviceAccount:github-actions@rag-llm-langchain.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### G2. Grant the pipeline Artifact Registry write. Why?
It pushes the images it builds.

```bash
gcloud artifacts repositories add-iam-policy-binding rag-llm-langchain \
  --location=us-central1 \
  --member="serviceAccount:github-actions@rag-llm-langchain.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### G3. Wire up GitHub repo Variables & Secrets. Why?
The workflow reads these via `${{ vars.* }}` (Variables) and `${{ secrets.* }}`
(Secrets). We got burned storing the WIF values as Secrets while the workflow
read them as Variables.

```bash
gh variable set WIF_PROVIDER \
  --body "projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider"

gh variable set WIF_SERVICE_ACCOUNT \
  --body "github-actions@rag-llm-langchain.iam.gserviceaccount.com"

gh secret set PROJECT_ID --body "rag-llm-langchain"
```

### G4. Commit the workflow and let CI drive deploys. Why?
Now a push to `main` builds, pushes, and deploys on its own
(`.github/workflows/build-push.yml`):

```bash
git add .github/workflows/build-push.yml
git commit -m "ci: WIF-driven build and push"
git push origin main

gh run watch          # watch the pipeline go green
```

---

## Phase H — Smoke test the live contract. Why?

A "deployed" system is only done when its API contract is proven from the
cluster itself.

```bash
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80 & sleep 3

curl -s http://localhost:8080/healthz        # {"status":"ok"}
curl -s http://localhost:8080/models         # qwen2.5:0.5b

curl -s -X POST http://localhost:8080/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello"}]}'

curl -s -X POST http://localhost:8080/rag -H 'Content-Type: application/json' \
  -d '{"query":"What is RAG?"}'                       # grounded answer from internal docs

kill %1
```

---

## Everyday operations

| Task                         | Command                                                       |
| ---------------------------- | ------------------------------------------------------------- |
| Watch the workloads          | `kubectl -n rag-llm-langchain get pods -w`                                |
| Tail the gateway logs        | `kubectl -n rag-llm-langchain logs deploy/gateway -f`                     |
| Re-ingest the knowledge base | `kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n rag-llm-langchain` |
| Force ArgoCD sync after push | `kubectl patch app rag-llm-langchain -n argocd --type merge -p '{"operation":{"sync":{"revision":"main","prune":true}}}'` |
| Port-forward for local test  | `kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80`                    |
| Rebuild rag image after code change | `docker build --platform linux/amd64 -t $AR/rag:<NEW_TAG> rag/ && docker push $AR/rag:<NEW_TAG>` |
| Tear down the app            | `kubectl delete -k k8s/overlays/prod`                        |
| Tear down the cluster        | `terraform destroy` (data volumes persist until deleted)       |

Every phase exists because the previous one left a gap: infra before cluster,
registry before pull, PVs before data, overlay before images, ArgoCD before
continuous deploy, docs before RAG, WIF before CI, and the smoke test before
you call it done.