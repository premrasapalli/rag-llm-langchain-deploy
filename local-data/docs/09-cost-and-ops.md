# Cost Awareness & Everyday Operations — From 0 to Live

Commands to monitor, operate, and tear down the live platform.

---

## Everyday ops cheat-sheet

| Task | Command |
|------|---------|
| Watch all workloads live | `kubectl -n genai get pods -w` |
| Tail gateway logs | `kubectl -n genai logs deploy/gateway -f` |
| Tail rag-service logs | `kubectl -n genai logs deploy/rag-service -f` |
| Tail LLM logs | `kubectl -n genai logs deploy/serving-llm -f` |
| Re-ingest knowledge base | `kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai` |
| Restart after image rebuild | `kubectl -n genai rollout restart deploy/gateway deploy/rag-service deploy/serving-llm deploy/serving-embedding` |
| See public IP | `kubectl -n get svc gateway-lb` |
| See reserved static IP | `gcloud compute addresses describe gateway-static --region=us-central1 --format='value(address)'` |
| Health check live | `curl -s http://<EXTERNAL-IP>/healthz` |
| Check vector store count | `R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}'); kubectl exec -n genai "$R" -- python -c "from config import get_store; print(get_store()._collection.count())"` |
| Check PVC usage | `kubectl -n genai get pvc` |
| Check cluster nodes | `gcloud container node-pools list --cluster genai-cluster --region us-central1` |
| Check running images | `kubectl -n get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'` |

---

## Teardown (in the right order)

```bash
# 1. Remove workloads first
kubectl delete -k k8s/base

# 2. Delete the LoadBalancer (avoids orphaned external IP)
kubectl -n genai delete svc gateway-lb --ignore-not-found

# 3. Tear down infrastructure (Terraform)
terraform destroy

# 4. PVCs persist until explicitly deleted — check
kubectl -n genai get pvc   # should be empty after destroy
```

> Always `kubectl delete -k k8s/base` before `terraform destroy` to avoid
> orphaned external resources like the static IP.

---

## Cost awareness

| Resource | Approximate cost | Notes |
|----------|------------------|-------|
| Filestore `rag-data` | ~$170/month | Billed at 1 TiB minimum, PVC asks for 100 GiB — biggest fixed cost |
| `cpu-pool` (3x e2-standard-8) | ~$400/month (on-demand) | Core compute cost |
| GPU pool (when enabled) | $$$$$ | L4 GPU is the largest variable cost |
| Artifact Registry | negligible | Storage for ~3 images |
| Load Balancer | ~$18/month | Standard GCE LB forwarding rule |
| GCS bucket | negligible | Docs only |

---

## Check current GCP spend

```bash
# Billing export to BigQuery (if configured)
bq query --use_legacy_sql=false \
  'SELECT service.description, SUM(cost) as total FROM `project.dataset.gcp_billing_export` GROUP BY 1 ORDER BY total DESC'

# Or check via console
# https://console.cloud.google.com/billing/linkedaccount?project=aiml-project-idp
```

---

## Quota check (do this before scaling)

```bash
gcloud compute regions describe us-central1 \
  --format='table(quotas[].metric,quotas[].limit,quotas[].usage)'
```

| Metric | Limit | What it controls |
|--------|-------|------------------|
| `CPUS` | 24+ | CPU node pool |
| `GPUS_ALL_REGIONS` | 0 (was) | L4 GPU pool — request increase to enable |
| `DISKS_TOTAL_GB` | varies | PVC storage |
| `IN_USE_ADDRESSES` | 1+ | Load balancer IPs |
