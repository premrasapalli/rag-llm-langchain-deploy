"""Document ingestion job: reads markdown/txt files and upserts chunks."""
import argparse
import logging
import os
from pathlib import Path

from config import get_store
from loader import load_and_chunk

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag.ingest")

SUPPORTED = {".md", ".txt"}


def ingest_dir(data_dir: str) -> int:
    store = get_store()
    paths = [p for p in Path(data_dir).rglob("*") if p.suffix in SUPPORTED]
    total = 0

    for path in paths:
        chunks = load_and_chunk(str(path))
        if not chunks:
            continue
        ids = [f"{path.stem}-{i}" for i in range(len(chunks))]
        store.add_documents(chunks, ids=ids)
        total += len(chunks)
        logger.info("Ingested %s -> %d chunks", path, len(chunks))

    logger.info("Done. Total chunks: %d", total)
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/data/docs")
    args = parser.parse_args()
    if not os.path.isdir(args.dir):
        raise SystemExit(f"Directory not found: {args.dir}")
    ingest_dir(args.dir)
