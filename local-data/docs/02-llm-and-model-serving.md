# LLM Fundamentals, Model Serving & Embeddings — From ZERO to Live

This file covers the AI building blocks from scratch: what a language model is,
how models are served behind HTTP APIs, how text is turned into vectors, and how
to test each piece from zero with real commands.

---

# What Is a Large Language Model (LLM)?

A Large Language Model is a neural network trained on a massive amount of text
to predict the next word in a sentence. Given a prompt like "The capital of
France is", it continues: "...Paris."

## Key ideas

- **Tokens** — the units the model reads/writes (roughly a short piece of a
  word). "hello world" might split into `"hello"`, `" world"`.
- **Context window** — the maximum number of tokens the model can consider at
  once.
- **Parameters** — the learned numbers inside the model. A "0.5B" model has
  0.5 billion parameters. Bigger = smarter but needs a GPU.
- **Inference** — running the model to produce a reply (what the *serving*
  services do).

## Why run your own model?

- **Privacy** — your prompts and documents never leave your infrastructure.
- **Cost control** — no per-token API fees at scale.
- **Customization** — choose any open model and swap freely.

## The model used in the CPU demo

Because GPU quota is 0 in this project, the CPU demo serves `qwen2.5:0.5b`
(0.5 billion parameters, fast on CPU). When a GPU becomes available, the same
code serves `Qwen2.5-7B-Instruct`.

---

# Model Serving: vLLM and Ollama

"Model serving" means running a trained model behind an HTTP API.

## vLLM (GPU path)

vLLM is a high-performance inference server for GPUs. It exposes an
**OpenAI-compatible API** at `/v1` on port 8000:

```bash
# When GPU is available (vLLM is configured in k8s/base/serving-llm.yaml + GPU overlay)
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"genai-model","messages":[{"role":"user","content":"Hello"}]}'
```

## Ollama (CPU path)

Currently active (no GPU quota). Ollama runs LLMs on plain CPUs with the same
OpenAI-compatible API:

```bash
# Test via port-forward to the running LLM service
kubectl -n genai port-forward svc/serving-llm 8000:8000 & PF=$!
sleep 5
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"What is 2+2?"}]}'
kill $PF
```

## Why OpenAI-compatible matters

Every downstream component (gateway, RAG chain) uses the standard OpenAI Python
client. Swapping vLLM <-> Ollama changes a config value, never code.

```bash
# Verify the gateway sees the LLM
curl -s http://localhost:8080/models   # after port-forwarding gateway
```

---

# Embeddings and Text Embeddings Inference (TEI)

An **embedding** is a list of numbers (vector) that captures the *meaning* of
text. "How do I reset my password?" and "I forgot my login" produce vectors
that are close together, even though they share almost no words.

## What is an embedding model?

We use `BAAI/bge-small-en-v1.5` — small, fast, works on CPU, produces
384-dimensional vectors.

## How TEI serves embeddings

```bash
# Test the embedding endpoint directly
kubectl -n genai port-forward svc/serving-embedding 8001:8001 & PF=$!
sleep 5
curl -s http://localhost:8001/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"BAAI/bge-small-en-v1.5","input":"What is RAG?"}' | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print('dims:', len(d['data'][0]['embedding']))"
kill $PF
# dims: 384
```

## Where embeddings are used

- **At ingest time:** every document chunk → embedding → stored in Chroma.
- **At query time:** user question → embedding → find closest stored chunks.

```bash
# Verify the rag-service can reach the embedding server
R=$(kubectl get pod -n genai -l app=rag-service -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n genai "$R" -- python -c "
from openai import OpenAI
c = OpenAI(base_url='http://serving-embedding:8001/v1', api_key='none')
r = c.embeddings.create(model='BAAI/bge-small-en-v1.5', input='test')
print('embedding ok, dims:', len(r.data[0].embedding))
"
```

## Critical rule: same embedding model at ingest and query time

```bash
# Confirm both use the same model
kubectl -n genai get deploy serving-embedding -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -c \
  "import sys,json; [print(e['name'], '=', e['value']) for e in json.load(sys.stdin)]"
```
