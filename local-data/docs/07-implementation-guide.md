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
gcloud config set project aiml-project-idp
gcloud auth application-default login   # for terraform locally
```

### 0.3 Enable billing on the project. Why?
No billing = virtually every create call fails with confusing quota/denial
errors. This bit us.

```bash
gcloud billing projects link aiml-project-idp \
  --billing-account=01716C-ECBC7F-34FFF7
gcloud billing projects describe aiml-project-idp   # expect billingEnabled: true
```

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
gcloud container clusters get-credentials genai-cluster \
  --region=us-central1 --project=aiml-project-idp
kubectl config current-context             # should print the genai cluster
```

---

## Phase A — Infrastructure (Terraform)

### A1. Write the infrastructure as code. Why?
Everything (VPC, cluster, node pools, registry, static IP) is declared in
`terraform/` so it is reproducible and reviewable instead of console clicking.

Files:
- `providers.tf` — Google provider + variables (`region`, `gpu_zone`,
  `enable_gpu_pool`, ...)
- `main.tf` — VPC network, GKE cluster (`genai-cluster`), node pools, artifact
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
gcloud container node-pools list --cluster genai-cluster --region us-central1
```

---

## Phase B — Storage: enable Filestore CSI + create storage classes

### B1. Enable the Filestore CSI add-on on the cluster. Why?
`rag-data` needs a read-write-many (RWX) volume shared by the ingest job
(writer) and the rag service (reader). Filestore is the managed way to get RWX
on GKE.

```bash
gcloud container clusters update genai-cluster --region=us-central1 \
  --update-addons=GcpFilestoreCsiDriver=ENABLED
```

> Note the key is `GcpFilestoreCsiDriver`, not "FilestoreCSI".

### B2. Deploy the storage classes. Why?
Two access profiles:
- `premium-rwo` (SSD, single-writer) → `model-store`, `embed-store`
- `nfs-filestore` (Filestore CSI, `basic-hdd`) → `rag-data` (RWX)

They are already in `k8s/base/storageclass-filestore.yaml` and applied with the
base manifests in Phase D.

---

## Phase C — Build & push the container images

### C1. Build with Cloud Build (x86 hosts). Why?
Local `docker build` on Apple Silicon produces arm64 images that amd64 GKE
nodes refuse with `exec format error`. Cloud Build runs on x86, so images always
match the cluster.

```bash
gcloud builds submit --region=us-central1 --config=cloudbuild.yaml .
```

This builds and pushes `gateway:1.0.0`, `rag:1.0.0`, `model-loader:1.0.0`
(linux/amd64) to `us-central1-docker.pkg.dev/aiml-project-idp/genai`.

Or build the three images manually:

```bash
gcloud builds submit --region=us-central1 \
  --tag=us-central1-docker.pkg.dev/aiml-project-idp/genai/gateway:1.0.0 gateway/
```

Verify they landed:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/aiml-project-idp/genai
```

### C2. Grant the nodes pull rights. Why?
Cluster nodes pull images as `genai-gke@aiml-project-idp.iam.gserviceaccount.com`.
Without `roles/artifactregistry.reader`, every pod lands in `ImagePullBackOff`.

```bash
gcloud projects add-iam-policy-binding aiml-project-idp \
  --member="serviceAccount:genai-gke@aiml-project-idp.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

Also grant it read access to the docs bucket (for seed-docs, Phase F):

```bash
gsutil iam ch \
  serviceAccount:genai-gke@aiml-project-idp.iam.gserviceaccount.com:objectViewer \
  gs://aiml-project-idp-rag-docs
```

---

## Phase D — Deploy the workloads (Kustomize)

### D1. Apply the base manifest set. Why?
One declarative pass creates namespace `genai`, secrets, services, deployments,
the ingest CronJob, PVCs, and the storage class.

```bash
kubectl apply -k k8s/base
```

### D2. Apply the prod overlay. Why?
Base carries short names like `genai/gateway:1.0.0`. The `prod` overlay rewrites
them to the full registry path via the `images:` kustomize transformer. Applying
base alone makes pods pull `docker.io/genai/gateway` and fail.

```bash
kubectl apply -k k8s/overlays/prod
```

### D3. Watch everything become healthy. Why?
Proof the rollout converged.

```bash
kubectl -n genai get pods -w
kubectl -n genai rollout status deploy/gateway deploy/rag-service \
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

