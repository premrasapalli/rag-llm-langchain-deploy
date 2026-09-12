# Fix: Google Cloud WIF Authentication Failure in GitHub Actions

## Issue

`build-push.yml` failed at the `google-github-actions/auth@v2` step with:

```
Run google-github-actions/auth@v2
Error: google-github-actions/auth failed with: the GitHub Action workflow must specify exactly one of "workload_identity_provider" or "credentials_json"! If you are specifying input values via GitHub secrets, ensure the secret is being injected into the environment. By default, secrets are not passed to workflows triggered from forks, including Dependabot.
```

The workflow also logged a Node.js 20 deprecation warning at the top:

```
Node 20 is being deprecated. This workflow is running with Node 24 by default.
```

This warning is informational only and is NOT the cause of the failure.

## Root Cause

The workflow reads the workload identity values through the **variables** context:

```yaml
- name: Authenticate to Google Cloud (Workload Identity Federation)
  id: auth
  uses: google-github-actions/auth@v2
  with:
    token_format: access_token
    workload_identity_provider: ${{ vars.WIF_PROVIDER }}
    service_account: ${{ vars.WIF_SERVICE_ACCOUNT }}
    access_token_lifetime: "600s"
```

`${{ vars.X }}` only resolves **repository/org Variables** (Settings → Secrets and variables → Actions → Variables tab).

However, `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` were stored as **Secrets** (Settings → Secrets and variables → Actions → Secrets tab), which are only readable via `${{ secrets.X }}`.

Because the variables were empty, the auth action received neither `workload_identity_provider` nor `credentials_json`, so it failed immediately.

## Fix Applied

Added the values as repository **Variables** so the `vars.` context resolves them:

| Name                 | Value                                                                                                                  |
|----------------------|------------------------------------------------------------------------------------------------------------------------|
| `WIF_PROVIDER`       | `projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider`                   |
| `WIF_SERVICE_ACCOUNT`| `github-actions@aiml-project-idp.iam.gserviceaccount.com`                                                              |

Commands used:

```bash
gh variable set WIF_PROVIDER \
  --body "projects/784802248985/locations/global/workloadIdentityPools/github-pool/providers/github-provider"

gh variable set WIF_SERVICE_ACCOUNT \
  --body "github-actions@aiml-project-idp.iam.gserviceaccount.com"
```

## Verified Prerequisites

All GCP-side prerequisites confirmed present and correct:

- Workload Identity Pool: `projects/784802248985/locations/global/workloadIdentityPools/github-pool` (ACTIVE)
- OIDC Provider: `.../providers/github-provider`
  - Issuer: `https://token.actions.githubusercontent.com`
  - Attribute condition: `assertion.repository_owner == 'premrasapalli'`
- Service account: `github-actions@aiml-project-idp.iam.gserviceaccount.com`
  - IAM binding on the SA: `roles/iam.serviceAccountTokenCreator` for the principalset matching `premrasapalli/gke-genai-deployment`
- Secret: `PROJECT_ID` = `aiml-project-idp`
- Artifact Registry repo `genai` (us-central1, DOCKER) with `roles/artifactregistry.writer` granted to the SA

## Alternative Approach

If you prefer not to keep values in both Secrets and Variables, you can switch the workflow to the **secrets** context instead of the **vars** context:

```yaml
workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
service_account: ${{ secrets.WIF_SERVICE_ACCOUNT }}
```

And then delete the duplicate Variables. Both approaches work; pick one to avoid confusion.