# Troubleshooting Guide

## GitHub Actions Workflow Issues

### 1. Workload Identity Federation Authentication Error

**Error:**
```
google-github-actions/auth failed with: the GitHub Action workflow must specify exactly one of "workload_identity_provider" or "credentials_json"
```

**Cause:** Secrets `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` not set in GitHub repository.

**Solution:**
1. Create Workload Identity Pool:
   ```bash
   gcloud iam workload-identity-pools create github-pool \
     --location=global \
     --display-name="GitHub Actions Pool"
   ```

2. Create OIDC Provider:
   ```bash
   gcloud iam workload-identity-pools providers create-oidc github-provider \
     --workload-identity-pool=github-pool \
     --location=global \
     --display-name="GitHub Provider" \
     --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
     --issuer-uri="https://token.actions.githubusercontent.com" \
     --attribute-condition="assertion.repository_owner == 'YOUR_GITHUB_USERNAME'"
   ```

3. Create Service Account:
   ```bash
   gcloud iam service-accounts create github-actions \
     --display-name="GitHub Actions SA"
   ```

4. Grant Workload Identity User Role:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding github-actions@aiml-project-idp.iam.gserviceaccount.com \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/YOUR_GITHUB_REPO"
   ```

5. Get variable values:
   ```bash
   gcloud iam workload-identity-pools providers describe github-provider \
     --workload-identity-pool=github-pool \
     --location=global \
     --format="value(name)"
   ```

6. Add GitHub repository variables (Settings → Secrets and variables → Actions → Variables tab):
   - `WIF_PROVIDER`: (value from step 5)
   - `WIF_SERVICE_ACCOUNT`: `github-actions@aiml-project-idp.iam.gserviceaccount.com`

---

### 2. Service Account Token Creator Permission Error

**Error:**
```
Permission 'iam.serviceAccounts.getAccessToken' denied on resource
```

**Cause:** Service account lacks `iam.serviceAccountTokenCreator` role.

**Solution:**
1. Go to: https://console.cloud.google.com/iam-admin/serviceaccounts?project=aiml-project-idp
2. Click on `github-actions@aiml-project-idp.iam.gserviceaccount.com`
3. Click **Permissions** tab
4. Click **Grant Access**
5. Add principal: `principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/premrasapalli/gke-genai-deployment`
6. Select role: **Service Account Token Creator**
7. Click **Save**

---

### 3. Invalid Docker Image Tag Format

**Error:**
```
ERROR: failed to build: invalid tag "us-central1-docker.pkg.dev//genai/model-loader:1.0.0": invalid reference format
```

**Cause:** `PROJECT_ID` secret not set, causing double slashes `//` in image tag.

**Solution:**
Add `PROJECT_ID` secret in GitHub (Settings → Secrets and variables → Actions → Secrets tab):
- Name: `PROJECT_ID`
- Value: `aiml-project-idp`

---

### 4. Artifact Registry Upload Permission Error

**Error:**
```
Permission 'artifactregistry.repositories.uploadArtifacts' denied on resource
```

**Cause:** Service account lacks Artifact Registry write permissions.

**Solution:**
```bash
gcloud artifacts repositories add-iam-policy-binding genai \
  --location=us-central1 \
  --member="serviceAccount:github-actions@aiml-project-idp.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

---

## Required GitHub Secrets/Variables Summary

| Type | Name | Value |
|------|------|-------|
| Variable | `WIF_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| Variable | `WIF_SERVICE_ACCOUNT` | `github-actions@aiml-project-idp.iam.gserviceaccount.com` |
| Secret | `PROJECT_ID` | `aiml-project-idp` |

## Required GCP IAM Roles

| Service Account | Role | Scope |
|-----------------|------|-------|
| `github-actions@aiml-project-idp.iam.gserviceaccount.com` | `roles/iam.workloadIdentityUser` | Project |
| `github-actions@aiml-project-idp.iam.gserviceaccount.com` | `roles/iam.serviceAccountTokenCreator` | Project |
| `github-actions@aiml-project-idp.iam.gserviceaccount.com` | `roles/artifactregistry.writer` | `genai` repository |