## Phase E — Expose it with a public URL

### E1. Create a LoadBalancer service. Why?
The GCE Ingress controller did not provision a load balancer, so we exposed the
gateway with a classic `type: LoadBalancer` service, which the cloud provider
fulfills reliably.

```bash
kubectl -n genai create service loadbalancer gateway-lb --tcp=80:8080 \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n genai get svc gateway-lb
# NAME         TYPE           CLUSTER-IP    EXTERNAL-IP
# gateway-lb   LoadBalancer   10.x.x.x      34.63.204.167   <- copy this IP
```

### E2. Verify health from the public URL. Why?
The IP is useless until the API actually answers.

```bash
curl -s http://34.63.204.167/healthz        # {"status":"ok"}
curl -s http://34.63.204.167/models         # qwen2.5:0.5b
```

---

## Phase F — Seed the RAG knowledge base

### F1. Upload source documents to GCS. Why?
The CronJob's `seed-docs` initContainer rsyncs `DOCS_GCS_URI` into `/data/docs`.
GCS is a durable, versionable doc source instead of `kubectl cp` races.

```bash
gcloud storage buckets create gs://aiml-project-idp-rag-docs --location=us-central1
gcloud storage cp -r local-data/docs gs://aiml-project-idp-rag-docs/docs
```

The ingest CronJob and its manual clone read this URI from the manifest
(`DOCS_GCS_URI`).

### F2. Run a manual ingest. Why?
So the index exists immediately — the 6-hourly CronJob is just the safety net.

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl wait --for=condition=complete job/rag-ingest-manual -n genai --timeout=300s
kubectl logs -n genai job/rag-ingest-manual --tail=5
# expect: Ingested ... -> N chunks  /  Done. Total chunks: N
```

### F3. Verify the vector store persisted. Why?
A silent failure mode (in-memory Chroma) logs "success" but writes nothing.

```bash
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n genai "$R" -- ls /data/chroma          # must show chroma.sqlite3
kubectl exec -n genai "$R" -- python -c \
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
  github-actions@aiml-project-idp.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/premrasapalli/gke-genai-deployment"

gcloud projects add-iam-policy-binding aiml-project-idp \
  --member="serviceAccount:github-actions@aiml-project-idp.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### G2. Grant the pipeline Artifact Registry write. Why?
It pushes the images it builds.

```bash
gcloud artifacts repositories add-iam-policy-binding genai \
  --location=us-central1 \
  --member="serviceAccount:github-actions@aiml-project-idp.iam.gserviceaccount.com" \
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
  --body "github-actions@aiml-project-idp.iam.gserviceaccount.com"

gh secret set PROJECT_ID --body "aiml-project-idp"
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
outside world.

```bash
IP=34.63.204.167

curl -s http://$IP/healthz                                    # {"status":"ok"}
curl -s http://$IP/models                                     # qwen2.5:0.5b

curl -s -X POST http://$IP/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello"}]}'

curl -s -X POST http://$IP/rag -H 'Content-Type: application/json' \
  -d '{"query":"What is RAG?"}'                               # grounded answer
```

---

## Everyday operations

| Task                         | Command                                                       |
| ---------------------------- | ------------------------------------------------------------- |
| Watch the workloads          | `kubectl -n genai get pods -w`                                |
| Tail the gateway logs        | `kubectl -n genai logs deploy/gateway -f`                     |
| Re-ingest the knowledge base | `kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai` |
| Restart after image rebuild  | `kubectl -n genai rollout restart deploy/gateway deploy/rag-service deploy/serving-llm deploy/serving-embedding` |
| See the ingress address      | `gcloud compute addresses describe gateway-static --region=us-central1 --format='value(address)'` |
| Tear down the app            | `kubectl delete -k k8s/base`                                  |
| Tear down the cluster        | `terraform destroy` (data volumes persist until deleted)       |

Every phase exists because the previous one left a gap: infra before cluster,
registry before pull, PVs before data, overlay before images, LB before URL,
docs+GCS before RAG, WIF before CI, and the smoke test before you call it done.