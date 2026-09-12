"""Realtime feed ingestion for the RAG knowledge base.

Polls RSS/Atom feeds, fetches the full text of new articles, and upserts their
chunks into the same Chroma store used by the rag-service. Feed items are
deduplicated by their stable id/link (stored in chunk metadata), so restarts
and repeated polls never re-ingest the same item.
"""
import hashlib
import logging
import os
import urllib.error
import urllib.request

import feedparser
from bs4 import BeautifulSoup
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from loader import CHUNK_OVERLAP, CHUNK_SIZE

logger = logging.getLogger("rag.feeds")

# Comma-separated RSS/Atom URLs as an env var, e.g.
# RSS_FEEDS="https://hnrss.org/newest?points=100,https://feeds.bbci.co.uk/news/rss.xml"
RSS_FEEDS = [u.strip() for u in os.environ.get("RSS_FEEDS", "").split(",") if u.strip()]
MAX_ITEMS_PER_FEED = int(os.environ.get("RSS_MAX_ITEMS", "20"))
FETCH_TIMEOUT = int(os.environ.get("RSS_FETCH_TIMEOUT", "15"))
USER_AGENT = os.environ.get(
    "RSS_USER_AGENT", "Mozilla/5.0 (rag-llm-langchain-deploy realtime feed ingester)"
)


def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ".", " "],
    )


def _fetch_article_text(url: str) -> str:
    """Fetch an article URL and return plain text, or '' on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            if resp.status >= 400:
                return ""
            if "html" not in resp.headers.get("Content-Type", "").lower():
                return ""
            raw = resp.read(2_000_000).decode("utf-8", errors="ignore")
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        return " ".join(soup.get_text(" ").split())
    except (OSError, urllib.error.URLError):
        logger.debug("Could not fetch article text: %s", url)
        return ""


def fetch_entries(feeds: list[str] | None = None) -> list[tuple]:
    """Fetch each feed and return (entry, feed_url) pairs, newest first."""
    urls = feeds or RSS_FEEDS
    pairs = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001 - network/parse failures are routine
            logger.warning("Feed fetch failed %s: %s", url, exc)
            continue
        if getattr(feed, "bozo", False):
            logger.warning("Feed parsing issue for %s: %s", url, feed.bozo_exception)
        for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
            pairs.append((entry, url))
    logger.info("Fetched %d entries from %d feed(s)", len(pairs), len(urls))
    return pairs


def entry_to_doc(entry, feed_url: str) -> Document:
    """Convert a feedparser entry into a LangChain Document with a stable guid."""
    title = entry.get("title", "").strip()
    link = entry.get("link", "").strip()
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    published = (entry.get("published") or entry.get("updated") or "").strip()

    guid = entry.get("id") or link
    if not guid:
        guid = "hash:" + hashlib.sha1(
            f"{feed_url}|{title}|{published}".encode("utf-8")
        ).hexdigest()

    body = _fetch_article_text(link) if link else ""
    content = body if body else summary
    parts = [p for p in (title, summary, content) if p]
    text = "\n\n".join(dict.fromkeys(parts)) or title or link or guid

    return Document(
        page_content=text,
        metadata={
            "feed_guid": guid,
            "title": title,
            "link": link,
            "published": published,
            "feed_url": feed_url,
            "source": "rss",
        },
    )


def _known_guids(store) -> set[str]:
    try:
        data = store._collection.get(include=["metadatas"])
        return {
            m["feed_guid"]
            for m in data["metadatas"]
            if m and m.get("feed_guid")
        }
    except Exception:  # noqa: BLE001 - tolerate store quirks; re-poll next round
        return set()


def ingest_feeds(feeds: list[str] | None = None) -> int:
    """Poll feeds and upsert new chunks into Chroma. Returns chunks added."""
    from config import get_store  # lazy: keep module import light for tooling

    store = get_store()
    known = _known_guids(store)
    splitter = _splitter()
    seen: set[str] = set()
    added = 0

    for entry, feed_url in fetch_entries(feeds):
        doc = entry_to_doc(entry, feed_url)
        guid = doc.metadata["feed_guid"]
        if guid in known or guid in seen:
            continue
        seen.add(guid)

        chunks = splitter.split_documents([doc])
        ids = [
            "feed-"
            + hashlib.sha1(f"{guid}#{i}".encode("utf-8")).hexdigest()
            for i in range(len(chunks))
        ]
        store.add_documents(chunks, ids=ids)
        added += len(chunks)
        logger.info("New feed item -> %d chunks: %s", len(chunks), doc.metadata["title"])
    logger.info("Feed ingest round done: %d new chunks", added)
    return added