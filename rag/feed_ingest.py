"""Continuous feed-ingestion loop. Run: python -m feed_ingest [--once]

Polls the configured RSS/Atom feeds and upserts new items into the shared
Chroma knowledge base until stopped. New chunks become retrievable by the
rag-service immediately (retrieval scans the whole collection each query).
"""
import argparse
import logging
import os
import time

from feeds import RSS_FEEDS, ingest_feeds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag.feed_ingest")

POLL_SECONDS = int(os.environ.get("FEED_POLL_SECONDS", "300"))


def poll_once() -> int:
    try:
        return ingest_feeds()
    except Exception:  # noqa: BLE001 - keep the loop alive across failures
        logger.exception("Feed poll round failed")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Realtime RSS/Atom feed ingester")
    parser.add_argument("--once", action="store_true", help="Run a single poll and exit")
    args = parser.parse_args()

    if not RSS_FEEDS:
        logger.warning("RSS_FEEDS is not set; feed ingestion is disabled")
        return

    logger.info(
        "Feed poller started: %d feed(s), every %d s", len(RSS_FEEDS), POLL_SECONDS
    )
    while True:
        added = poll_once()
        logger.info("Poll round added %d new chunks", added)
        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()