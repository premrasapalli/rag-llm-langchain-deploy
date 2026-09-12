"""Downloads a Hugging Face model into a shared volume, then exits.

Runs as a Kubernetes initContainer so the model is present before the vLLM
serving container starts. Override MODEL via env.
"""
import argparse
import os
from pathlib import Path

from huggingface_hub import snapshot_download

DEFAULT_MODEL = os.environ.get("HF_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
DEST = os.environ.get("HF_DEST", "/models")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dest", default=DEST)
    args = parser.parse_args()

    Path(args.dest).mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=args.model, local_dir=args.dest)
    print(f"Downloaded {args.model} -> {args.dest}")


if __name__ == "__main__":
    main()
