# Troubleshooting — From 0 to Live, Every Fix Command

Every failure we hit between "nothing exists" and the live URL, with the real
error message, root cause, and the **exact command** that fixed it.

---

## 1. GPU pool stuck in "PROVISIONING"

```text
Quota 'GPUS_ALL_REGIONS' exceeded.  Limit: 0.0 globally.
```

**Root cause:** L4 GPU quota is global, not per region. Ours was 0.

**Fix:**

```bash
# Disable GPU pool in Terraform
# terraform.tfvars:
#   enable_gpu_pool = false

terraform apply

# Or request a quota increase:
# Google Cloud Console → IAM & Admin → Quotas → search GPUS_ALL_REGIONS
```

---

## 2. Billing disabled

Symptom: various "permission" / "denied" errors across the project.

**Fix:**

```bash
gcloud billing projects link aiml-project-idp \
  --billing-account=01716C-ECBC7F-34FFF7

# Verify
gcloud billing projects describe aiml-project-idp
# billingEnabled: true
```

---

## 3. Terraform state locked

```
Error acquiring the state lock
```

**Root cause:** an earlier `terraform apply` died while holding the lock.

**Fix:**

```bash
# Find and kill the stuck process
ps aux | grep terraform
kill <pid>

# Force-unlock
terraform force-unlock <lock-id>

# Verify
terraform plan    # should run without lock error
```

---

## 4. Drift — gpu-pool ERROR, state inconsistent

Terraform says the pool exists but GKE shows ERROR/lost.

**Fix:**

```bash
# Refresh state
gcloud container node-pools list --cluster genai-cluster --region us-central1

# Delete the errored pool through GKE
gcloud container node-pools delete gpu-pool --cluster genai-cluster --region us-central1

# Re-apply CPU-only
terraform apply
```

---

## 5. `exec format error`

```text
standard_init_linux.go:... exec format error
```

**Root cause:** image built on Apple Silicon (arm64) but GKE nodes are amd64.

**Fix:**

```bash
# Build via Cloud Build (x86 runners, always amd64)
gcloud builds submit --region=us-central1 --config=cloudbuild.yaml .

# Verify the image arch
gcloud artifacts docker images describe \
  us-central1-docker.pkg.dev/aiml-project-idp/genai/gateway:1.0.0 \
  --format="value(summary)"
# amd64
```

---

## 6. ImagePullBackOff / `docker.io/genai/...` not found

**Root cause:** applied `k8s/base` instead of `k8s/overlays/prod`. Base uses
short names (`genai/gateway`); the overlay rewrites them to the full registry path.

**Fix:**

```bash
kubectl apply -k k8s/overlays/prod
kubectl -n genai get pods
# Pods should start pulling from us-central1-docker.pkg.dev/aiml-project-idp/genai/...
```

---

## 7. `No module named 'app'` in gateway

**Root cause:** Dockerfile copied the app flat (`COPY app ./`) instead of as a
package (`COPY app/ ./app/`).

**Fix:**

```dockerfile
# Works:
COPY app/ ./app/
```

Rebuild and redeploy:

```bash
gcloud builds submit --region=us-central1 --config=cloudbuild.yaml .
kubectl -n genai rollout restart deploy/gateway
```

---

## 8. vLLM has no CPU wheel

```text
Failed to infer device type
```

**Root cause:** vLLM requires CUDA/GPU. CPU nodes cannot run it.

**Fix:** use Ollama for CPU:

```bash
# In k8s/base/serving-llm.yaml, ensure the Ollama image is used
# and the model is qwen2.5:0.5b
kubectl -n genai logs deploy/serving-llm --tail=5
# Should show "Listening on" with Ollama
```

---

## 9. Language model download fails

```text
PermissionError on /models
HF: "relative URL without a base"
```

**Fix:**

```bash
# Add to pod spec (in k8s/base/serving-llm.yaml):
#   securityContext:
#     fsGroup: 1001
#     fsGroupChangePolicy: Always

# Set env var to avoid Xet transport:
#   HF_HUB_DISABLE_XET=1

# Or preload via model-loader initContainer into a PVC
kubectl -n genai get pods -l app=serving-llm -o jsonpath='{.items[0].spec.initContainers[*].name}'
```

---

## 10. RAG answers generically — "Ingested N chunks" but queries find nothing (THE BIG ONE)

