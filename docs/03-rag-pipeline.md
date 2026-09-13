# The RAG Pipeline — From 0 to Live

Every step below is a real command you run to build, seed, ingest, query, and
verify the complete RAG pipeline from scratch.

## Why RAG exists

LLMs do not know your private documents. Ask about them and the model makes
things up (hallucination). RAG fixes this:

1. **Retrieve** the most relevant snippets from your documents.
2. **Generate** the answer with those snippets in the prompt.

---

## Full RAG flow (as commands)

```
Document ---> split into chunks ---> embed each chunk ---> store in Chroma
                                                              |
User question ---> embed the question ----------------------->|
                                                              |
         find the top-k closest chunks <-----------------------+
         prompt = "Context: <retrieved chunks> Question: <question>"
         LLM generates a grounded answer <-------------------+
```

---

## Step 1: Seed the documents into the PVC

The live `prod` overlay runs CPU-only and seeds the single internal-data file
into the `rag-data` PVC directly (no GCS involved). The share is RWX Filestore,
so any pod writing to `/data/docs` works:

```bash
# Spin up a scratch pod that mounts the rag-data PVC
kubectl -n rag-llm-langchain run seed-docs \
  --image=busybox:1.36 --restart=Never --command -- sh -c "sleep 600"

# Copy the local doc into the shared volume
kubectl cp local-data/10-internal-data-dump.md \
  rag-llm-langchain/seed-docs:/data/docs/10-internal-data-dump.md

# Clean up the scratch pod
kubectl delete pod seed-docs -n rag-llm-langchain
```

(Alternative: the CronJob's `seed-docs` initContainer can seed from GCS by
setting `DOCS_GCS_URI` — see §"The seed-docs initContainer" below.)

## Step 2: Run a manual ingest

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n rag-llm-langchain
kubectl wait --for=condition=complete job/rag-ingest-manual -n rag-llm-langchain --timeout=300s
kubectl logs -n rag-llm-langchain job/rag-ingest-manual --tail=10
```

Expected output:
```
Ingested ... -> N chunks
Done. Total chunks: N
```

The CronJob re-runs this every 6 hours automatically. Re-run manually any time
you change docs.

## Step 3: Verify the vector store persisted

```bash
R=$(kubectl get pod -n rag-llm-langchain -l app=rag-service -o jsonpath='{.items[0].metadata.name}')

# Collection count must be > 0
kubectl exec -n rag-llm-langchain "$R" -- python -c \
  "from config import get_store; print('count:', get_store()._collection.count())"

# chroma.sqlite3 file must exist (proves persistence, not in-memory)
kubectl exec -n rag-llm-langchain "$R" -- ls /data/chroma
```

If count is 0 or `chroma.sqlite3` is missing, see the chromadb pin gotcha below.

## Step 4: Ask a grounded question

The gateway is a ClusterIP service; reach it with a port-forward:

```bash
kubectl port-forward -n rag-llm-langchain svc/gateway 8080:80 & sleep 3
curl -s -X POST http://localhost:8080/rag -H 'Content-Type: application/json' \
  -d '{"query":"What endpoints does the API gateway expose?"}' | python3 -m json.tool
```

A healthy answer quotes the actual endpoints (`/healthz`, `/models`, `/chat`,
`/rag`) and cites the context — proof the model read your document.

---

## How ingestion works (under the hood)

The `rag-ingest` CronJob runs `python -m ingest --wipe --paths` on the seeded
file:

1. Reads `/data/docs/10-internal-data-dump.md`.
2. Loads and splits it into overlapping chunks (`CHUNK_SIZE=300`,
   `CHUNK_OVERLAP=30`).
3. Embeds every chunk via TEI in batches of `EMBED_BATCH=16`
   (`POST /embeddings`).
4. Upserts chunk text + embedding into Chroma collection `knowledge_base`.

> **TEI hard limits:** at most **32 items / 512 tokens per request**. The live
> index uses `CHUNK_SIZE=300` + `EMBED_BATCH=16` specifically to stay under
> those limits (raising them → 413 `Validation` errors). Last ingest:
> **3,489 chunks**.

```bash
# Inspect what gets seeded
kubectl -n rag-llm-langchain get cronjob rag-ingest -o yaml | grep -A5 DOCS_GCS_URI
```

## The seed-docs initContainer

Before ingestion runs, a `seed-docs` initContainer copies `DOCS_GCS_URI` (if
set) into `/data/docs` on the shared PVC. In the live CPU deploy `DOCS_GCS_URI`
is empty and the file is put there with `kubectl cp` (see Step 1) — either way
the ingest job starts from the file already in `/data/docs`:

---

## How retrieval works

At query time (`rag/retriever.py`):

1. Embeds the question with the **same embedding model** used at ingest.
2. Runs a similarity search in Chroma, returns the `k` most similar chunks
   (default `k=4`).
3. Passes those chunks as context in a grounded prompt.

```bash
# Verify retrieval has data to find
R=$(kubectl get pod -n rag-llm-langchain -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n rag-llm-langchain "$R" -- python -c "
from config import get_store
s = get_store()
print('total chunks in collection:', s._collection.count())
results = s.similarity_search('What is RAG?', k=2)
for i, r in enumerate(results):
    print(f'chunk {i}:', r.page_content[:100])
"
```

---

## The RAG answer chain

`rag/chain.py` calls `rag_answer(query, k=4)`:

1. Retrieve context via `rag/retriever.py`.
2. Build a grounded prompt:

   > "You are a precise assistant. Answer ONLY from the provided context.
   > If the context does not contain the answer, say you don't know."

3. Send to the LLM via the OpenAI client pointed at `LLM_BASE_URL`.

```bash
# Full end-to-end RAG test from inside the cluster
kubectl -n rag-llm-langchain exec deploy/rag-service -- python3 -c "
from chain import rag_answer
print(rag_answer('What is RAG?'))
"
```

---

## The critical chromadb gotcha

`langchain-chroma 0.1.4` silently falls back to **in-memory** (non-persistent)
when paired with `chromadb>=0.5`. Signs:

- Ingest logs "Ingested ... N chunks" but the query finds nothing.
- No `chroma.sqlite3` file under `/data/chroma`.

**Fix:** this repo pins `chromadb==0.4.24`. Verify the pin:

```bash
kubectl -n rag-llm-langchain exec deploy/rag-service -- pip show chromadb | grep Version
# Expected: Version: 0.4.24
```

After a rebuild, delete old ingest jobs and re-ingest.

---

## Re-index after changing documents

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n rag-llm-langchain
kubectl wait --for=condition=complete job/rag-ingest-manual -n rag-llm-langchain --timeout=300s
kubectl logs -n rag-llm-langchain job/rag-ingest-manual --tail=5
```

---

## Model-name match requirement

The RAG chain calls the LLM with `model=LLM_MODEL`. This must match exactly
what the serving backend exposes:

```bash
# What the LLM serves
kubectl -n rag-llm-langchain exec deploy/serving-llm -- curl -s http://localhost:8000/v1/models

# What the rag-service uses (must match above)
kubectl -n rag-llm-langchain get deploy rag-service -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -c "import sys,json; [print(e['name'], e['value']) for e in json.load(sys.stdin) if e['name']=='LLM_MODEL']"
```

Mismatch = `404 model not found` from the LLM API.
