import os
import subprocess
import time

import requests

MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def pull_ready(timeout: int = 1800) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        r = requests.post(
            "http://localhost:11434/api/pull",
            json={"name": MODEL, "stream": False},
            timeout=120,
        )
        if r.status_code == 200:
            return True
        time.sleep(10)
    return False


if __name__ == "__main__":
    print(f"Pulling model {MODEL} ...")
    if pull_ready():
        print(f"Model {MODEL} ready.")
    else:
        raise SystemExit("Failed to pull model")