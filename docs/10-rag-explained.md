# The RAG + LLM stack explained — from 0 to live, in plain words

Here is the whole thing explained like you're new to every word in it. This
reflects the actual live repo setup.

---

# The big picture in 3 sentences

This is a "question-answering bot" that has read the internal documentation
file (`local-data/10-internal-data-dump.md`, an 890KB reference document) and
can answer questions **using only the information in that file** — not from
general knowledge.

To do that it needs four kinds of software working together:

1. A **brain** that generates text (the LLM).
2. A **library index** that finds relevant paragraphs fast (the vector database).
3. A **pipeline** that cuts the document into pieces and turns each piece into
   numbers (ingestion + embeddings).
4. **Glue** that combines the question + the found paragraphs and asks the
   brain (the gateway + chain).

Everything runs on **Google Cloud's Kubernetes** (GKE), driven automatically by
ArgoCD reading the GitHub repo.

---

# Step 1 — First, the words (the glossary)

| Word | Plain meaning |
|------|--------------|
| **LLM** | "Large Language Model" — the brain. A program trained on huge amounts of text to predict the next word, which lets it answer questions. This project uses **Qwen2.5 0.5B**, a small model that runs on a CPU. |
| **RAG** | "Retrieval-Augmented Generation" — instead of asking the brain straight ("write an answer"), you first **retrieve** relevant text from your documents and hand it to the brain. "Retrieval" = finding; "Augmented" = the brain now gets extra info; "Generation" = the brain writes the answer. The brain doesn't know your private document — RAG makes it read it first. |
| **Embedding** | A way to turn text into a **list of numbers** (a vector) that captures *meaning*. "How do I reset my password" and "I forgot my login" become similar numbers even though the words differ. |
| **Vector database** | A database that stores these number-lists and can find the ones *closest in meaning* to a question. This project uses **Chroma**, stored in a folder. |
| **Chunk** | The document is too big to feed the brain whole, so the system cuts it into small pieces (chunks). Each chunk gets embedded and stored. Current index: **3,489 chunks**. |
| **Semantic search** | Searching by *meaning*, not by keywords. That's what embedding + vector search gives you. |
| **Token** | A piece of a word ("tokenize"). Models count tokens, not words. 512 tokens ≈ a few paragraphs. |
| **Docker image** | A frozen, self-contained snapshot of a program + its runtime, ready to run anywhere. |
| **Container** | A running instance of an image — like a zombie image that's now alive and executing. |
| **Registry / Artifact Registry** | The **warehouse** where images are stored, like GitHub but for Docker images. |
| **Kubernetes (k8s)** | A system that runs and supervises containers: starts them, restarts them if they die, scales them. |
| **GKE** | Google's managed version of Kubernetes. |
| **Pod** | The smallest thing Kubernetes runs — one or more containers together. |
| **Deployment** | Kubernetes' instruction card: "keep N copies of this pod running forever." |
| **Service** | Kubernetes' phone directory: a stable network address so other programs can find a pod by name (e.g. `serving-llm`) instead of a changing IP. |
| **PVC / PersistentVolumeClaim** | A request for **disk space** that survives even when pods die and restart. Without this, everything is erased. |
| **StorageClass** | A template describing *what kind* of disk a PVC gets (speed, how it's made). |
| **StorageClass topology / instance-location** | Where the disk's physical zone must be so the cluster node can reach it. |
| **Namespace** | A named box that separates groups of objects. This project's is `rag-llm-langchain`. |
| **CronJob** | A Kubernetes "alarm clock" — runs a job on a schedule (this one: every 6 hours). |
| **initContainer** | A helper container that runs *first*, finishes, and exits before the main container starts. |
| **Probe** | A health check. `readiness` = "am I ready to receive requests?"; `liveness` = "if I'm dead, restart me." |
| **Ingress / LoadBalancer** | The front door at the edge of the cluster giving an external URL/IP. |
| **Kustomize / overlay** | A way to reuse one base config and slightly tweak it for different environments. |
| **ArgoCD** | A "GitOps" robot. It watches the GitHub repo and makes the cluster's state match the repo automatically. If the repo says "deploy this", ArgoCD deploys it. |
| **Terraform** | A program that creates cloud *infrastructure* (clusters, IPs, disk) from text files, deterministically. |
| **Service Account (SA)** | An identity for a machine/program (not a human) to use when talking to Google APIs. |
| **IAM role** | A permission card — e.g. "this SA may read images from the registry." |
| **Filestore** | Google's managed network file storage, shared by many pods at once. |
| **RWX / RWO** | ReadWriteMany (many pods read+write) vs ReadWriteOnce (one pod at a time). |
| **WIF** | Workload Identity Federation — lets GitHub log in to Google without passwords (CI/CD). |

---

# Step 2 — The pieces you actually wrote (the app code)

The app is made of 4 code packages in the repo:

### `gateway/` — the front door (FastAPI, port 80/8080)
A small HTTP server (`gateway/app/main.py` + `routes.py`) exposing these endpoints:
- `/chat` — plain chat with the brain.
- `/rag` — the "grounded in your document" answer.
- `/models` — list what brain models exist.
- `/healthz` — a heartbeat that returns `{"status":"ok"}`.
- `/` — a tiny webpage (playground) so you can click buttons instead of typing curl.

It does **not** do thinking itself. It's a **proxy**: it receives your HTTP
request and relays it to the right inner service. See `gateway/app/routes.py:131`
— `/chat` calls the LLM at `config.LLM_URL`; `routes.py:143` — `/rag` calls the
RAG service at `config.RAG_URL`.

### `rag/` — the RAG brain (FastAPI, port 8080)
- `rag/config.py` — reading configuration from environment variables (like
  `EMBEDDING_MODEL`), and creates the Chroma store + embeddings client.
- `rag/loader.py` — reads the markdown file and **chunks** it:
  `CHUNK_SIZE=300` characters per piece, `CHUNK_OVERLAP=30` (30 characters of
  the previous chunk carried into the next so meaning isn't cut in half at seams).
- `rag/ingest.py` — the **offline** job: builds chunks, converts them to
  numbers (embeddings) via TEI, and stores them into Chroma. It wipes the old
  collection first (`--wipe`) so the index always equals exactly the file.
- `rag/retriever.py` — at question time: takes the question, embeds it, and
  finds the **8 nearest chunks** in Chroma by math (see below).
- `rag/chain.py` — the final assembly: takes the question + the found chunks,
  builds a prompt telling the brain "answer ONLY from this context, say
  'I don't know' if nothing fits", and calls the LLM.
- `rag/service.py` — the small FastAPI wrapper exposing `/answer`.

### `serving/` — model helpers
- `serving/Dockerfile.ollama` → builds the LLM container (Ollama).
- `serving/Dockerfile.model-loader` → a tiny helper that downloads a model's
  files into a shared disk before the real server starts.
- `serving/download_model.py`, `probe.py`, etc. — download + health-check utilities.

### The models themselves (not your code)
- **Ollama + Qwen2.5 0.5B** — the brain. Runs on CPU (`serving-llm` pod, port 8000).
- **TEI (Text Embeddings Inference)** with **BAAI/bge-small-en-v1.5** — the
  embedding engine. Runs on CPU (`serving-embedding` pod, port 8001). Converts
  text ↔ numbers.

---

# Step 3 — How retrieval actually works (the math, in words)

When you ask `rag-service` a question:

1. `retriever.py:27` — the question is embedded: one request to TEI converts
   the words into a list of ~384 numbers.
2. `retriever.py:25` — all stored chunks are loaded; each is already a list of numbers.
3. `retriever.py:30` — it computes **cosine similarity** between the question's
   numbers and every chunk's numbers. Cosine similarity is a number between -1
   and 1 describing how similar the *directions* of the two number-lists are
   (1 = facing same way = similar meaning).
4. `retriever.py:33` — it sorts by similarity and takes the top 8 (`k=8`) chunks.
5. `chain.py:18` — it even drops chunks whose similarity score is too low
   (`max_score = 0.6`) so it doesn't feed garbage to the brain.
6. `chain.py:26` — the winning chunks get pasted into a prompt
   ("Question: ... Context: ..."), and the brain (`Ollama`) writes the final answer.

Why do this math manually instead of letting Chroma do it? See the comment in
`rag/retriever.py:4-9`: Chroma's built-in index gave inconsistent, "inflated"
distances with this bge model, and it'd silently embed with a different model.
Doing it in Python with `numpy` is simple, correct, and fast at this scale.

---

# Step 4 — Zero-to-live, phase by phase

Now the journey of getting this running on Google Cloud. Each phase exists
because the previous left a gap.

### Phase A — Write the code (done in the repo)
The `gateway/`, `rag/`, `serving/` folders. Run locally with `docker-compose.yml`
— that's the "hello world" sanity check before touching the cloud.

### Phase B — Provision infrastructure (Terraform)

`terraform/main.tf` is a text contract that creates, on Google Cloud:
- `google_container_cluster.rag_llm_langchain` (`main.tf:11`) — the GKE cluster
  named `rag-llm-langchain-cluster`.
- `google_container_node_pool.cpu` (`main.tf:29`) — a pool of **CPU machines**
  (`e2-standard-8`: 8 vCPUs, 32GB RAM). This is where everything runs today.
- `google_container_node_pool.gpu` (`main.tf:43`) — a GPU pool **turned off**
  via `count = var.enable_gpu_pool ? 1 : 0`. It's declared for later, but the
  global GPU quota is 0, so it's disabled.
- `google_artifact_registry_repository.docker` (`main.tf:69`) — the image warehouse.
- `google_compute_global_address` (`main.tf:79`) — a reserved public IP for a
  future domain.

Running `terraform apply` reads this file and creates (or updates) those cloud
resources. It's "infrastructure as code": the cluster config is versioned in
git and reproducible.

### Phase C — Turn the code into images and put them in the warehouse

A GKE node can't run raw Python. It runs **containers**. So each package is
built into a Docker image:

```bash
docker build --platform linux/amd64 -t $AR/rag:1.0.3 rag/
```

- `--platform linux/amd64` is crucial: your Mac is `arm64` (Apple Silicon).
  The cloud nodes are `amd64` (Intel-ish). Without this flag you'd ship an image
  that can't run on the cluster ("exec format error").
- `$AR` = `us-central1-docker.pkg.dev/rag-llm-langchain/rag-llm-langchain` (the
  warehouse address).

Then `docker push` uploads the image to **Artifact Registry**. Each image is
versioned: `rag:1.0.3`, `gateway:1.0.0`, `model-loader:1.0.0`. TEI comes
pre-built from Hugging Face
(`ghcr.io/huggingface/text-embeddings-inference:cpu-1.5`).

### Phase D — Deploy the manifests (Kubernetes, via ArgoCD)

The `k8s/` folder holds the "instruction cards" of what pods to run. There's a
**base** (`k8s/base/`) with plain instructions, and a **prod overlay**
(`k8s/overlays/prod/`) that only tweaks image names.

`k8s/overlays/prod/kustomization.yaml` is only 22 lines: it says "take all of
`../../base`, and point each image to the real warehouse, with these exact
versions (`rag:1.0.3`)." That's the entire trick of Kustomize: base = reusable,
overlay = environment-specific.

Here's what the base tells Kubernetes to run (13ish objects in namespace
`rag-llm-langchain`):

| Object | What it is | From file |
|--------|-----------|-----------|
| `serving-llm` Deployment | The brain. Starts Ollama, pulls `qwen2.5:0.5b`, serves OpenAI-style API on :8000. Volume: `model-store` PVC. | `serving-llm.yaml:1`, `serving-llm.yaml:33` |
| `serving-embedding` Deployment | The embedding engine. Its `initContainer` first downloads the bge model onto the `embed-store` disk, then TEI serves it on :8001. | `serving-embedding.yaml:1` |
| `rag-service` Deployment | The RAG API on :8080. Mounts the Chroma folder on the shared `rag-data` disk. | `rag-service.yaml` |
| `gateway` Deployment | The front door on :80. | `gateway.yaml` |
| `rag-ingest` CronJob | Every 6h: wipe Chroma and re-index the one doc file. | `ingest-cronjob.yaml:1` |
| 4 Services | Stable DNS names: `serving-llm`, `serving-embedding`, `rag-service`, `gateway`. | `svc-*.yaml` |
| 3 PVCs | `embed-store`, `model-store`, `rag-data` — persistent disks. | `pvc-*.yaml` |
| 1 StorageClass | `nfs-filestore` (network file disk, with the topology fix). | `storageclass-filestore.yaml` |
| 2 Secrets | `.env`-style values (gateway API key, HF token). | `gateway-secret.yaml`, `hf-secret.yaml` |
| 1 Namespace | `rag-llm-langchain`. | `namespace.yaml` |

**How does it get from GitHub to the cluster?** That's ArgoCD
(`argocd/rag-llm-langchain-app.yaml`). It says:
- `source.repoURL` — watch this GitHub repo
- `source.path` — specifically the `k8s/overlays/prod` folder
- `syncPolicy.automated` — whenever the repo changes, **apply the changes to the
  cluster**; `selfHeal` = revert anything someone manually messes with;
  `prune` = delete anything that's no longer in the repo.

So the flow is: `git push` to GitHub → ArgoCD sees the change → ArgoCD runs
Kustomize on `k8s/overlays/prod` → applies to GKE. You never `kubectl apply` by
hand in production; git is the single source of truth. That's **GitOps**.

### Phase E — The cluster starts everything

The pods boot in this order (enforced by `initContainer` + probes):
1. `serving-embedding`'s initContainer downloads the embedding model onto disk.
2. TEI starts serving embeddings on :8001.
3. `serving-llm` starts Ollama and downloads Qwen2.5 0.5B
   (`serving-llm.yaml:33`). This takes a minute or two — that's why the pod
   showed `Progressing` health.
4. `rag-service` and `gateway` become ready only after probing their dependencies.

To access it locally, port-forward the internal service to the machine:

```bash
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80
curl http://localhost:8080/healthz   # {"status":"ok"}
```

### Phase F — Feed the brain your document (seeding + ingest)

The doc is big (890KB) and lives only on the local machine, so:
1. A scratch container (`seed-docs`) attached to the shared `rag-data` disk lets
   you copy the file in:
   ```bash
   kubectl cp local-data/10-internal-data-dump.md rag-llm-langchain/seed-docs:/data/docs/...
   ```
2. You trigger the ingest manually (same command the CronJob runs every 6h):
   ```bash
   kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n rag-llm-langchain
   ```
   This runs `python -m ingest --wipe --paths /data/docs/10-internal-data-dump.md`
   inside the `rag` image: chunk → embed (TEI) → upsert into Chroma.
   Result: **3,489 chunks**.

---

# Step 5 — Trace one real request end to end

You do `curl -X POST localhost:8080/rag -d '{"query":"What does the gateway do?"}'`:

```
YOU
 │ 1. HTTP POST to gateway :80 (through port-forward)
 ▼
GATEWAY (gateway/app/routes.py:143)
 │ 2. Validates X-API-Key (off by default)
 │ 3. Forwards to rag-service:8080/answer  {"query": ..., "k": 4}
 ▼
RAG-SERVICE (rag/service.py:26)
 │ 4. Calls rag_answer(query)  (rag/chain.py:26)
 │
 │  ├─ 5. retriever.retrieve(query, k=12)   (retriever.py:17)
 │  │      - embed the question via TEI (:8001)
 │  │      - compute cosine similarity vs all 3,489 chunks
 │  │      - keep top 12, then chain drops any with score > 0.6
 │  │
 │  └─ 6. _build_context()  → the best chunks joined with "---"
 │
 │ 7. Builds prompt: "System: answer ONLY from this context...
 │         User: Question: ... Context: <the chunks>"
 │ 8. Sends to SERVING-LLM (Ollama :8000/v1)
 ▼
OLLAMA (qwen2.5:0.5b)
 │ 9. Generates the answer text
 ▼
RAG-SERVICE → GATEWAY → YOU
```

The `/chat` path is simpler: gateway → Ollama directly, no retrieval.

---

# Step 6 — The "gotchas" we hit and fixed along the way

These explain the *weird* details in the config:

1. **TEI limits → `EMBED_BATCH` and chunk size.**
   TEI rejects requests with more than **32 texts** or more than **512 tokens**
   per text (it responds 413 = Too Large). The document chunks were 500 chars
   and 1000 at a time — both over. Fixes: `rag/loader.py:5` `CHUNK_SIZE=300`,
   `CHUNK_OVERLAP=30`; `rag/ingest.py:10` `EMBED_BATCH=16` (send 16 chunks per
   request). That's why the `rag` image got bumped `1.0.1 → 1.0.2 → 1.0.3`.

2. **Image pull permission.** When pods can't pull from the registry you get
   `ErrImagePull` / `403 Forbidden`. The **node's identity**
   (`rag-llm-langchain-gke@...`) had no permission. Fix: added IAM role
   `roles/artifactregistry.reader` to that Service Account.
   (`terraform/main.tf:1-4` creates this SA.)

3. **Filestore needs a zone.** The `nfs-filestore` StorageClass used "Immediate"
   binding but didn't say *which zone*, so the disk was created in a zone the
   node couldn't reach → `no available topology found`. Fix:
   `instance-location: us-central1-a` in `storageclass-filestore.yaml`.
   Also — StorageClass `parameters` are **immutable**, so the old class was
   deleted and ArgoCD recreated it.

4. **arm64 vs amd64.** Building on macOS without `--platform linux/amd64`
   produces images that fail with "exec format error" on the cluster.

5. **GPU pool declared but disabled.** The project's GPU quota is 0, so the GPU
   node pool (`enable_gpu_pool`) is off, and the running LLM is the small CPU
   model `qwen2.5:0.5b`. The GPU overlay (`k8s/overlays/gpu/`) is ready but unused.

6. **Billing (current blocker).** The project's billing account is not linked
   (`billingEnabled: false`), which causes GKE's API to return
   `403 This API method requires billing to be enabled` and `kubectl` to time
   out to the API server. This isn't a code problem; it's a Cloud project setup
   problem — the cluster exists but you can't talk to it until billing is linked.

7. **RSS/feeds removed.** There's a `feed-ingest.yaml` and `rag/feeds.py`, but
   the knowledge base is just the internal file, so those aren't deployed. They
   remain as code for later.

---

# Where things stand right now

| Piece | State |
|-------|-------|
| Images in Artifact Registry | `rag:1.0.3`, `gateway:1.0.0`, `model-loader:1.0.0` (all `linux/amd64`) |
| Cluster | Exists (`rag-llm-langchain-cluster`, us-central1, `cpu-pool` only) |
| ArgoCD | Installed, app → `k8s/overlays/prod`, auto-sync on `main` |
| Pods | All ran: gateway 2/2, rag-service 1/1, serving-llm 1/1, serving-embedding 1/1 |
| Data | 3,489 chunks in Chroma on 100Gi Filestore (`rag-data`) |
| Verified | `/healthz` → ok, `/chat` answered — before the billing outage |
| Blocked | GKE API unreachable because billing isn't linked (your call to leave it) |
| Docs | Updated to match all of the above and pushed to GitHub (`1a88551`) |

---

**The one-line summary:** Terraform builds the GKE cluster → the three apps are
compiled to Docker images and pushed to Artifact Registry → ArgoCD watches the
GitHub repo and installs the K8s manifests (Ollama LLM + TEI embeddings + RAG
service + gateway, all sharing persistent disks) → the 890KB doc gets chunked,
embedded, and stored in Chroma → a request to `/rag` finds the closest chunks
by math and asks the LLM to answer using only them.