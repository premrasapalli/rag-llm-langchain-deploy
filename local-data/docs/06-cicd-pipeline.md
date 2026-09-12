# CI/CD Pipeline — From 0 to Live

Every command to set up, run, verify, and debug the GitHub Actions CI/CD
pipeline with Workload Identity Federation.

## Pipeline at a glance

```
push/PR → WIF auth → build amd64 → push to registry → apply manifests → rolling update → health check
```

---

## Step 1: Set up Workload Identity Federation (WIF)

### 1.1 Create the identity pool

```bash
gcloud iam workload-identity-pools create github-pool \
  --location=global --display-name="GitHub Actions Pool"
```

### 1.2 Create the OIDC provider

```bash
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --workload-identity-pool=github-pool --location=global \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-condition="assertion.repository_owner == 'premrasapalli'"
```

### 1.3 Create the service account

```bash
gcloud iam service-accounts create github-actions \
  --display-name="GitHub Actions SA"
```

### 1.4 Grant it permission to be impersonated

```bash
gcloud iam service-accounts add-iam-policy-binding \
  github-actions@rag-llm-langchain.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/premrasapalli/rag-llm-langchain-deploy"
```

### 1.5 Grant token-creator + Artifact Registry writer

```bash
gcloud projects add-iam-policy-binding rag-llm-langchain \
  --member="serviceAccount:github-actions@rag-llm-langchain.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

gcloud artifacts repositories add-iam-policy-binding rag-llm-langchain \
  --location=us-central1 \
  --member="serviceAccount:github-actions@rag-llm-langchain.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### 1.6 Get the provider resource name

```bash
gcloud iam workload-identity-pools providers describe github-provider \
  --workload-identity-pool=github-pool --location=global \
  --format="value(name)"
# Output: projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

### 1.7 Set GitHub repository Variables and Secrets

```bash
gh variable set WIF_PROVIDER \
  --body "projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider"

gh variable set WIF_SERVICE_ACCOUNT \
  --body "github-actions@rag-llm-langchain.iam.gserviceaccount.com"

gh secret set PROJECT_ID --body "rag-llm-langchain"
```

> **The gotcha we hit:** these must be repository **Variables** (not Secrets)
> because the workflow reads them via `${{ vars.* }}`.

---

## Step 2: The workflow (`.github/workflows/build-push.yml`)

```yaml
permissions:
  contents: read
  id-token: write          # required for OIDC token exchange

jobs:
  build:
    steps:
      - uses: actions/checkout@v4

      - name: Authenticate to Google Cloud (Workload Identity Federation)
        id: auth
        uses: google-github-actions/auth@v2
        with:
          token_format: access_token
          workload_identity_provider: ${{ vars.WIF_PROVIDER }}
          service_account: ${{ vars.WIF_SERVICE_ACCOUNT }}
          access_token_lifetime: "600s"

      - name: Configure docker for Artifact Registry
        run: |
          echo "${{ steps.auth.outputs.access_token }}" | \
            docker login -u oauth2accesstoken --password-stdin \
            https://${{ env.REGION }}-docker.pkg.dev

      - name: Build & push gateway
        uses: docker/build-push-action@v6
        with:
          context: gateway
          push: true
          tags: ${{ env.REPO }}/gateway:1.0.0
```

---

## Step 3: Trigger and watch

### Push to main (triggers the workflow)

```bash
git add . && git commit -m "ci: test pipeline" && git push origin main
```

### Watch the run

```bash
gh run list --limit=5
gh run watch                    # live stream of the running job
gh run view --log               # full logs after completion
```

---

## Step 4: Verify each stage

### 4.1 WIF auth passed

```bash
gh run view --log | grep "Authenticating to Google Cloud"
# Should show "Successfully authenticated" — no "access denied"
```

### 4.2 Images were pushed

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain \
  --sort-by=UPDATE_TIME | head -10
# gateway:1.0.0, rag:1.0.0, model-loader:1.0.0 with recent timestamps
```

### 4.3 Pods picked up the new image

```bash
kubectl -n rag-llm-langchain get pods -o wide
kubectl -n rag-llm-langchain describe deploy gateway | grep -A5 Image
# Image: us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain/gateway:1.0.0
```

### 4.4 Health check from the public IP

```bash
curl -s http://34.63.204.167/healthz    # {"status":"ok"}
curl -s http://34.63.204.167/models     # qwen2.5:0.5b
```

---

## Rolling update (what Kubernetes does in the background)

1. New ReplicaSet created with the new pod definition.
2. New pod started with the (re-pulled) image — `imagePullPolicy: Always`
   ensures the latest `1.0.0` is used, not a cached copy.
3. Readiness probes (`/healthz`) must pass before old pod is terminated.
4. If the new pod fails probes repeatedly → rollout stops → `CrashLoopBackOff`.

---

## Manual deploy (without CI)

```bash
# Build locally via Cloud Build (still x86, amd64 images)
gcloud builds submit --region=us-central1 --config=cloudbuild.yaml .

# Apply manifests
kubectl apply -k k8s/overlays/prod

# Force new pods to pull the fresh image
kubectl -n rag-llm-langchain rollout restart deploy/gateway deploy/rag-service \
  deploy/serving-llm deploy/serving-embedding
```

---

## Rollback

```bash
# Roll back to previous image tag by editing the overlay
kubectl -n rag-llm-langchain rollout undo deploy/gateway
kubectl -n rag-llm-langchain rollout undo deploy/rag-service

# Or force a previous git commit
git checkout <commit-sha> -- k8s/
kubectl apply -k k8s/overlays/prod
```

---

## What actually happened in this project

1. Edited `rag/requirements.txt` (chromadb pin) and pushed.
2. GitHub Actions (WIF auth) built `rag-llm-langchain/rag:1.0.0` for amd64, pushed to
   Artifact Registry.
3. `kubectl rollout restart deploy/rag-service` (imagePullPolicy: `Always`)
   pulled the fresh tag.
4. Readiness probes confirmed new pod healthy; old pod retired.
5. Manual ingest re-indexed docs with the new library.

---

## Summary: what each piece proves

| Piece | Value |
|-------|-------|
| Identity Pool | `github-pool` (global) |
| OIDC Provider | `github-provider` |
| Service Account | `github-actions@rag-llm-langchain.iam.gserviceaccount.com` |
| WIF Roles | `workloadIdentityUser` + `serviceAccountTokenCreator` |
| Registry Role | `artifactregistry.writer` on `rag-llm-langchain` |
| GitHub Variables | `WIF_PROVIDER`, `WIF_SERVICE_ACCOUNT` |
| GitHub Secret | `PROJECT_ID` |

**Result: the whole pipeline works — no keys stored in the repo.**