Symptom: ingest logs `Ingested ... -> 4 chunks`, but `/rag` returns generic
answers and the store count is 0.

**Root cause:** `langchain-chroma 0.1.4` silently falls back to in-memory when
paired with `chromadb>=0.5`. Data is lost on pod restart.

**Fix:**

```bash
# 1. Verify the chromadb version
kubectl -n genai exec deploy/rag-service -- pip show chromadb | grep Version
# Must be: 0.4.24

# 2. If wrong, pin it in rag/requirements.txt:
#    chromadb==0.4.24

# 3. Rebuild and push
gcloud builds submit --region=us-central1 --config=cloudbuild.yaml .

# 4. Restart rag-service
kubectl -n genai rollout restart deploy/rag-service

# 5. Delete old manual ingest jobs
kubectl -n genai delete job rag-ingest-manual --ignore-not-found

# 6. Re-ingest
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl wait --for=condition=complete job/rag-ingest-manual -n genai --timeout=300s

# 7. Verify persistence
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n genai "$R" -- ls /data/chroma
# chroma.sqlite3 MUST exist

kubectl exec -n genai "$R" -- python -c \
  "from config import get_store; print(get_store()._collection.count())"
# > 0
```

---

## 11. `kubectl cp` fails (`pods ... not found`)

**Root cause:** pod name changed between resolving and copying (deployment roll).

**Fix:** use GCS seed path instead:

```bash
# Seed docs to GCS
gcloud storage cp -r local-data/docs gs://aiml-project-idp-rag-docs/docs

# Run the ingest job (reads from GCS, writes to PVC directly)
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl wait --for=condition=complete job/rag-ingest-manual -n genai --timeout=300s
```

---

## 12. Browser root path gives 404

```text
{"detail":"Not Found"}
```

**Fix:** added an HTML playground at `/` (chat + RAG tabs). The API endpoints
were always only `/healthz`, `/chat`, `/rag`, `/models`.

---

## 13. Storage class "not found"

```text
storageclasses.storage.k8s.io "pd-ssd" not found
```

**Fix:**

```bash
# Enable Filestore CSI
gcloud container clusters update genai-cluster --region=us-central1 \
  --update-addons=GcpFilestoreCsiDriver=ENABLED

# Verify storage classes exist
kubectl get storageclass
# premium-rwo      pd.csi.storage.gke.io       Delete
# nfs-filestore    filestore.csi.storage.gke.io Delete
```

---

## 14. GCE Ingress never gets an address

Symptom: ingress shows no ADDRESS, no forwarding rules for hours.

**Fix:** use LoadBalancer instead:

```bash
kubectl -n genai create service loadbalancer gateway-lb --tcp=80:8080 \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n genai get svc gateway-lb
# EXTERNAL-IP assigned immediately
```

---

## 15. NodePort URL unreachable

```text
http://<node-ip>:30080/healthz times out
```

**Fix:** add firewall rule:

```bash
gcloud compute firewall-rules create genai-gateway-nodeport \
  --allow=tcp:30080 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=$(gcloud compute instances list --filter="name:genai" --format="value(tags.items[0])" | head -1)
```

Or just use the LoadBalancer — it sidesteps this entirely.

---

## 16. Port-forward dies between commands

**Fix:** put port-forward and curl in the same command:

```bash
kubectl port-forward -n genai svc/gateway 8080:80 >/dev/null 2>&1 &
PF=$!; sleep 3
curl -s http://localhost:8080/healthz
kill $PF
```

---

## General debugging kit

```bash
# Pod events (pull errors, start errors)
kubectl -n genai describe pod <pod-name>

# Recent cluster events
kubectl -n genai get events --sort-by=.lastTimestamp

# App logs
kubectl -n genai logs deploy/gateway --tail=20
kubectl -n genai logs deploy/rag-service --tail=20
kubectl -n genai logs deploy/serving-llm --tail=20

# Ingest job logs
kubectl -n genai logs job/rag-ingest-manual --tail=20

# Did the image actually build?
gcloud builds list --limit=5

# LB node health
gcloud compute target-pools get-health <pool> --region=us-central1
```

## Hygiene after any rebuild

```bash
kubectl -n genai rollout restart deploy/gateway deploy/rag-service \
  deploy/serving-llm deploy/serving-embedding
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl -n genai get pods
curl -s http://<IP>/healthz
```
