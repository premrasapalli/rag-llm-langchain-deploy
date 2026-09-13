"""Document ingestion job: reads markdown/txt files (dir or explicit paths) and upserts chunks."""
import argparse
import logging
import os
from pathlib import Path

from config import get_store
from loader import load_and_chunk

EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "16"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag.ingest")

SUPPORTED = {".md", ".txt"}


def _paths_from_dir(data_dir: str) -> list[Path]:
    return sorted(p for p in Path(data_dir).rglob("*") if p.suffix in SUPPORTED)


def ingest_paths(paths: list[Path], wipe: bool = False) -> int:
    store = get_store()
    if wipe:
        try:
            store.delete_collection()
            logger.info("Wiped existing collection")
        except Exception:  # noqa: BLE001 - nothing to wipe is fine
            logger.info("Collection did not exist; nothing to wipe")
        store = get_store()
    total = 0

    for path in paths:
        chunks = load_and_chunk(str(path))
        if not chunks:
            continue
        ids = [f"{path.stem}-{i}" for i in range(len(chunks))]
        for i in range(0, len(chunks), EMBED_BATCH):
            store.add_documents(chunks[i : i + EMBED_BATCH], ids=ids[i : i + EMBED_BATCH])
        total += len(chunks)
        logger.info("Ingested %s -> %d chunks", path, len(chunks))

    logger.info("Done. Total chunks: %d", total)
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=None, help="Directory to scan recursively")
    parser.add_argument(
        "--paths", nargs="*", default=None, help="Explicit files to ingest (instead of --dir)"
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="Delete the existing collection first (knowledge base == exactly these files)",
    )
    args = parser.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
        missing = [str(p) for p in targets if not p.is_file()]
        if missing:
            raise SystemExit(f"File not found: {', '.join(missing)}")
    elif args.dir and os.path.isdir(args.dir):
        targets = _paths_from_dir(args.dir)
    else:
        raise SystemExit("Provide an existing --dir or --paths")

    ingest_paths(targets, wipe=args.wipe)