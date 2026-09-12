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

## Step 1: Seed the documents into GCS

```bash
# Create the bucket
gcloud storage buckets create gs://aiml-project-idp-rag-docs --location=us-central1

# Upload the knowledge base docs
gcloud storage cp -r local-data/docs gs://aiml-project-idp-rag-docs/docs

# Grant the node SA read access (for the seed-docs initContainer)
gsutil iam ch \
  serviceAccount:genai-gke@aiml-project-idp.iam.gserviceaccount.com:objectViewer \
  gs://aiml-project-idp-rag-docs
```

## Step 2: Run a manual ingest

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl wait --for=condition=complete job/rag-ingest-manual -n genai --timeout=300s
kubectl logs -n genai job/rag-ingest-manual --tail=10
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
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')

# Collection count must be > 0
kubectl exec -n genai "$R" -- python -c \
  "from config import get_store; print('count:', get_store()._collection.count())"

# chroma.sqlite3 file must exist (proves persistence, not in-memory)
kubectl exec -n genai "$R" -- ls /data/chroma
```

If count is 0 or `chroma.sqlite3` is missing, see the chromadb pin gotcha below.

## Step 4: Ask a grounded question

```bash
IP=$(kubectl -n genai get svc gateway-lb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl -s -X POST http://$IP/rag -H 'Content-Type: application/json' \
  -d '{"query":"What endpoints does the API gateway expose?"}' | python3 -m json.tool
```

A healthy answer quotes the actual endpoints (`/healthz`, `/models`, `/chat`,
`/rag`) and cites the context — proof the model read your document.

---

## How ingestion works (under the hood)

The `rag-ingest` CronJob runs `python -m ingest --dir /data/docs`:

1. Walks `/data/docs` for `.md` and `.txt` files.
2. Loads and splits each into overlapping chunks.
3. Embeds every chunk via TEI (`POST /embeddings`).
4. Upserts chunk text + embedding into Chroma collection `knowledge_base`.

```bash
# Inspect what was seeded
kubectl -n genai get cronjob rag-ingest -o yaml | grep -A5 DOCS_GCS_URI
```

## The seed-docs initContainer

Before ingestion runs, a `seed-docs` initContainer rsyncs `DOCS_GCS_URI` into
`/data/docs` on the shared PVC. This means:

- Docs live in GCS (durable, versionable).
- The ingest job always starts from fresh docs.

---

## How retrieval works

At query time (`rag/retriever.py`):

1. Embeds the question with the **same embedding model** used at ingest.
2. Runs a similarity search in Chroma, returns the `k` most similar chunks
   (default `k=4`).
3. Passes those chunks as context in a grounded prompt.

```bash
# Verify retrieval has data to find
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n genai "$R" -- python -c "
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
kubectl -n genai exec deploy/rag-service -- python3 -c "
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
kubectl -n genai exec deploy/rag-service -- pip show chromadb | grep Version
# Expected: Version: 0.4.24
```

After a rebuild, delete old ingest jobs and re-ingest.

---

## Re-index after changing documents

```bash
kubectl create job --from=cronjob/rag-ingest rag-ingest-manual -n genai
kubectl wait --for=condition=complete job/rag-ingest-manual -n genai --timeout=300s
kubectl logs -n genai job/rag-ingest-manual --tail=5
```

---

## Model-name match requirement

The RAG chain calls the LLM with `model=LLM_MODEL`. This must match exactly
what the serving backend exposes:

```bash
# What the LLM serves
kubectl -n genai exec deploy/serving-llm -- curl -s http://localhost:8000/v1/models

# What the rag-service uses (must match above)
kubectl -n genai get deploy rag-service -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -c "import sys,json; [print(e['name'], e['value']) for e in json.load(sys.stdin) if e['name']=='LLM_MODEL']"
```

Mismatch = `404 model not found` from the LLM API.
