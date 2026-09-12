import os
import time

import requests

BASE = os.environ.get("VLLM_URL", "http://localhost:8000/v1")
MODEL = os.environ.get("VLLM_MODEL", "genai-model")


def wait_ready(timeout: int = 600) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{BASE}/models", timeout=5)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
    return False


def chat(prompt: str) -> str:
    r = requests.post(
        f"{BASE}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 256,
            "temperature": 0.2,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


if __name__ == "__main__":
    print(f"Waiting for vLLM at {BASE} ...")
    if not wait_ready():
        print("ERROR: model server never became ready")
        raise SystemExit(1)
    print("vLLM is ready.")
    print("Answer:", chat("Explain Retrieval Augmented Generation in two sentences."))