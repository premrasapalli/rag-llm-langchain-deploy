# Workload Identity Federation (WIF) — From Scratch to Working

This guide documents how Workload Identity Federation was configured for GitHub
Actions in this project, step by step with real commands, and how we finally
made the whole pipeline work end-to-end.

## What We Built

A GitHub Actions workflow that authenticates to Google Cloud **without any
stored service-account keys**. GitHub exchanges its OIDC token for a short-lived
Google Cloud credential via Workload Identity Federation, then builds and pushes
the Docker images to Artifact Registry.

## Final Working State

- **Workflow:** `.github/workflows/build-push.yml`
- **Identity Pool:** `projects/784802248985/locations/global/workloadIdentityPools/github-pool`
- **OIDC Provider:** `.../providers/github-provider`
  - Issuer: `https://token.actions.githubusercontent.com`
  - Attribute condition: `assertion.repository_owner == 'premrasapalli'`
- **Service Account:** `github-actions@rag-llm-langchain.iam.gserviceaccount.com`
- **GitHub Variables:**
  - `WIF_PROVIDER` = `projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider`
  - `WIF_SERVICE_ACCOUNT` = `github-actions@rag-llm-langchain.iam.gserviceaccount.com`
- **GitHub Secret:** `PROJECT_ID` = `rag-llm-langchain`

---

## Step 1 — Prerequisites

- A GCP project with billing enabled (`rag-llm-langchain`).
- `gcloud` CLI authenticated:
  ```bash
  gcloud auth login
  gcloud config set project rag-llm-langchain
  ```
- An Artifact Registry repository:
  ```bash
  gcloud artifacts repositories create rag-llm-langchain \
    --repository-format=docker \
    --location=us-central1 \
    --description="RAG LLM LangChain container images"
  ```
- The GitHub repository URL (e.g. `premrasapalli/rag-llm-langchain-deploy`).

---

## Step 2 — Create the Workload Identity Pool

```bash
gcloud iam workload-identity-pools create github-pool \
  --location=global \
  --display-name="GitHub Actions Pool"
```

---

## Step 3 — Create the OIDC Provider

```bash
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --workload-identity-pool=github-pool \
  --location=global \
  --display-name="GitHub Provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-condition="assertion.repository_owner == 'premrasapalli'"
```

This tells GCP to trust tokens issued by GitHub's OIDC endpoint, and only for
repos owned by `premrasapalli`.

---

## Step 4 — Create the Service Account

```bash
gcloud iam service-accounts create github-actions \
  --display-name="GitHub Actions SA"
```

---

## Step 5 — Allow the Pool to Impersonate the Service Account

```bash
gcloud iam service-accounts add-iam-policy-binding \
  github-actions@rag-llm-langchain.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/premrasapalli/rag-llm-langchain-deploy"
```

Replace `PROJECT_NUMBER` with your project number. This binding lets the GitHub
workflow exchange its OIDC token for an access token as this service account.

---

## Step 6 — Grant the Service Account the Permissions It Needs

The workflow needs to read an access token and push images:

```bash
gcloud projects add-iam-policy-binding rag-llm-langchain \
  --member="serviceAccount:github-actions@rag-llm-langchain.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

gcloud artifacts repositories add-iam-policy-binding rag-llm-langchain \
  --location=us-central1 \
  --member="serviceAccount:github-actions@rag-llm-langchain.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

---

## Step 7 — Get the Provider Resource Name

```bash
gcloud iam workload-identity-pools providers describe github-provider \
  --workload-identity-pool=github-pool \
  --location=global \
  --format="value(name)"
```

Output (used as `WIF_PROVIDER`):

```
projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

---

## Step 8 — Configure GitHub Repository Variables & Secrets

> **Important gotcha we hit:** the workflow reads the WIF values through the
> `vars.` context, so they must be stored as repository **Variables**, not
> Secrets.

Set the variables:

```bash
gh variable set WIF_PROVIDER \
  --body "projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider"

gh variable set WIF_SERVICE_ACCOUNT \
  --body "github-actions@rag-llm-langchain.iam.gserviceaccount.com"
```

Set the secret:

```bash
gh secret set PROJECT_ID --body "rag-llm-langchain"
```

---

## Step 9 — Write the Workflow (`.github/workflows/build-push.yml`)

Key piece — the authentication step:

```yaml
name: build-and-push

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

env:
  REGION: us-central1
  PROJECT_ID: ${{ secrets.PROJECT_ID }}

jobs:
  build:
    runs-on: ubuntu-latest
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
          echo "REPO=${{ env.REGION }}-docker.pkg.dev/${{ env.PROJECT_ID }}/rag-llm-langchain" >> "$GITHUB_ENV"
```

Two things are required for WIF to work in the workflow:

1. `permissions: id-token: write` — lets the runner request the OIDC token.
2. `workload_identity_provider` and `service_account` from `${{ vars.* }}`.

---

## Step 10 — The Issues We Hit, and the Fixes That Finally Made It Work

### Issue 1 — Workflow failed immediately at the auth step

```
google-github-actions/auth failed with: the GitHub Action workflow must specify
exactly one of "workload_identity_provider" or "credentials_json"
```

**Root cause:** `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` were stored as GitHub
**Secrets**, but the workflow reads them via `${{ vars.WIF_PROVIDER }}`, which
only resolves repository **Variables**. The values were empty, so the auth step
got neither.

**Fix:** set the values as repository Variables (Step 8 above).

### Issue 2 — Insufficient permissions

```
Permission 'iam.serviceAccounts.getAccessToken' denied on resource
```

**Root cause:** the service account lacked the token-creator role.

**Fix:** grant `roles/iam.serviceAccountTokenCreator` (Step 6).

### Issue 3 — Invalid Docker image tag / Artifact Registry upload denied

```
invalid tag "us-central1-docker.pkg.dev//rag-llm-langchain/model-loader:1.0.0": invalid reference format
Permission 'artifactregistry.repositories.uploadArtifacts' denied on resource
```

**Root cause:** `PROJECT_ID` secret missing (double `//` in the tag), and the
service account lacked Artifact Registry write permission.

**Fix:** set `PROJECT_ID` secret and grant `roles/artifactregistry.writer`
(Steps 6 and 8).

---

## Step 11 — Verify It Works

Trigger the workflow manually and watch it succeed:

```bash
gh workflow run build-push.yml
gh run watch
```

A passing run proves the whole chain works:

```
push/trigger -> OIDC token -> WIF auth (no keys!) -> docker build amd64
             -> push to Artifact Registry -> deploy
```

You can also verify the GCP side directly:

```bash
# Pool is ACTIVE
gcloud iam workload-identity-pools describe github-pool --location=global

# The images landed
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain
```

## Summary

| Piece | Value |
|-------|-------|
| Identity Pool | `github-pool` (global) |
| OIDC Provider | `github-provider` |
| Service Account | `github-actions@rag-llm-langchain.iam.gserviceaccount.com` |
| WIF Roles | `workloadIdentityUser` + `serviceAccountTokenCreator` |
| Registry Role | `artifactregistry.writer` on the `rag-llm-langchain` repo |
| GitHub Variables | `WIF_PROVIDER`, `WIF_SERVICE_ACCOUNT` |
| GitHub Secret | `PROJECT_ID` |

**Final result: the whole process works.** No service-account keys are ever
stored in the repository — GitHub alone, via WIF, drives the pipeline end to end.